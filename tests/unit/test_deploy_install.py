import runpy
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast

RECOVERY = runpy.run_path(
    str(Path(__file__).parents[2] / "deploy" / "release_recovery.py"),
    run_name="outpost_release_recovery",
)
inspect_database = cast(Callable[..., Any], RECOVERY["inspect_database"])
plan_rollback = cast(Callable[..., dict[str, Any]], RECOVERY["plan_rollback"])
restore_database = cast(Callable[..., Any], RECOVERY["restore_database"])
snapshot_database = cast(Callable[..., Any], RECOVERY["snapshot_database"])
write_metadata = cast(Callable[..., dict[str, Any]], RECOVERY["write_metadata"])


def test_installer_stages_health_checked_release_with_rollback() -> None:
    script = (Path(__file__).parents[2] / "deploy" / "install.sh").read_text()

    assert "useradd --system --gid outpost" in script
    assert 'pip" install -c "$PROJECT_DIR/requirements.lock"' in script
    assert "pre-upgrade-$RELEASE_ID.db" in script
    assert 'release_recovery.py" record' in script
    assert '"$RELEASE_DIR/rollback.json"' in script
    assert 'mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"' in script
    assert 'curl -fsS "$HEALTH_URL"' in script
    assert 'ln -sfn "$CURRENT_LINK/bin/outpost-setup-token"' in script
    assert 'ln -sfn "$CURRENT_LINK/bin/outpost-diagnostics"' in script
    assert 'ln -sfn "$CURRENT_LINK/bin/outpost-onboarding"' in script
    assert 'setup-hotspot.sh" /usr/local/sbin/outpost-setup-hotspot' in script
    assert "sudo outpost-setup-token show" in script
    assert "sudo outpost-setup-token reset" in script
    assert "New release failed health verification; rolling back." in script
    assert "Python 3.12 or 3.13 is required" in script
    assert "apt-get install -y --no-install-recommends rtl-sdr" in script
    assert "samedec-$SAMEDEC_VERSION/samedec-$SAMEDEC_TARGET" in script
    assert "sha256sum -c -" in script
    assert 'SAME_ENABLED" -eq 1' in script
    assert 'AI_PROVIDER" = hailo' in script
    assert 'AI_PROVIDER" = hailo_vlm' in script
    assert "expected /dev/hailo0 or /dev/h1x-0" in script
    assert "hailo-ollama.service" in script
    assert '"$CONFIG_DIR/hailo-ollama/hailo-ollama.json"' in script
    assert "OUTPOST_HAILORT_WHEEL" in script
    assert "OUTPOST_HAILO_VLM_MODEL" in script
    assert "3e302b1d0bdc4beaf4ff982cb34f18bc957d3acd1e20e275eb0dd3536b3043a7" in script
    assert "from hailo_platform.genai import VLM" in script
    assert "systemctl disable --now hailo-ollama.service" in script
    assert "OUTPOST_MDNS:-1" in script
    assert "render_avahi.py" in script
    assert "avahi-daemon.service" in script

    hotspot = (Path(__file__).parents[2] / "deploy" / "setup-hotspot.sh").read_text()
    assert "802-11-wireless.ap-isolation yes" in hotspot
    assert 'iifname "$interface" drop' in hotspot
    assert "hook forward" in hotspot
    assert "outpost-setup-hotspot-expiry" in hotspot
    assert '[ "$minutes" -ge 5 ] && [ "$minutes" -le 60 ]' in hotspot


def test_acceptance_host_installer_keeps_test_tools_out_of_production() -> None:
    script = (Path(__file__).parents[2] / "deploy" / "install-test-host.sh").read_text()

    assert "TEST_ENV=${OUTPOST_TEST_VENV:-$PROJECT_DIR/.venv}" in script
    assert 'pip" install -e "$PROJECT_DIR[dev,radio]"' in script
    assert "run as the normal checkout user, not root or sudo" in script
    assert "Python 3.12 or 3.13 is required" in script
    assert "playwright install chromium" in script
    assert "systemctl" not in script
    assert "/opt/outpost/current (unchanged)" in script


def test_release_update_verifies_artifacts_before_checkout_and_install() -> None:
    script = (Path(__file__).parents[2] / "deploy" / "update.sh").read_text()

    checksum = script.index("tools/verify_release.py")
    attestation = script.index('gh attestation verify "$artifact"')
    checkout = script.index('git -C "$PROJECT_DIR" checkout --detach "$target"')
    install = script.index('sudo "$SCRIPT_DIR/install.sh"')
    assert checksum < attestation < checkout < install
    assert 'case "$TARGET" in' in script
    assert "v[0-9]*)" in script
    assert "source checkout with uncommitted changes" not in script
    assert "Refusing to update a checkout with uncommitted changes." in script
    assert 'if [ -n "$release_commit" ] && [ "$target" != "$release_commit" ]' in script
    assert (
        "Development update from $TARGET; signed release verification applies to v* tags." in script
    )


