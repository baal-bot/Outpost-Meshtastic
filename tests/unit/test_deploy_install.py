from pathlib import Path


def test_installer_stages_health_checked_release_with_rollback() -> None:
    script = (Path(__file__).parents[2] / "deploy" / "install.sh").read_text()

    assert "useradd --system --gid outpost" in script
    assert 'pip" install -c "$PROJECT_DIR/requirements.lock"' in script
    assert "pre-upgrade-$RELEASE_ID.db" in script
    assert 'mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"' in script
    assert 'curl -fsS "$HEALTH_URL"' in script
    assert "New release failed health verification; rolling back." in script


def test_service_uses_atomic_current_release() -> None:
    unit = (Path(__file__).parents[2] / "deploy" / "outpost.service").read_text()

    assert "ExecStart=/opt/outpost/current/bin/python -m outpost" in unit
    assert "ExecStartPre=/opt/outpost/current/bin/python" in unit
