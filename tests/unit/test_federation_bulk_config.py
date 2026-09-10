import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from outpost.config import Config, FedBulkConfig, load_config

SETTINGS = {
    "enabled": True,
    "listen_address": "192.168.1.10",
    "certificate_file": "/private/local.crt",
    "private_key_file": "/private/local.key",
    "trust_root_file": "/private/root.crt",
    "peers": [
        {
            "mesh_id": "!00000002",
            "address": "192.168.1.20",
            "certificate_identity": "peer.local",
            "public_key_sha256": "a" * 64,
        }
    ],
}


def test_existing_configuration_keeps_bulk_disabled():
    for path in (Path("config/config.example.yaml"), Path("config/config.yaml")):
        assert load_config(path).fed.bulk.enabled is False
    assert Config().fed.bulk == FedBulkConfig()
    config = Config.model_validate({"fed": {"bulk": SETTINGS}})
    assert config.fed.bulk.peers[0].port == 8444


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",  # noqa: S104 -- explicitly rejected, never bound
        "::",
        "127.0.0.1",
        "::1",
        "169.254.169.254",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "8.8.8.8",
        "192.168.1.2%eth0",
        "::ffff:192.168.1.1",
        "https://192.168.1.20",
        "peer.local",
        "192.168.1.20/path",
    ],
)
def test_endpoint_never_implicitly_resolves_or_selects_an_unapproved_address(address):
    value = copy.deepcopy(SETTINGS)
    value["peers"][0]["address"] = address
    with pytest.raises(ValidationError):
        FedBulkConfig.model_validate(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "true"),
        ("listen_port", True),
        ("listen_port", 0),
        ("certificate_file", "relative.crt"),
        ("private_key_file", None),
        ("trust_root_file", None),
        ("listen_address", None),
        ("peers", []),
    ],
)
def test_activation_contract_requires_explicit_tls_and_endpoints(field, value):
    settings = copy.deepcopy(SETTINGS)
    settings[field] = value
    with pytest.raises(ValidationError):
        FedBulkConfig.model_validate(settings)


@pytest.mark.parametrize(
    "field,value",
    [
        ("mesh_id", "!REMOTE"),
        ("public_key_sha256", "a" * 63),
        ("certificate_identity", "*.local"),
        ("certificate_identity", "https://peer.local"),
        ("port", 65536),
    ],
)
def test_peer_identity_and_pin_are_exact(field, value):
    settings = copy.deepcopy(SETTINGS)
    settings["peers"][0][field] = value
    with pytest.raises(ValidationError):
        FedBulkConfig.model_validate(settings)


def test_loopback_requires_an_exclusively_local_test_configuration():
    value = copy.deepcopy(SETTINGS)
    value["test_only_loopback"] = True
    with pytest.raises(ValidationError):
        FedBulkConfig.model_validate(value)
    value["listen_address"] = "127.0.0.1"
    value["peers"][0]["address"] = "::1"
    assert FedBulkConfig.model_validate(value).test_only_loopback
    value["test_only_loopback"] = False
    value["listen_address"] = "fd01::1"
    value["peers"][0]["address"] = "fd01::2"
    value["peers"][0]["certificate_identity"] = "fd01::2"
    assert FedBulkConfig.model_validate(value).peers[0].certificate_identity == "fd01::2"


def test_peer_list_is_finite_and_has_no_duplicate_authority():
    value = copy.deepcopy(SETTINGS)
    value["peers"] *= 2
    with pytest.raises(ValidationError, match="unique"):
        FedBulkConfig.model_validate(value)
    value["peers"] *= 17
    with pytest.raises(ValidationError):
        FedBulkConfig.model_validate(value)
