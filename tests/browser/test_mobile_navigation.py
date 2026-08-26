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


def route_operator_workspace(page: object, seen_audit_urls: list[str]) -> None:
    page.route(
        "**/api/v1/members*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "items": [],
                    "approved_count": 0,
                    "discovered_count": 0,
                    "trusted_count": 0,
                }
            ),
        ),
    )
    page.route(
        "**/api/v1/security/safety-floor",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "summary": {"attempts": 0, "coalesced": 0},
                    "items": [],
                }
            ),
        ),
    )

    def audit(route: object) -> None:
        seen_audit_urls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "items": [
                        {
                            "id": 1,
                            "actor_kind": "web",
                            "actor_ref": "operator-with-a-long-reference",
                            "action": "federation.origin_adopt",
                            "target": "fed_peer:an-unbroken-target-value-that-must-wrap-safely",
                            "detail": '{\n  "old_mesh_id": "!00000001",\n  "safe": true\n}',
                            "detail_format": "json",
                            "outcome": "success",
                            "created_at": "2026-08-26T20:00:00Z",
                        }
                    ],
                    "total": 1,
                    "next_cursor": None,
                }
            ),
        )

    page.route("**/api/v1/audit*", audit)


@pytest.mark.parametrize("width", (320, 390))
def test_mobile_audit_cards_filter_wrap_expand_and_copy(
    browser: object, dashboard_url: str, width: int
) -> None:
    page = prepare_page(browser, width, dashboard_url, theme="daylight")
    seen_audit_urls: list[str] = []
    route_operator_workspace(page, seen_audit_urls)
    try:
        page.goto(f"{dashboard_url}/operator.html#audit", wait_until="domcontentloaded")
        wait_for_navigation(page)
        event = page.locator("#audit-list .audit-event")
        event.wait_for()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert event.locator(".audit-target small").is_visible()
        assert (
            event.locator(".audit-target span").evaluate(
                "element => getComputedStyle(element).overflowWrap"
            )
            == "anywhere"
        )

        page.locator("#audit-actor").fill("web:operator")
        page.locator("#audit-action").fill("origin")
        page.get_by_role("button", name="Apply filters").click()
        page.wait_for_function(
            "() => document.querySelector('#audit-summary').textContent.includes('1 of 1')"
        )
        assert any(
            "actor=web%3Aoperator" in url and "action=origin" in url for url in seen_audit_urls
        )

        event.locator("summary").click()
        assert '"old_mesh_id"' in event.locator("pre").text_content()
        page.evaluate(
            "() => Object.defineProperty(navigator, 'clipboard', {configurable: true, "
            "value: {writeText: async value => { window.__auditCopied = value; }}})"
        )
        event.get_by_role("button", name="Copy details").click()
        page.wait_for_function("() => window.__auditCopied?.includes('old_mesh_id')")
        assert event.get_by_role("status").text_content() == "Copied"
    finally:
        page.close()


def test_desktop_audit_retains_dense_scanning_columns(browser: object, dashboard_url: str) -> None:
    page = prepare_page(browser, 1280, dashboard_url, theme="dark")
    seen_audit_urls: list[str] = []
    route_operator_workspace(page, seen_audit_urls)
    try:
        page.goto(f"{dashboard_url}/operator.html#audit", wait_until="domcontentloaded")
        wait_for_navigation(page)
        event = page.locator("#audit-list .audit-event")
        event.wait_for()
        assert page.locator(".audit-columns").is_visible()
        assert (
            len(
                event.evaluate(
                    "element => getComputedStyle(element).gridTemplateColumns.split(' ')"
                )
            )
            == 4
        )
        assert page.screenshot().startswith(b"\x89PNG")
    finally:
        page.close()


