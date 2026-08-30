import os
import runpy
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

RECOVERY = runpy.run_path(
    str(Path(__file__).parents[2] / "deploy" / "release_recovery.py"),
    run_name="outpost_release_recovery",
)
inspect_database = cast(Callable[..., Any], RECOVERY["inspect_database"])
plan_rollback = cast(Callable[..., dict[str, Any]], RECOVERY["plan_rollback"])
restore_database = cast(Callable[..., Any], RECOVERY["restore_database"])
snapshot_database = cast(Callable[..., Any], RECOVERY["snapshot_database"])
write_metadata = cast(Callable[..., dict[str, Any]], RECOVERY["write_metadata"])


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _installer_harness(tmp_path: Path, *, transport: str = "trusted_http") -> dict[str, Any]:
    root = Path(__file__).parents[2]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    prefix = tmp_path / "prefix"
    state = tmp_path / "state"
    config_dir = tmp_path / "config"
    system_root = tmp_path / "system"
    config_dir.mkdir()
    state.mkdir()
    database = state / "outpost.db"
    _database(database, 1, "before")
    config = yaml.safe_load((root / "config/config.example.yaml").read_text())
    config["store"]["path"] = str(database)
    config["modules"]["env"]["enabled"] = False
    config["web"]["transport"].update(
        {
            "mode": transport,
            "certificate_file": str(config_dir / "tls/cert.pem")
            if transport == "direct_https"
            else None,
            "private_key_file": str(config_dir / "tls/key.pem")
            if transport == "direct_https"
            else None,
            "trusted_proxies": ["127.0.0.1/32"] if transport == "trusted_proxy" else [],
        }
    )
    (config_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))

    _write_executable(fake_bin / "id", '#!/bin/sh\n[ "${1:-}" = -u ] && echo 0\n')
    for command in ("getent", "groupadd", "useradd", "usermod", "udevadm", "chown"):
        _write_executable(fake_bin / command, "#!/bin/sh\nexit 0\n")
    _write_executable(fake_bin / "sleep", "#!/bin/sh\nexit 0\n")
    _write_executable(
        fake_bin / "install",
        """#!/usr/bin/python3
import subprocess, sys
arguments = []
skip = False
for index, value in enumerate(sys.argv[1:]):
    if skip:
        skip = False
        continue
    if value in {"-o", "-g"}:
        skip = True
        continue
    arguments.append(value)
raise SystemExit(subprocess.run(["/usr/bin/install", *arguments], check=False).returncode)
""",
    )
    _write_executable(
        fake_bin / "python3",
        r"""#!/bin/sh
if [ "${1:-}" = -m ] && [ "${2:-}" = venv ]; then
  [ "${3:-}" = --help ] && exit 0
  release=$3
  mkdir -p "$release/bin"
  printf '%s\n' "${HARNESS_SCHEMA_CAP:-1}" > "$release/.schema-cap"
  cat > "$release/bin/python" <<'EOF'
#!/bin/sh
if [ "${1:-}" = - ]; then
  wrapper_input=$(mktemp)
  trap 'rm -f "$wrapper_input"' EXIT HUP INT TERM
  cat > "$wrapper_input"
  if grep -q 'migrations.glob' "$wrapper_input"; then
    cat "$(dirname -- "$0")/../.schema-cap"
    exit 0
  fi
  shift
  "$REAL_PYTHON" - "$@" < "$wrapper_input"
  exit $?
fi
exec "$REAL_PYTHON" "$@"
EOF
  cat > "$release/bin/pip" <<'EOF'
#!/bin/sh
exit 0
EOF
  chmod 0755 "$release/bin/python" "$release/bin/pip"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/bin/sh
printf 'systemctl %s\n' "$*" >> "$HARNESS_LOG"
case " $* " in
  *" start "*|*" restart "*)
    if [ "${HARNESS_MUTATION:-none}" != none ] && [ ! -e "$HARNESS_MUTATION_STATE" ]; then
      : > "$HARNESS_MUTATION_STATE"
      "$REAL_PYTHON" -c 'import os, sqlite3
path = os.environ["HARNESS_DATABASE"]
connection = sqlite3.connect(path)
if os.environ["HARNESS_MUTATION"] == "schema":
    connection.execute("INSERT OR IGNORE INTO schema_version VALUES(2,2)")
connection.execute("UPDATE sample SET value=?", ("during-failed-release",))
connection.commit()
connection.close()'
    fi
    ;;
esac
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/bin/sh
printf 'curl %s\n' "$*" >> "$HARNESS_LOG"
for argument in "$@"; do probe_url=$argument; done
case "${HARNESS_HEALTH:-success}" in
  fail) exit 7 ;;
  probe-error) exit 3 ;;
  tls)
    case "$*|$probe_url" in
      *-fkSs*\\|https://*) ;;
      *) exit 60 ;;
    esac
    ;;
esac
case "$probe_url" in
  */metrics) echo '# HELP outpost_test harness' ;;
  *) echo '{"status":"ok"}' ;;
esac
""",
    )
    _write_executable(
        fake_bin / "mv",
        '#!/bin/sh\nprintf \'mv %s\\n\' "$*" >> "$HARNESS_LOG"\nexec /bin/mv "$@"\n',
    )
    log = tmp_path / "commands.log"
    return {
        "root": root,
        "fake_bin": fake_bin,
        "prefix": prefix,
        "state": state,
        "config": config_dir,
        "system_root": system_root,
        "database": database,
        "log": log,
    }


