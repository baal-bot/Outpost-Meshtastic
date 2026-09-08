"""Real offline operator review: explicit consent, changed-key conflict and no retry."""

import pytest
from playwright.async_api import async_playwright, expect

from tests.browser.test_incident_g6 import Dashboard
from tests.integration.test_node_loss import BASE, NEW, OLD
from tests.integration.test_node_loss import adoption_node as adoption_node

pytestmark = pytest.mark.production_wiring


@pytest.mark.parametrize("width,retired_state", [(320, "rejected"), (1280, "forgotten")])
async def test_real_browser_requires_review_and_does_not_retry_stale_adoption(
    adoption_node, width, retired_state
):
    node = adoption_node
    if retired_state == "forgotten":
        await node.app.federation.forget(OLD)
    await node.db.write("UPDATE fed_peer SET policy_configured=1")
    runtime = await node.stack.enter_async_context(async_playwright())
    browser = await runtime.chromium.launch()
    node.stack.push_async_callback(browser.close)
    context = await browser.new_context(viewport={"width": width, "height": 1000})
    node.stack.push_async_callback(context.close)
    await context.add_init_script(
        "localStorage.setItem('outpost.map.basemap-mode', 'offline-only')"
    )
    dashboard = Dashboard(context, node.client)
    await context.route("**/*", dashboard.route)
    page = await context.new_page()
    page.on("pageerror", lambda error: dashboard.errors.append(str(error)))
    await page.goto("http://outpost.test/federation.html")
    button = page.locator(f'[data-adopt-origin="{OLD}"]')
    await expect(button).to_be_visible()
    await button.focus()
    await page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog")
    await expect(dialog).to_contain_text("fresh empty replacement")
    await expect(dialog).to_contain_text("No incidents, alerts, keys, accounts")
    await expect(page.locator(f'[data-origin-peer="{OLD}"]')).to_be_disabled()
    assert not await node.db.read("SELECT * FROM fed_peer_successor")
    if width == 320:
        await page.keyboard.press("Escape")
    else:
        await dialog.get_by_role("button", name="Cancel", exact=True).click()
    await expect(dialog).not_to_be_visible()
    await expect(button).to_be_focused()
    assert not [r for r in dashboard.requests if r[0] == "POST" and r[1] == BASE]
    await button.click()
    await expect(dialog).to_contain_text("Confirm BBS namespace continuity")
    await node.db.write("UPDATE fed_peer SET shared_secret=? WHERE mesh_id=?", (b"n" * 32, NEW))
    await dialog.get_by_role("button", name="Confirm same BBS namespace", exact=True).click()
    await expect(dialog).to_contain_text("Identity context changed or review expired")
    await expect(dialog).to_contain_text("No automatic retry")
    assert len([r for r in dashboard.requests if r[0] == "POST" and r[1] == BASE]) == 1
    assert not await node.db.read("SELECT * FROM fed_peer_successor")
    await dialog.get_by_role("button", name="Close", exact=True).click()
    await expect(button).to_be_focused()
    await page.locator("#refresh-origins").click()
    await button.click()
    await expect(dialog).to_contain_text("Confirm BBS namespace continuity")
    await dialog.get_by_role("button", name="Confirm same BBS namespace", exact=True).click()
    await expect(page.locator("#origin-history")).to_contain_text("Successor:")
    await expect(page.locator("#refresh-origins")).to_be_focused()
    assert len(await node.db.read("SELECT * FROM fed_peer_successor")) == 1
    assert (
        len(await node.db.read("SELECT * FROM audit_log WHERE action='federation.origin_adopt'"))
        == 1
    )
    assert not dashboard.external and not dashboard.errors and not node.app.radio.sent
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
