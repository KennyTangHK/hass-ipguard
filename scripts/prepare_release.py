"""Prepare tagged integration metadata without resolving or changing dependencies."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Any, cast

WORKSPACE = Path(__file__).resolve().parents[1]
RELEASE_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:b[1-9][0-9]*)?"
)
PUBLIC_VERSION = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
)
INPUTS = (
    "pyproject.toml",
    "custom_components/ipguard/manifest.json",
    "uv.lock",
)
TABLE = re.compile(r"(?m)^[ \t]*\[[^\r\n]*\][ \t]*(?:#[^\r\n]*)?\r?$")
PACKAGE = re.compile(r"(?m)^\[\[package\]\][ \t]*(?:#[^\r\n]*)?\r?$")


def _version(value: object, *, integration: bool = False) -> str:
    pattern = RELEASE_VERSION if integration else PUBLIC_VERSION
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("Unsupported project version format.")
    return value


def _pin(value: object) -> str:
    if not isinstance(value, list) or len(value) != 1:
        raise ValueError("Expected exactly one exact SDK dependency pin.")
    requirement = value[0]
    prefix = "ipguard-api=="
    if not isinstance(requirement, str) or not requirement.startswith(prefix):
        raise ValueError("Expected exactly one exact SDK dependency pin.")
    return _version(requirement.removeprefix(prefix))


def _project(document: dict[str, Any], name: str) -> dict[str, Any]:
    project = document.get("project")
    if not isinstance(project, dict) or project.get("name") != name:
        raise ValueError("Unexpected or missing project metadata.")
    return cast(dict[str, Any], project)


def validate_sdk_source(document: dict[str, Any]) -> None:
    """Require registry dependencies without local or Git source overrides."""
    uv = document.get("tool", {}).get("uv", {})
    if not isinstance(uv, dict) or "workspace" in uv or uv.get("sources"):
        raise ValueError(
            "The SDK must use the pinned PyPI release without source overrides."
        )


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for name, value in pairs:
        if name in document:
            raise ValueError("Duplicate manifest field.")
        document[name] = value
    return document


def _manifest(text: str) -> dict[str, Any]:
    document = json.loads(text, object_pairs_hook=_json_pairs)
    if not isinstance(document, dict) or document.get("domain") != "ipguard":
        raise ValueError("Unexpected integration manifest.")
    return cast(dict[str, Any], document)


def _table(text: str, name: str) -> tuple[int, int]:
    headers = list(TABLE.finditer(text))
    selected = [
        index
        for index, header in enumerate(headers)
        if header.group().split("#", 1)[0].strip() == f"[{name}]"
    ]
    if len(selected) != 1:
        raise ValueError("Expected one editable TOML metadata table.")
    index = selected[0]
    end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
    return headers[index].end(), end


def _toml_field(text: str, table: str, name: str, value: object) -> str:
    start, end = _table(text, table)
    section = text[start:end]
    pattern = re.compile(
        rf"(?m)^(?P<prefix>[ \t]*{re.escape(name)}[ \t]*=[ \t]*)"
        r"(?P<value>\[[^\]]*\]|\"[^\"\r\n]*\"|'[^'\r\n]*')"
        r"(?P<suffix>[ \t]*(?:#[^\r\n]*)?)(?=\r?$)"
    )
    matches = list(pattern.finditer(section))
    if len(matches) != 1:
        raise ValueError("Unsupported editable TOML field layout.")
    match = matches[0]
    if tomllib.loads(f"value = {match.group('value')}")["value"] == value:
        return text
    replacement = json.dumps(value)
    field_start, field_end = match.span("value")
    return text[: start + field_start] + replacement + text[start + field_end :]


def _json_field(text: str, name: str, value: object) -> str:
    """Locate a top-level member without altering surrounding or nested JSON."""
    decoder = json.JSONDecoder()
    position = text.index("{") + 1
    while True:
        while text[position].isspace():
            position += 1
        key, position = decoder.raw_decode(text, position)
        while text[position].isspace():
            position += 1
        if text[position] != ":":
            raise ValueError("Unsupported manifest field layout.")
        position += 1
        while text[position].isspace():
            position += 1
        start = position
        current, position = decoder.raw_decode(text, position)
        if key == name:
            if current == value:
                return text
            return text[:start] + json.dumps(value) + text[position:]
        while text[position].isspace():
            position += 1
        if text[position] != ",":
            raise ValueError("Missing editable manifest field.")
        position += 1


def _local_packages(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if type(lock.get("version")) is not int or lock["version"] != 1:
        raise ValueError("Unsupported lockfile format version.")
    packages = lock.get("package")
    if not isinstance(packages, list) or not all(
        isinstance(package, dict)
        and isinstance(package.get("name"), str)
        and bool(package["name"])
        and isinstance(package.get("version"), str)
        and bool(package["version"])
        and isinstance(package.get("source"), dict)
        for package in packages
    ):
        raise ValueError("Missing lockfile package records.")
    selected: dict[str, dict[str, Any]] = {}
    for name, source in (
        ("ha-ipguard", {"editable": "."}),
        ("ipguard-api", {"registry": "https://pypi.org/simple"}),
    ):
        matches = [package for package in packages if package.get("name") == name]
        if len(matches) != 1 or matches[0].get("source") != source:
            raise ValueError(
                "Expected one local integration and one pinned PyPI SDK package."
            )
        _version(matches[0].get("version"), integration=name == "ha-ipguard")
        selected[name] = matches[0]
    integration = selected["ha-ipguard"]
    if integration.get("dependencies") != [{"name": "ipguard-api"}]:
        raise ValueError("Unexpected integration lockfile dependencies.")
    metadata = integration.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("requires-dist") != [
        {"name": "ipguard-api", "specifier": f"=={selected['ipguard-api']['version']}"}
    ]:
        raise ValueError("Unexpected integration lockfile dependency metadata.")
    return selected


def _lock_versions(text: str, versions: dict[str, str]) -> str:
    headers = list(PACKAGE.finditer(text))
    if not headers:
        raise ValueError("Unsupported lockfile package layout.")
    chunks = [text[: headers[0].start()]]
    seen: set[str] = set()
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[header.start() : end]
        package = tomllib.loads(block)["package"][0]
        name = package.get("name")
        if name in versions:
            if name in seen:
                raise ValueError("Duplicate editable lockfile package.")
            seen.add(name)
            # The version belongs to the package table, before any child tables.
            block = _toml_field(
                block.replace("[[package]]", "[local]", 1),
                "local",
                "version",
                versions[name],
            )
            block = block.replace("[local]", "[[package]]", 1)
        chunks.append(block)
    if seen != set(versions):
        raise ValueError("Missing editable lockfile package.")
    return "".join(chunks)


def _regular_file(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Release preparation inputs must not use symlinks.")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Release preparation inputs must be regular files.")


def _stage(path: Path, content: bytes) -> Path:
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.prepare-", delete=False
    ) as temporary:
        staged = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            os.chmod(staged, stat.S_IMODE(path.stat().st_mode))
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    return staged


def _write_updates(originals: dict[Path, bytes], updates: dict[Path, bytes]) -> None:
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for path, content in updates.items():
            if content != originals[path]:
                staged[path] = _stage(path, content)
                backups[path] = _stage(path, originals[path])
        for path, original in originals.items():
            _regular_file(path)
            if path.read_bytes() != original:
                raise ValueError(
                    "Release preparation inputs changed during preparation."
                )
        try:
            for path, temporary in staged.items():
                os.replace(temporary, path)
                committed.append(path)
        except BaseException:
            for path in reversed(committed):
                try:
                    os.replace(backups[path], path)
                except OSError as rollback_error:
                    raise RuntimeError(
                        "Release preparation rollback failed."
                    ) from rollback_error
            raise
    finally:
        for temporary in (*staged.values(), *backups.values()):
            temporary.unlink(missing_ok=True)


def prepare_release(root: Path, tag: str = "") -> None:
    """Set the integration version and manifest pin without changing registry packages."""
    if not isinstance(tag, str) or (
        tag and (not tag.startswith("v") or RELEASE_VERSION.fullmatch(tag[1:]) is None)
    ):
        raise ValueError("Release tag must be vX.Y.Z or vX.Y.ZbN.")
    root = Path(os.path.abspath(root))
    originals: dict[Path, bytes] = {}
    for relative in INPUTS:
        path = root / relative
        _regular_file(path)
        originals[path] = path.read_bytes()
    texts = {path: content.decode("utf-8") for path, content in originals.items()}
    project_path, manifest_path, lock_path = (root / name for name in INPUTS)
    project = tomllib.loads(texts[project_path])
    manifest = _manifest(texts[manifest_path])
    lock = tomllib.loads(texts[lock_path])
    integration = _project(project, "ha-ipguard")
    validate_sdk_source(project)
    version = _version(integration.get("version"), integration=True)
    sdk_version = _pin(integration.get("dependencies"))
    _version(manifest.get("version"), integration=True)
    _pin(manifest.get("requirements"))
    if _local_packages(lock)["ipguard-api"]["version"] != sdk_version:
        raise ValueError("Run uv lock after changing the SDK dependency pin.")
    version = tag[1:] if tag else version
    requirement = [f"ipguard-api=={sdk_version}"]

    project_text = _toml_field(texts[project_path], "project", "version", version)
    manifest_text = _json_field(texts[manifest_path], "version", version)
    manifest_text = _json_field(manifest_text, "requirements", requirement)
    lock_text = _lock_versions(texts[lock_path], {"ha-ipguard": version})

    expected_project = copy.deepcopy(project)
    expected_project["project"].update(version=version, dependencies=requirement)
    expected_manifest = copy.deepcopy(manifest)
    expected_manifest.update(version=version, requirements=requirement)
    expected_lock = copy.deepcopy(lock)
    expected_packages = _local_packages(expected_lock)
    expected_packages["ha-ipguard"]["version"] = version
    if (
        tomllib.loads(project_text) != expected_project
        or _manifest(manifest_text) != expected_manifest
        or tomllib.loads(lock_text) != expected_lock
    ):
        raise ValueError("Preparation would alter unapproved metadata fields.")
    _write_updates(
        originals,
        {
            project_path: project_text.encode("utf-8"),
            manifest_path: manifest_text.encode("utf-8"),
            lock_path: lock_text.encode("utf-8"),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=WORKSPACE)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()
    try:
        prepare_release(args.root, args.tag)
    except (OSError, ValueError) as error:
        parser.exit(2, f"Release preparation failed: {error}\n")


if __name__ == "__main__":
    main()
