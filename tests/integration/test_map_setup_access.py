"""Map setup uses the normal account, role and CSRF boundaries."""

import pytest
from fastapi.testclient import TestClient

from outpost.maps.setup import MapSetupService
from outpost.store import Database
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService
from tests.integration.test_web_auth import _permanent_operator

pytestmark = pytest.mark.production_wiring


async def test_map_setup_requires_operator_session_and_csrf(tmp_path):
    database = Database(tmp_path / "station.db")
    await database.open()
    maps = MapSetupService(tmp_path / "tiles")
    auth = WebAuthService(database, 12)
    app = create_web_app(lambda: {}, database, auth, map_setup=maps, tile_path=maps.root)
    client = TestClient(app)
    try:
        assert client.get("/api/v1/maps").status_code == 401
        assert client.post("/api/v1/maps/plan", json={}).status_code == 401
        csrf = await _permanent_operator(auth, client, "test-map-operator-password-42")
        assert client.get("/api/v1/maps").status_code == 200
        assert client.post("/api/v1/maps/pause").status_code == 403
        assert client.post("/api/v1/maps/pause", headers={"x-csrf-token": csrf}).status_code == 202
        # Downgrade the synthetic session to exercise the default-deny viewer contract.
        await database.write("UPDATE web_account SET role='viewer' WHERE username='operator'")
        assert client.get("/api/v1/maps").status_code == 403
        assert client.post("/api/v1/maps/pause", headers={"x-csrf-token": csrf}).status_code == 403
        assert client.get("/tiles/manifest.json").status_code == 404
    finally:
        maps.close()
        await database.close()
