"""Actual authenticated ASGI and offline Chromium file review/download/upload."""

import asyncio
from contextlib import AsyncExitStack

import httpx
import pytest
from playwright.async_api import async_playwright, expect

import outpost.web.routes.bundles as bundle_routes
from outpost.fed import bundle_format as fmt
from outpost.web.api import create_web_app
from tests.browser.test_incident_g6 import Dashboard, operator_client
from tests.integration.test_federation_bundles import bundle_nodes as bundle_nodes
from tests.integration.test_federation_bundles import exported, incident

pytestmark = pytest.mark.production_wiring
BASE = "/api/v1/federation/bundles"


async def test_auth_csrf_strict_bodies_upload_bounds_and_no_optional_auth_bypass(bundle_nodes):
    source, target = bundle_nodes
    await incident(source)
    raw, _ = await exported(source)
    async with AsyncExitStack() as stack:
        anonymous = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=target.app.web), base_url="http://outpost.test"
            )
        )
        viewer = await operator_client(target.app, stack, role="viewer", username="observer")
        noauth = create_web_app(
            target.app.status, database=target.app.database, federation_bundles=target.service
        )
        bypass = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=noauth), base_url="http://outpost.test"
            )
        )
        for client, status in ((anonymous, 401), (viewer, 403), (bypass, 403)):
            assert (await client.get(BASE)).status_code == status
            assert (
                await client.post(
                    BASE + "/import",
                    content=raw,
                    headers={"content-type": "application/octet-stream"},
                )
            ).status_code == status
        assert (await target.client.get(BASE)).headers["cache-control"] == "no-store"
        headers = {"content-type": "application/octet-stream"}
        assert (
            await target.client.post(
                BASE + "/import", content=raw, headers={**headers, "x-csrf-token": "wrong"}
            )
        ).status_code == 403
        for bad_headers in (
            {"content-type": "application/json"},
            {**headers, "content-length": "-1"},
            {**headers, "content-length": "9999999999"},
            {**headers, "x-bundle-review": "not-a-token"},
            {**headers, "x-bundle-public": "yes"},
        ):
            assert (
                await target.client.post(BASE + "/import", content=raw, headers=bad_headers)
            ).status_code == 400
        assert (
            await target.client.post(
                BASE + "/import", content=b"x" * (fmt.MAX_FILE_BYTES + 1), headers=headers
            )
        ).status_code == 400

        async def chunks():
            yield b"x" * 100000
            yield b"x" * 100000

        assert (
            await target.client.post(BASE + "/import", content=chunks(), headers=headers)
        ).status_code == 400
        body = {"destination": target.identity, "stream": "incidents", "public_labels": True}
        for extra in (
            {"actor": "administrator"},
            {"after": True},
            {"public_labels": "true"},
            {"expected_token": "invalid"},
        ):
            assert (
                await source.client.post(BASE + "/export", json={**body, **extra})
            ).status_code == 422
        assert (
            await source.client.post(BASE + "/identity", json={"identity": source.identity})
        ).status_code == 200
        assert (
            await source.client.post(BASE + "/identity", json={"identity": target.identity})
        ).status_code == 403
        valid = await target.client.post(BASE + "/import", content=raw, headers=headers)
        assert valid.status_code == 200 and valid.headers["cache-control"] == "no-store"
        assert not await target.app.database.read("SELECT * FROM incident")
        target.app.config.modules.fed.enabled = False
        assert (await target.client.get(BASE)).status_code == 409
        assert (
            await target.client.post(BASE + "/import", content=raw, headers=headers)
        ).status_code == 409


