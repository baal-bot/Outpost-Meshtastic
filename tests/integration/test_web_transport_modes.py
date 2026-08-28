"""Web transport behavior across direct and proxied request modes."""

from __future__ import annotations

import socket
import ssl
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from outpost.config import Config, WebConfig, WebTransport
from outpost.diagnostics import _live_status
from outpost.store import Database
from outpost.web.api import create_web_app
from outpost.web.auth import WebAuthService
from outpost.web.transport import uvicorn_options


def _app(config: WebConfig):
    return create_web_app(lambda: {"radio": "up"}, web_config=config)


def _certificate_pair(directory: Path) -> tuple[Path, Path]:
    now = datetime.now(UTC)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path, key_path = directory / "certificate.pem", directory / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def test_offline_http_ignores_forwarded_headers_and_warns_nonloopback() -> None:
    config = WebConfig(bind="0.0.0.0")  # noqa: S104
    client = TestClient(_app(config), client=("192.168.50.20", 50000))

    response = client.get(
        "/api/v1/web/transport",
        headers={"x-forwarded-proto": "https", "x-forwarded-for": "127.0.0.1"},
    )

    assert response.status_code == 200
    assert response.json()["request_encrypted"] is False
    assert response.json()["warning"]["code"] == "trusted_http_nonloopback"
    assert "strict-transport-security" not in response.headers


def test_direct_https_sets_hsts_only_on_tls_request() -> None:
    config = WebConfig.model_construct(
        bind="0.0.0.0",  # noqa: S104
        port=8443,
        auth=WebConfig().auth,
        transport=WebTransport.model_construct(
            mode="direct_https",
            certificate_file=Path("/unused/certificate.pem"),
            private_key_file=Path("/unused/key.pem"),
            trusted_proxies=[],
            public_port=443,
            hsts_seconds=31_536_000,
        ),
    )
    client = TestClient(_app(config), base_url="https://outpost.test")

    response = client.get("/api/v1/web/transport")

    assert response.json()["request_encrypted"] is True
    assert response.json()["warning"] is None
    assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_direct_https_completes_a_real_tls_handshake(tmp_path: Path) -> None:
    certificate, key = _certificate_pair(tmp_path)
    config = WebConfig(
        bind="127.0.0.1",
        port=8443,
        transport=WebTransport(
            mode="direct_https",
            certificate_file=certificate,
            private_key_file=key,
        ),
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = int(sock.getsockname()[1])
    config.port = port
    server = uvicorn.Server(
        uvicorn.Config(
            _app(config),
            host="127.0.0.1",
            port=port,
            log_level="critical",
            **uvicorn_options(config),
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    context = ssl.create_default_context(cafile=str(certificate))
    try:
        with urllib.request.urlopen(  # noqa: S310
            f"https://localhost:{port}/api/v1/health",
            timeout=3,
            context=context,
        ) as response:
            assert response.status == 200
            assert response.headers["strict-transport-security"] == "max-age=31536000"
        assert _live_status(Config(web=config))["reachable"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_trusted_proxy_sets_effective_scheme_and_client_only_for_allowlisted_peer() -> None:
    config = WebConfig(
        bind="127.0.0.1",
        transport=WebTransport(
            mode="trusted_proxy",
            trusted_proxies=["127.0.0.1/32"],
        ),
    )
    trusted = TestClient(_app(config), client=("127.0.0.1", 50000))
    untrusted = TestClient(_app(config), client=("192.0.2.44", 50000))
    headers = {"x-forwarded-proto": "https", "x-forwarded-for": "198.51.100.24"}

    accepted = trusted.get("/api/v1/web/transport", headers=headers)
    spoofed = untrusted.get("/api/v1/web/transport", headers=headers)

    assert accepted.json()["request_encrypted"] is True
    assert accepted.headers["strict-transport-security"] == "max-age=31536000"
    assert spoofed.json()["request_encrypted"] is False
    assert "strict-transport-security" not in spoofed.headers


@pytest.mark.asyncio
async def test_secure_cookie_and_login_source_follow_trusted_proxy(tmp_path: Path) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    setup = await auth.ensure_credential()
    assert setup is not None
    config = WebConfig(
        bind="127.0.0.1",
        transport=WebTransport(
            mode="trusted_proxy",
            trusted_proxies=["127.0.0.1/32"],
        ),
    )
    client = TestClient(
        create_web_app(lambda: {"radio": "up"}, database, auth, web_config=config),
        client=("127.0.0.1", 50000),
        headers={"x-forwarded-proto": "https", "x-forwarded-for": "198.51.100.24"},
    )

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": setup.path.read_text().strip()},
    )

    assert login.status_code == 200
    assert "secure" in login.headers["set-cookie"].lower()
    assert login.headers["strict-transport-security"] == "max-age=31536000"
    sessions = await database.read("SELECT source FROM web_session")
    assert sessions[0]["source"] == "198.51.100.24"
    await database.close()


@pytest.mark.asyncio
async def test_spoofed_proxy_headers_cannot_set_secure_cookie_or_client_source(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "outpost.db")
    await database.open()
    auth = WebAuthService(database, 12)
    setup = await auth.ensure_credential()
    assert setup is not None
    config = WebConfig(
        bind="0.0.0.0",  # noqa: S104
        transport=WebTransport(
            mode="trusted_proxy",
            trusted_proxies=["127.0.0.1/32"],
        ),
    )
    client = TestClient(
        create_web_app(lambda: {"radio": "up"}, database, auth, web_config=config),
        client=("192.0.2.44", 50000),
        headers={"x-forwarded-proto": "https", "x-forwarded-for": "127.0.0.1"},
    )

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "operator", "password": setup.path.read_text().strip()},
    )

    assert login.status_code == 200
    assert "secure" not in login.headers["set-cookie"].lower()
    assert "strict-transport-security" not in login.headers
    sessions = await database.read("SELECT source FROM web_session")
    assert sessions[0]["source"] == "192.0.2.44"
    await database.close()


def test_proxy_rejects_ambiguous_forwarded_client_chain() -> None:
    config = WebConfig(
        bind="0.0.0.0",  # noqa: S104
        transport=WebTransport(
            mode="trusted_proxy",
            trusted_proxies=["192.0.2.44/32"],
        ),
    )
    client = TestClient(_app(config), client=("192.0.2.44", 50000))

    diagnostics = client.get(
        "/api/v1/diagnostics/status",
        headers={
            "x-forwarded-proto": "http",
            "x-forwarded-for": "203.0.113.2, 127.0.0.1",
        },
    )

    # The ambiguous client value is ignored, so it cannot manufacture a loopback source.
    assert diagnostics.status_code == 403