def test_setup_hotspot_applies_network_isolation_before_activation(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    commands = {
        "id": '#!/bin/sh\n[ "${1:-}" = -u ] && echo 0\n',
        "nmcli": (
            "#!/bin/sh\n"
            'printf \'nmcli %s\\n\' "$*" >> "$HOTSPOT_LOG"\n'
            "[ \"$*\" = '-g GENERAL.CONNECTION device show wlan9' ] && echo --\n"
            "exit 0\n"
        ),
        "nft": (
            "#!/bin/sh\n"
            'printf \'nft %s\\n\' "$*" >> "$HOTSPOT_LOG"\n'
            '[ "${1:-}" = -f ] && sed \'s/^/rule /\' >> "$HOTSPOT_LOG"\n'
            "exit 0\n"
        ),
        "systemctl": '#!/bin/sh\nprintf \'systemctl %s\\n\' "$*" >> "$HOTSPOT_LOG"\n',
        "systemd-run": ('#!/bin/sh\nprintf \'systemd-run %s\\n\' "$*" >> "$HOTSPOT_LOG"\n'),
    }
    for name, content in commands.items():
        command = fake_bin / name
        command.write_text(content)
        command.chmod(0o755)
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOTSPOT_LOG": str(log),
        "OUTPOST_CONFIG": str(root / "config/config.example.yaml"),
        "OUTPOST_PYTHON": sys.executable,
    }

    result = subprocess.run(  # noqa: S603
        ["/bin/sh", str(root / "deploy/setup-hotspot.sh"), "start", "wlan9", "5"],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "Temporary setup access is active for at most 5 minutes." in result.stdout
    actions = log.read_text()
    assert "802-11-wireless.ap-isolation yes" in actions
    assert 'rule     iifname "wlan9" drop' in actions
    assert "rule   chain forward" in actions
    assert actions.index("nft -f -") < actions.index("nmcli connection up outpost-setup")
    assert "systemd-run --quiet --collect --unit outpost-setup-hotspot-expiry" in actions
    assert "--on-active=5m" in actions


def test_service_uses_atomic_current_release() -> None:
    unit = (Path(__file__).parents[2] / "deploy" / "outpost.service").read_text()

    assert "ExecStart=/opt/outpost/current/bin/python -m outpost" in unit
    assert "ExecStartPre=/opt/outpost/current/bin/python" in unit
    assert "SupplementaryGroups=dialout outpost-sdr" in unit
    assert "DeviceAllow=char-usb_device rw" in unit
    rules = (Path(__file__).parents[2] / "deploy" / "70-outpost-rtl-sdr.rules").read_text()
    assert 'GROUP="outpost-sdr"' in rules
    assert 'ATTR{idProduct}=="2838"' in rules

    ai_unit = (Path(__file__).parents[2] / "deploy" / "hailo-ollama.service").read_text()
    assert "Environment=OLLAMA_HOST=127.0.0.1:8000" in ai_unit
    assert "Environment=XDG_CONFIG_HOME=/etc/outpost" in ai_unit
    assert "ConditionPathExists=|/dev/hailo0" in ai_unit
    assert "ConditionPathExists=|/dev/h1x-0" in ai_unit
    assert "DeviceAllow=/dev/hailo0 rw" in ai_unit
    assert "DeviceAllow=/dev/h1x-0 rw" in ai_unit
    hailo_config = (Path(__file__).parents[2] / "deploy" / "hailo-ollama.json").read_text()
    assert '"host": "127.0.0.1"' in hailo_config


def test_service_startup_never_prints_reusable_dashboard_credentials() -> None:
    application = (Path(__file__).parents[2] / "src" / "outpost" / "app.py").read_text()

    assert "OUTPOST INITIAL OPERATOR PASSWORD" not in application
    assert "setup_path.read_text" not in application


def _database(path: Path, schema: int, value: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at INTEGER);"
        "CREATE TABLE sample(value TEXT);"
    )
    connection.execute("INSERT INTO schema_version VALUES(?,1)", (schema,))
    connection.execute("INSERT INTO sample VALUES(?)", (value,))
    connection.commit()
    connection.close()


def test_release_recovery_restores_snapshot_across_schema_migration(tmp_path: Path) -> None:
    old_release, new_release = tmp_path / "old", tmp_path / "new"
    old_release.mkdir()
    new_release.mkdir()
    live, backup = tmp_path / "outpost.db", tmp_path / "pre-upgrade.db"
    _database(live, 1, "before")
    snapshot_database(live, backup)
    metadata = new_release / "rollback.json"
    write_metadata(
        metadata,
        upgrade_release=new_release,
        previous_release=old_release,
        database=live,
        backup=backup,
        pre_upgrade_schema=1,
        previous_schema_cap=1,
        upgrade_schema_cap=2,
    )
    connection = sqlite3.connect(live)
    connection.execute("INSERT INTO schema_version VALUES(2,2)")
    connection.execute("UPDATE sample SET value='after'")
    connection.commit()
    connection.close()

    plan = plan_rollback(metadata, current_release=new_release, target_release=old_release)
    assert plan["action"] == "restore"
    safety = tmp_path / "safety.db"
    snapshot_database(live, safety)
    restore_database(backup, live, maximum_schema=1)
    assert inspect_database(live).schema_version == 1
    with closing(sqlite3.connect(live)) as restored:
        assert restored.execute("SELECT value FROM sample").fetchone()[0] == "before"

    # A failed code rollback can put the exact pre-attempt data back.
    restore_database(safety, live, maximum_schema=2)
    assert inspect_database(live).schema_version == 2
    with closing(sqlite3.connect(live)) as recovered:
        assert recovered.execute("SELECT value FROM sample").fetchone()[0] == "after"


def test_release_recovery_skips_data_restore_without_schema_change(tmp_path: Path) -> None:
    old_release, new_release = tmp_path / "old", tmp_path / "new"
    old_release.mkdir()
    new_release.mkdir()
    live, backup = tmp_path / "outpost.db", tmp_path / "pre-upgrade.db"
    _database(live, 1, "current")
    snapshot_database(live, backup)
    metadata = new_release / "rollback.json"
    write_metadata(
        metadata,
        upgrade_release=new_release,
        previous_release=old_release,
        database=live,
        backup=backup,
        pre_upgrade_schema=1,
        previous_schema_cap=1,
        upgrade_schema_cap=1,
    )

    plan = plan_rollback(metadata, current_release=new_release, target_release=old_release)
    assert plan["action"] == "code-only"
    assert plan["candidate"] is None