def route_operations_inbox(page: object, mutations: list[tuple[str, object]]) -> None:
    conversation = {
        "conversation_key": "fed:!bbbbbbbb:abc123",
        "subject": "Moderation review",
        "message_kind": "member",
        "participant_handle": "666",
        "operator_actor": "web:operator",
        "route_kind": "federated",
        "peer_mesh_id": "!bbbbbbbb",
        "peer_name": "Denver Outpost",
        "transports": ["radio", "mqtt"],
        "latest_from": "666@DEN",
        "latest_to": "operator",
        "latest_direction": "in",
        "latest_state": "delivered",
        "created_at": "2026-08-26T18:00:00Z",
        "updated_at": "2026-08-26T18:05:00Z",
        "message_count": 2,
        "unread_count": 1,
        "failed_count": 0,
        "action_required": True,
        "archived_at": None,
        "reply_available": True,
        "reply_address": "666",
    }
    messages = [
        {
            "id": 1,
            "uid": "fed-out:one",
            "conversation_key": conversation["conversation_key"],
            "federation_conversation_id": "abc123",
            "from_label": "operator@PIT",
            "to_label": "666",
            "subject": "Moderation review",
            "body": "Please review the reported post.",
            "created_at": "2026-08-26T18:00:00Z",
            "delivered_at": "2026-08-26T18:01:00Z",
            "operator_read_at": "2026-08-26T18:00:00Z",
            "archived_at": None,
            "state": "delivered",
            "message_kind": "member",
            "mail_direction": "out",
            "source_peer_mesh_id": "!bbbbbbbb",
            "reply_recipient_handle": "666",
            "participant_handle": "666",
            "operator_actor": "web:operator",
            "node_name": "Denver Outpost",
            "transports": ["radio", "mqtt"],
        },
        {
            "id": 2,
            "uid": "fed:two",
            "conversation_key": conversation["conversation_key"],
            "federation_conversation_id": "abc123",
            "from_label": "666@DEN",
            "to_label": "operator",
            "subject": "Moderation review",
            "body": "I reviewed it and removed the post.",
            "created_at": "2026-08-26T18:05:00Z",
            "delivered_at": "2026-08-26T18:05:00Z",
            "operator_read_at": None,
            "archived_at": None,
            "state": "delivered",
            "message_kind": "member",
            "mail_direction": "in",
            "source_peer_mesh_id": "!bbbbbbbb",
            "reply_recipient_handle": "666",
            "participant_handle": "666",
            "operator_actor": "member:@666",
            "node_name": "Denver Outpost",
            "transports": ["radio", "mqtt"],
        },
    ]

    def mail(route: object) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path == "/api/v1/mail/conversations":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "items": [conversation],
                        "total": 1,
                        "counts": {"unread": 1, "actionable": 1, "failed": 0},
                    }
                ),
            )
        elif path.endswith("/reply"):
            mutations.append(("reply", request.post_data_json))
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"relay_id":"reply","state":"sent"}',
            )
        elif request.method == "PATCH":
            mutations.append(("state", request.post_data_json))
            route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
        else:
            detail = {**conversation, "unread_count": 0}
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"conversation": detail, "messages": messages}),
            )

    page.route("**/api/v1/mail/conversations**", mail)
    page.route(
        "**/api/v1/federation/peers*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "items": [
                        {
                            "mesh_id": "!bbbbbbbb",
                            "node_name": "Denver Outpost",
                            "state": "active",
                            "relay_mail": True,
                            "discovery_transports": ["radio", "mqtt"],
                        }
                    ]
                }
            ),
        ),
    )

    def compose(route: object) -> None:
        mutations.append(("compose", route.request.post_data_json))
        route.fulfill(
            status=200,
            content_type="application/json",
            body='{"relay_id":"new","state":"sent"}',
        )

    page.route("**/api/v1/federation/mail", compose)


