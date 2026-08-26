from __future__ import annotations

import json
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
ACTION_SECTIONS = (
    ("/federation.html", "Nearby and paired Outposts"),
    ("/operator.html", "Community members"),
    ("/radio.html", "Message log"),
    ("/watch.html", "Open incidents"),
)
MODULE_PAGES = (
    ("bbs", "BBS", "/bbs.html", "Community boards is offline"),
    ("watch", "Watch", "/watch.html", "Community Watch is offline"),
    ("env", "Environment", "/environment.html", "Environment is offline"),
    ("fed", "Federation", "/federation.html", "Federation is offline"),
)


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


def test_weather_forecast_provenance_and_unavailable_values_are_visible(
    browser: object, dashboard_url: str
) -> None:
    page = prepare_page(browser, 1280, dashboard_url)
    try:
        page.route(
            "**/api/v1/environment/weather",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=(
                    '{"provider":"nws","source_kind":"forecast",'
                    '"source_detail":"This Afternoon","temperature_c":20.0,'
                    '"apparent_c":null,"precipitation_mm":null,"wind_kph":null,'
                    '"wind_direction":null,"weather_code":null,'
                    '"valid_at":"2026-08-26T15:00:00-04:00",'
                    '"valid_age_seconds":300,"age_seconds":5,"stale":false,'
                    '"units":"metric"}'
                ),
            ),
        )
        page.evaluate("refreshWeather()")

        assert page.locator("#weather-kind").text_content() == "LOCAL FORECAST"
        assert page.locator("#weather-title").text_content() == "Near-term forecast"
        summary = page.locator("#weather-summary").text_content()
        assert "nws" in summary and "This Afternoon" in summary and "valid 5m ago" in summary
        assert page.locator("#weather-reading span").text_content() == "Feels-like unavailable"
        assert page.locator("#weather-details b").all_text_contents() == ["—", "—", "—"]
        assert page.locator("#weather-details em").all_text_contents() == [
            "Wind speed unavailable",
            "Precipitation unavailable",
            "Wind direction unavailable",
        ]
    finally:
        page.close()


def heading_action_layout(heading: object) -> dict[str, object]:
    return heading.evaluate(
        """heading => {
          const actions = heading.querySelector('.heading-actions');
          if (!actions) return {enhanced: false};
          const box = actions.getBoundingClientRect();
          const children = [...actions.children].map(child => {
            const childBox = child.getBoundingClientRect();
            return {
              left: childBox.left,
              right: childBox.right,
              clippedText: child.scrollWidth > child.clientWidth + 1
                || child.scrollHeight > child.clientHeight + 1,
            };
          });
          return {
            enhanced: true,
            left: box.left,
            right: box.right,
            viewport: window.innerWidth,
            clippedContent: actions.scrollWidth > actions.clientWidth + 1,
            children,
          };
        }"""
    )


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("width", VIEWPORTS)
def test_federation_heading_actions_have_visual_coverage_without_clipping(
    browser: object, dashboard_url: str, theme: str, width: int
) -> None:
    page = prepare_page(browser, width, dashboard_url, theme=theme)
    try:
        page.goto(f"{dashboard_url}/federation.html", wait_until="domcontentloaded")
        wait_for_navigation(page)
        heading = page.locator(".heading").filter(has_text="Nearby and paired Outposts").first
        heading.locator(".heading-actions").wait_for()
        layout = heading_action_layout(heading)

        assert layout["enhanced"] is True
        assert layout["left"] >= 0 and layout["right"] <= layout["viewport"]
        assert layout["clippedContent"] is False
        assert all(
            child["left"] >= 0
            and child["right"] <= layout["viewport"]
            and child["clippedText"] is False
            for child in layout["children"]
        )
        screenshot = heading.screenshot(animations="disabled")
        assert screenshot.startswith(b"\x89PNG") and len(screenshot) > 1_000
    finally:
        page.close()


@pytest.mark.parametrize(("path", "title"), ACTION_SECTIONS)
def test_action_heavy_sections_reflow_at_320px_and_200_percent_text(
    browser: object, dashboard_url: str, path: str, title: str
) -> None:
    page = prepare_page(browser, 320, dashboard_url, theme="daylight")
    try:
        page.goto(f"{dashboard_url}{path}", wait_until="domcontentloaded")
        wait_for_navigation(page)
        page.add_style_tag(content="html { font-size: 200% !important; }")
        heading = page.locator(".heading").filter(has_text=title).first
        heading.locator(".heading-actions").wait_for()
        layout = heading_action_layout(heading)

        assert layout["enhanced"] is True
        assert layout["left"] >= 0 and layout["right"] <= layout["viewport"]
        assert layout["clippedContent"] is False
        assert all(
            child["left"] >= 0
            and child["right"] <= layout["viewport"]
            and child["clippedText"] is False
            for child in layout["children"]
        )
        screenshot = heading.screenshot(animations="disabled")
        assert screenshot.startswith(b"\x89PNG") and len(screenshot) > 1_000
    finally:
        page.close()


@pytest.mark.parametrize(("module", "label", "path", "heading"), MODULE_PAGES)
def test_disabled_module_pages_are_explained_and_inert(
    browser: object,
    dashboard_url: str,
    module: str,
    label: str,
    path: str,
    heading: str,
) -> None:
    page = prepare_page(browser, 390, dashboard_url, theme="daylight")
    states = {
        name: {"enabled": name != module, "restart_required_to_change": True}
        for name in ("bbs", "ai", "watch", "env", "fed")
    }
    page.route(
        "**/api/v1/modules",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": states, "change_policy": "restart_required"}),
        ),
    )
    try:
        page.goto(f"{dashboard_url}{path}", wait_until="domcontentloaded")
        wait_for_navigation(page)
        page.get_by_role("heading", name=heading).wait_for()
        link = page.locator(f'.rail nav a[aria-label="{label}"]')
        assert link.get_attribute("aria-disabled") == "true"
        assert "restart required" in link.get_attribute("title")
        assert page.locator("main > :not(.module-disabled-banner)[inert]").count() > 0
        assert "restart the service" in page.locator(".module-disabled-banner").text_content()
    finally:
        page.close()


def test_capability_cards_reflect_disabled_modules(browser: object, dashboard_url: str) -> None:
    page = prepare_page(browser, 1280, dashboard_url, theme="dark")
    states = {
        name: {
            "enabled": name not in {"bbs", "watch", "ai"},
            "restart_required_to_change": True,
        }
        for name in ("bbs", "ai", "watch", "env", "fed")
    }
    page.route(
        "**/api/v1/modules",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": states, "change_policy": "restart_required"}),
        ),
    )
    try:
        page.goto(dashboard_url, wait_until="domcontentloaded")
        wait_for_navigation(page)
        page.wait_for_function(
            "() => document.querySelectorAll("
            "'.capability-grid article.module-disabled').length === 3"
        )
        disabled = page.locator(".capability-grid article.module-disabled")
        assert disabled.locator("b").all_text_contents() == [
            "Moderation",
            "AI settings",
            "Emergency settings",
        ]
        assert disabled.locator(".phase").all_text_contents() == [
            "DISABLED",
            "DISABLED",
            "DISABLED",
        ]
        assert page.locator('.rail nav a[aria-label="AI"]').get_attribute("aria-disabled") == (
            "true"
        )
    finally:
        page.close()
