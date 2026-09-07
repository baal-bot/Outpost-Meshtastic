"""Real SQLite, crypto, fresh application startup and named-operator verification."""

import asyncio
import hashlib
import json
import sqlite3
import threading
from contextlib import AsyncExitStack, closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from outpost import recovery
from outpost import recovery_format as fmt
from outpost.app import OutpostApp
from outpost.clock import VirtualClock
from outpost.config import Config
from outpost.recovery_format import RecoveryError
from outpost.store import Database, StoreError
from outpost.store import recovery_snapshot as snapshots
from outpost.store.members import MemberRepo
from outpost.store.recovery_snapshot import open_image
from outpost.transport.simulated import SimulatedRadioLink

pytestmark = pytest.mark.production_wiring
PASSPHRASE = "synthetic long recovery passphrase only"  # noqa: S105 - synthetic fixture only.
PASSWORD = "synthetic operator password only"  # noqa: S105 - synthetic fixture only.


@pytest.fixture
async def recovery_source(tmp_path):
    intents = tmp_path / "source-intents.yaml"
    intents.write_text("[]\n")
    config = Config.model_validate(
        {
            "store": {"path": str(tmp_path / "source.db")},
            "router": {"intents_file": str(intents)},
            "modules": {"fed": {"enabled": True}, "watch": {"enabled": True}},
        }
    )
    clock = VirtualClock()
    app = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock, node_id="!00000001"))
    await app.database.open()
    async with AsyncExitStack() as stack:
        stack.push_async_callback(app.database.close)
        stack.push_async_callback(app.ai_service.close)
        await app.web_auth.create_account(
            "operator", "Restoring operator", "administrator", PASSWORD, "test"
        )
        await app.database.write("UPDATE web_account SET must_change=0")
        await app.federation_relay.initialize()
        await app.database.write("INSERT INTO fed_bundle_identity VALUES(1,'!00000001',1,'test')")
        incident, _ = await app.incidents.create(
            "hazard synthetic closed crossing", None, force=True, operator_label="test"
        )
        assert incident is not None
        await app.database.write(
            "INSERT INTO runtime_setting VALUES('node.name','\"Recovered community\"',1)"
        )
        member = await MemberRepo(app.database, clock).resolve("!00000002")
        await app.database.write(
            "INSERT INTO mail(uid,from_id,from_label,to_id,to_label,body,created_at,expires_at) "
            "VALUES('recovery-private',?,'synthetic',?,'synthetic',"
            "'private mail fixture',1,253402300799)",
            (member.id, member.id),
        )
        await app.database.write(
            "INSERT INTO checkin(member_id,status,note,created_at) VALUES(?,'need_help',"
            "'private welfare fixture',1)",
            (member.id,),
        )
        await app.database.write(
            "INSERT INTO incident_responsibility"
            "(incident_id,version,owner_id,next_action,updated_at) "
            "VALUES(?,1,(SELECT id FROM incident_responsibility_target WHERE member_id=?),"
            "'private responsibility fixture',1)",
            (incident.id, member.id),
        )
        yield app


