"""Exercise deterministic release preparation using only synthetic local metadata."""

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts import prepare_release as preparer

PROJECT = """# Preserve the selected build backend and allowlist.
[build-system]
requires = ["example-build==1.0"]
build-backend = "example.build"

[project]
name = "ha-ipguard"
version = "0.1.0b4" # Integration snapshot
description = "Preserve this text and its spacing."
dependencies = [
    "ipguard-api==0.1.0b1", # Existing SDK pin
]

[tool.example]
version = "unrelated"
only-include = ["source.py", "manifest.json"]
"""
SDK = """[project]
name = "ipguard-api"
version = "0.1.0b3"
dependencies = ["pyzmq>=27.2.0,<28"]

[tool.example]
profile = "unchanged"
"""
REGISTRY = """[[package]]
name = "pyzmq"
version = "27.2.0"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://example.invalid/pyzmq.tar.gz", hash = "sha256:0123456789abcdef", size = 1234 }
wheels = [
    { url = "https://example.invalid/pyzmq.whl", hash = "sha256:fedcba9876543210", size = 5678 },
]
"""
HA_LOCK = """[[package]]
name = "ha-ipguard"
version = "0.1.0b2"
source = { editable = "." }
dependencies = [
    { name = "ipguard-api" },
]

[package.metadata]
requires-dist = [{ name = "ipguard-api", editable = "ipguard-api" }]

[package.metadata.requires-dev]
dev = [{ name = "example-build", specifier = "==1.0" }]

"""
SDK_LOCK = """[[package]]
name = "ipguard-api"
version = "0.1.0b1"
source = { editable = "ipguard-api" }
dependencies = [{ name = "pyzmq" }]

[package.metadata]
requires-dist = [{ name = "pyzmq", specifier = ">=27.2.0,<28" }]

"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    files = {
        "pyproject.toml": PROJECT,
        "ipguard-api/pyproject.toml": SDK,
        "custom_components/ipguard/manifest.json": (
            '{\n  "domain": "ipguard",\n  "version": "0.1.0b2",\n'
            '  "requirements": ["ipguard-api==0.1.0b1"],\n'
            '  "extra": {"version": "nested", "requirements": []}\n}\n'
        ),
        "uv.lock": "version = 1\nrevision = 3\n\n" + HA_LOCK + SDK_LOCK + REGISTRY,
        "ipguard-api/src/ipguard_api/models.py": 'PROFILE = "Pixel 7"\n',
        "custom_components/ipguard/const.py": 'DOMAIN = "ipguard"\n',
        "build-constraints.txt": "example-build==1.0 --hash=sha256:123456\n",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _replace(root: Path, name: str, old: str, new: str) -> None:
    path = root / name
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))


def _assert_metadata(root: Path, version: str, sdk_version: str) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    sdk = tomllib.loads((root / "ipguard-api/pyproject.toml").read_text())["project"]
    manifest = json.loads(
        (root / "custom_components/ipguard/manifest.json").read_text()
    )
    packages = tomllib.loads((root / "uv.lock").read_text())["package"]
    lock = {package["name"]: package for package in packages}
    requirement = [f"ipguard-api=={sdk_version}"]
    assert (
        project["version"]
        == manifest["version"]
        == lock["ha-ipguard"]["version"]
        == version
    )
    assert sdk["version"] == lock["ipguard-api"]["version"] == sdk_version
    assert project["dependencies"] == manifest["requirements"] == requirement


@pytest.mark.parametrize("tag", ["v2.3.4b5", "v2.3.4"])
def test_tag_prepares_mismatched_snapshot_and_preserves_other_bytes(
    workspace: Path, tag: str
) -> None:
    before = _snapshot(workspace)
    preparer.prepare_release(workspace, tag)
    after = _snapshot(workspace)
    _assert_metadata(workspace, tag[1:], "0.1.0b3")
    assert after.keys() == before.keys()
    assert {name for name in before if before[name] != after[name]} == {
        "pyproject.toml",
        "custom_components/ipguard/manifest.json",
        "uv.lock",
    }
    assert after["pyproject.toml"] == before["pyproject.toml"].replace(
        b'"0.1.0b4"', f'"{tag[1:]}"'.encode(), 1
    ).replace(
        b'[\n    "ipguard-api==0.1.0b1", # Existing SDK pin\n]',
        b'["ipguard-api==0.1.0b3"]',
        1,
    )
    assert after["uv.lock"] == before["uv.lock"].replace(
        b'"0.1.0b2"', f'"{tag[1:]}"'.encode(), 1
    ).replace(b'"0.1.0b1"', b'"0.1.0b3"', 1)
    assert after["custom_components/ipguard/manifest.json"] == before[
        "custom_components/ipguard/manifest.json"
    ].replace(b'"0.1.0b2"', f'"{tag[1:]}"'.encode(), 1).replace(
        b'"ipguard-api==0.1.0b1"', b'"ipguard-api==0.1.0b3"', 1
    )
    assert (workspace / "uv.lock").read_text().endswith(REGISTRY)


def test_no_tag_uses_snapshot_version_and_independent_sdk_version(
    workspace: Path,
) -> None:
    _replace(workspace, "ipguard-api/pyproject.toml", "0.1.0b3", "9.8.7rc2.post1.dev3")
    sdk_bytes = (workspace / "ipguard-api/pyproject.toml").read_bytes()
    preparer.prepare_release(workspace)
    _assert_metadata(workspace, "0.1.0b4", "9.8.7rc2.post1.dev3")
    assert (workspace / "ipguard-api/pyproject.toml").read_bytes() == sdk_bytes
    assert (workspace / "uv.lock").read_text().endswith(REGISTRY)


def test_preserves_crlf_line_endings_and_file_permissions(workspace: Path) -> None:
    for name in preparer.INPUTS:
        path = workspace / name
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        path.chmod(0o640)
    sdk_bytes = (workspace / "ipguard-api/pyproject.toml").read_bytes()
    preparer.prepare_release(workspace, "v2.0.0")
    _assert_metadata(workspace, "2.0.0", "0.1.0b3")
    for name in preparer.INPUTS:
        path = workspace / name
        assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")
        assert path.stat().st_mode & 0o777 == 0o640
    assert (workspace / "ipguard-api/pyproject.toml").read_bytes() == sdk_bytes


def test_repeat_preparation_is_byte_idempotent_without_writes(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preparer.prepare_release(workspace, "v1.2.3b4")
    before = _snapshot(workspace)

    def unexpected_write(path: Path, content: bytes) -> Path:
        raise AssertionError("Idempotent preparation must not stage writes.")

    monkeypatch.setattr(preparer, "_stage", unexpected_write)
    preparer.prepare_release(workspace, "v1.2.3b4")
    assert _snapshot(workspace) == before


@pytest.mark.parametrize(
    "tag",
    [
        "1.2.3",
        "v01.2.3",
        "v1.02.3",
        "v1.2.03",
        "v1.2.3b0",
        "v1.2.3b01",
        "v1.2.3rc1",
        "v1.2.3+local",
        "v1.2.3\n",
        "refs/tags/v1.2.3",
    ],
)
def test_invalid_tag_makes_no_writes(workspace: Path, tag: str) -> None:
    before = _snapshot(workspace)
    with pytest.raises(ValueError, match="Release tag"):
        preparer.prepare_release(workspace, tag)
    assert _snapshot(workspace) == before


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("pyproject.toml", 'name = "ha-ipguard"', 'name = "other"'),
        ("pyproject.toml", 'version = "0.1.0b4"', 'version = "0.1.0rc4"'),
        ("pyproject.toml", '"ipguard-api==0.1.0b1",', '"ipguard-api>=0.1.0b1",'),
        (
            "pyproject.toml",
            '"ipguard-api==0.1.0b1",',
            '"ipguard-api==0.1.0b1", "other==1.0",',
        ),
        (
            "ipguard-api/pyproject.toml",
            'version = "0.1.0b3"',
            'version = "unsafe/value"',
        ),
        ("ipguard-api/pyproject.toml", 'name = "ipguard-api"', 'name = "other"'),
        (
            "custom_components/ipguard/manifest.json",
            '"domain": "ipguard"',
            '"domain": "other"',
        ),
        (
            "custom_components/ipguard/manifest.json",
            '"version": "0.1.0b2"',
            '"version": "0.1.0rc2"',
        ),
        (
            "custom_components/ipguard/manifest.json",
            '["ipguard-api==0.1.0b1"]',
            '["ipguard-api==0.1.0b1", "other==1.0"]',
        ),
        (
            "custom_components/ipguard/manifest.json",
            '"domain": "ipguard",',
            '"domain": "ipguard", "domain": "ipguard",',
        ),
        (
            "uv.lock",
            'source = { editable = "." }',
            'source = { editable = "elsewhere" }',
        ),
        (
            "uv.lock",
            'source = { editable = "ipguard-api" }',
            'source = { registry = "https://pypi.org/simple" }',
        ),
        (
            "uv.lock",
            'source = { editable = "ipguard-api" }',
            'source = { editable = "ipguard-api", extra = "unknown" }',
        ),
        (
            "uv.lock",
            '{ name = "ipguard-api" },',
            '{ name = "ipguard-api" }, { name = "other" },',
        ),
        ("uv.lock", 'editable = "ipguard-api" }]', 'editable = "wrong" }]'),
        ("uv.lock", SDK_LOCK, SDK_LOCK + SDK_LOCK),
        ("uv.lock", HA_LOCK, HA_LOCK + HA_LOCK),
        ("uv.lock", SDK_LOCK, ""),
        ("uv.lock", HA_LOCK, ""),
        ("uv.lock", 'version = "0.1.0b1"', 'version = "unsafe/value"'),
        ("uv.lock", "version = 1", "version = 2"),
        ("uv.lock", "version = 1", "version = true"),
        (
            "uv.lock",
            '[[package]]\nname = "pyzmq"',
            '[[package]]\nname = ["pyzmq"]',
        ),
        ("uv.lock", "version = 1", "version = [invalid"),
        ("pyproject.toml", "[project]", "[project"),
    ],
)
def test_invalid_metadata_fails_before_any_write(
    workspace: Path, name: str, old: str, new: str
) -> None:
    _replace(workspace, name, old, new)
    before = _snapshot(workspace)
    with pytest.raises(ValueError):
        preparer.prepare_release(workspace, "v2.0.0")
    assert _snapshot(workspace) == before


@pytest.mark.parametrize("name", preparer.INPUTS)
def test_symlink_input_is_rejected_without_mutating_its_target(
    workspace: Path, name: str
) -> None:
    path = workspace / name
    target = workspace.parent / (name.replace("/", "_") + ".original")
    path.replace(target)
    path.symlink_to(target)
    before = _snapshot(workspace)
    target_bytes = target.read_bytes()
    with pytest.raises(ValueError, match="symlinks"):
        preparer.prepare_release(workspace, "v2.0.0")
    assert _snapshot(workspace) == before
    assert path.is_symlink()
    assert target.read_bytes() == target_bytes


def test_symlink_parent_directory_is_rejected(workspace: Path) -> None:
    directory = workspace / "custom_components"
    target = workspace.parent / "components"
    directory.replace(target)
    directory.symlink_to(target, target_is_directory=True)
    before = _snapshot(workspace)
    target_bytes = (target / "ipguard/manifest.json").read_bytes()
    with pytest.raises(ValueError, match="symlinks"):
        preparer.prepare_release(workspace, "v2.0.0")
    assert _snapshot(workspace) == before
    assert (target / "ipguard/manifest.json").read_bytes() == target_bytes


def test_failed_staging_makes_no_writes(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(workspace)
    stage = preparer._stage
    count = 0

    def fail_stage(path: Path, content: bytes) -> Path:
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("Synthetic disk failure")
        return stage(path, content)

    monkeypatch.setattr(preparer, "_stage", fail_stage)
    with pytest.raises(OSError, match="Synthetic disk failure"):
        preparer.prepare_release(workspace, "v2.0.0")
    assert _snapshot(workspace) == before


def test_failed_commit_rolls_back_prior_replacements(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(workspace)
    replace = os.replace
    count = 0

    def fail_second_replace(source: Path, target: Path) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("Synthetic commit failure")
        replace(source, target)

    monkeypatch.setattr(os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="Synthetic commit failure"):
        preparer.prepare_release(workspace, "v2.0.0")
    assert count == 3  # First commit, failed second commit, first-file rollback.
    assert _snapshot(workspace) == before


def test_semantic_safeguard_rejects_unapproved_transform(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _snapshot(workspace)
    transform = preparer._toml_field

    def contaminated_transform(text: str, table: str, name: str, value: object) -> str:
        return transform(text, table, name, value).replace(
            'description = "Preserve this text and its spacing."',
            'description = "unapproved change"',
        )

    monkeypatch.setattr(preparer, "_toml_field", contaminated_transform)
    with pytest.raises(ValueError, match="unapproved"):
        preparer.prepare_release(workspace, "v2.0.0")
    assert _snapshot(workspace) == before


def test_cli_runs_before_dependencies_are_available(workspace: Path) -> None:
    script = Path(preparer.__file__)
    interpreter = Path(sys.executable)
    result = subprocess.run(
        [
            str(interpreter),
            "-I",
            "-S",
            str(script),
            "--root",
            str(workspace),
            "--tag",
            "v3.2.1b4",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(interpreter.parent)},
    )
    assert result.returncode == 0, result.stderr
    _assert_metadata(workspace, "3.2.1b4", "0.1.0b3")
    assert re.search(r"prepare-", "\n".join(_snapshot(workspace))) is None
