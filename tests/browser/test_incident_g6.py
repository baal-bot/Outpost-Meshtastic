"""Field intake through real ASGI/browser maps, with simulated radios and humans."""

import asyncio
import json
import secrets
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from tests.support.incident_mesh import incident_mesh, synthetic_tiles

pytestmark = pytest.mark.production_wiring


async def operator_client(app, stack):
    password = secrets.token_urlsafe(24)
    await app.web_auth.create_account("coordinator", "Coordinator", "operator", password, "test:g6")
    client = await stack.enter_async_context(
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.web),
            base_url="http://outpost.test",
        )
    )
    login = await client.post(
        "/api/v1/auth/login", json={"username": "coordinator", "password": password}
    )
    assert login.status_code == 200, login.text
    changed = await client.post(
        "/api/v1/auth/password",
        headers={"x-csrf-token": login.json()["csrf_token"]},
        json={"current_password": password, "new_password": password + "-changed"},
    )
    assert changed.status_code == 200, changed.text
    login = await client.post(
        "/api/v1/auth/login", json={"username": "coordinator", "password": password + "-changed"}
    )
    assert login.status_code == 200 and not login.json()["must_change"]
    return client


class Dashboard:
    def __init__(self, context, client):
        self.context, self.client = context, client
        self.external = []
        self.errors = []
        self.requests = []
        self.pages = []

    async def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.netloc != "outpost.test":
            self.external.append(request.url)
            await route.abort("internetdisconnected")
            return
        headers = {
            k: v
            for k, v in (await request.all_headers()).items()
            if k not in ("host", "content-length")
        }
        response = await self.client.request(
            request.method,
            request.url,
            headers=headers,
            content=request.post_data_buffer,
        )
        self.requests.append((request.method, parsed.path, response.status_code))
        # Actual production ASGI responses, including auth, CSP, static assets,
        # data and mutations. No manufactured API/map/receipt responses.
        await route.fulfill(
            status=response.status_code,
            headers={
                k: v
                for k, v in response.headers.items()
                if k not in ("content-encoding", "content-length")
            },
            body=response.content,
        )

    async def page(self, path, clock):
        page = await self.context.new_page()
        self.pages.append(page)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        await page.clock.install(time=clock.now())
        await page.clock.pause_at(clock.now())
        await page.goto("http://outpost.test" + path, wait_until="networkidle")
        if path == "/watch.html":
            # Initialization installs the real ten-second refresh scheduler.
            assert await page.evaluate(
                "() => window.OutpostScheduler?.snapshot().some(t => t.name === 'watch-main')"
            )
        else:
            try:
                await page.locator('html[data-federation-ready="true"]').wait_for(
                    state="attached", timeout=3000
                )
            except Exception as error:
                raise AssertionError((self.errors, self.requests, page.url)) from error
        return page


