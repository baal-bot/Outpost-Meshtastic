"""Operator time permissions and honest queue status in the real dashboard."""

from contextlib import AsyncExitStack

import httpx
import pytest
from playwright.async_api import expect

from tests.browser.test_incident_g6 import operator_client
from tests.browser.test_outage_readiness_ui import evidence_page
from tests.integration.test_outage_readiness import appliance as appliance

pytestmark = pytest.mark.production_wiring


async def paired(app):
    await app.federation.discover("!remote", "Time source", 1, {"time_v1": True}, "radio")
    await app.database.write(
        "UPDATE fed_peer SET state='active',shared_secret=?,policy_configured=1,"
        "local_approved=1,remote_approved=1",
        (bytes(range(32)),),
    )


async def test_time_controls_require_operator_csrf_and_explicit_strict_permissions(appliance):
    await paired(appliance)
    async with AsyncExitStack() as stack:
        operator = await operator_client(appliance, stack)
        viewer = await operator_client(appliance, stack, role="viewer", username="observer")
        anonymous = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=appliance.web), base_url="http://outpost.test"
            )
        )
        path = "/api/v1/federation/peers/!remote/time-policy"
        body = {"trust": True, "serve": False}
        assert (await anonymous.put(path, json=body)).status_code == 401
        assert (await viewer.put(path, json=body)).status_code == 403
        assert (await operator.put(path, json=body)).status_code == 403
        csrf = (await operator.get("/api/v1/auth/session")).json()["csrf_token"]
        headers = {"x-csrf-token": csrf}
        assert (
            await operator.put(path, json={"trust": "yes", "serve": False}, headers=headers)
        ).status_code == 422
        result = await operator.put(path, json=body, headers=headers)
        assert result.status_code == 200, result.text
        assert result.json()["peers"]["!remote"]["trust"]
        assert "credential" not in result.text
        assert not appliance.radio.sent
        queued = await operator.post(path.replace("time-policy", "time-check"), headers=headers)
        assert queued.status_code == 200 and queued.json()["state"] == "waiting"
        assert not appliance.radio.sent
        rate = await operator.post(path.replace("time-policy", "time-check"), headers=headers)
        assert rate.status_code == 409
        for trust, serve in ((False, True), (False, False)):
            assert (
                await operator.put(path, json={"trust": trust, "serve": serve}, headers=headers)
            ).status_code == 200
        assert not appliance.federation_time.policies


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("theme", ["dark", "daylight", "night"])
async def test_peer_time_controls_and_waiting_status(appliance, width, theme):
    await paired(appliance)
    async with evidence_page(appliance, width) as (dashboard, page, client):
        await dashboard.context.add_init_script(
            f"localStorage.setItem('outpost.appearance.theme', '{theme}');"
        )
        page = await dashboard.page("/federation.html", appliance.clock)
        await expect(page.locator("html")).to_have_attribute("data-theme", theme)
        await page.get_by_role("button", name="Time backup", exact=True).click()
        await page.get_by_label("Trust this peer’s time").check()
        await page.get_by_role("button", name="Save permissions").click()
        await expect(page.locator("#peer-time-result")).to_have_text("Time permissions saved.")
        assert not appliance.radio.sent
        await page.get_by_role("button", name="Check time", exact=True).click()
        await expect(page.locator("#peer-time-result")).to_contain_text("time is not yet verified")
        await expect(page.locator("#peer-time-form")).to_contain_text("do not adjust the Pi clock")
        assert not appliance.radio.sent
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        await page.get_by_role("button", name="Close", exact=True).click()
        assert not dashboard.errors and not dashboard.external
