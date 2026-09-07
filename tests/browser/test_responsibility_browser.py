"""Responsibility through authenticated ASGI and actual offline Chromium forms."""

import asyncio
import json
from contextlib import AsyncExitStack

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from outpost.web.api import create_web_app
from tests.browser.test_incident_g6 import Dashboard, operator_client
from tests.integration.test_incident_responsibility import coordination as coordination
from tests.integration.test_outage_readiness import appliance as appliance

pytestmark = pytest.mark.production_wiring


async def linked_client(app, stack, member_id, *, username="coordinator", role="operator"):
    client = await operator_client(app, stack, username=username, role=role)
    if member_id is not None:
        await app.database.write(
            "UPDATE web_account SET radio_member_id=? WHERE username=?", (member_id, username)
        )
    session = (await client.get("/api/v1/auth/session")).json()
    client.headers["x-csrf-token"] = session["csrf_token"]
    return client


async def test_role_csrf_validation_and_version_conflict_are_enforced(coordination):
    app, incident, actors, _ = coordination
    path = f"/api/v1/incidents/{incident.id}/responsibility"
    async with AsyncExitStack() as stack:
        client = await linked_client(app, stack, actors[1].member_id)
        viewer = await linked_client(app, stack, None, username="viewer", role="viewer")
        anonymous = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.web), base_url="http://outpost.test"
            )
        )
        for suffix in ("", "/history"):
            assert (await anonymous.get(path + suffix)).status_code == 401
            assert (await viewer.get(path + suffix)).status_code == 403
        assert (
            await viewer.get("/api/v1/incidents/responsibility/targets?kind=group")
        ).status_code == 403
        state = await client.get(path)
        assert state.status_code == 200 and state.headers["cache-control"] == "no-store"
        targets = (await client.get("/api/v1/incidents/responsibility/targets?kind=group")).json()[
            "items"
        ]
        body = {
            "action": "offer",
            "review_token": state.json()["review_token"],
            "target_kind": "group",
            "target_ref": targets[0]["reference"],
            "next_action": "Check the public junction",
        }
        assert (
            await client.post(path, json=body, headers={"x-csrf-token": "wrong"})
        ).status_code == 403
        assert (await client.post(path, json={**body, "actor": "administrator"})).status_code == 422
        assert (await client.post(path, json={**body, "target_ref": True})).status_code == 422
        assert (await client.post(path, json={**body, "next_action": "🔥" * 41})).status_code == 400
        offered = await client.post(path, json=body)
        assert offered.status_code == 200 and offered.json()["owner"] is None
        assert (await client.post(path, json=body)).status_code == 409
        accepted = await client.post(
            path, json={"action": "accept", "review_token": offered.json()["review_token"]}
        )
        assert accepted.status_code == 200 and accepted.json()["owner"]["kind"] == "group"
        app.config.modules.watch.enabled = False
        assert (await client.get(path)).status_code == 409
        assert (await client.post(path, json=body)).status_code == 409
        assert not app.radio.sent


async def test_optional_auth_construction_is_fail_closed_for_responsibility(coordination):
    app, incident, _, _ = coordination
    web = create_web_app(app.status, database=app.database, incidents=app.incidents)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web), base_url="http://outpost.test"
    ) as client:
        for path in (
            "/api/v1/incidents/responsibility/targets?kind=group",
            f"/api/v1/incidents/{incident.id}/responsibility",
            f"/api/v1/incidents/{incident.id}/responsibility/history",
        ):
            response = await client.get(path)
            assert response.status_code == 403
            assert response.headers["cache-control"] == "no-store"
    async with AsyncExitStack() as stack:
        client = await linked_client(app, stack, None)
        for reference in (0, 99999, 2**64):
            assert (
                await client.get(f"/api/v1/incidents/{reference}/responsibility")
            ).status_code == 400
            assert (
                await client.get(f"/api/v1/incidents/{reference}/responsibility/history")
            ).status_code == 400


