from __future__ import annotations

import ipaddress
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from starlette.types import ASGIApp, Receive, Scope, Send

from outpost.config import WebConfig, WebTransport


@dataclass(frozen=True)
class TLSMaterial:
    fingerprint_sha256: str
    not_before: datetime
    not_after: datetime


def bind_is_loopback(bind: str) -> bool:
    if bind.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def _proxy_networks(config: WebTransport) -> tuple[Any, ...]:
    return tuple(ipaddress.ip_network(value, strict=False) for value in config.trusted_proxies)


def _trusted_source(source: object, networks: tuple[Any, ...]) -> bool:
    try:
        address = ipaddress.ip_address(str(source))
    except ValueError:
        return False
    return any(address in network for network in networks)


def _single_forwarded_value(scope: Scope, name: bytes) -> str | None:
    values = [
        value.decode("latin1").strip()
        for key, value in scope.get("headers", [])
        if key.lower() == name
    ]
    if len(values) != 1 or not values[0] or "," in values[0]:
        return None
    return values[0]


class WebTransportMiddleware:
    """Apply forwarding metadata only across the configured proxy trust boundary."""

    def __init__(self, app: ASGIApp, config: WebTransport) -> None:
        self.app = app
        self.config = config
        self.networks = _proxy_networks(config)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and self.config.mode == "trusted_proxy":
            client = scope.get("client")
            source = client[0] if client else ""
            if _trusted_source(source, self.networks):
                proto = _single_forwarded_value(scope, b"x-forwarded-proto")
                if proto in {"http", "https"}:
                    scope["scheme"] = proto
                forwarded = _single_forwarded_value(scope, b"x-forwarded-for")
                if forwarded is not None:
                    try:
                        address = ipaddress.ip_address(forwarded)
                    except ValueError:
                        pass
                    else:
                        scope["client"] = (str(address), 0)
        await self.app(scope, receive, send)


def transport_status(config: WebConfig, *, request_secure: bool) -> dict[str, object]:
    mode = config.transport.mode
    backend_http_exposed = mode != "direct_https" and not bind_is_loopback(config.bind)
    warning: dict[str, str] | None = None
    if mode == "trusted_http" and backend_http_exposed:
        warning = {
            "code": "trusted_http_nonloopback",
            "title": "Trusted local HTTP",
            "message": (
                "Dashboard traffic is not encrypted. Keep this operator-only port on an "
                "isolated LAN, Outpost hotspot, or encrypted VPN."
            ),
        }
    elif mode == "trusted_proxy" and backend_http_exposed:
        warning = {
            "code": "proxy_backend_nonloopback",
            "title": "Proxy backend is network-reachable",
            "message": (
                "Restrict the HTTP backend port to the configured proxy addresses so clients "
                "cannot bypass HTTPS."
            ),
        }
    return {
        "mode": mode,
        "request_encrypted": request_secure,
        "backend_bind": config.bind,
        "backend_port": config.port,
        "public_port": config.transport.public_port if mode == "trusted_proxy" else config.port,
        "operator_only_default": True,
        "warning": warning,
    }


def validate_tls_material(
    config: WebTransport, *, now: datetime | None = None
) -> TLSMaterial | None:
    if config.mode != "direct_https":
        return None
    certificate_file = config.certificate_file
    private_key_file = config.private_key_file
    assert certificate_file is not None and private_key_file is not None
    try:
        certificate = x509.load_pem_x509_certificate(Path(certificate_file).read_bytes())
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read a valid TLS certificate from {certificate_file}") from error
    current = now or datetime.now(UTC)
    if current < certificate.not_valid_before_utc:
        raise ValueError(f"TLS certificate is not valid until {certificate.not_valid_before_utc}")
    if current >= certificate.not_valid_after_utc:
        raise ValueError(f"TLS certificate expired at {certificate.not_valid_after_utc}")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(str(certificate_file), str(private_key_file))
    except (OSError, ssl.SSLError) as error:
        raise ValueError("TLS certificate and private key could not be loaded together") from error
    return TLSMaterial(
        fingerprint_sha256=certificate.fingerprint(hashes.SHA256()).hex(),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
    )


def uvicorn_options(config: WebConfig) -> dict[str, object]:
    """Return server options after validating direct-TLS material."""
    validate_tls_material(config.transport)
    options: dict[str, object] = {"proxy_headers": False}
    if config.transport.mode == "direct_https":
        assert config.transport.certificate_file is not None
        assert config.transport.private_key_file is not None
        options.update(
            ssl_certfile=str(config.transport.certificate_file),
            ssl_keyfile=str(config.transport.private_key_file),
        )
    return options
