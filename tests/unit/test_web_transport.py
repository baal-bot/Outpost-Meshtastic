from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from outpost.config import WebConfig, WebTransport
from outpost.web.transport import bind_is_loopback, uvicorn_options, validate_tls_material


def _certificate_pair(
    directory: Path,
    *,
    name: str,
    not_before: datetime,
    not_after: datetime,
) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name}.outpost.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(f"{name}.outpost.test")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "fullchain.pem"
    key_path = directory / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def _direct_config(certificate: Path, key: Path) -> WebConfig:
    return WebConfig(
        transport=WebTransport(
            mode="direct_https",
            certificate_file=certificate,
            private_key_file=key,
        )
    )


def test_valid_direct_tls_material_builds_uvicorn_options(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    certificate, key = _certificate_pair(
        tmp_path,
        name="valid",
        not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=30),
    )
    config = _direct_config(certificate, key)

    material = validate_tls_material(config.transport, now=now)
    options = uvicorn_options(config)

    assert material is not None and len(material.fingerprint_sha256) == 64
    assert options == {
        "proxy_headers": False,
        "ssl_certfile": str(certificate),
        "ssl_keyfile": str(key),
    }


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (timedelta(days=-30), timedelta(seconds=-1), "expired"),
        (timedelta(minutes=1), timedelta(days=30), "not valid until"),
    ],
)
def test_invalid_certificate_dates_fail_before_server_start(
    tmp_path: Path,
    start: timedelta,
    end: timedelta,
    message: str,
) -> None:
    now = datetime.now(UTC)
    certificate, key = _certificate_pair(
        tmp_path,
        name="invalid-date",
        not_before=now + start,
        not_after=now + end,
    )

    with pytest.raises(ValueError, match=message):
        validate_tls_material(_direct_config(certificate, key).transport, now=now)


def test_certificate_rotation_reloads_new_pair_and_detects_mismatch(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    certificate, key = _certificate_pair(
        tmp_path,
        name="before",
        not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=30),
    )
    config = _direct_config(certificate, key)
    before = validate_tls_material(config.transport, now=now)

    rotated_directory = tmp_path / "rotated"
    rotated_directory.mkdir()
    rotated_certificate, rotated_key = _certificate_pair(
        rotated_directory,
        name="after",
        not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=60),
    )
    certificate.write_bytes(rotated_certificate.read_bytes())
    with pytest.raises(ValueError, match="could not be loaded together"):
        validate_tls_material(config.transport, now=now)
    key.write_bytes(rotated_key.read_bytes())

    after = validate_tls_material(config.transport, now=now)
    assert before is not None and after is not None
    assert after.fingerprint_sha256 != before.fingerprint_sha256
    assert after.not_after > before.not_after


def test_trusted_http_recovery_needs_no_certificate_or_network() -> None:
    config = WebConfig(bind="127.0.0.1", transport=WebTransport(mode="trusted_http"))

    assert validate_tls_material(config.transport) is None
    assert uvicorn_options(config) == {"proxy_headers": False}
    assert bind_is_loopback(config.bind)
    assert not bind_is_loopback("0.0.0.0")  # noqa: S104