async def test_memory_snapshot_and_complete_fenced_restore(recovery_source, tmp_path):
    source = recovery_source
    session = await source.web_auth.login(PASSWORD, "local", username="operator")
    assert session is not None
    image = await source.database.recovery_snapshot()
    memory = open_image(image)
    assert memory.execute("SELECT count(*) FROM incident").fetchone()[0] == 1
    memory.close()
    assert image[18:20] == bytes([1, 1])
    bundle = tmp_path / "encrypted.opr"
    evidence = await asyncio.to_thread(recovery.export, source.config, bundle, PASSPHRASE)
    assert evidence["sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert b"Recovered community" not in bundle.read_bytes()
    assert b"SQLite format 3" not in bundle.read_bytes()
    assert b"private welfare fixture" not in bundle.read_bytes()
    assert recovery.verify(bundle.read_bytes(), PASSPHRASE)["verified"]
    target = tmp_path / "fresh-node"
    result = recovery.restore(bundle.read_bytes(), PASSPHRASE, target)
    assert result["state"] == "review_required"
    config = recovery.workbench_config(target)
    with closing(sqlite3.connect(config.store.path)) as restored_database:
        assert restored_database.execute("SELECT count(*) FROM web_session").fetchone()[0] == 0
    clock = VirtualClock()
    radio = SimulatedRadioLink(clock)
    app = OutpostApp(config, clock=clock, radio=radio)
    await app.startup()
    try:
        assert app.config.node.name == "Recovered community"
        assert app.recovery_fence.active
        assert [task.get_name() for task in app._tasks] == ["recovery-review"]
        assert radio.state.value == "down"
        assert not radio.sent
        for table in (
            "member",
            "mail",
            "checkin",
            "incident",
            "incident_responsibility",
            "outbound_work",
            "fed_revision",
            "fed_revision_lineage",
            "fed_revision_receipt",
            "fed_relay_identity",
            "fed_bundle_identity",
            "fed_peer",
            "sqlite_sequence",
        ):
            before = await source.database.read(f"SELECT * FROM {table}")  # noqa: S608 - fixed names.
            after = await app.database.read(f"SELECT * FROM {table}")  # noqa: S608 - fixed names.
            assert [tuple(row) for row in before] == [tuple(row) for row in after], table
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.web), base_url="http://localhost"
        ) as client:
            assert (await client.get("/api/v1/recovery/review")).status_code == 401
            login = await client.post(
                "/api/v1/auth/login", json={"username": "operator", "password": PASSWORD}
            )
            assert login.status_code == 200, login.text
            review = await client.get("/api/v1/recovery/review")
            assert review.status_code == 200, review.text
            assert review.json()["counts"]["incident"] == 1
            assert review.json()["recorded_mesh_id"] == "!00000001"
            assert (
                review.json()["recorded_signing_public_key"]
                == (
                    await source.database.read("SELECT hex(public_key) key FROM fed_relay_identity")
                )[0]["key"].lower()
            )
            health = await client.get("/api/v1/health")
            assert health.status_code == 503
            assert health.json()["status"] == "recovery_review_required"
            for method, path in (
                ("POST", "/api/v1/alerts"),
                ("GET", "/api/v1/radio/config"),
                ("GET", "/api/v1/ai/status"),
                ("POST", "/api/v1/federation/bundles/export"),
                ("POST", "/api/v1/backups"),
                ("GET", "/api/v1/unknown-future-service"),
            ):
                assert (await client.request(method, path)).status_code == 423
        assert not radio.sent
    finally:
        await app.shutdown()
    assert (target.stat().st_mode & 0o777) == 0o700
    for path in target.iterdir():
        if path.is_file():
            assert path.stat().st_mode & 0o077 == 0, path.name

    restarted = OutpostApp(config, clock=clock, radio=SimulatedRadioLink(clock))
    await restarted.startup()
    try:
        assert restarted.recovery_fence.active
        assert [task.get_name() for task in restarted._tasks] == ["recovery-review"]
    finally:
        await restarted.shutdown()


async def test_failed_recovery_never_overwrites_source_or_existing_directory(
    recovery_source, tmp_path
):
    bundle = tmp_path / "encrypted.opr"
    recovery.export(recovery_source.config, bundle, PASSPHRASE)
    target = tmp_path / "fresh-node"
    for data, password in (
        (bundle.read_bytes(), "wrong but long passphrase"),
        (bundle.read_bytes()[:-1], PASSPHRASE),
        (bundle.read_bytes()[:-1] + bytes([bundle.read_bytes()[-1] ^ 1]), PASSPHRASE),
    ):
        with pytest.raises(RecoveryError):
            recovery.restore(data, password, target)
        assert not target.exists()
    target.mkdir()
    marker = target / "do-not-touch"
    marker.write_bytes(b"existing node")
    with pytest.raises(RecoveryError):
        recovery.restore(bundle.read_bytes(), PASSPHRASE, target)
    assert marker.read_bytes() == b"existing node"
    assert len(await recovery_source.database.read("SELECT 1 FROM incident")) == 1


