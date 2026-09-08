"""The actual local vector renderer works with an empty browser cache and no WAN."""

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from axe_playwright_python.sync_playwright import Axe

from outpost.maps.regions import Region
from outpost.maps.setup import MapSetupService
from outpost.web.api import create_web_app
from tests.browser.test_mobile_navigation import browser as browser
from tests.support.maps import source_factory
from tests.unit.test_regional_maps import install


@pytest.fixture(scope="module")
def maps_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("regional-map-ui")
    service = MapSetupService(root)
    factory = source_factory()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(service, "_source", factory)
        patch.setattr("outpost.maps.setup.RangeSource", factory)
        install(service, Region(-1.2864, 36.8172, 2, 12))
    app = create_web_app(
        lambda: {"radio": "up"},
        tile_path=root,
        map_setup=service,
        map_position=lambda: {
            "available": False,
            "detail": "No recent GPS fix. Enter coordinates.",
        },
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)
    sock.close()
    service.close()


@pytest.mark.parametrize("theme", ["dark", "daylight", "night"])
@pytest.mark.parametrize("width", [320, 768, 1280])
def test_vector_maps_render_offline_in_all_themes(browser, maps_url, theme, width):
    context = browser.new_context(viewport={"width": width, "height": 900})
    context.add_init_script(f'localStorage.setItem("outpost.appearance.theme", "{theme}")')
    page = context.new_page()
    external = []
    errors = []
    assets = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "response", lambda response: assets.append(response.url) if response.status == 200 else None
    )

    def route(request):
        if request.request.url.endswith("/api/v1/auth/session"):
            request.fulfill(json={"csrf_token": "test", "role": "administrator"})
        elif request.request.url.startswith(maps_url + "/"):
            request.continue_()
        else:
            external.append(request.request.url)
            request.abort()

    page.route("**/*", route)
    page.goto(maps_url + "/maps.html")
    page.wait_for_function(
        "() => document.querySelector('#map-installed').textContent.includes('Installed:')"
    )
    page.wait_for_function(
        "() => document.querySelector('#map-preview').outpostMapController.vector != null"
    )
    assert page.locator("#map-preview canvas").count() == 1
    page.locator("#map-preview").scroll_into_view_if_needed()
    page.wait_for_timeout(1000)
    assert "Offline regional map" in page.locator(".outpost-map-basemap-state").inner_text()
    assert not errors and not external
    assert any("/tiles/vector/" in url for url in assets)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # The shared controller uses 256-pixel camera units; MVT uses 512. The displayed
    # vector zoom and detail warning must match the source's actual tile zoom.
    page.evaluate("""() => document.querySelector('#map-preview').outpostMapController
      .setView({lat:-1.2864,lon:36.8172,zoom:13})""")
    page.wait_for_function("""() => document.querySelector('.outpost-map-coordinates')
      .textContent.endsWith('z12')""")
    assert "Offline regional map" in page.locator(".outpost-map-basemap-state").inner_text()
    page.evaluate("""() => document.querySelector('#map-preview').outpostMapController
      .setView({zoom:14})""")
    page.wait_for_function("""() => document.querySelector('.outpost-map-basemap-state')
      .textContent.includes('detail ends at z12')""")
    page.locator("#map-preview").focus()
    before = page.evaluate(
        "() => document.querySelector('#map-preview').outpostMapController.getView().lon"
    )
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(100)
    assert (
        page.evaluate(
            "() => document.querySelector('#map-preview').outpostMapController.getView().lon"
        )
        != before
    )
    page.evaluate(
        "() => document.querySelector('#map-preview').outpostMapController"
        ".setView({lat:0,lon:0,zoom:3})"
    )
    page.wait_for_function(
        "() => document.querySelector('.outpost-map-basemap-state').textContent"
        ".includes('World overview only')"
    )
    page.wait_for_timeout(800)
    assert any("/fonts/" in url for url in assets)
    assert not external
    violations = Axe().run(page).response["violations"]
    assert not [v for v in violations if v["impact"] in {"serious", "critical"}]
    output = Path(".data/regional-map-ui-review")
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"{theme}-{width}.png"), full_page=True)
    context.close()


@pytest.mark.parametrize(
    ("path", "selector"),
    [
        ("/watch.html", "#incident-map"),
        ("/environment.html", "#environment-map"),
        ("/operator.html", "#member-map"),
        ("/federation.html", "#topology-map"),
    ],
)
def test_operational_maps_use_local_vector_pack_and_keep_markers(browser, maps_url, path, selector):
    from tests.browser.test_mobile_navigation import (
        prepare_page,
        route_shared_operator_api,
        route_visual_content_api,
    )

    page = prepare_page(browser, 1280, maps_url)
    route_shared_operator_api(page)
    route_visual_content_api(page)
    page.route("**/tiles/**", lambda route: route.continue_())
    external = []
    page.route("https://**/*", lambda route: (external.append(route.request.url), route.abort()))
    try:
        page.goto(maps_url + path, wait_until="networkidle")
        page.wait_for_function(
            "s => Boolean(document.querySelector(s)?.outpostMapController?.vector)", arg=selector
        )
        page.locator(selector).scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        # Sample the synthetic marker in the same frame it is installed. Normal
        # domain refreshes legitimately replace it with their empty fixture lists.
        rendered = page.evaluate(
            """s => {
              const root = document.querySelector(s);
              const map = root.outpostMapController;
              map.setView({lat:-1.2864,lon:36.8172,zoom:11});
              map.setMarkers([{id:'map-test',lat:-1.2864,lon:36.8172,label:'Test marker'}]);
              map.renderNow();
              const marker = root.querySelector('[data-marker-id="map-test"]');
              return {canvas:root.querySelectorAll('canvas').length,
                markerWidth:marker.getBoundingClientRect().width,
                position:getComputedStyle(root.querySelector('.outpost-vector-map')).position,
                status:root.querySelector('.outpost-map-basemap-state').textContent};
            }""",
            selector,
        )
        assert rendered["canvas"] == 1 and rendered["markerWidth"] > 0
        assert rendered["position"] == "absolute"
        assert "Offline regional map" in rendered["status"]
        assert not external
    finally:
        page.close()
