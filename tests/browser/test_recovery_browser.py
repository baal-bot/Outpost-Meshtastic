"""Actual offline workbench after encrypted restore, including login and fencing."""

from contextlib import AsyncExitStack

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from outpost import recovery
from outpost.app import OutpostApp
from outpost.transport.simulated import SimulatedRadioLink
from tests.browser.test_incident_g6 import Dashboard
from tests.integration.test_encrypted_recovery import PASSPHRASE, PASSWORD
from tests.integration.test_encrypted_recovery import recovery_source as recovery_source

pytestmark = pytest.mark.production_wiring


@pytest.mark.parametrize("width,change_password", [(320, False), (1280, True)])
async def test_actual_restored_browser_login_verification_and_logout_are_offline(
    recovery_source, tmp_path, width, change_password
):
    if change_password:
        await recovery_source.database.write("UPDATE web_account SET must_change=1")
    output, target = tmp_path / "backup.opr", tmp_path / "fresh"
    recovery.export(recovery_source.config, output, PASSPHRASE)
    recovery.restore(output.read_bytes(), PASSPHRASE, target)
    radio = SimulatedRadioLink(recovery_source.clock)
    app = OutpostApp(recovery.workbench_config(target), clock=recovery_source.clock, radio=radio)
    await app.startup()
    async with AsyncExitStack() as stack:
        stack.push_async_callback(app.shutdown)
        client = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.web), base_url="http://outpost.test"
            )
        )
        playwright = await stack.enter_async_context(async_playwright())
        browser = await playwright.chromium.launch(headless=True)
        stack.push_async_callback(browser.close)
        context = await browser.new_context(viewport={"width": width, "height": 900})
        stack.push_async_callback(context.close)
        dashboard = Dashboard(context, client)
        await context.route("**/*", dashboard.route)
        page = await context.new_page()
        page.on("pageerror", lambda error: dashboard.errors.append(str(error)))
        await page.goto("http://outpost.test/recovery.html")
        await page.get_by_label("Username", exact=True).fill("operator")
        await page.get_by_label("Password", exact=True).fill(PASSWORD)
        if change_password:
            await page.get_by_label("New password, only").fill(PASSWORD + "-changed")
        await page.get_by_role("button", name="Sign in and verify").click()
        await expect(page.locator("#recovery-status")).to_contain_text(
            "Restored operator access verified"
        )
        await expect(page.locator("#recovery-evidence")).to_contain_text('"incident": 1')
        await expect(page.locator("#recovery-evidence")).to_contain_text("!00000001")
        assert not await page.get_by_label("Password", exact=True).input_value()
        assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not dashboard.external and not dashboard.errors
        assert not radio.sent
        assert [task.get_name() for task in app._tasks] == ["recovery-review"]
        denied = await client.post("/api/v1/radio/reconnect")
        assert denied.status_code == 423
        await page.get_by_role("button", name="Sign out", exact=True).click()
        await expect(page.locator("#recovery-status")).to_contain_text("Signed out")
        assert (await client.get("/api/v1/recovery/review")).status_code == 401
        assert not radio.sent