async def test_snapshot_writer_cancellation_holds_ownership(recovery_source, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = snapshots.snapshot

    def paused(connection):
        entered.set()
        assert release.wait(10)
        return original(connection)

    monkeypatch.setattr(snapshots, "snapshot", paused)
    task = asyncio.create_task(recovery_source.database.recovery_snapshot())
    assert await asyncio.to_thread(entered.wait, 10)
    task.cancel()
    task.cancel()
    next_writer = asyncio.create_task(
        recovery_source.database.write("UPDATE incident SET title='later'")
    )
    try:
        await asyncio.sleep(0.02)
        assert not task.done() and not next_writer.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await next_writer


async def test_snapshot_schema_corruption_storage_and_time_bounds(
    recovery_source, monkeypatch, tmp_path
):
    image = await recovery_source.database.recovery_snapshot()
    for change in (
        "CREATE TABLE unexpected(value TEXT)",
        "DELETE FROM schema_version WHERE version=185",
        "PRAGMA auto_vacuum=NONE; VACUUM",
        "PRAGMA foreign_keys=OFF; UPDATE checkin SET member_id=999999",
    ):
        memory = open_image(image)
        memory.executescript(change)
        damaged = memory.serialize()
        memory.close()
        with pytest.raises(RecoveryError):
            open_image(damaged)
    for invalid in (b"x", b"SQLite format 3\x00" + b"x" * 300):
        with pytest.raises(RecoveryError):
            open_image(invalid)
    with monkeypatch.context() as scoped:
        scoped.setattr(snapshots, "MAX_DATABASE", 1)
        with pytest.raises(RecoveryError, match="limit"):
            await recovery_source.database.recovery_snapshot()
    with monkeypatch.context() as scoped:
        times = iter((0, 1000))
        scoped.setattr(snapshots.time, "monotonic", lambda: next(times))
        with pytest.raises(RecoveryError, match="time bound"):
            # The sync read-only entry avoids changing the event loop's clock.
            recovery.snapshot_file(Path(recovery_source.config.store.path))
    closed = Database(tmp_path / "not-open.db")
    with pytest.raises(StoreError, match="not open"):
        await closed.recovery_snapshot()
    await closed.close()


async def test_disk_full_interruption_and_publication_are_fresh_only(
    recovery_source, tmp_path, monkeypatch
):
    output = tmp_path / "recovery.opr"
    recovery.export(recovery_source.config, output, PASSPHRASE)
    data = output.read_bytes()
    target = tmp_path / "restored"
    with monkeypatch.context() as scoped:
        scoped.setattr(recovery.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
        with pytest.raises(RecoveryError, match="space"):
            recovery.restore(data, PASSPHRASE, target)
        with pytest.raises(RecoveryError, match="space"):
            recovery.publish_bundle(tmp_path / "no-space.opr", data)
    assert not target.exists()
    original_write = recovery._write
    for boundary in ("outpost.db", "intents.yaml", "config.json", "RECOVERY.json", "READY"):

        def interrupted(path, value, boundary=boundary):
            original_write(path, value)
            if path.name == boundary:
                raise KeyboardInterrupt

        with monkeypatch.context() as scoped:
            scoped.setattr(recovery, "_write", interrupted)
            with pytest.raises(KeyboardInterrupt):
                recovery.restore(data, PASSPHRASE, target)
        assert not target.exists()
    with monkeypatch.context() as scoped:

        def broken_sync(_):
            raise OSError("synthetic full media")

        scoped.setattr(recovery, "_sync_directory", broken_sync)
        with pytest.raises(OSError):
            recovery.restore(data, PASSPHRASE, target)
    assert not target.exists()
    with pytest.raises(RecoveryError, match="exists"):
        recovery.publish_bundle(output, b"must not replace")
    assert output.read_bytes() == data
    assert not list(tmp_path.glob(".outpost-encrypted-*"))
    assert len(await recovery_source.database.read("SELECT 1 FROM incident")) == 1


async def test_symlinks_devices_and_mutating_inputs_are_rejected(
    recovery_source, tmp_path, monkeypatch
):
    output = tmp_path / "recovery.opr"
    recovery.export(recovery_source.config, output, PASSPHRASE)
    link = tmp_path / "link"
    link.symlink_to(output)
    with pytest.raises(OSError):
        recovery.read_regular(link, fmt.MAX_BUNDLE)
    with pytest.raises(RecoveryError):
        recovery.snapshot_file(link)
    with pytest.raises(RecoveryError):
        recovery.read_regular(output, 1)
    import os

    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(RecoveryError):
        recovery.read_regular(fifo, 100)
    original_stat = recovery.os.fstat
    calls = 0

    def changed(descriptor):
        nonlocal calls
        calls += 1
        value = original_stat(descriptor)
        return SimpleNamespace(
            st_mode=value.st_mode,
            st_size=value.st_size,
            st_ctime_ns=value.st_ctime_ns,
            st_mtime_ns=value.st_mtime_ns + (calls % 2 == 0),
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(recovery.os, "fstat", changed)
        with pytest.raises(RecoveryError, match="changed"):
            recovery.read_regular(output, fmt.MAX_BUNDLE)
    alias = tmp_path / "directory-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    for target in (Path("relative"), alias / "new-node", link):
        with pytest.raises(RecoveryError):
            recovery.restore(output.read_bytes(), PASSPHRASE, target)


async def test_selected_support_and_credentials_are_encrypted_not_environment_dumped(
    recovery_source, tmp_path, monkeypatch
):
    config = recovery_source.config.model_copy(deep=True)
    config.ai.provider = "openai_compat"
    config.ai.openai_compat.api_key_env = "SYNTHETIC_RECOVERY_API_KEY"
    monkeypatch.setenv("SYNTHETIC_RECOVERY_API_KEY", "synthetic-provider-secret")
    monkeypatch.setenv("UNRELATED_SYNTHETIC_SECRET", "must-not-be-exported")
    profile = tmp_path / "radio-profile.bin"
    profile.write_bytes(b"synthetic-radio-profile-secret")
    output = tmp_path / "recovery.opr"
    recovery.export(config, output, PASSPHRASE, include_provider_key=True, radio_profile=profile)
    components, restored_config = recovery.verified_components(output.read_bytes(), PASSPHRASE)
    assert restored_config.ai.provider == config.ai.provider
    assert json.loads(components["provider_credentials"]) == {
        "SYNTHETIC_RECOVERY_API_KEY": "synthetic-provider-secret"
    }
    assert components["radio_profile"] == profile.read_bytes()
    assert b"must-not-be-exported" not in b"".join(components.values())
    assert b"synthetic-provider-secret" not in output.read_bytes()
    recovery.restore(output.read_bytes(), PASSPHRASE, tmp_path / "target")
    assert json.loads((tmp_path / "target/provider-credentials.json").read_text()) == json.loads(
        components["provider_credentials"]
    )
    monkeypatch.delenv("SYNTHETIC_RECOVERY_API_KEY")
    with pytest.raises(RecoveryError, match="not configured or available"):
        recovery.capture_material(config, include_provider_key=True)
    with pytest.raises(RecoveryError):
        recovery.capture_material(recovery_source.config, include_provider_key=True)
    for name, value in (
        ("config", b'{"unexpected":"synthetic-secret"}'),
        ("provider_credentials", b'{"WRONG":"synthetic-secret"}'),
        ("database", b"not a database"),
    ):
        changed = {**components, name: value}
        invalid = fmt.encrypt(fmt.pack(changed, snapshots.schema_fingerprint(), 1), PASSPHRASE)
        with pytest.raises(RecoveryError):
            recovery.restore(invalid, PASSPHRASE, tmp_path / "invalid-target")
        assert not (tmp_path / "invalid-target").exists()


async def test_cli_export_verify_restore_and_safe_errors(
    recovery_source, tmp_path, monkeypatch, capsys
):
    import sys

    from outpost import __main__ as entrypoint

    source = tmp_path / "source-config.json"
    source.write_bytes(fmt.canonical(recovery_source.config.model_dump(mode="json")))
    output = tmp_path / "bundle.opr"
    target = tmp_path / "target"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(recovery.getpass, "getpass", lambda _: PASSPHRASE)
    for arguments in (
        ["export", "--config", str(source), "--output", str(output)],
        ["verify", str(output)],
        ["restore", str(output), str(target)],
    ):
        monkeypatch.setattr(sys, "argv", ["outpost-recovery", *arguments])
        assert recovery.main() == 0
        printed = capsys.readouterr()
        assert not printed.err
        assert PASSPHRASE not in printed.out and PASSWORD not in printed.out
        assert "private welfare" not in printed.out
    called = []
    monkeypatch.setattr(entrypoint, "main", lambda config: called.append(config))
    monkeypatch.setattr(sys, "argv", ["outpost-recovery", "serve", str(target)])
    assert recovery.main() == 0
    assert called[0].store.path == str(target / "outpost.db")
    monkeypatch.setattr(sys, "argv", ["outpost-recovery", "verify", str(output)])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert recovery.main() == 1
    assert "interactive terminal" in capsys.readouterr().err
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def interrupted(_):
        raise EOFError

    monkeypatch.setattr(recovery.getpass, "getpass", interrupted)
    assert recovery.main() == 1
    assert "interrupted" in capsys.readouterr().err
    monkeypatch.setattr(
        sys,
        "argv",
        ["outpost-recovery", "export", "--config", str(source), "--output", str(output)],
    )
    answers = iter((PASSPHRASE, "different"))
    monkeypatch.setattr(recovery.getpass, "getpass", lambda _: next(answers))
    assert recovery.main() == 1
    assert "confirmation" in capsys.readouterr().err
    monkeypatch.setattr(recovery.getpass, "getpass", lambda _: PASSPHRASE)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "outpost-recovery",
            "export",
            "--config",
            str(tmp_path / "missing"),
            "--output",
            str(output),
        ],
    )
    assert recovery.main() == 1
    assert "configuration is required" in capsys.readouterr().err
    source.write_text('{"unexpected":"synthetic-secret-do-not-print"}')
    monkeypatch.setattr(
        sys,
        "argv",
        ["outpost-recovery", "export", "--config", str(source), "--output", str(output)],
    )
    assert recovery.main() == 1
    assert "synthetic-secret" not in capsys.readouterr().err


async def test_missing_or_changed_workbench_fence_denies_launch(recovery_source, tmp_path):
    output = tmp_path / "bundle.opr"
    target = tmp_path / "target"
    recovery.export(recovery_source.config, output, PASSPHRASE)
    recovery.restore(output.read_bytes(), PASSPHRASE, target)
    metadata = (target / "RECOVERY.json").read_bytes()
    (target / "RECOVERY.json").write_bytes(b"{}")
    with pytest.raises(RecoveryError, match="Incomplete"):
        recovery.workbench_config(target)
    (target / "RECOVERY.json").write_bytes(metadata)
    config_raw = (target / "config.json").read_bytes()
    changed = json.loads(config_raw)
    changed["web"]["bind"] = "0.0.0.0"  # noqa: S104 - verify rejection.
    (target / "config.json").write_bytes(fmt.canonical(changed))
    with pytest.raises(RecoveryError, match="own local"):
        recovery.workbench_config(target)
    unsafe = OutpostApp(
        Config.model_validate(changed),
        clock=recovery_source.clock,
        radio=SimulatedRadioLink(recovery_source.clock),
    )
    try:
        with pytest.raises(RecoveryError, match="loopback"):
            await unsafe.startup()
        assert not unsafe._tasks
    finally:
        await unsafe.shutdown()
    (target / "config.json").write_bytes(config_raw)
    with closing(sqlite3.connect(target / "outpost.db")) as database, database:
        database.execute("DELETE FROM recovery_fence")
    with pytest.raises(RecoveryError, match="fence is missing"):
        recovery.workbench_config(target)


async def test_tls_support_integrity_and_missing_material(recovery_source, tmp_path):
    from tests.unit.test_web_transport import _certificate_pair, _direct_config

    now = datetime.now(UTC)
    certificate, key = _certificate_pair(
        tmp_path,
        name="recovery",
        not_before=now - timedelta(days=1),
        not_after=now + timedelta(days=30),
    )
    config = recovery_source.config.model_copy(deep=True)
    config.web = _direct_config(certificate, key)
    output = tmp_path / "tls-bundle.opr"
    recovery.export(config, output, PASSPHRASE)
    components, _ = recovery.verified_components(output.read_bytes(), PASSPHRASE)
    target = tmp_path / "tls-target"
    recovery.restore(output.read_bytes(), PASSPHRASE, target)
    assert (target / "tls-private-key.pem").read_bytes() == key.read_bytes()
    assert recovery.workbench_config(target).web.transport.mode == "trusted_http"
    assert (
        json.loads((target / "effective-config.json").read_text())["web"]["transport"]["mode"]
        == "direct_https"
    )
    second = tmp_path / "other-cert"
    second.mkdir()
    _, other_key = _certificate_pair(
        second, name="other", not_before=now - timedelta(days=1), not_after=now + timedelta(days=30)
    )
    for key_material in (b"bad PEM", other_key.read_bytes()):
        changed = {**components, "tls_private_key": key_material}
        with pytest.raises(RecoveryError, match="support material"):
            recovery.validate_support(changed, config)
    changed = {name: value for name, value in components.items() if name != "tls_private_key"}
    invalid = fmt.encrypt(fmt.pack(changed, snapshots.schema_fingerprint(), 1), PASSPHRASE)
    with pytest.raises(RecoveryError, match="missing"):
        recovery.verify(invalid, PASSPHRASE)
    with pytest.raises(RecoveryError, match="support material"):
        recovery.validate_support(components, recovery_source.config)


async def test_invalid_intents_and_missing_recovery_operator_fail_before_target(
    recovery_source, tmp_path
):
    components = recovery.capture_material(recovery_source.config)
    for invalid in (
        b"{}",
        b"[{}]",
        b"[{pattern: '', command: MENU}]",
        b"[{pattern: '[', command: MENU}]",
        b"[{pattern: 1, command: MENU}]",
        b"[" * 2000,
    ):
        with pytest.raises(RecoveryError):
            recovery.validate_support({**components, "intents": invalid}, recovery_source.config)
    recovery.validate_support(
        {**components, "intents": b"[{pattern: '^help$', command: MENU}]"}, recovery_source.config
    )
    image = open_image(await recovery_source.database.recovery_snapshot())
    try:
        with image:
            image.execute("UPDATE web_account SET enabled=0")
        components["database"] = image.serialize()
    finally:
        image.close()
    invalid = fmt.encrypt(fmt.pack(components, snapshots.schema_fingerprint(), 1), PASSPHRASE)
    with pytest.raises(RecoveryError, match="enabled named operator"):
        recovery.restore(invalid, PASSPHRASE, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_passphrase_echo_fallback_and_json_string_limits(monkeypatch):
    import warnings

    def echo_fallback(_):
        warnings.warn("synthetic echo fallback", recovery.getpass.GetPassWarning, stacklevel=1)
        raise AssertionError("must not accept echoed input")

    monkeypatch.setattr(recovery.getpass, "getpass", echo_fallback)
    with pytest.raises(RecoveryError, match="disable terminal echo"):
        recovery._passphrase("synthetic prompt")
    assert fmt.strict_json(b'{"a":"brackets [] {} and \\" quote and \\\\ slash"}') == {
        "a": 'brackets [] {} and " quote and \\ slash'
    }
    with pytest.raises(RecoveryError, match="bound"):
        fmt.strict_json(b"x" * (1024 * 1024 + 1))


def test_accidental_passphrase_argument_is_rejected_without_echo(monkeypatch, capsys):
    import sys

    monkeypatch.setattr(
        sys,
        "argv",
        ["outpost-recovery", "verify", "synthetic.opr", "--passphrase", "synthetic-do-not-echo"],
    )
    with pytest.raises(SystemExit) as error:
        recovery.main()
    assert error.value.code == 2
    printed = capsys.readouterr()
    assert "synthetic-do-not-echo" not in printed.out + printed.err
    assert "Invalid recovery arguments" in printed.err
