"""Actual operator API and rendered time holds, without network/time manipulation."""

import pytest
from playwright.async_api import expect

from tests.browser.test_outage_readiness_ui import evidence_page
from tests.integration.test_outage_readiness import appliance as appliance
from tests.integration.test_time_confidence import monitor

pytestmark = pytest.mark.production_wiring


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("theme", ["dark", "daylight", "night"])
async def test_operator_sees_time_hold_and_create_never_claims_queued(
    appliance, monkeypatch, width, theme
):
    monitor(monkeypatch, appliance.clock, synchronized=False)
    async with evidence_page(appliance, width) as (dashboard, page, client):
        await dashboard.context.add_init_script(
            f"localStorage.setItem('outpost.appearance.theme', '{theme}');"
        )
        page = await dashboard.page("/federation.html", appliance.clock)
        await expect(page.locator("html")).to_have_attribute("data-theme", theme)
        await expect(page.locator("#relay-summary")).to_contain_text("Time confidence is uncertain")
        await page.locator("#relay-destination").fill("!bbbbbbbb")
        await page.locator("#relay-payload").fill('{"status":"synthetic"}')
        await page.get_by_role("button", name="Queue signed envelope").click()
        await expect(page.locator("#relay-result")).to_contain_text("Verify the station's UTC")
        assert not await appliance.database.read("SELECT * FROM fed_relay_envelope")
        assert not appliance.radio.sent
        assert not dashboard.errors and not dashboard.external
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
