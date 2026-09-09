"""Actual operator API and rendered time holds, without network/time manipulation."""

from datetime import timedelta

import pytest
from playwright.async_api import expect

from outpost.timekeeping import TimeSource
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


@pytest.mark.parametrize("width", [320, 1280])
async def test_operator_sees_startup_recovery_without_rtc_certification(
    appliance, monkeypatch, width
):
    source = monitor(monkeypatch, appliance.clock, synchronized=False)
    appliance.clock.epoch += timedelta(seconds=259.806)
    source[0] = TimeSource(True, 0.01)
    async with evidence_page(appliance, width) as (dashboard, page, _):
        # This fixture authenticates through ASGI; mirror the sign-in page's
        # browser hint so the real navigation scheduler runs on the direct URL.
        await page.evaluate("sessionStorage.setItem('outpost.operator.authenticated', 'true')")
        await page.locator(".readiness-details > summary").click(force=True)
        clock_check = page.locator('.readiness-check[data-check="time_confidence"]')
        await expect(clock_check).to_contain_text("Recovery is automatic")
        appliance.clock.advance(30)
        await page.clock.run_for(31_000)
        await expect(clock_check).to_contain_text("automatic startup clock recovery")
        report = appliance.self_check.snapshot()
        assert report["trigger"] == "startup-time-recovered"
        evidence = next(check for check in report["checks"] if check["name"] == "time_confidence")
        assert evidence["evidence"]["timestamp_safe"]
        assert evidence["evidence"]["startup_recovered"]
        assert evidence["state"] == "unknown"
        await expect(clock_check).to_contain_text("RTC retention is unqualified")
        assert not await appliance.database.read(
            "SELECT * FROM audit_log WHERE action='readiness.observation'"
        )
        assert not dashboard.errors and not dashboard.external
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