@pytest.mark.parametrize(
    ("width", "theme"),
    ((390, "daylight"), (1280, "dark"), (1280, "daylight"), (1280, "night")),
)
def test_operations_inbox_conversation_reply_compose_and_responsive_layout(
    browser: object, dashboard_url: str, width: int, theme: str
) -> None:
    page = prepare_page(browser, width, dashboard_url, theme=theme)
    mutations: list[tuple[str, object]] = []
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    route_operations_inbox(page, mutations)
    try:
        page.goto(f"{dashboard_url}/mail.html", wait_until="domcontentloaded")
        wait_for_navigation(page)
        page.get_by_role("button", name="Moderation review").wait_for()
        assert page.locator("#unread-count").text_content() == "1"
        assert page.locator(".mail-badge.unread").is_visible()
        nav_badge = page.locator('.rail nav a[href="/mail.html"] .nav-review-badge')
        nav_badge.wait_for(state="attached")
        assert nav_badge.get_attribute("aria-label") == "1 actionable mail conversations"
        page.get_by_role("button", name="Moderation review").click()
        page.wait_for_timeout(250)
        assert page_errors == []
        page.locator(".conversation-detail").wait_for()
        assert "MEMBER · @666" in page.locator(".identity-row").text_content()
        assert "@666 at Denver Outpost" in page.locator(".conversation-reply").text_content()
        assert page.locator(".mail-message").count() == 2
        page.locator("#reply-body").fill("Thank you for handling it.")
        with page.expect_request(lambda request: request.url.endswith("/reply")):
            page.locator("#conversation-reply").get_by_role("button", name="Send reply").click()
        assert ("reply", {"body": "Thank you for handling it."}) in mutations

        page.get_by_role("button", name="New message").click()
        dialog = page.get_by_role("dialog", name="New operations message")
        dialog.wait_for()
        dialog.get_by_label("Recipient").select_option("member")
        dialog.get_by_label("Member handle").fill("777")
        dialog.get_by_label("Subject / context").fill("Admin follow-up")
        dialog.get_by_label("Message").fill("Please contact the operator.")
        assert "@777 at Denver Outpost" in dialog.locator(".route-preview").text_content()
        dialog.get_by_role("button", name="Queue encrypted message").click()
        page.wait_for_function("() => !document.querySelector('#compose-dialog').open")
        assert (
            "compose",
            {
                "peer_mesh_id": "!bbbbbbbb",
                "recipient_handle": "777",
                "subject": "Admin follow-up",
                "body": "Please contact the operator.",
            },
        ) in mutations
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if width == 1280:
            results = Axe().run(
                page,
                options={
                    "runOnly": {
                        "type": "tag",
                        "values": ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"],
                    },
                    "resultTypes": ["violations"],
                },
            )
            assert results.violations_count == 0, results.generate_report()
    finally:
        page.close()


def route_federation_policy_workspace(page: object, applied: list[dict[str, object]]) -> None:
    peer = {
        "id": 1,
        "mesh_id": "!00000002",
        "node_name": "Denver Outpost",
        "state": "active",
        "protocol_version": 1,
        "capabilities": {"weather": True, "alerts": True, "bbs": True},
        "discovery_transports": ["radio", "mqtt"],
        "tx_counter": 2,
        "rx_counter": 3,
        "last_seen_at": 2_000_000_000,
        "local_approved": True,
        "remote_approved": True,
        "boards": ["roads"],
        "sync_incidents": False,
        "incident_lat": 39.7392,
        "incident_lon": -104.9903,
        "incident_radius_km": 40,
        "relay_alerts": False,
        "relay_mail": False,
        "quota_items_per_hour": 37,
        "quota_mail_per_hour": 11,
        "policy_configured": True,
        "policy_applied_by": "web:operator",
        "policy_applied_at": 2_000_000_000,
        "policy_review_at": None,
        "service_permissions": ["weather"],
        "quota_services_per_hour": 9,
        "service_concurrency": 2,
        "service_max_response_bytes": 900,
        "service_airtime_seconds_per_hour": 12,
    }

    def federation(route: object) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path.endswith("/sync-policy") and request.method == "PUT":
            applied.append(request.post_data_json)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(peer))
        elif path == "/api/v1/federation/peers":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"items": [peer]}),
            )
        elif path == "/api/v1/federation/mqtt":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "available": False,
                        "enabled": False,
                        "address": "",
                        "root": "msh",
                        "tls_enabled": True,
                        "channels": [
                            {
                                "index": 0,
                                "name": "Primary",
                                "uplink_enabled": False,
                                "downlink_enabled": False,
                            }
                        ],
                    }
                ),
            )
        elif path in {
            "/api/v1/federation/services",
            "/api/v1/federation/inbox",
            "/api/v1/federation/origins",
            "/api/v1/federation/mail",
        }:
            route.fulfill(status=200, content_type="application/json", body='{"items":[]}')
        elif path == "/api/v1/federation/sync-status":
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"items":[],"outbound":{"frames_24h":0,"last_at":null}}',
            )
        else:
            route.fulfill(status=404, content_type="application/json", body='{"error":{}}')

    page.route("**/api/v1/federation/**", federation)
    page.route(
        "**/api/v1/status",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"radio":"up"}'
        ),
    )
    page.route(
        "**/api/v1/boards*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "items": [
                        {"id": 1, "slug": "gen", "title": "General", "federated": 0},
                        {"id": 2, "slug": "roads", "title": "Roads", "federated": 1},
                    ]
                }
            ),
        ),
    )


