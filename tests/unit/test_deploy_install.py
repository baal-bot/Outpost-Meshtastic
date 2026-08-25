from pathlib import Path


def test_installer_reuses_group_and_restarts_upgraded_service() -> None:
    script = (Path(__file__).parents[2] / "deploy" / "install.sh").read_text()

    assert "useradd --system --gid outpost" in script
    assert 'pip install --upgrade --force-reinstall "$PROJECT_DIR[radio]"' in script
    assert "systemctl restart outpost.service" in script
    assert "systemctl enable --now outpost.service" not in script
