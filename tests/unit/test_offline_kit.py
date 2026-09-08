"""Public synthetic kits; real pip installs with no index, cache or source tree."""

import base64
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from deploy import offline_kit as kit


def wheel(path, name, version, files, *, requires=()):
    info = name.replace("-", "_") + "-" + version + ".dist-info/"
    files = {**files}
    files[info + "METADATA"] = (
        f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
        "License-Expression: MIT\n" + "".join(f"Requires-Dist: {item}\n" for item in requires)
    ).encode()
    files[info + "WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: synthetic-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    files[info + "licenses/LICENSE"] = b"Synthetic test fixture, not a distributed dependency.\n"
    record = io.StringIO()
    writer = csv.writer(record)
    for filename, raw in files.items():
        encoded = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        writer.writerow([filename, "sha256=" + encoded, len(raw)])
    writer.writerow([info + "RECORD", "", ""])
    files[info + "RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for filename, raw in files.items():
            archive.writestr(filename, raw)


@pytest.fixture
def prepared(tmp_path):
    source, wheels = tmp_path / "source", tmp_path / "wheels"
    source.mkdir()
    wheels.mkdir()
    for name in kit.PUBLIC_FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Synthetic public reference\n")
    (source / "pyproject.toml").write_text('[project]\nname="outpost"\nversion="0.1.0"\n')
    (source / "requirements.lock").write_text("kit-fixture-dependency==1.0\n")
    files = {
        "outpost/__init__.py": b'__version__ = "0.1.0"\n',
        "outpost/store/migrations/0000_core.sql": b"CREATE TABLE synthetic(value);\n",
    }
    for name, raw in files.items():
        path = source / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    wheel(
        wheels / "outpost-0.1.0-py3-none-any.whl",
        "outpost",
        "0.1.0",
        files,
        requires=("kit-fixture-dependency==1.0",),
    )
    wheel(
        wheels / "kit_fixture_dependency-1.0-py3-none-any.whl",
        "kit-fixture-dependency",
        "1.0",
        {"kit_fixture_dependency/__init__.py": b'value = "offline fixture"\n'},
    )
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Synthetic tester",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "Synthetic offline kit fixture",
        ],
    ):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)  # noqa: S603,S607
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()  # noqa: S603,S607
    evidence = tmp_path / "ci.json"
    evidence.write_text(
        json.dumps(
            {
                "commit": commit,
                "run_id": 1,
                "status": "completed",
                "conclusion": "success",
                "url": "https://github.com/baal-bot/Outpost-Meshtastic/actions/runs/1",
            }
        )
    )
    return SimpleNamespace(source=source, wheels=wheels, evidence=evidence, output=tmp_path / "kit")


def assemble(prepared):
    return kit.build(prepared.source, prepared.wheels, prepared.evidence, prepared.output)


def test_actual_offline_install_uses_only_verified_copy_and_never_activates(
    prepared, tmp_path, monkeypatch
):
    checksum = assemble(prepared)
    manifest = kit.verify(prepared.output, checksum)
    assert manifest["schema_fingerprint"] == kit.digest(
        b"0000_core.sql\x00CREATE TABLE synthetic(value);\n\x00"
    )
    assert len(manifest["packages"]) == 2
    assert manifest["packages"]["outpost"]["license_expression"] == "MIT"
    copy = tmp_path / "independent copy"
    shutil.copytree(prepared.output, copy)
    shutil.rmtree(prepared.source)
    shutil.rmtree(prepared.wheels)
    shutil.rmtree(prepared.output)
    cache = tmp_path / "empty-cache"
    cache.mkdir()
    monkeypatch.setenv("PIP_CACHE_DIR", str(cache))
    monkeypatch.setenv("PIP_INDEX_URL", "http://127.0.0.1:9/unavailable")
    monkeypatch.setenv("PIP_TARGET", str(tmp_path / "wrong-target"))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "untrusted-python"))
    destination = tmp_path / "fresh runtime"
    kit.install(copy, checksum, destination)
    result = kit.run(
        [
            str(destination / "bin/python"),
            "-I",
            "-c",
            "import kit_fixture_dependency; print(kit_fixture_dependency.value)",
        ],
        cwd=copy,
    )
    assert result == "offline fixture"
    installed = json.loads((destination / "offline-kit.json").read_text())
    assert installed["state"] == "inactive" and installed["manifest_sha256"] == checksum
    assert kit.verify(copy, checksum) == manifest
    assert not (tmp_path / "wrong-target").exists() and not list(cache.iterdir())
    assert not list(destination.rglob("*.db")) and not (destination / "config.yaml").exists()
    with pytest.raises(FileExistsError):
        kit.install(copy, checksum, destination)
    assert (destination / "offline-kit.json").exists()