@pytest.mark.parametrize("change", ["role", "session"])
async def test_operator_revocation_after_http_auth_before_writer_is_rechecked(
    bundle_nodes, monkeypatch, change
):
    source, target = bundle_nodes
    await incident(source)
    raw, _ = await exported(source)
    headers = {"content-type": "application/octet-stream"}
    view = (await target.client.post(BASE + "/import", content=raw, headers=headers)).json()
    entered, release = asyncio.Event(), asyncio.Event()
    original = target.service.receive

    async def pause(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(target.service, "receive", pause)
    request = asyncio.create_task(
        target.client.post(
            BASE + "/import",
            content=raw,
            headers={
                **headers,
                "x-bundle-review": view["review_token"],
                "x-bundle-public": "true",
                "x-bundle-labels": "true",
            },
        )
    )
    await entered.wait()
    await target.app.database.write(
        "DELETE FROM web_session" if change == "session" else "UPDATE web_account SET role='viewer'"
    )
    release.set()
    assert (await request).status_code == 403
    assert not await target.app.database.read("SELECT * FROM incident")
    assert not await target.app.database.read("SELECT * FROM fed_bundle_receipt")


async def test_slow_streamed_upload_has_a_deadline_without_starting_an_import(
    bundle_nodes, monkeypatch
):
    source, target = bundle_nodes
    monkeypatch.setattr(bundle_routes, "UPLOAD_SECONDS", 0.01)

    async def slow_chunks():
        yield b"{"
        await asyncio.sleep(1)
        yield b"}"

    response = await target.client.post(
        BASE + "/import",
        content=slow_chunks(),
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 400 and "limit" in response.json()["error"]["message"]
    assert not await target.app.database.read("SELECT * FROM fed_bundle_receipt")
    assert not await target.app.database.read("SELECT * FROM fed_inbox_item")


async def open_browser(stack, node, width):
    playwright = await stack.enter_async_context(async_playwright())
    browser = await playwright.chromium.launch(headless=True)
    stack.push_async_callback(browser.close)
    context = await browser.new_context(
        viewport={"width": width, "height": 900}, accept_downloads=True
    )
    stack.push_async_callback(context.close)
    dashboard = Dashboard(context, node.client)
    await context.route("**/*", dashboard.route)
    page = await context.new_page()
    page.on("pageerror", lambda error: dashboard.errors.append(str(error)))
    await page.goto("http://outpost.test/bundles.html")
    await expect(page.locator("#bundle-status")).to_contain_text("Ready for operator review")
    return page, dashboard


@pytest.mark.parametrize("width", [320, 1280])
async def test_actual_browser_signed_download_preview_upload_commit_is_offline_and_safe(
    bundle_nodes, width
):
    source, target = bundle_nodes
    row = await incident(source)
    await source.app.database.write(
        "UPDATE incident SET body='<img src=x onerror=window.bundle_xss=1>' WHERE id=?", (row.id,)
    )
    async with AsyncExitStack() as stack:
        page, sender = await open_browser(stack, source, width)
        await page.locator('#bundle-export input[name="destination"]').fill(target.identity)
        await page.locator('#bundle-export input[name="labels"]').check()
        await page.get_by_role("button", name="Preview export", exact=True).click()
        await expect(page.locator("#bundle-export-preview")).to_contain_text("synthetic")
        await expect(page.locator("#bundle-download")).to_be_enabled()
        await page.locator("#bundle-download").click()
        await expect(page.locator("#bundle-status")).to_contain_text("Explicit public-content")
        await page.locator("#bundle-export-consent").check()
        async with page.expect_download() as downloaded:
            await page.locator("#bundle-download").click()
        download = await downloaded.value
        from pathlib import Path

        raw = await asyncio.to_thread(Path(await download.path()).read_bytes)
        assert fmt.decode(raw).core["origin"] == source.identity
        receiver, target_dashboard = await open_browser(stack, target, width)
        await receiver.locator('#bundle-import input[name="file"]').set_input_files(
            {"name": "carried.opb", "mimeType": "application/octet-stream", "buffer": raw}
        )
        await receiver.get_by_role("button", name="Verify and preview import").click()
        await expect(receiver.locator("#bundle-status")).to_contain_text("Preview only")
        assert not await target.app.database.read("SELECT * FROM incident")
        await receiver.locator("#bundle-apply").click()
        await expect(receiver.locator("#bundle-status")).to_contain_text("Approve public content")
        await receiver.locator("#bundle-import-consent").check()
        await receiver.locator("#bundle-import-labels").check()
        await receiver.locator("#bundle-apply").click()
        await expect(receiver.locator("#bundle-status")).to_contain_text(
            "Import committed and audited: 1 imported"
        )
        for document in (page, receiver):
            assert await document.evaluate("window.bundle_xss") is None
            assert await document.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not await document.locator("pre img").count()
        assert not sender.external and not target_dashboard.external
        assert not sender.errors and not target_dashboard.errors
        assert not source.app.radio.sent and not target.app.radio.sent
        assert len(await target.app.database.read("SELECT * FROM incident")) == 1
        assert not await target.app.database.read("SELECT * FROM outbound_work")


@pytest.mark.parametrize("failure", ["conflict", "lost_response", "denied"])
async def test_browser_stale_or_lost_response_never_resubmits_a_decision(
    bundle_nodes, monkeypatch, failure
):
    source, target = bundle_nodes
    await incident(source)
    raw, _ = await exported(source)
    async with AsyncExitStack() as stack:
        page, dashboard = await open_browser(stack, target, 320)
        await page.locator('#bundle-import input[name="file"]').set_input_files(
            {"name": "carried.opb", "mimeType": "application/octet-stream", "buffer": raw}
        )
        await page.get_by_role("button", name="Verify and preview import").click()
        await expect(page.locator("#bundle-status")).to_contain_text("Preview only")
        calls = 0
        original = target.service.receive

        async def receive(*args, **kwargs):
            nonlocal calls
            if kwargs.get("expected_token"):
                calls += 1
            return await original(*args, **kwargs)

        monkeypatch.setattr(target.service, "receive", receive)
        if failure == "conflict":
            await incident(target, "hazard concurrent change")
        elif failure == "denied":
            await target.app.database.write("UPDATE fed_relay_origin_key SET state='rejected'")
        else:

            async def lose_response(route):
                if route.request.headers.get("x-bundle-review"):
                    response = await target.client.post(
                        BASE + "/import",
                        content=route.request.post_data_buffer,
                        headers=route.request.headers,
                    )
                    assert response.status_code == 200
                    await route.abort("failed")
                else:
                    await dashboard.route(route)

            await page.route("**/api/v1/federation/bundles/import", lose_response)
        await page.locator("#bundle-import-consent").check()
        await page.locator("#bundle-import-labels").check()
        await page.locator("#bundle-apply").click()
        await expect(page.locator("#bundle-status")).to_contain_text("No automatic retry")
        await expect(page.locator("#bundle-apply")).to_be_disabled()
        await page.wait_for_timeout(150)
        assert calls == 1
        receipts = await target.app.database.read("SELECT * FROM fed_bundle_receipt")
        assert len(receipts) == (1 if failure == "lost_response" else 0)
        assert not dashboard.external
