"""Actual authenticated ASGI and browser operator observations, no hardware exercise."""

from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from outpost.self_check import MAX_REPORT_AGE
from tests.browser.test_incident_g6 import Dashboard, operator_client
from tests.integration.test_outage_readiness import appliance as appliance
from tests.integration.test_outage_readiness import checks, observe

pytestmark = pytest.mark.production_wiring


async def test_observation_api_requires_operator_csrf_current_version_and_strict_input(appliance):
    app = appliance
    await app.self_check.run("test")
    async with AsyncExitStack() as stack:
        operator = await operator_client(app, stack)
        viewer = await operator_client(app, stack, role="viewer", username="observer")
        anonymous = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.web), base_url="http://outpost.test"
            )
        )
        path = "/api/v1/readiness/observations"
        report = (await operator.get("/api/v1/readiness")).json()
        body = {
            "check": "station_power",
            "outcome": "pass",
            "observed_at": int(app.clock.now().timestamp()),
            "review_token": checks(report)["station_power"]["review_token"],
        }
        assert (await anonymous.post(path, json=body)).status_code == 401
        assert (await viewer.get("/api/v1/readiness")).status_code == 403
        csrf = (await operator.get("/api/v1/auth/session")).json()["csrf_token"]
        viewer_csrf = (await viewer.get("/api/v1/auth/session")).json()["csrf_token"]
        assert (
            await viewer.post(path, json=body, headers={"x-csrf-token": viewer_csrf})
        ).status_code == 403
        assert (await operator.post(path, json=body)).status_code == 403
        headers = {"x-csrf-token": csrf}
        for invalid in (
            {**body, "observed_at": True},
            {**body, "actor": "someone_else"},
            {**body, "review_token": ""},
        ):
            assert (await operator.post(path, json=invalid, headers=headers)).status_code == 422
        saved = await operator.post(path, json=body, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.headers["cache-control"] == "no-store"
        assert checks(saved.json())["station_power"]["state"] == "attested"
        stale = await operator.post(path, json={**body, "outcome": "fail"}, headers=headers)
        assert stale.status_code == 409
        audit = await app.database.read(
            "SELECT * FROM audit_log WHERE action='readiness.observation'"
        )
        assert len(audit) == 1 and audit[0]["actor_kind"] == "web"
        assert not app.radio.sent and not await app.database.read("SELECT * FROM outbound_work")


@asynccontextmanager
async def evidence_page(app, width=1280, *, run=True):
    if run:
        await app.self_check.run("test")
    async with AsyncExitStack() as stack:
        # Routed browser requests must finish before their ASGI client closes.
        client = await operator_client(app, stack)
        runtime = await stack.enter_async_context(async_playwright())
        browser = await runtime.chromium.launch()
        stack.push_async_callback(browser.close)
        context = await browser.new_context(
            viewport={"width": width, "height": 1000}, service_workers="block"
        )
        stack.push_async_callback(context.close)
        await context.add_cookies(
            [
                {
                    "name": "outpost_session",
                    "value": client.cookies.get("outpost_session"),
                    "url": "http://outpost.test",
                    "httpOnly": True,
                    "sameSite": "Strict",
                }
            ]
        )
        await context.add_init_script(
            "localStorage.setItem('outpost.map.basemap-mode', 'offline-only');"
        )
        dashboard = Dashboard(context, client)
        await context.route("**/*", dashboard.route)
        page = await dashboard.page("/watch.html", app.clock)
        yield dashboard, page, client


@pytest.mark.parametrize("width", [320, 1280])
async def test_operator_can_review_all_evidence_and_record_an_explicit_observation(
    appliance, width
):
    app = appliance
    async with evidence_page(app, width) as (dashboard, page, _):
        await page.locator(".readiness-details > summary").click(force=True)
        await expect(page.locator(".readiness-check")).to_have_count(18)
        station = page.locator('.readiness-check[data-check="station_power"]')
        await expect(station).to_contain_text("UNKNOWN:")
        await station.locator('input[type="checkbox"]').check(force=True)
        await station.get_by_role("button", name="Record passed observation").click(force=True)
        await expect(station).to_contain_text("ATTESTED:")
        await expect(station).to_contain_text("not independently measured proof")
        assert await page.locator(".readiness-details").evaluate("element => element.open")
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not dashboard.errors and not dashboard.external
        assert checks(await app.self_check.latest())["station_power"]["passed"] is False
        assert (await app.self_check.latest())["status"] != "ready"
        assert not app.radio.sent and not await app.database.read("SELECT * FROM alert")


@pytest.mark.parametrize("fault", ["conflict", "lost_response"])
async def test_conflicting_or_unconfirmed_browser_observation_never_resubmits(appliance, fault):
    async with evidence_page(appliance) as (dashboard, page, client):
        await page.locator(".readiness-details > summary").click(force=True)
        station = page.locator('.readiness-check[data-check="station_power"]')
        if fault == "conflict":
            await observe(appliance, outcome="fail")
        else:

            async def lose_response(route):
                request = route.request
                if request.method != "POST" or not request.url.endswith("/readiness/observations"):
                    await route.fallback()
                    return
                headers = {
                    k: v
                    for k, v in (await request.all_headers()).items()
                    if k not in ("host", "content-length")
                }
                response = await client.request(
                    request.method, request.url, headers=headers, content=request.post_data_buffer
                )
                assert response.status_code == 200
                dashboard.requests.append(("POST", "/api/v1/readiness/observations", 200))
                await route.abort("failed")

            await dashboard.context.route("**/*", lose_response)
        await station.locator('input[type="checkbox"]').check(force=True)
        await station.get_by_role("button", name="Record passed observation").click(force=True)
        await expect(station.locator('[role="status"]')).to_contain_text(
            "Evidence changed" if fault == "conflict" else "outcome is unconfirmed"
        )
        await page.clock.run_for(1000)
        assert (
            sum(
                method == "POST" and path == "/api/v1/readiness/observations"
                for method, path, _ in dashboard.requests
            )
            == 1
        )
        assert (
            len(
                await appliance.database.read(
                    "SELECT * FROM audit_log WHERE action='readiness.observation'"
                )
            )
            == 1
        )
        assert not dashboard.errors and not dashboard.external
        assert not appliance.radio.sent


async def test_missing_report_warns_until_an_explicit_real_readiness_run(appliance):
    async with evidence_page(appliance, run=False) as (dashboard, page, _):
        banner = page.locator(".readiness-banner")
        await expect(banner).to_contain_text("Outage readiness has not been measured")
        await banner.get_by_role("button", name="Run readiness check").click(force=True)
        await expect(page.locator(".readiness-check")).to_have_count(18)
        assert (await appliance.self_check.latest())["status"] != "ready"
        assert not dashboard.errors and not dashboard.external
        assert not appliance.radio.sent


async def test_metrics_only_monitor_ages_evidence_without_rerunning_probes(appliance, monkeypatch):
    await appliance.self_check.run("test")
    async with AsyncExitStack() as stack:
        client = await operator_client(appliance, stack)

        async def unexpected_probe(*args, **kwargs):
            pytest.fail("Metrics scrape must not rerun readiness probes")

        monkeypatch.setattr(appliance.self_check, "run", unexpected_probe)
        appliance.clock.advance(MAX_REPORT_AGE + 1)
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert (
            'outpost_self_check_state{check="storage_reserve",severity="operations"} 0.0'
            in response.text
        )
        assert (
            'outpost_self_check_evidence_state{check="storage_reserve",state="stale"} 1.0'
            in response.text
        )
        assert not appliance.radio.sent
