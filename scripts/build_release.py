"""Build integration ZIPs that install their pinned SDK from PyPI."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

WORKSPACE = Path(__file__).resolve().parents[1]
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

from scripts.packaging_policy import (  # noqa: E402
    load_policy,
    portable_readme,
    source_bytes,
    validate_build_constraints,
)
from scripts.sdk_package import read_sdk, validate_sdk_artifacts  # noqa: E402

SDK_SOURCE = "PyPI"


def build_release(workspace: Path, wheel: Path, output: Path) -> Path:
    """Keep normal SDK imports and verify the exact published dependency bytes."""
    policy = load_policy(workspace)
    validate_build_constraints(workspace)
    sdk = validate_sdk_artifacts(workspace, wheel.parent)
    if wheel.name != sdk.wheel.filename:
        raise ValueError("The selected SDK wheel differs from the PyPI lock.")
    version = tomllib.loads((workspace / "pyproject.toml").read_text())["project"][
        "version"
    ]
    manifest = json.loads(
        (workspace / "custom_components/ipguard/manifest.json").read_text()
    )
    requirements = [f"ipguard-api=={sdk.version}"]
    if manifest["version"] != version or manifest.get("requirements") != requirements:
        raise ValueError("Integration manifest and package metadata disagree.")
    prefix = "custom_components/ipguard/"
    contents = source_bytes(workspace, policy.component_files)
    if contents[prefix + "api.py"].decode().count("from ipguard_api import ") != 1:
        raise ValueError("Missing independent-library import in the API adapter.")
    contents[prefix + "LICENSE"] = (workspace / "LICENSE").read_bytes()
    contents[prefix + "README.md"] = portable_readme(workspace, version)
    contents.update(
        {
            prefix + name: data
            for name, data in source_bytes(workspace, policy.documents).items()
        }
    )
    provenance = {
        "integration_version": version,
        "sdk_name": "ipguard-api",
        "sdk_version": sdk.version,
        "sdk_wheel_sha256": sdk.wheel.sha256,
        "sdk_sdist_sha256": sdk.sdist.sha256,
        "runtime_requirements": requirements,
        "sdk_source": SDK_SOURCE,
    }
    contents[prefix + "release.json"] = (
        json.dumps(provenance, indent=2) + "\n"
    ).encode()

    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"ipguard-{version}.zip"
    _write_archive(contents, archive)
    # HACS extracts into custom_components/ipguard, so its ZIP must be flat.
    _write_archive(
        {name.removeprefix(prefix): data for name, data in contents.items()},
        output / "ipguard.zip",
    )
    return archive


def _write_archive(contents: dict[str, bytes], archive: Path) -> None:
    temporary = archive.with_suffix(".zip.tmp")
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as release:
            for name, data in sorted(contents.items()):
                entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                release.writestr(entry, data)
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--output", type=Path, default=WORKSPACE / "dist")
    args = parser.parse_args()
    sdk = read_sdk(WORKSPACE)
    wheel = args.wheel or args.output / sdk.wheel.filename
    archive = build_release(WORKSPACE, wheel.resolve(), args.output.resolve())
    print(archive)


if __name__ == "__main__":
    main()
