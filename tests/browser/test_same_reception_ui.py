"""Actual authenticated Environment and readiness APIs with synthetic SDR evidence."""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from tests.browser.test_incident_g6 import operator_client
from tests.browser.test_outage_readiness_ui import evidence_page
from tests.integration.test_outage_readiness import appliance as appliance
from tests.integration.test_outage_readiness import observe
from tests.integration.test_same_reception import TEST_HEADER

pytestmark = pytest.mark.production_wiring


def configure_receiver(app):
    app.config.modules.env.enabled = True
    app.config.env.same.enabled = True
    app.config.env.same.county_codes = ["042003"]
    app.same_receiver.state = "listening"
    app.same_events.record_audio(1200)


async def test_reception_api_requires_login_and_separates_qualification_from_decode(appliance):
    app = appliance
    configure_receiver(app)
    async with AsyncExitStack() as stack:
        anonymous = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.web), base_url="http://outpost.test"
            )
        )
        assert (await anonymous.get("/api/v1/environment/same")).status_code == 401
        client = await operator_client(app, stack)
        response = await client.get("/api/v1/environment/same")
        assert response.status_code == 200, response.text
        health = response.json()["health"]
        assert health["audio_state"] == "fresh" and health["decode_state"] == "never"
        assert health["station_qualification"]["state"] == "not_recorded"
        await observe(app, "same_reception")
        response = await client.get("/api/v1/environment/same")
        assert response.status_code == 200
        health = response.json()["health"]
        assert health["station_qualification"]["state"] == "attested"
        assert health["decode_state"] == "never"
        await app.same_events.ingest(TEST_HEADER, from_receiver=True)
        payload = (await client.get("/api/v1/environment/same")).json()
        assert payload["health"]["decode_state"] == "fresh"
        assert payload["items"][0]["is_test"] == 1
        # Expiry belongs to the statement, independently of a 15-minute report cache.
        app.clock.advance(901)
        health = (await client.get("/api/v1/environment/same")).json()["health"]
        assert health["station_qualification"]["state"] == "attested"
        app.config.env.same.frequency_mhz = 162.4
        changed = (await client.get("/api/v1/environment/same")).json()["health"]
        assert changed["station_qualification"]["state"] == "stale"
        assert changed["decode_state"] == "configuration_changed"
        assert not app.radio.sent and not await app.database.read("SELECT * FROM outbound_work")


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("theme", ["dark", "daylight", "night"])
async def test_operator_can_distinguish_audio_drill_decode_and_station_observation(
    appliance, width, theme
):
    app = appliance
    configure_receiver(app)
    async with evidence_page(app, width, theme=theme) as (dashboard, page, _):
        await page.goto("http://outpost.test/environment.html", wait_until="networkidle")
        await expect(page.locator("#same-pipeline")).to_have_text("Running")
        await expect(page.locator("#same-signal")).to_have_text("Above threshold")
        await expect(page.locator("#same-qualification")).to_have_text("Not qualified")
        await expect(page.locator("#same-decode-detail")).to_contain_text("Never verified")
        await expect(page.locator("#same-receiver-state")).to_contain_text("needs verification")
        # Missing weather location/forecast does not hide the separate offline receiver.
        await expect(page.locator("#env-state")).to_contain_text("Data unavailable")
        await app.same_events.ingest(TEST_HEADER, from_receiver=True)
        await page.clock.run_for(61_000)
        await expect(page.locator("#same-decode-detail")).to_contain_text("Fresh decoder evidence")
        await expect(page.locator("#same-event-list")).to_contain_text("DRILL / TEST · log only")
        await expect(page.locator("[data-same-approve]")).to_have_count(0)
        await expect(page.locator("#same-qualification")).to_have_text("Not qualified")
        await observe(app, "same_reception")
        await page.clock.run_for(61_000)
        await expect(page.locator("#same-qualification")).to_have_text("Operator attested")
        await expect(page.locator("#same-receiver-state")).to_have_text(
            "Operator attested · decode current"
        )
        await expect(page.locator("#same-qualification-detail")).to_contain_text("review by")
        output = Path(".data/sdr-reception-ui-review")
        await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
        await page.locator(".same-workspace").screenshot(
            path=str(output / f"reception-{theme}-{width}.png")
        )
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not dashboard.errors and not dashboard.external
        assert not app.radio.sent and not await app.database.read("SELECT * FROM alert")


@pytest.mark.parametrize("failure", ["quiet", "restart", "expired", "api"])
async def test_stale_or_unavailable_reception_never_leaves_a_green_current_claim(
    appliance, failure
):
    app = appliance
    configure_receiver(app)
    await app.same_events.ingest(TEST_HEADER, from_receiver=True)
    await observe(app, "same_reception")
    async with evidence_page(app) as (dashboard, page, _):
        await page.goto("http://outpost.test/environment.html", wait_until="networkidle")
        await expect(page.locator("#same-receiver-state")).to_contain_text("decode current")
        if failure == "quiet":
            app.same_events.record_audio(1)
        elif failure == "restart":
            app.same_events.reset_pipeline_evidence()
            app.same_receiver.state = "backoff"
        elif failure == "expired":
            app.clock.advance(app.config.env.same.decode_stale_hours * 3600)
            app.same_events.record_audio(1000)
        else:
            # Inject only a transport failure; all successful content is actual ASGI.
            await page.route(
                "**/api/v1/environment/same", lambda route: route.abort("internetdisconnected")
            )
        await page.clock.run_for(61_000)
        await expect(page.locator("#same-receiver-state")).not_to_contain_text("decode current")
        await expect(page.locator("#same-receiver-state")).not_to_have_class(
            "ui-pill ui-pill--success"
        )
        if failure == "expired":
            await expect(page.locator("#same-decode-detail")).to_contain_text(
                "Decode evidence expired"
            )
            await expect(page.locator("#same-qualification")).to_have_text(
                "Observation needs review"
            )
        if failure == "api":
            await expect(page.locator("#same-pipeline")).to_have_text("Unavailable")
        assert not dashboard.errors and not dashboard.external
        assert not app.radio.sent
