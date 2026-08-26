from __future__ import annotations

import shutil
import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
import uvicorn

from outpost.web.api import create_web_app

playwright = pytest.importorskip("playwright.sync_api")

BROWSER_PATH = next(
    (
        path
        for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
        if (path := shutil.which(name)) is not None
    ),
    None,
)
VIEWPORTS = (320, 390, 768, 1280)
DESTINATIONS = (
    ("Overview", "/"),
    ("Members", "/operator.html"),
    ("BBS", "/bbs.html"),
    ("Mail", "/mail.html"),
    ("Watch", "/watch.html"),
    ("Environment", "/environment.html"),
    ("Radio", "/radio.html"),
    ("Federation", "/federation.html"),
    ("Backups", "/backups.html"),
    ("Activity", "/#activity"),
    ("System", "/#system"),
    ("AI", "/#system"),
    ("API", "/api/docs"),
)


@pytest.fixture(scope="module")
def dashboard_url() -> Iterator[str]:
    app = create_web_app(lambda: {"radio": "up"})
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = int(sock.getsockname()[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        pytest.fail("dashboard test server did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
    sock.close()


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with playwright.sync_playwright() as runtime:
        try:
            instance = runtime.chromium.launch(headless=True, executable_path=BROWSER_PATH)
        except playwright.Error as error:
            pytest.skip(f"Chromium is required for responsive navigation tests: {error}")
        yield instance
        instance.close()


def prepare_page(browser: object, width: int, dashboard_url: str) -> object:
    page = browser.new_page(viewport={"width": width, "height": 900})  # type: ignore[attr-defined]
    page.route(
        "**/api/v1/auth/session",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"authenticated":true,"csrf_token":"test","must_change":false}',
        ),
    )
    page.goto(dashboard_url, wait_until="domcontentloaded")
    page.locator(".rail nav a").first.wait_for(state="attached")
    return page


@pytest.mark.parametrize("width", VIEWPORTS)
def test_every_destination_is_reachable_from_navigation(
    browser: object, dashboard_url: str, width: int
) -> None:
    page = prepare_page(browser, width, dashboard_url)
    try:
        labels = page.locator(".rail nav a").evaluate_all(
            "links => links.map(link => link.getAttribute('aria-label'))"
        )
        assert labels == [label for label, _target in DESTINATIONS]
        for label, target in DESTINATIONS:
            page.goto(dashboard_url, wait_until="domcontentloaded")
            page.locator(".rail nav a").first.wait_for(state="attached")
            if width <= 820:
                page.get_by_role("button", name="Open navigation").click()
            link = page.locator(f'.rail nav a[aria-label="{label}"]')
            link.scroll_into_view_if_needed()
            assert link.is_visible()
            link.click()
            expected = urlparse(dashboard_url + target)
            page.wait_for_function(
                "expected => location.pathname + location.hash === expected",
                arg=f"{expected.path}{expected.fragment and '#' + expected.fragment}",
            )
    finally:
        page.close()


def test_mobile_menu_has_keyboard_current_page_and_review_states(
    browser: object, dashboard_url: str
) -> None:
    page = prepare_page(browser, 390, dashboard_url)
    try:
        page.route(
            "**/api/v1/federation/inbox?state=pending",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body='{"items":[{"stream":"incidents"}]}',
            ),
        )
        page.goto(f"{dashboard_url}/watch.html", wait_until="domcontentloaded")
        toggle = page.get_by_role("button", name="Open navigation")
        toggle.focus()
        page.keyboard.press("Enter")
        navigation = page.get_by_role("navigation", name="Primary navigation")
        navigation.wait_for(state="visible")
        assert (
            page.locator(".rail nav a[aria-current='page']").get_attribute("aria-label") == "Watch"
        )
        page.locator('.rail nav a[aria-label="Watch"] .nav-review-badge').wait_for()
        assert page.locator('.rail nav a[aria-label="Watch"]').get_attribute("class") == (
            "active needs-review"
        )
        page.keyboard.press("Escape")
        assert not navigation.is_visible()
        assert page.evaluate(
            "document.activeElement === document.querySelector('.mobile-nav-toggle')"
        )
    finally:
        page.close()