@pytest.mark.parametrize(
    "fault",
    [
        "failed_ci",
        "wrong_commit",
        "dirty_source",
        "missing_wheel",
        "wrong_version",
        "source_mismatch",
        "extra_package_file",
        "duplicate_wheel",
        "symlink",
        "sdist",
    ],
)
def test_build_rejects_unqualified_or_incomplete_inputs_before_publication(prepared, fault):
    if fault in {"failed_ci", "wrong_commit"}:
        data = json.loads(prepared.evidence.read_text())
        data["conclusion" if fault == "failed_ci" else "commit"] = (
            "failure" if fault == "failed_ci" else "0" * 40
        )
        prepared.evidence.write_text(json.dumps(data))
    elif fault == "dirty_source":
        (prepared.source / "README.md").write_text("Changed after review")
    elif fault in {"missing_wheel", "symlink", "sdist"}:
        path = prepared.wheels / "kit_fixture_dependency-1.0-py3-none-any.whl"
        path.unlink()
        if fault == "symlink":
            path.symlink_to(prepared.evidence)
        elif fault == "sdist":
            (prepared.wheels / "source.tar.gz").write_bytes(b"not a wheel")
    elif fault == "duplicate_wheel":
        shutil.copyfile(
            prepared.wheels / "outpost-0.1.0-py3-none-any.whl",
            prepared.wheels / "outpost-0.1.0-1-py3-none-any.whl",
        )
    else:
        files = {
            path.relative_to(prepared.source / "src").as_posix(): path.read_bytes()
            for path in (prepared.source / "src").rglob("*")
            if path.is_file()
        }
        if fault == "source_mismatch":
            files["outpost/__init__.py"] = b'__version__ = "changed"\n'
        elif fault == "extra_package_file":
            files["outpost/undeclared.py"] = b"# Not in the source revision\n"
        wheel(
            prepared.wheels / "outpost-0.1.0-py3-none-any.whl",
            "outpost",
            "0.2.0" if fault == "wrong_version" else "0.1.0",
            files,
        )
    with pytest.raises((OSError, ValueError)):
        assemble(prepared)
    assert not prepared.output.exists()


@pytest.mark.parametrize(
    "fault", ["changed", "removed", "extra", "symlink", "wrong_digest", "manifest_changed"]
)
def test_copy_verification_rejects_tampered_or_incomplete_media(prepared, fault):
    checksum = assemble(prepared)
    reference = prepared.output / "reference/README.md"
    if fault == "changed":
        reference.write_text("Altered")
    elif fault in {"removed", "symlink"}:
        reference.unlink()
        if fault == "symlink":
            reference.symlink_to(prepared.evidence)
    elif fault == "extra":
        (prepared.output / "extra.txt").write_text("Unexpected")
    elif fault == "wrong_digest":
        checksum = "0" * 64
    else:
        (prepared.output / kit.MANIFEST).write_text("{}")
    with pytest.raises(ValueError):
        kit.verify(prepared.output, checksum)


@pytest.mark.parametrize("fault", ["host", "disk", "install", "postcheck"])
def test_install_failure_never_leaves_a_success_record_or_overwrites_existing(
    prepared, tmp_path, monkeypatch, fault
):
    checksum = assemble(prepared)
    destination = tmp_path / "target"
    if fault == "host":
        monkeypatch.setattr(kit, "host", lambda: {})
    elif fault == "disk":
        monkeypatch.setattr(kit.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    else:
        original = kit.run

        def fail(args, *, cwd):
            if (fault == "install" and "install" in args) or (
                fault == "postcheck" and "check" in args
            ):
                raise ValueError("Synthetic staging failure")
            return original(args, cwd=cwd)

        monkeypatch.setattr(kit, "run", fail)
    with pytest.raises(ValueError):
        kit.install(prepared.output, checksum, destination)
    assert not destination.exists()
    assert kit.verify(prepared.output, checksum)


def test_build_refuses_existing_output_and_cleans_its_own_failed_staging(prepared, monkeypatch):
    prepared.output.mkdir()
    marker = prepared.output / "keep"
    marker.write_text("Existing operator content")
    with pytest.raises(FileExistsError):
        assemble(prepared)
    assert marker.read_text() == "Existing operator content"
    marker.unlink()
    prepared.output.rmdir()

    def fail(*args, **kwargs):
        raise ValueError("Synthetic incompatible wheel tags")

    monkeypatch.setattr(kit, "pip_install", fail)
    with pytest.raises(ValueError, match="incompatible wheel"):
        assemble(prepared)
    assert not prepared.output.exists()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a//b", "a/./b"])
def test_inventory_paths_cannot_escape_the_copy(prepared, name):
    assemble(prepared)
    path = prepared.output / kit.MANIFEST
    data = json.loads(path.read_text())
    data["files"][name] = {"bytes": 0, "sha256": kit.digest(b"")}
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="inventory path"):
        kit.verify(prepared.output, kit.digest(raw))


def test_nonregular_files_are_rejected_without_blocking(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        kit.regular(path)


def test_build_bounds_input_bytes_before_publishing(prepared, monkeypatch):
    monkeypatch.setattr(kit, "MAX_TOTAL", 16)
    with pytest.raises(ValueError, match="bounded regular file"):
        assemble(prepared)
    assert not prepared.output.exists()


@pytest.mark.parametrize("text", [b"", b"anything>=1", b"a==1\na==2", b"a==1;python_version>'3'"])
def test_lock_requires_exact_unconditional_unique_pins(text):
    with pytest.raises(ValueError):
        kit.lock_pins(text)
