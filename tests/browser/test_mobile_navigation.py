from __future__ import annotations

import shutil
import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlparse

import pytest
import uvicorn
from axe_playwright_python.sync_playwright import Axe

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
OPERATOR_PAGES = tuple(
    dict.fromkeys(target.split("#", 1)[0] for _label, target in DESTINATIONS[:-1])
)
THEMES = ("dark", "daylight", "night")


def wait_for_navigation(page: object) -> None:
    page.wait_for_function(
        f"() => document.querySelectorAll('.rail nav a[aria-label]').length === {len(DESTINATIONS)}"
    )
    page.wait_for_function("() => !document.querySelector('.rail')?.inert")


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


def prepare_page(
    browser: object, width: int, dashboard_url: str, *, theme: str | None = None
) -> object:
    page = browser.new_page(viewport={"width": width, "height": 900})  # type: ignore[attr-defined]
    if theme is not None:
        page.add_init_script(f"localStorage.setItem('outpost.appearance.theme', {theme!r})")
    page.route(
        "**/api/v1/auth/session",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"authenticated":true,"csrf_token":"test","must_change":false}',
        ),
    )
    page.goto(dashboard_url, wait_until="domcontentloaded")
    wait_for_navigation(page)
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
            wait_for_navigation(page)
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


@pytest.mark.parametrize("theme", THEMES)
def test_operator_pages_pass_wcag_axe_rules(
    browser: object, dashboard_url: str, theme: str
) -> None:
    page = prepare_page(browser, 1280, dashboard_url, theme=theme)
    axe = Axe()
    try:
        for target in OPERATOR_PAGES:
            page.goto(f"{dashboard_url}{target}", wait_until="domcontentloaded")
            wait_for_navigation(page)
            results = axe.run(
                page,
                options={
                    "runOnly": {
                        "type": "tag",
                        "values": ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"],
                    },
                    "resultTypes": ["violations"],
                },
            )
            assert results.violations_count == 0, f"{theme} {target}\n{results.generate_report()}"
    finally:
        page.close()


def test_shared_dialogs_manage_focus_escape_validation_and_live_regions(
    browser: object, dashboard_url: str
) -> None:
    page = prepare_page(browser, 1280, dashboard_url)
    try:
        opener = page.locator("#open-settings")
        opener.focus()
        page.evaluate("document.querySelector('#settings-screen').classList.remove('hidden')")
        settings = page.get_by_role("dialog", name="Identity and locality")
        settings.wait_for(state="visible")
        assert page.evaluate("document.querySelector('.shell').inert")
        page.keyboard.press("Escape")
        assert not settings.is_visible()
        assert page.evaluate("document.activeElement === document.querySelector('#open-settings')")

        page.evaluate(
            "() => { window.confirmResult = null; "
            "window.OutpostUI.confirm({title:'Restart receiver?', message:'Test confirmation'})"
            ".then(value => window.confirmResult = value); }"
        )
        confirmation = page.get_by_role("dialog", name="Restart receiver?")
        confirmation.wait_for(state="visible")
        page.keyboard.press("Escape")
        page.wait_for_function("() => window.confirmResult === false")

        page.evaluate(
            "() => { window.promptResult = null; "
            "window.OutpostUI.prompt({title:'Typed approval', message:'Test prompt', "
            "label:'Confirmation', verification:'APPROVE'})"
            ".then(value => window.promptResult = value); }"
        )
        prompt = page.get_by_role("dialog", name="Typed approval")
        prompt.get_by_role("textbox", name="Confirmation").fill("wrong")
        prompt.get_by_role("button", name="Continue").click()
        assert prompt.get_by_role("alert").text_content() == (
            "The confirmation text does not match."
        )
        prompt.get_by_role("textbox", name="Confirmation").fill("APPROVE")
        prompt.get_by_role("button", name="Continue").click()
        page.wait_for_function("() => window.promptResult === 'APPROVE'")
        assert page.locator("#backup-result").get_attribute("role") == "status"
        assert page.locator("#settings-error").get_attribute("role") == "alert"
    finally:
        page.close()


def test_map_targets_and_list_alternatives_are_keyboard_ready(
    browser: object, dashboard_url: str
) -> None:
    page = prepare_page(browser, 390, dashboard_url)
    try:
        page.goto(f"{dashboard_url}/environment.html", wait_until="domcontentloaded")
        wait_for_navigation(page)
        size = page.evaluate(
            """() => {
              const marker = document.createElement('button');
              marker.className = 'environment-marker quake';
              document.querySelector('#environment-markers').appendChild(marker);
              const box = marker.getBoundingClientRect();
              return {width: box.width, height: box.height};
            }"""
        )
        assert size["width"] >= 24 and size["height"] >= 24
        scripts = " ".join(
            page.request.get(f"{dashboard_url}/{name}").text()
            for name in ("environment.js", "member-map.js", "watch.js")
        )
        assert "data-waypoint-focus" in scripts
        assert "member-map-row-open" in scripts
        assert "data-incident-open" in scripts
    finally:
        page.close()


def test_first_run_uses_one_time_token_and_forces_clean_sign_in(
    browser: object, dashboard_url: str
) -> None:
    page = browser.new_page(viewport={"width": 390, "height": 844})  # type: ignore[attr-defined]
    state = {"complete": False}

    def setup_route(route: object) -> None:
        body = (
            '{"required":false,"available":false,"expires_at":null}'
            if state["complete"]
            else '{"required":true,"available":true,"expires_at":2000000000}'
        )
        route.fulfill(status=200, content_type="application/json", body=body)

    def password_route(route: object) -> None:
        state["complete"] = True
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ok":true,"reauthenticate":true}',
        )

    page.route("**/api/v1/auth/setup", setup_route)
    page.route(
        "**/api/v1/auth/session",
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body='{"error":{"code":"unauthorized"}}',
        ),
    )
    page.route(
        "**/api/v1/auth/login",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"csrf_token":"bootstrap-csrf","must_change":true}',
        ),
    )
    page.route("**/api/v1/auth/password", password_route)
    try:
        page.goto(dashboard_url, wait_until="domcontentloaded")
        page.get_by_role("heading", name="Finish Outpost setup").wait_for()
        results = Axe().run(
            page,
            options={
                "runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag22aa"]},
                "resultTypes": ["violations"],
            },
        )
        assert results.violations_count == 0, results.generate_report()
        token = page.get_by_label("One-time setup token")
        token.fill("one-time-value")
        page.get_by_role("button", name="Continue setup").click()

        permanent = page.get_by_label("New permanent password")
        permanent.fill("permanent-password-42")
        page.get_by_label("Confirm permanent password").fill("permanent-password-42")
        page.get_by_role("button", name="Complete setup").click()

        page.get_by_role("heading", name="Sign in to the console").wait_for()
        assert page.locator("#login-error").text_content() == (
            "Permanent password saved. Sign in to continue."
        )
        assert page.locator("#current-password").count() == 0
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