@pytest.mark.parametrize("count,human_delay", [(2, 5), (3, 5), (3, None)])
async def test_field_report_only_reaches_reviewed_browser_maps_without_wan(
    tmp_path, count, human_delay
):
    synthetic_tiles(tmp_path / "tiles")
    async with incident_mesh(tmp_path, count) as mesh, AsyncExitStack() as stack:
        runtime = await stack.enter_async_context(async_playwright())
        browser = await runtime.chromium.launch()
        stack.push_async_callback(browser.close)
        dashboards, maps, inboxes = [], [], []
        for index, app in enumerate(mesh.apps):
            client = await operator_client(app, stack)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 1000}, service_workers="block"
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
                "localStorage.setItem('outpost.map.basemap-mode', 'offline-only'); "
                "Math.random = () => 1;"
            )
            dashboard = Dashboard(context, client)
            await context.route("**/*", dashboard.route)
            dashboards.append(dashboard)
            maps.append(await dashboard.page("/watch.html", mesh.clock))
            inboxes.append(await dashboard.page("/federation.html", mesh.clock) if index else None)
        incident = await mesh.report()
        assert incident.lat is not None and not await mesh.apps[0].database.read(
            "SELECT * FROM alert"
        )
        received_at, reviewed_at, visible_at = {}, {}, {}
        for elapsed in range(91):
            await mesh.tick()
            for index, (app, inbox) in enumerate(zip(mesh.apps, inboxes, strict=True)):
                if inbox is None:
                    continue
                rows = await app.database.read("SELECT id,state FROM fed_inbox_item")
                if rows and index not in received_at:
                    received_at[index] = elapsed
                    assert not await app.database.read("SELECT * FROM incident")
                    await inbox.locator("#refresh-inbox").click(force=True)
                    await expect(inbox.locator("#fed-inbox")).to_contain_text("fallen tree")
                    await inbox.locator("#fed-inbox summary").click(force=True)
                    await expect(inbox.locator("#fed-inbox pre")).to_contain_text("40.4406")
                if (
                    index in received_at
                    and index not in reviewed_at
                    and human_delay is not None
                    and elapsed >= received_at[index] + human_delay
                ):
                    # A simulated human reads the displayed current version for
                    # five seconds and approves through the actual UI. Forced
                    # clicks bypass Playwright's frozen-RAF stability wait only;
                    # visibility and the production handler/token remain checked.
                    button = inbox.get_by_role("button", name="Approve import")
                    await expect(button).to_be_visible()
                    await button.click(force=True)
                    await expect(inbox.locator("#fed-review-result")).to_have_text(
                        "Import approved; audit recorded."
                    )
                    reviewed_at[index] = elapsed
            for index, page in enumerate(maps):
                if (
                    index not in visible_at
                    and await page.locator('[data-marker-id^="incident-"]').evaluate_all(
                        """markers => markers.some(marker => {
                          if (!marker.style.left || !marker.style.top) return false;
                          const point = marker.getBoundingClientRect();
                          const map = marker.closest('#incident-map').getBoundingClientRect();
                          return point.width > 0 && point.height > 0 && point.left >= map.left &&
                            point.right <= map.right && point.top >= map.top &&
                            point.bottom <= map.bottom;
                        })"""
                    )
                    and await page.locator(
                        '#map-tiles img[data-source="local"][data-state="loaded"]'
                    ).count()
                ):
                    visible_at[index] = elapsed
            if len(visible_at) == count or (human_delay is None and elapsed == 60):
                break
            mesh.clock.advance(1)
            await asyncio.gather(
                *(page.clock.run_for(1000) for d in dashboards for page in d.pages)
            )
            await asyncio.sleep(0)
        result = {
            "nodes": count,
            "human_delay": human_delay,
            "field_first_hop_seconds": mesh.field_airtime,
            "received": received_at,
            "reviewed": reviewed_at,
            "visible": visible_at,
        }
        print("\nG6 browser simulation: " + json.dumps(result, sort_keys=True))
        if human_delay is None:
            assert set(received_at) == set(range(1, count)) and not reviewed_at, result
            assert set(visible_at) == {0}, result
            delivery = (await mesh.apps[0].incident_delivery.status())["items"]
            assert len(delivery) == count - 1
            assert all(item["remote_storage"] == "observed" for item in delivery)
            assert all(item["human_review"] == "not_reported" for item in delivery)
        else:
            assert len(visible_at) == count, result
            assert max(visible_at.values()) + mesh.field_airtime <= 60, result
        for index, (app, dashboard, page) in enumerate(
            zip(mesh.apps, dashboards, maps, strict=True)
        ):
            assert not dashboard.external and not dashboard.errors
            assert not await app.database.read("SELECT * FROM alert")
            marker = page.locator('[data-marker-id^="incident-"]')
            assert not await page.locator('#map-tiles img:not([data-state="loaded"])').count()
            if index and human_delay is None:
                await expect(marker).to_have_count(0)
                assert not await app.database.read("SELECT * FROM incident")
                assert not await app.database.read("SELECT * FROM incident_origin")
                assert not any(method == "PATCH" for method, _, _ in dashboard.requests)
                continue
            await expect(marker).to_be_visible()
            await marker.click(force=True)
            await expect(page.locator("#map-detail")).to_contain_text(
                "fallen tree", ignore_case=True
            )
            if index:
                assert await app.database.read("SELECT * FROM incident_origin")
                assert reviewed_at[index] - received_at[index] == human_delay
                assert any(
                    method == "PATCH"
                    and path.startswith("/api/v1/federation/inbox/")
                    and status == 200
                    for method, path, status in dashboard.requests
                )
