"""Validate release metadata and the exact assets published by the workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from scripts.build_release import SDK_SOURCE
from scripts.packaging_policy import (
    load_policy,
    portable_readme,
    source_bytes,
    validate_build_constraints,
    validate_sdist_payload,
    validate_wheel_payload,
    zip_contents,
)
from scripts.prepare_release import prepare_release, validate_sdk_source
from scripts.sdk_package import read_sdk, validate_sdk_artifacts

ROOT = Path(__file__).resolve().parents[1]

VERSION = re.compile(
    r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?"
    r"(?:\.post[0-9]+)?(?:\.dev[0-9]+)?"
)
RELEASE_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:b[1-9][0-9]*)?"
)


@dataclass(frozen=True)
class ReleaseMetadata:
    """The integration and independently versioned SDK in this checkout."""

    version: str
    sdk_version: str

    @property
    def prerelease(self) -> bool:
        return "b" in self.version

    @property
    def latest(self) -> bool:
        return not self.prerelease

    @property
    def assets(self) -> tuple[str, ...]:
        """Return only the expected assets, excluding stale build output."""
        return (
            "ipguard.zip",
            f"ipguard-{self.version}.zip",
            f"ha_ipguard-{self.version}-py3-none-any.whl",
            f"ha_ipguard-{self.version}.tar.gz",
            f"ipguard_api-{self.sdk_version}-py3-none-any.whl",
            f"ipguard_api-{self.sdk_version}.tar.gz",
        )

    @property
    def publication_assets(self) -> tuple[str, ...]:
        """Require the exact nine archives and checksum files published together."""
        return (
            *self.assets,
            "SHA256SUMS",
            "ipguard.zip.sha256",
            f"ipguard-{self.version}.zip.sha256",
        )


def read_metadata(root: Path, tag: str = "") -> ReleaseMetadata:
    """Reject inconsistent versions before running a build or publishing."""
    document = tomllib.loads((root / "pyproject.toml").read_text())
    validate_sdk_source(document)
    project = document["project"]
    sdk = read_sdk(root)
    manifest = json.loads(
        (root / "custom_components/ipguard/manifest.json").read_text()
    )
    hacs = json.loads((root / "hacs.json").read_text())
    if project["name"] != "ha-ipguard":
        raise ValueError("Unexpected project distribution names.")
    version = _version(project["version"])
    if RELEASE_VERSION.fullmatch(version) is None:
        raise ValueError("Integration versions must be X.Y.Z or X.Y.ZbN.")
    sdk_version = sdk.version
    if manifest.get("domain") != "ipguard" or manifest.get("version") != version:
        raise ValueError("Integration manifest and package versions disagree.")
    requirement = f"ipguard-api=={sdk_version}"
    if requirement not in project.get("dependencies", []):
        raise ValueError("Integration package must pin the current SDK version.")
    if manifest.get("requirements") != [requirement]:
        raise ValueError("Source manifest must pin the current SDK version.")
    if hacs.get("filename") != "ipguard.zip" or hacs.get("zip_release") is not True:
        raise ValueError("HACS must use the generated ipguard.zip release asset.")
    if tag:
        if not tag.startswith("v") or RELEASE_VERSION.fullmatch(tag[1:]) is None:
            raise ValueError("Release tag must be vX.Y.Z or vX.Y.ZbN.")
        if tag != f"v{version}":
            raise ValueError(
                f"Release tag {tag} does not match integration version {version}; "
                f"expected v{version}. Run python -m scripts.release_metadata "
                f"--prepare --tag {tag} "
                "to generate metadata from this tag before validating."
            )
    return ReleaseMetadata(version, sdk_version)


def _version(value: object) -> str:
    if not isinstance(value, str) or VERSION.fullmatch(value) is None:
        raise ValueError("Project versions must be safe, normalized public versions.")
    return value


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _check_distribution(
    metadata: bytes, name: str, version: str, requirements: list[str]
) -> None:
    parsed = BytesParser().parsebytes(metadata)
    if parsed["Name"] != name or parsed["Version"] != version:
        raise ValueError("Built distribution metadata does not match its project.")
    if parsed.get_all("Requires-Dist", []) != requirements:
        raise ValueError(
            "Built distribution dependencies do not match the approved project."
        )


def _check_wheel(path: Path, name: str, version: str, requirements: list[str]) -> None:
    with ZipFile(path) as wheel:
        metadata_files = [
            item for item in wheel.namelist() if item.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise ValueError("Each wheel must contain exactly one distribution.")
        _check_distribution(wheel.read(metadata_files[0]), name, version, requirements)


def _check_sdist(path: Path, name: str, version: str, requirements: list[str]) -> None:
    with tarfile.open(path, "r:gz") as sdist:
        metadata_files = [
            item
            for item in sdist.getmembers()
            if item.name.count("/") == 1 and item.name.endswith("/PKG-INFO")
        ]
        if len(metadata_files) != 1 or not metadata_files[0].isfile():
            raise ValueError("Each source distribution must contain one PKG-INFO.")
        source = sdist.extractfile(metadata_files[0])
        if source is None:
            raise ValueError("Source distribution metadata is unreadable.")
        with source:
            _check_distribution(source.read(), name, version, requirements)


def _archive_contents(path: Path, prefix: str = "") -> dict[str, bytes]:
    return zip_contents(path, prefix)


def validate_assets(
    dist: Path, metadata: ReleaseMetadata, root: Path = ROOT
) -> tuple[Path, ...]:
    """Validate exact archive layouts and both distributions against the PyPI lock."""
    policy = load_policy(root)
    validate_build_constraints(root)
    assets = tuple(dist / name for name in metadata.assets)
    if any(not path.is_file() or path.is_symlink() for path in assets):
        raise ValueError(
            "A required release asset is missing or is not a regular file."
        )
    sdk = validate_sdk_artifacts(root, dist)
    for stem, name, version in (
        ("ha_ipguard", "ha-ipguard", metadata.version),
        ("ipguard_api", "ipguard-api", metadata.sdk_version),
    ):
        requirements = (
            [f"ipguard-api=={metadata.sdk_version}"]
            if name == "ha-ipguard"
            else ["pyzmq<28,>=27.2.0"]
        )
        _check_wheel(
            dist / f"{stem}-{version}-py3-none-any.whl", name, version, requirements
        )
        _check_sdist(dist / f"{stem}-{version}.tar.gz", name, version, requirements)
    component_sources = source_bytes(root, policy.integration_wheel)
    validate_wheel_payload(
        dist / f"ha_ipguard-{metadata.version}-py3-none-any.whl",
        "ha-ipguard",
        metadata.version,
        component_sources,
        (root / "LICENSE").read_bytes(),
    )
    validate_sdist_payload(
        dist / f"ha_ipguard-{metadata.version}.tar.gz",
        f"ha_ipguard-{metadata.version}",
        source_bytes(root, policy.integration_sdist),
    )
    flat = _archive_contents(dist / "ipguard.zip")
    manual = _archive_contents(
        dist / f"ipguard-{metadata.version}.zip", "custom_components/ipguard/"
    )
    if flat != manual:
        raise ValueError("HACS and manual ZIPs must contain the same integration.")
    required = {
        "manifest.json",
        "api.py",
        "release.json",
        "__init__.py",
        "button.py",
        "config_flow.py",
        "LICENSE",
        "brand/icon.png",
        "docs/USAGE.md",
    }
    if not required.issubset(flat):
        raise ValueError(
            "Release ZIP is missing required integration files, licenses, or provenance."
        )
    expected = (
        {
            name.removeprefix("custom_components/ipguard/")
            for name in policy.component_files
        }
        | set(policy.documents)
        | {
            "LICENSE",
            "README.md",
            "release.json",
        }
    )
    if set(flat) != expected:
        raise ValueError(
            "Release ZIP payload differs from its explicit file allowlist."
        )
    if flat["LICENSE"] != (root / "LICENSE").read_bytes():
        raise ValueError("Integration license differs from its approved source.")
    if flat["README.md"] != portable_readme(root, metadata.version):
        raise ValueError("Release README differs from its approved source.")
    for name, data in source_bytes(root, policy.component_files).items():
        relative = name.removeprefix("custom_components/ipguard/")
        if flat[relative] != data:
            raise ValueError(
                "Release component differs from its approved source files."
            )
    if any(
        flat[name] != data
        for name, data in source_bytes(root, policy.documents).items()
    ):
        raise ValueError("Release documentation differs from its approved sources.")
    manifest = json.loads(flat["manifest.json"])
    expected_manifest = json.loads(
        component_sources["custom_components/ipguard/manifest.json"]
    )
    requirements = [f"ipguard-api=={metadata.sdk_version}"]
    if (
        manifest != expected_manifest
        or manifest.get("domain") != "ipguard"
        or manifest.get("version") != metadata.version
        or manifest.get("requirements") != requirements
    ):
        raise ValueError(
            "Portable ZIP must use public runtime dependencies and its own version."
        )
    provenance = json.loads(flat["release.json"])
    if provenance != {
        "integration_version": metadata.version,
        "sdk_name": "ipguard-api",
        "sdk_version": metadata.sdk_version,
        "sdk_wheel_sha256": sdk.wheel.sha256,
        "sdk_sdist_sha256": sdk.sdist.sha256,
        "runtime_requirements": requirements,
        "sdk_source": SDK_SOURCE,
    }:
        raise ValueError("SDK provenance does not match the pinned PyPI artifacts.")
    for path in assets[:2]:
        checksum = path.with_suffix(".zip.sha256")
        if (
            not checksum.is_file()
            or checksum.is_symlink()
            or checksum.read_text() != f"{_digest(path)}  {path.name}\n"
        ):
            raise ValueError("A ZIP checksum does not match its archive.")
    return assets


def checksum_text(assets: tuple[Path, ...]) -> str:
    """Generate the aggregate checksum file in a fixed, explicit order."""
    return "".join(f"{_digest(path)}  {path.name}\n" for path in assets)


def release_notes(metadata: ReleaseMetadata) -> str:
    """Describe release assets without claiming live provider validation."""
    channel = "beta " if metadata.prerelease else ""
    install = (
        "Find IPGUARD in HACS (`KennyTangHK/hass-ipguard`), enable beta versions, "
        "and install this release. HACS downloads `ipguard.zip`.\n\n"
        if metadata.prerelease
        else "Find IPGUARD in HACS (`KennyTangHK/hass-ipguard`) and install this "
        "release. HACS downloads `ipguard.zip`.\n\n"
    )
    return (
        f"IPGUARD Home Assistant {channel}{metadata.version}.\n\n"
        "**Beta software.** This integration is for testing. It is not a stable release.\n\n"
        "HACS catalogue inclusion is pending. In HACS, open the three-dot menu, "
        "select Custom repositories, and add `https://github.com/KennyTangHK/"
        "hass-ipguard` as an Integration.\n\n"
        + install
        + f"For manual installation, extract `ipguard-{metadata.version}.zip` into "
        "your Home Assistant configuration directory, then restart Home Assistant.\n\n"
        f"Home Assistant installs `ipguard-api` {metadata.sdk_version} from PyPI. "
        "The exact published wheel and source distribution are also "
        "included for verification. `SHA256SUMS` covers all six release archives.\n\n"
        "Buttons and the incoming-call event share one device per account. "
        "Auto hangup is enabled by default and attempts to hang up the internet call to "
        "trigger carrier-call fallback sooner. Change it for each account in "
        "the integration's Configure screen; ring notifications remain enabled "
        "when auto hangup is off. "
        "Video calling, camera streaming and audio conversation are unsupported.\n\n"
        "Validation includes static checks, automated tests, HACS validation, "
        "and Home Assistant container tests with a local simulated provider. "
        "These checks do not contact the IPGUARD service or operate a real door.\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="")
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Generate the integration version and synchronize its manifest pin before validating.",
    )
    parser.add_argument("--ref-type")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--write-checksums", action="store_true")
    parser.add_argument("--asset-list", type=Path)
    parser.add_argument("--notes-file", type=Path)
    args = parser.parse_args()
    if (args.write_checksums or args.asset_list) and args.dist is None:
        parser.error("--write-checksums and --asset-list require --dist")
    try:
        if args.ref_type is not None and (args.ref_type != "tag" or not args.tag):
            raise ValueError("Release workflows must run against a version tag.")
        if args.prepare:
            prepare_release(ROOT, args.tag)
        metadata = read_metadata(ROOT, args.tag)
        validate_build_constraints(ROOT)
        load_policy(ROOT)
        if args.dist is not None:
            assets = validate_assets(args.dist, metadata, root=ROOT)
            sums = args.dist / "SHA256SUMS"
            expected = checksum_text(assets)
            if args.write_checksums:
                sums.write_text(expected)
            elif not sums.is_file() or sums.read_text() != expected:
                raise ValueError(
                    "SHA256SUMS must exactly match the expected release assets."
                )
            if args.asset_list is not None:
                args.asset_list.write_text(
                    "".join(
                        f"{args.dist / name}\n" for name in metadata.publication_assets
                    )
                )
        if args.notes_file is not None:
            args.notes_file.write_text(release_notes(metadata))
        if args.github_output is not None:
            with args.github_output.open("a") as output:
                output.write(
                    f"version={metadata.version}\nsdk_version={metadata.sdk_version}\n"
                    f"prerelease={str(metadata.prerelease).lower()}\n"
                    f"latest={str(metadata.latest).lower()}\n"
                )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        BadZipFile,
    ) as exc:
        print(f"Release validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