def _run_install(
    harness: dict[str, Any],
    release: str,
    *,
    health: str = "success",
    mutation: str = "none",
    schema_cap: int = 1,
    script: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    root = cast(Path, harness["root"])
    environment = {
        **os.environ,
        "PATH": f"{harness['fake_bin']}:/usr/bin:/bin",
        "PYTHONPATH": str(root / "src"),
        "REAL_PYTHON": sys.executable,
        "OUTPOST_PROJECT_DIR": str(root),
        "OUTPOST_PREFIX": str(harness["prefix"]),
        "OUTPOST_STATE_DIR": str(harness["state"]),
        "OUTPOST_CONFIG_DIR": str(harness["config"]),
        "OUTPOST_SYSTEM_ROOT": str(harness["system_root"]),
        "OUTPOST_MDNS": "0",
        "OUTPOST_NONINTERACTIVE": "1",
        "OUTPOST_ALLOW_UNVERIFIED_CI": "1",
        "OUTPOST_HAILO_RELEASE_GRACE_SECONDS": "0",
        "OUTPOST_HEALTH_ATTEMPTS": "2",
        "OUTPOST_HEALTH_DELAY_SECONDS": "0",
        "OUTPOST_RELEASE_ID": release,
        "HARNESS_SCHEMA_CAP": str(schema_cap),
        "HARNESS_HEALTH": health,
        "HARNESS_MUTATION": mutation,
        "HARNESS_MUTATION_STATE": str(harness["state"] / f"mutation-{release}"),
        "HARNESS_DATABASE": str(harness["database"]),
        "HARNESS_LOG": str(harness["log"]),
    }
    return subprocess.run(  # noqa: S603
        ["/bin/sh", str(script or root / "deploy/install.sh")],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_successful_install(harness: dict[str, Any], result, release: str) -> None:
    assert result.returncode == 0, result.stderr
    expected = cast(Path, harness["prefix"]) / "releases" / release
    assert (cast(Path, harness["prefix"]) / "current").resolve() == expected
    assert (
        inspect_database(
            cast(Path, harness["state"]) / "backups" / f"pre-upgrade-{release}.db"
        ).integrity
        == "ok"
    )
    log = cast(Path, harness["log"]).read_text()
    assert f"mv -Tf {harness['prefix']}/current.next {harness['prefix']}/current" in log
    assert "systemctl start outpost.service" in log


def test_installer_executes_staging_snapshot_atomic_activation_and_service(tmp_path: Path) -> None:
    harness = _installer_harness(tmp_path)
    first = _run_install(harness, "initial")
    _assert_successful_install(harness, first, "initial")
    second = _run_install(harness, "upgrade")
    _assert_successful_install(harness, second, "upgrade")

    prefix = cast(Path, harness["prefix"])
    assert (prefix / "previous").resolve() == prefix / "releases/initial"
    assert (prefix / "releases/upgrade/rollback.json").is_file()
    assert "systemctl stop outpost.service" in cast(Path, harness["log"]).read_text()


def test_failed_code_only_upgrade_restores_code_without_discarding_live_data(
    tmp_path: Path,
) -> None:
    harness = _installer_harness(tmp_path)
    _assert_successful_install(harness, _run_install(harness, "initial"), "initial")

    failed = _run_install(harness, "broken", health="fail", mutation="code")

    assert failed.returncode != 0
    prefix = cast(Path, harness["prefix"])
    assert (prefix / "current").resolve() == prefix / "releases/initial"
    with closing(sqlite3.connect(harness["database"])) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == (
            "during-failed-release"
        )
    assert "Rollback is code-only; the live database was left untouched." in failed.stderr
    assert not Path(f"{harness['database']}.failed-broken").exists()


def test_failed_schema_upgrade_restores_snapshot_and_preserves_forensic_copy(
    tmp_path: Path,
) -> None:
    harness = _installer_harness(tmp_path)
    _assert_successful_install(harness, _run_install(harness, "initial"), "initial")

    failed = _run_install(harness, "schema-broken", health="fail", mutation="schema", schema_cap=2)

    assert failed.returncode != 0
    assert inspect_database(harness["database"]).schema_version == 1
    with closing(sqlite3.connect(harness["database"])) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "before"
    forensic = Path(f"{harness['database']}.failed-schema-broken")
    assert inspect_database(forensic).schema_version == 2
    with closing(sqlite3.connect(forensic)) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == (
            "during-failed-release"
        )
    assert "writes after that point were discarded" in failed.stderr
    assert f"Failed-release forensic copy: {forensic}" in failed.stderr


def _run_rollback(harness: dict[str, Any], health: str) -> subprocess.CompletedProcess[str]:
    root = cast(Path, harness["root"])
    environment = {
        **os.environ,
        "PATH": f"{harness['fake_bin']}:/usr/bin:/bin",
        "PYTHONPATH": str(root / "src"),
        "REAL_PYTHON": sys.executable,
        "OUTPOST_PREFIX": str(harness["prefix"]),
        "OUTPOST_CONFIG_DIR": str(harness["config"]),
        "OUTPOST_RECOVERY_HELPER": str(root / "deploy/release_recovery.py"),
        "OUTPOST_HEALTH_HELPER": str(root / "deploy/health_probe.sh"),
        "OUTPOST_HEALTH_ATTEMPTS": "2",
        "OUTPOST_HEALTH_DELAY_SECONDS": "0",
        "HARNESS_HEALTH": health,
        "HARNESS_MUTATION": "none",
        "HARNESS_MUTATION_STATE": str(harness["state"] / "rollback-mutation"),
        "HARNESS_DATABASE": str(harness["database"]),
        "HARNESS_LOG": str(harness["log"]),
    }

    return subprocess.run(  # noqa: S603
        ["/bin/sh", str(root / "deploy/rollback.sh")],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("transport", ["trusted_http", "trusted_proxy", "direct_https"])
def test_manual_rollback_uses_shared_probe_for_every_transport(
    tmp_path: Path, transport: str
) -> None:
    harness = _installer_harness(tmp_path, transport=transport)
    health = "tls" if transport == "direct_https" else "success"
    _assert_successful_install(harness, _run_install(harness, "initial", health=health), "initial")
    _assert_successful_install(harness, _run_install(harness, "upgrade", health=health), "upgrade")
    result = _run_rollback(harness, health)

    assert result.returncode == 0, result.stderr
    assert (cast(Path, harness["prefix"]) / "current").resolve() == (
        cast(Path, harness["prefix"]) / "releases/initial"
    )
    curl = "-fkSs https" if transport == "direct_https" else "-fsS http"
    assert f"curl {curl}://127.0.0.1:8080/api/v1/health" in cast(Path, harness["log"]).read_text()


def test_manual_rollback_probe_error_fails_before_code_or_data_change(tmp_path: Path) -> None:
    harness = _installer_harness(tmp_path)
    _assert_successful_install(harness, _run_install(harness, "initial"), "initial")
    _assert_successful_install(harness, _run_install(harness, "upgrade"), "upgrade")
    log = cast(Path, harness["log"])
    before = log.read_text()

    result = _run_rollback(harness, "probe-error")

    assert result.returncode != 0
    prefix = cast(Path, harness["prefix"])
    assert (prefix / "current").resolve() == prefix / "releases/upgrade"
    assert "health probe configuration failed before rollback" in result.stderr
    assert "systemctl stop" not in log.read_text()[len(before) :]
    with closing(sqlite3.connect(harness["database"])) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "before"


@pytest.mark.parametrize("mutation", ["rollback", "snapshot", "atomic"])
def test_installer_harness_rejects_safety_mutants(tmp_path: Path, mutation: str) -> None:
    harness = _installer_harness(tmp_path)
    root = cast(Path, harness["root"])
    deploy = tmp_path / "mutated-deploy"
    shutil.copytree(root / "deploy", deploy)
    script = deploy / "install.sh"
    source = script.read_text()
    if mutation == "rollback":
        _assert_successful_install(harness, _run_install(harness, "initial"), "initial")
        source = source.replace(
            'if [ "$healthy" -ne 1 ]; then',
            'if [ "$healthy" -ne 1 ] && false; then',
            1,
        )
        script.write_text(source)
        result = _run_install(harness, "mutant", health="fail", script=script)
        with pytest.raises(AssertionError):
            assert result.returncode != 0
            assert (cast(Path, harness["prefix"]) / "current").resolve() == (
                cast(Path, harness["prefix"]) / "releases/initial"
            )
    elif mutation == "snapshot":
        snapshot_call = (
            'python3 "$SCRIPT_DIR/release_recovery.py" snapshot \\\n'
            '    --source "$DATABASE_PATH" --destination "$BACKUP_PATH" >/dev/null'
        )
        source = source.replace(
            snapshot_call,
            "true",
            1,
        )
        script.write_text(source)
        result = _run_install(harness, "mutant", script=script)
        with pytest.raises(AssertionError):
            _assert_successful_install(harness, result, "mutant")
    else:
        script.write_text(source.replace("mv -Tf", "mv -f"))
        result = _run_install(harness, "mutant", script=script)
        with pytest.raises(AssertionError):
            _assert_successful_install(harness, result, "mutant")


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
    attestation = script.index('"$GH" attestation verify "$artifact"')
    ci_verification = script.index("tools/check_ci_evidence.py")
    checkout = script.index('git -C "$PROJECT_DIR" checkout --detach "$target"')
    install = script.index('"$SCRIPT_DIR/install.sh"')
    assert checksum < attestation < ci_verification < checkout < install
    assert 'case "$TARGET" in' in script
    assert "v[0-9]*)" in script
    assert "source checkout with uncommitted changes" not in script
    assert "Refusing to update a checkout with uncommitted changes." in script
    assert 'if [ -n "$release_commit" ] && [ "$target" != "$release_commit" ]' in script
    assert (
        "Development update from $TARGET; signed release verification applies to v* tags." in script
    )
    assert 'OUTPOST_CI_VERIFIED_REVISION="$target"' in script
    assert 'OUTPOST_ALLOW_UNVERIFIED_CI="$ALLOW_UNVERIFIED_CI"' in script
    assert 'OUTPOST_HAILORT_WHEEL="$HAILORT_WHEEL"' in script
    assert "Refusing to deploy $target without successful exact-commit CI." in script
    assert "origin fetch failed; using only the already-local target revision" in script
    assert "/home/linuxbrew/.linuxbrew/bin/gh" in script

    pre_push = (Path(__file__).parents[2] / "tools" / "pre-push.sh").read_text()
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml").read_text()
    expected_scope = (
        "src tests tools/build_release_metadata.py tools/check_capabilities.py "
        "tools/check_ci_evidence.py tools/check_static_markup.py tools/verify_release.py "
        "deploy/configure.py deploy/render_avahi.py"
    )
    assert expected_scope in " ".join(pre_push.replace("\\\n", "").split())
    assert " ".join(workflow.split()).count(expected_scope) == 2
    assert "python tools/check_static_markup.py" in pre_push


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