@pytest.mark.parametrize("revocation", ["role", "session"])
async def test_role_or_session_revoked_after_http_auth_cannot_accept_in_writer(
    coordination, monkeypatch, revocation
):
    app, incident, actors, _ = coordination
    async with AsyncExitStack() as stack:
        client = await linked_client(app, stack, actors[1].member_id)
        path = f"/api/v1/incidents/{incident.id}/responsibility"
        view = (await client.get(path)).json()
        target = (await app.incidents.responsibility.targets("group"))[0]["reference"]
        offer = await client.post(
            path,
            json={
                "action": "offer",
                "review_token": view["review_token"],
                "target_kind": "group",
                "target_ref": target,
                "next_action": "Test current authorization",
            },
        )
        assert offer.status_code == 200
        entered, release = asyncio.Event(), asyncio.Event()
        original = app.incidents.responsibility.apply

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(app.incidents.responsibility, "apply", delayed)
        pending = asyncio.create_task(
            client.post(
                path, json={"action": "accept", "review_token": offer.json()["review_token"]}
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            if revocation == "role":
                await app.database.write(
                    "UPDATE web_account SET role='viewer' WHERE username='coordinator'"
                )
            else:
                await app.database.write(
                    "DELETE FROM web_session WHERE account_id=("
                    "SELECT id FROM web_account WHERE username='coordinator')"
                )
        finally:
            release.set()
        response = await pending
        assert response.status_code == 403
        assert (await app.incidents.responsibility.snapshot(incident.id))["owner"] is None
        assert len(await app.incidents.responsibility.history(incident.id)) == 1


@pytest.mark.parametrize("width", [320, 1280])
async def test_phone_desktop_offer_accept_handoff_and_no_xss(coordination, width):
    app, incident, actors, _ = coordination
    async with AsyncExitStack() as stack:
        client = await linked_client(app, stack, actors[1].member_id)
        runtime = await stack.enter_async_context(async_playwright())
        browser = await runtime.chromium.launch()
        stack.push_async_callback(browser.close)
        context = await browser.new_context(
            viewport={"width": width, "height": 1000}, service_workers="block"
        )
        stack.push_async_callback(context.close)
        dashboard = Dashboard(context, client)
        await context.route("**/*", dashboard.route)
        page = await context.new_page()
        page.on("pageerror", lambda error: dashboard.errors.append(str(error)))
        await page.goto(
            f"http://outpost.test/incident-report.html?id={incident.id}", wait_until="networkidle"
        )
        root = page.locator("#incident-responsibility")
        await expect(root).to_contain_text("Accepted owner: none")
        await page.locator("#responsibility-target").select_option(index=1)
        await page.locator("#responsibility-next").fill(
            '<img src=x onerror="window.assignmentXss=1"> Public junction'
        )
        await page.locator("#responsibility-consent").check()
        await root.get_by_role("button", name="Record offer", exact=True).click()
        await expect(root).to_contain_text("Pending acceptance: Search team")
        await expect(root).to_contain_text("Accepted owner: none")
        await page.locator("#responsibility-action").select_option("accept")
        await page.locator("#responsibility-consent").check()
        await root.get_by_role("button", name="Record accept", exact=True).click()
        await expect(root).to_contain_text("Accepted owner: Search team")
        assert await page.evaluate("window.assignmentXss") is None
        assert await root.locator("img").count() == 0
        await page.locator("#responsibility-action").select_option("offer")
        await page.locator("#responsibility-kind").select_option("account")
        await expect(page.locator("#responsibility-target option")).to_have_count(2)
        await page.locator("#responsibility-target").select_option(index=1)
        await page.locator("#responsibility-next").fill(
            "Finish coordination from the operator desk"
        )
        await page.locator("#responsibility-consent").check()
        await root.get_by_role("button", name="Record offer", exact=True).click()
        await expect(root).to_contain_text("Pending acceptance: coordinator")
        await expect(root).to_contain_text("Accepted owner: Search team")
        await page.locator("#responsibility-action").select_option("accept")
        await page.locator("#responsibility-consent").check()
        await root.get_by_role("button", name="Record accept", exact=True).click()
        await expect(root).to_contain_text("Accepted owner: coordinator")
        await page.locator("#responsibility-action").select_option("complete")
        await page.locator("#responsibility-consent").check()
        await root.get_by_role("button", name="Record complete", exact=True).click()
        await expect(root).to_contain_text("Accepted owner: none · completed")
        assert (await app.incidents.by_ref(incident.local_ref)).status == "open"
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not dashboard.errors and not dashboard.external and not app.radio.sent


@pytest.mark.parametrize("fault", ["conflict", "lost_response", "forbidden"])
async def test_browser_does_not_retry_stale_or_unconfirmed_decisions(coordination, fault):
    app, incident, actors, _ = coordination
    async with AsyncExitStack() as stack:
        client = await linked_client(app, stack, actors[1].member_id)
        runtime = await stack.enter_async_context(async_playwright())
        browser = await runtime.chromium.launch()
        stack.push_async_callback(browser.close)
        context = await browser.new_context(service_workers="block")
        stack.push_async_callback(context.close)
        dashboard = Dashboard(context, client)
        attempted = []

        async def route(request_route):
            request = request_route.request
            if request.method == "POST" and request.url.endswith("/responsibility"):
                attempted.append(json.loads(request.post_data))
                body = attempted[-1]
                if fault == "conflict":
                    first = await client.post(request.url, json=body)
                    assert first.status_code == 200
                elif fault == "lost_response":
                    first = await client.post(request.url, json=body)
                    assert first.status_code == 200
                    await request_route.abort("connectionclosed")
                    return
                else:
                    await app.database.write(
                        "UPDATE web_account SET role='viewer' WHERE username='coordinator'"
                    )
            await dashboard.route(request_route)

        await context.route("**/*", route)
        page = await context.new_page()
        await page.goto(
            f"http://outpost.test/incident-report.html?id={incident.id}", wait_until="networkidle"
        )
        await page.locator("#responsibility-target").select_option(index=1)
        await page.locator("#responsibility-next").fill("One bounded decision")
        await page.locator("#responsibility-consent").check()
        button = page.locator("#incident-responsibility").get_by_role(
            "button", name="Record offer", exact=True
        )
        await button.click()
        expected = {
            "conflict": "Responsibility changed",
            "lost_response": "Outcome unconfirmed",
            "forbidden": "Not accepted",
        }[fault]
        await expect(page.locator("#incident-responsibility [role=status]")).to_contain_text(
            expected
        )
        await expect(button).to_be_disabled()
        assert len(attempted) == 1
        events = await app.database.read("SELECT id FROM incident_responsibility_event")
        assert len(events) == (0 if fault == "forbidden" else 1)