@pytest.mark.parametrize(
    ("width", "theme"),
    ((390, "daylight"), (1280, "dark"), (1280, "daylight"), (1280, "night")),
)
def test_federation_policy_wizard_presets_diff_and_global_board_confirmation(
    browser: object, dashboard_url: str, width: int, theme: str
) -> None:
    page = prepare_page(browser, width, dashboard_url, theme=theme)
    applied: list[dict[str, object]] = []
    route_federation_policy_workspace(page, applied)
    try:
        page.goto(f"{dashboard_url}/federation.html", wait_until="domcontentloaded")
        wait_for_navigation(page)
        assert "Policy by web:operator" in page.locator(".peer-policy-meta").text_content()
        page.get_by_role("button", name="Sharing setup").click()
        dialog = page.get_by_role("dialog")
        dialog.wait_for()
        assert dialog.get_by_role("button", name="Discovery only").is_visible()
        assert dialog.get_by_role("button", name="BBS only").is_visible()
        assert dialog.get_by_role("button", name="Mutual aid").is_visible()
        dialog.get_by_role("button", name="Full trusted partner").click()
        dialog.locator("#wizard-review-date").fill("2030-06-01")
        dialog.get_by_role("button", name="Review sharing").click()

        assert dialog.locator(".wizard-diff-row").count() == 9
        assert "2 board stream(s)" in dialog.locator("#wizard-sharing-summary").text_content()
        confirmation = dialog.locator("#wizard-board-confirmation")
        assert confirmation.is_visible()
        assert confirmation.locator("strong").text_content() == "gen"
        if width == 1280:
            results = Axe().run(
                page,
                options={
                    "runOnly": {
                        "type": "tag",
                        "values": ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"],
                    },
                    "resultTypes": ["violations"],
                },
            )
            assert results.violations_count == 0, results.generate_report()
        dialog.get_by_role("button", name="Apply policy").click()
        assert applied == []
        assert "Confirm the global board" in dialog.locator("#wizard-result").text_content()

        confirmation.locator("input").check()
        dialog.get_by_role("button", name="Apply policy").click()
        page.wait_for_function("() => !document.querySelector('.sharing-wizard')")
        assert len(applied) == 1
        assert applied[0]["boards"] == ["gen", "roads"]
        assert applied[0]["enable_boards"] == ["gen"]
        assert applied[0]["confirm_enable_boards"] is True
        assert applied[0]["service_permissions"] == ["alerts", "knowledge", "weather"]
        assert applied[0]["quota_items_per_hour"] == 37
        assert applied[0]["quota_mail_per_hour"] == 11
        assert applied[0]["policy_review_at"] == "2030-06-01T23:59:59Z"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    finally:
        page.close()
