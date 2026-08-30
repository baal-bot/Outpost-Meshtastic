#!/usr/bin/env python3
"""Validate the deploy dependency lock and report stale direct pins."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
import tomllib
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements.lock"
PROJECT = ROOT / "pyproject.toml"
IGNORED_INSTALLED = {"outpost", "pip", "setuptools", "wheel"}


def parse_lock(text: str) -> tuple[dict[str, str], list[str]]:
    pins: dict[str, str] = {}
    errors: list[str] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.partition("#")[0].strip()
        if not line:
            continue
        try:
            requirement = Requirement(line)
        except ValueError as error:
            errors.append(f"line {line_number}: invalid requirement ({error})")
            continue
        specifications = list(requirement.specifier)
        if requirement.url or requirement.marker or requirement.extras or len(specifications) != 1:
            errors.append(
                f"line {line_number}: lock entries must be unconditional name==version pins"
            )
            continue
        specification = specifications[0]
        if specification.operator != "==" or specification.version.endswith(".*"):
            errors.append(f"line {line_number}: {requirement.name} is not pinned exactly")
            continue
        name = canonicalize_name(requirement.name)
        if name in pins:
            errors.append(f"line {line_number}: duplicate pin for {name}")
            continue
        pins[name] = specification.version
    if not pins:
        errors.append("lock contains no package pins")
    return pins, errors


def runtime_requirements(project: Mapping[str, Any]) -> list[Requirement]:
    metadata = project.get("project")
    if not isinstance(metadata, dict):
        raise ValueError("pyproject.toml has no [project] table")
    dependencies = metadata.get("dependencies", [])
    optional = metadata.get("optional-dependencies", {})
    radio = optional.get("radio", []) if isinstance(optional, dict) else []
    if not isinstance(dependencies, list) or not isinstance(radio, list):
        raise ValueError("project runtime and radio dependencies must be lists")
    return [Requirement(str(value)) for value in [*dependencies, *radio]]


def consistency_errors(pins: Mapping[str, str], requirements: Iterable[Requirement]) -> list[str]:
    errors = []
    for requirement in requirements:
        name = canonicalize_name(requirement.name)
        pin = pins.get(name)
        if pin is None:
            errors.append(f"direct runtime dependency {name} is missing from the lock")
        elif Version(pin) not in requirement.specifier:
            errors.append(f"{name}=={pin} does not satisfy project range {requirement.specifier}")
    return errors


def installed_errors(pins: Mapping[str, str], installed: Mapping[str, str]) -> list[str]:
    errors = []
    normalized = {canonicalize_name(name): version for name, version in installed.items()}
    for name, version in sorted(pins.items()):
        actual = normalized.get(name)
        if actual is None:
            errors.append(f"locked package {name}=={version} is not installed")
        elif Version(actual) != Version(version):
            errors.append(f"locked package {name}=={version}, but installed version is {actual}")
    for name, version in sorted(normalized.items()):
        if name not in pins and name not in IGNORED_INSTALLED:
            errors.append(f"installed runtime package {name}=={version} has no lock entry")
    return errors


def installed_distributions() -> dict[str, str]:
    return {
        distribution.metadata["Name"]: distribution.version
        for distribution in importlib.metadata.distributions()
        if distribution.metadata["Name"]
    }


def latest_compatible(requirement: Requirement, payload: Mapping[str, Any]) -> Version | None:
    releases = payload.get("releases", {})
    if not isinstance(releases, dict):
        return None
    python = Version(".".join(str(value) for value in sys.version_info[:3]))
    compatible: list[Version] = []
    for raw_version, raw_files in releases.items():
        try:
            version = Version(str(raw_version))
        except InvalidVersion:
            continue
        if version.is_prerelease or version not in requirement.specifier:
            continue
        if not isinstance(raw_files, list):
            continue
        supported = False
        for file in raw_files:
            if not isinstance(file, dict) or file.get("yanked"):
                continue
            requires_python = file.get("requires_python")
            try:
                supported = not requires_python or python in SpecifierSet(str(requires_python))
            except ValueError:
                continue
            if supported:
                break
        if supported:
            compatible.append(version)
    return max(compatible, default=None)


def pypi_payload(name: str) -> dict[str, Any]:
    encoded = urllib.parse.quote(name, safe="")
    url = f"https://pypi.org/pypi/{encoded}/json"
    with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310 - fixed HTTPS host
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError(f"PyPI returned a non-object response for {name}")
    return value


def stale_pins(
    pins: Mapping[str, str], requirements: Iterable[Requirement]
) -> list[tuple[str, str, str]]:
    stale = []
    for requirement in requirements:
        name = canonicalize_name(requirement.name)
        locked = pins[name]
        latest = latest_compatible(requirement, pypi_payload(name))
        if latest is not None and Version(locked) < latest:
            stale.append((name, locked, str(latest)))
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-installed", action="store_true")
    parser.add_argument("--report-stale", action="store_true")
    args = parser.parse_args()
    pins, errors = parse_lock(LOCK.read_text(encoding="utf-8"))
    try:
        project = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
        requirements = runtime_requirements(project)
        errors.extend(consistency_errors(pins, requirements))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        errors.append(str(error))
        requirements = []
    if args.check_installed:
        errors.extend(installed_errors(pins, installed_distributions()))
    if errors:
        print("Dependency lock validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Dependency lock is consistent: {len(pins)} exact runtime/radio pins.")
    if args.report_stale:
        try:
            stale = stale_pins(pins, requirements)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"Could not complete PyPI staleness report: {error}", file=sys.stderr)
            return 1
        if not stale:
            print("Dependency lock staleness report: all direct pins are current within range.")
        for name, locked, latest in stale:
            print(
                f"::warning file=requirements.lock::{name} is locked at {locked}; "
                f"latest compatible is {latest}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
