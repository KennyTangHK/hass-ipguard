"""Reject unreviewed code, credentials and inconsistent publication metadata."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import shutil
import stat
import tarfile
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

from scripts import release_metadata
from scripts.build_release import build_release
from scripts.packaging_policy import load_policy
from scripts.release_metadata import (
    ReleaseMetadata,
    checksum_text,
    read_metadata,
    validate_assets,
)


@pytest.fixture
def release(tmp_path: Path) -> tuple[Path, Path, ReleaseMetadata]:
    path = Path(__file__).parents[1] / "tests/test_release.py"
    spec = importlib.util.spec_from_file_location("packaging_fixtures", path)
    assert spec and spec.loader
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    root, wheel = fixtures.release_fixture(tmp_path, "1.2.3b4", "2.3.4")
    dist = root / "dist"
    shutil.copytree(wheel.parent, dist)
    metadata = read_metadata(root, "v1.2.3b4")
    build_release(root, dist / wheel.name, dist)
    return root, dist, metadata


def test_upgrade_baseline_matches_the_smoke_test_pin() -> None:
    root = Path(__file__).parents[1]
    source = ast.parse((root / "scripts/smoke_release.py").read_text())
    declarations = [
        statement.value
        for statement in source.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "BASELINE_SHA256"
            for target in statement.targets
        )
    ]
    assert len(declarations) == 1
    expected = ast.literal_eval(declarations[0])
    baseline = root / ".github/fixtures/ipguard-upgrade-baseline.zip"
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == expected


def test_upgrade_baseline_is_excluded_from_distributions_and_publication() -> None:
    root = Path(__file__).parents[1]
    policy = load_policy(root)
    baseline = ".github/fixtures/ipguard-upgrade-baseline.zip"
    for selection in (
        policy.integration_wheel,
        policy.integration_sdist,
        policy.sdk_wheel,
        policy.sdk_sdist,
    ):
        assert baseline not in selection
        assert not any(name.startswith(".github/fixtures/") for name in selection)
    assert Path(baseline).name not in read_metadata(root).publication_assets


def _write_archives(
    dist: Path,
    metadata: ReleaseMetadata,
    requirement: str | None = None,
    omit: str | None = None,
    changes: dict[str, bytes] | None = None,
) -> None:
    with ZipFile(dist / "ipguard.zip") as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    if requirement:
        manifest = json.loads(files["manifest.json"])
        manifest["requirements"] = [requirement]
        files["manifest.json"] = json.dumps(manifest).encode()
    if omit:
        del files[omit]
    if changes:
        files.update(changes)
    for filename, prefix in (
        ("ipguard.zip", ""),
        (f"ipguard-{metadata.version}.zip", "custom_components/ipguard/"),
    ):
        path = dist / filename
        with ZipFile(path, "w") as archive:
            for name, data in files.items():
                archive.writestr(prefix + name, data)
        path.with_suffix(".zip.sha256").write_text(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {filename}\n"
        )


def test_independent_versions_and_explicit_asset_set(
    release: tuple[Path, Path, ReleaseMetadata],
) -> None:
    root, dist, metadata = release
    (dist / "ipguard-old.zip").write_bytes(b"stale artifact")
    assets = validate_assets(dist, metadata, root=root)
    assert len(assets) == 6
    assert dist / "ipguard_api-2.3.4-py3-none-any.whl" in assets
    assert "stale" not in checksum_text(assets)
    assert len(checksum_text(assets).splitlines()) == 6
    assert len(metadata.publication_assets) == 9
    assert metadata.prerelease is True
    assert metadata.latest is False


@pytest.mark.parametrize(
    "tag",
    [
        "v1.2.3",
        "v1.2.3b0",
        "v1.2.3b5",
        "v01.2.3b4",
        "v1.2.3b4\nextra",
        "v1.2.3b4;echo BAD",
    ],
)
def test_reject_wrong_or_unsafe_tag(
    release: tuple[Path, Path, ReleaseMetadata], tag: str
) -> None:
    root, _, _ = release
    with pytest.raises(ValueError, match="Release tag"):
        read_metadata(root, tag)


@pytest.mark.parametrize("tag", ["v1.2.3b5", "v1.2.3"])
def test_valid_mismatched_tag_reports_actual_and_expected_versions(
    release: tuple[Path, Path, ReleaseMetadata], tag: str
) -> None:
    root, _, _ = release
    with pytest.raises(ValueError) as failure:
        read_metadata(root, tag)
    message = str(failure.value)
    assert f"Release tag {tag}" in message
    assert "integration version 1.2.3b4" in message
    assert "expected v1.2.3b4" in message
    assert f"--prepare --tag {tag}" in message


@pytest.mark.parametrize(
    "tag", ["v1.2.3b4\nprivate-text", "v1.2.3b4;echo private-text", "1.2.3b4"]
)
def test_malformed_tag_error_does_not_reflect_unvalidated_input(
    release: tuple[Path, Path, ReleaseMetadata], tag: str
) -> None:
    root, _, _ = release
    with pytest.raises(ValueError) as failure:
        read_metadata(root, tag)
    assert str(failure.value) == "Release tag must be vX.Y.Z or vX.Y.ZbN."
    assert tag not in str(failure.value)


@pytest.mark.parametrize(
    "version", ["01.2.3", "1.2.3b0", "1.2.3a1", "1.2.3rc1", "1.2.3.dev1", "1.2.3.post1"]
)
def test_reject_nonrelease_integration_version(
    release: tuple[Path, Path, ReleaseMetadata], version: str
) -> None:
    root, _, _ = release
    path = root / "pyproject.toml"
    path.write_text(path.read_text().replace("1.2.3b4", version))
    with pytest.raises(ValueError, match="Integration versions"):
        read_metadata(root, "v" + version)


def test_stable_version_and_install_notes(
    release: tuple[Path, Path, ReleaseMetadata],
) -> None:
    root, _, _ = release
    for filename in ("pyproject.toml", "custom_components/ipguard/manifest.json"):
        path = root / filename
        path.write_text(path.read_text().replace("1.2.3b4", "1.2.3"))
    metadata = read_metadata(root, "v1.2.3")
    assert metadata.prerelease is False
    assert metadata.latest is True
    assert "enable beta versions" not in release_metadata.release_notes(metadata)
    assert "beta" not in release_metadata.release_notes(metadata).splitlines()[0]


@pytest.mark.parametrize("version", ["1.2.3b4", "1.2.3"])
def test_release_notes_use_hacs_catalogue_for_both_channels(version: str) -> None:
    metadata = ReleaseMetadata(version, "2.3.4")
    notes = release_metadata.release_notes(metadata)
    assert "Find IPGUARD in HACS" in notes
    assert "KennyTangHK/hass-ipguard" in notes
    assert ("enable beta versions" in notes) is metadata.prerelease
    assert "custom repositor" not in notes.lower()
    assert "add this repository" not in notes.lower()


def test_release_workflow_accepts_matching_version_tag(
    release: tuple[Path, Path, ReleaseMetadata], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _ = release
    output, notes = root / "github-output", root / "release-notes.md"
    monkeypatch.setattr(release_metadata, "ROOT", root)
    monkeypatch.setattr(
        "sys.argv",
        [
            "release_metadata.py",
            "--ref-type",
            "tag",
            "--tag",
            "v1.2.3b4",
            "--github-output",
            str(output),
            "--notes-file",
            str(notes),
        ],
    )
    release_metadata.main()
    assert output.read_text() == (
        "version=1.2.3b4\nsdk_version=2.3.4\nprerelease=true\nlatest=false\n"
    )
    assert "Find IPGUARD in HACS" in notes.read_text()


@pytest.mark.parametrize("tag", ["v1.2.3b5", "v1.2.3"])
def test_release_preparation_derives_versions_and_flags_from_tag(
    release: tuple[Path, Path, ReleaseMetadata],
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
) -> None:
    root, _, _ = release
    output = root / "github-output"
    sdk_source = (root / "ipguard-api/pyproject.toml").read_bytes()
    monkeypatch.setattr(release_metadata, "ROOT", root)
    monkeypatch.setattr(
        "sys.argv",
        [
            "release_metadata.py",
            "--prepare",
            "--tag",
            tag,
            "--ref-type",
            "tag",
            "--github-output",
            str(output),
        ],
    )
    release_metadata.main()
    metadata = read_metadata(root, tag)
    assert metadata.version == tag[1:]
    assert metadata.sdk_version == "2.3.4"
    assert (root / "ipguard-api/pyproject.toml").read_bytes() == sdk_source
    assert output.read_text() == (
        f"version={tag[1:]}\nsdk_version=2.3.4\n"
        f"prerelease={str('b' in tag).lower()}\nlatest={str('b' not in tag).lower()}\n"
    )


@pytest.mark.parametrize("tag", ["v1.2.3b0", "1.2.3b5", "v1.2.3b5\nunsafe"])
def test_invalid_preparation_tag_changes_no_sources_or_outputs(
    release: tuple[Path, Path, ReleaseMetadata],
    monkeypatch: pytest.MonkeyPatch,
    tag: str,
) -> None:
    root, _, _ = release
    names = (
        "pyproject.toml",
        "custom_components/ipguard/manifest.json",
        "uv.lock",
    )
    before = {name: (root / name).read_bytes() for name in names}
    output = root / "github-output"
    monkeypatch.setattr(release_metadata, "ROOT", root)
    monkeypatch.setattr(
        "sys.argv",
        [
            "release_metadata.py",
            "--prepare",
            "--tag",
            tag,
            "--github-output",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as failure:
        release_metadata.main()
    assert failure.value.code == 1
    assert before == {name: (root / name).read_bytes() for name in names}
    assert not output.exists()


@pytest.mark.parametrize(
    "ref_type,tag",
    [
        ("branch", "v1.2.3b4"),
        ("branch", "v1.2.3"),
        ("unknown", "v1.2.3b4"),
        ("", "v1.2.3b4"),
        ("tag", ""),
    ],
)
def test_release_workflow_rejects_non_tag_refs_before_metadata_or_outputs(
    release: tuple[Path, Path, ReleaseMetadata],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref_type: str,
    tag: str,
) -> None:
    root, dist, _ = release
    before = {
        name: (root / name).read_bytes()
        for name in (
            "pyproject.toml",
            "custom_components/ipguard/manifest.json",
            "uv.lock",
        )
    }
    outputs = [
        root / "github-output",
        root / "release-notes.md",
        root / "release-assets.txt",
        dist / "SHA256SUMS",
    ]

    def unexpected_metadata_read(*args: object) -> ReleaseMetadata:
        raise AssertionError("Invalid release refs must fail before reading metadata.")

    monkeypatch.setattr(release_metadata, "ROOT", root)
    monkeypatch.setattr(release_metadata, "read_metadata", unexpected_metadata_read)
    monkeypatch.setattr(
        "sys.argv",
        [
            "release_metadata.py",
            "--prepare",
            "--ref-type",
            ref_type,
            "--tag",
            tag,
            "--github-output",
            str(outputs[0]),
            "--notes-file",
            str(outputs[1]),
            "--asset-list",
            str(outputs[2]),
            "--dist",
            str(dist),
            "--write-checksums",
        ],
    )
    with pytest.raises(SystemExit) as failure:
        release_metadata.main()
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Release workflows must run against a version tag." in captured.err
    assert all(not output.exists() for output in outputs)
    assert before == {name: (root / name).read_bytes() for name in before}


@pytest.mark.parametrize(
    "filename", ["pyproject.toml", "custom_components/ipguard/manifest.json"]
)
def test_reject_stale_sdk_pin(
    release: tuple[Path, Path, ReleaseMetadata], filename: str
) -> None:
    root, _, _ = release
    path = root / filename
    path.write_text(
        path.read_text().replace("ipguard-api==2.3.4", "ipguard-api==2.3.3")
    )
    with pytest.raises(ValueError, match="SDK version"):
        read_metadata(root)


def test_reject_unpublished_dependency_in_portable_zip(
    release: tuple[Path, Path, ReleaseMetadata],
) -> None:
    root, dist, metadata = release
    _write_archives(dist, metadata, "ipguard-api==2.3.4")
    with pytest.raises(ValueError, match="public runtime dependencies"):
        validate_assets(dist, metadata, root=root)


def test_reject_hacs_nested_layout(release: tuple[Path, Path, ReleaseMetadata]) -> None:
    root, dist, metadata = release
    (dist / "ipguard.zip").write_bytes(
        (dist / f"ipguard-{metadata.version}.zip").read_bytes()
    )
    with pytest.raises(ValueError, match="same integration"):
        validate_assets(dist, metadata, root=root)


@pytest.mark.parametrize(
    "path", ["../escaped.py", "a//b.py", "a/./b.py", "/absolute.py", "a\\b.py"]
)
def test_reject_unsafe_archive_path(
    release: tuple[Path, Path, ReleaseMetadata], path: str
) -> None:
    root, dist, metadata = release
    with ZipFile(dist / "ipguard.zip", "a") as archive:
        archive.writestr(path, "")
    with pytest.raises(ValueError, match="unsafe|noncanonical"):
        validate_assets(dist, metadata, root=root)


def test_reject_duplicate_and_symlink_zip_entries(
    release: tuple[Path, Path, ReleaseMetadata],
) -> None:
    root, dist, metadata = release
    entry = ZipInfo("linked.py")
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(dist / "ipguard.zip", "a") as archive:
        archive.writestr(entry, "api.py")
    with pytest.raises(ValueError, match="invalid members"):
        validate_assets(dist, metadata, root=root)


def test_reject_duplicate_wheel_member(
    release: tuple[Path, Path, ReleaseMetadata],
) -> None:
    root, dist, metadata = release
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        ZipFile(
            dist / f"ipguard_api-{metadata.sdk_version}-py3-none-any.whl", "a"
        ) as archive,
    ):
        archive.writestr("ipguard_api/models.py", "class Different: pass\n")
    with pytest.raises(ValueError, match="duplicate"):
        validate_assets(dist, metadata, root=root)


def test_reject_wrong_built_sdk(release: tuple[Path, Path, ReleaseMetadata]) -> None:
    root, dist, metadata = release
    with ZipFile(dist / "ipguard_api-2.3.4-py3-none-any.whl", "w") as wheel:
        wheel.writestr(
            "ipguard_api-2.3.4.dist-info/METADATA",
            "Name: ipguard-api\nVersion: 2.3.3\n",
        )
    with pytest.raises(ValueError, match="distribution metadata"):
        validate_assets(dist, metadata, root=root)


@pytest.mark.parametrize(
    "change", ["changed", "missing", "extra", "license", "initializer"]
)
def test_sdk_bytes_are_checked_despite_unchanged_wheel_hash(
    release: tuple[Path, Path, ReleaseMetadata], change: str
) -> None:
    root, dist, metadata = release
    if change == "missing":
        _write_archives(dist, metadata, omit="_vendor/ipguard_api/models.py")
    else:
        changes = {
            "changed": {"_vendor/ipguard_api/models.py": b"class Different: pass\n"},
            "extra": {
                "_vendor/ipguard_api/private.json": b'{"password":"SYNTHETIC_PRIVATE_VALUE"}'
            },
            "license": {"_vendor/LICENSE": b"different license"},
            "initializer": {"_vendor/__init__.py": b"import os\n"},
        }
        _write_archives(dist, metadata, changes=changes[change])
    with pytest.raises(ValueError, match="Bundled SDK files|allowlist|licenses differ"):
        validate_assets(dist, metadata, root=root)


@pytest.mark.parametrize(
    "member", ["session.json", "private.yaml", "_vendor/ipguard_api/credentials.json"]
)
def test_reject_stray_zip_credentials(
    release: tuple[Path, Path, ReleaseMetadata], member: str
) -> None:
    root, dist, metadata = release
    _write_archives(dist, metadata, changes={member: b"SYNTHETIC_PRIVATE_VALUE"})
    with pytest.raises(ValueError, match="allowlist"):
        validate_assets(dist, metadata, root=root)


@pytest.mark.parametrize("package", ["ha_ipguard", "ipguard_api"])
def test_reject_stray_wheel_credentials(
    release: tuple[Path, Path, ReleaseMetadata], package: str
) -> None:
    root, dist, metadata = release
    version = metadata.version if package == "ha_ipguard" else metadata.sdk_version
    with ZipFile(dist / f"{package}-{version}-py3-none-any.whl", "a") as archive:
        archive.writestr("credentials.json", "SYNTHETIC_PRIVATE_VALUE")
    with pytest.raises(ValueError, match="allowlist"):
        validate_assets(dist, metadata, root=root)


@pytest.mark.parametrize("package", ["ha_ipguard", "ipguard_api"])
@pytest.mark.parametrize("kind", ["extra", "symlink", "duplicate", "traversal"])
def test_reject_unapproved_sdist_member(
    release: tuple[Path, Path, ReleaseMetadata], package: str, kind: str
) -> None:
    root, dist, metadata = release
    version = metadata.version if package == "ha_ipguard" else metadata.sdk_version
    stem = f"{package}-{version}"
    path = dist / (stem + ".tar.gz")
    with tarfile.open(path, "r:gz") as archive:
        files = [
            (entry, archive.extractfile(entry).read()) for entry in archive.getmembers()
        ]
    with tarfile.open(path, "w:gz") as archive:
        for entry, data in files:
            archive.addfile(entry, io.BytesIO(data))
        added = tarfile.TarInfo(stem + "/credentials.json")
        data = b"SYNTHETIC_PRIVATE_VALUE"
        if kind == "duplicate":
            added = tarfile.TarInfo(files[0][0].name)
        elif kind == "symlink":
            added.type = tarfile.SYMTYPE
            added.linkname = "../README.md"
        elif kind == "traversal":
            added.name = stem + "/../credentials.json"
        added.size = len(data) if added.isfile() else 0
        archive.addfile(added, io.BytesIO(data) if added.isfile() else None)
    with pytest.raises(ValueError, match="allowlist|invalid members|unsafe"):
        validate_assets(dist, metadata, root=root)


@pytest.mark.parametrize("missing", [False, True])
def test_reject_bad_or_missing_zip_checksum(
    release: tuple[Path, Path, ReleaseMetadata], missing: bool
) -> None:
    root, dist, metadata = release
    path = dist / "ipguard.zip.sha256"
    if missing:
        path.unlink()
    else:
        path.write_text("0" * 64 + "  ipguard.zip\n")
    with pytest.raises(ValueError, match="checksum"):
        validate_assets(dist, metadata, root=root)


@pytest.mark.parametrize(
    "omitted",
    ["LICENSE", "_vendor/LICENSE", "brand/icon.png", "docs/USAGE.md", "button.py"],
)
def test_reject_incomplete_installation(
    release: tuple[Path, Path, ReleaseMetadata], omitted: str
) -> None:
    root, dist, metadata = release
    _write_archives(dist, metadata, omit=omitted)
    with pytest.raises(ValueError, match="required integration files"):
        validate_assets(dist, metadata, root=root)


def test_checksum_generation_and_publish_asset_list(
    release: tuple[Path, Path, ReleaseMetadata], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, dist, metadata = release
    output = root / "github-output"
    asset_list = root / "release-assets.txt"
    monkeypatch.setattr(release_metadata, "ROOT", root)
    monkeypatch.setattr(
        "sys.argv",
        [
            "release_metadata.py",
            "--dist",
            str(dist),
            "--write-checksums",
            "--github-output",
            str(output),
        ],
    )
    release_metadata.main()
    assert (
        output.read_text()
        == "version=1.2.3b4\nsdk_version=2.3.4\nprerelease=true\nlatest=false\n"
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "release_metadata.py",
            "--tag",
            "v1.2.3b4",
            "--dist",
            str(dist),
            "--asset-list",
            str(asset_list),
        ],
    )
    release_metadata.main()
    assert asset_list.read_text().splitlines() == [
        str(dist / name) for name in metadata.publication_assets
    ]


def test_publish_rejects_tampered_aggregate_checksum(
    release: tuple[Path, Path, ReleaseMetadata],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, dist, metadata = release
    (dist / "SHA256SUMS").write_text(
        checksum_text(validate_assets(dist, metadata, root=root)).replace(
            "ipguard.zip", "extra.zip", 1
        )
    )
    monkeypatch.setattr(release_metadata, "ROOT", root)
    monkeypatch.setattr(
        "sys.argv", ["release_metadata.py", "--tag", "v1.2.3b4", "--dist", str(dist)]
    )
    with pytest.raises(SystemExit) as failure:
        release_metadata.main()
    assert failure.value.code == 1
    assert "SHA256SUMS must exactly match" in capsys.readouterr().err
