"""Build a portable custom-integration ZIP from the independent SDK wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from email.parser import BytesParser
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
    validate_wheel_payload,
)

RUNTIME_REQUIREMENTS = ["pyzmq==27.2.0"]
VENDOR_INIT = b'"""Generated release dependencies."""\n'


def build_release(workspace: Path, wheel: Path, output: Path) -> Path:
    """Bundle the SDK without needing its publication or an absolute config path."""
    policy = load_policy(workspace)
    validate_build_constraints(workspace)
    sdk_project = tomllib.loads((workspace / "ipguard-api/pyproject.toml").read_text())
    ha_project = tomllib.loads((workspace / "pyproject.toml").read_text())
    version = ha_project["project"]["version"]
    sdk_version = sdk_project["project"]["version"]
    component = workspace / "custom_components/ipguard"
    manifest = json.loads((component / "manifest.json").read_text())
    if manifest["version"] != version:
        raise ValueError("Integration manifest and package versions disagree.")

    contents: dict[str, bytes] = {}
    prefix = "custom_components/ipguard/"
    contents.update(source_bytes(workspace, policy.component_files))

    adapter_path = prefix + "api.py"
    adapter = contents[adapter_path].decode()
    if adapter.count("from ipguard_api import ") != 1:
        raise ValueError("Missing independent-library import in the API adapter.")
    contents[adapter_path] = adapter.replace(
        "from ipguard_api import ", "from ._vendor.ipguard_api import "
    ).encode()

    with ZipFile(wheel) as sdk_wheel:
        metadata_paths = [
            name
            for name in sdk_wheel.namelist()
            if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_paths) != 1:
            raise ValueError("Expected one SDK distribution in the wheel.")
        metadata = BytesParser().parsebytes(sdk_wheel.read(metadata_paths[0]))
        if metadata["Name"] != "ipguard-api" or metadata["Version"] != sdk_version:
            raise ValueError("The wheel does not match the independent SDK project.")
        requirements = metadata.get_all("Requires-Dist", [])
        if requirements != ["pyzmq<28,>=27.2.0"]:
            raise ValueError(
                "Review bundle dependencies after changing SDK requirements."
            )
    sdk_sources = {
        name.removeprefix("src/"): data
        for name, data in source_bytes(
            workspace / "ipguard-api", policy.sdk_wheel
        ).items()
    }
    sdk_license = (workspace / "ipguard-api/LICENSE").read_bytes()
    package = validate_wheel_payload(
        wheel, "ipguard-api", sdk_version, sdk_sources, sdk_license
    )
    if "ipguard_api/__init__.py" not in package:
        raise ValueError("The SDK wheel has no importable package.")
    contents.update(
        {prefix + "_vendor/" + name: data for name, data in package.items()}
    )
    contents[prefix + "_vendor/__init__.py"] = VENDOR_INIT
    contents[prefix + "_vendor/LICENSE"] = (
        workspace / "ipguard-api/LICENSE"
    ).read_bytes()
    contents[prefix + "LICENSE"] = (workspace / "LICENSE").read_bytes()
    contents[prefix + "README.md"] = portable_readme(workspace, version)
    contents.update(
        {
            prefix + name: data
            for name, data in source_bytes(workspace, policy.documents).items()
        }
    )
    manifest["requirements"] = RUNTIME_REQUIREMENTS
    contents[prefix + "manifest.json"] = (
        json.dumps(manifest, indent=2) + "\n"
    ).encode()
    provenance = {
        "integration_version": version,
        "sdk_name": "ipguard-api",
        "sdk_version": sdk_version,
        "sdk_wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "runtime_requirements": RUNTIME_REQUIREMENTS,
        "sdk_source": "ipguard-api (separate workspace project, Apache-2.0)",
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
    sdk_project = tomllib.loads((WORKSPACE / "ipguard-api/pyproject.toml").read_text())
    sdk_version = sdk_project["project"]["version"]
    wheel = (
        args.wheel or WORKSPACE / "dist" / f"ipguard_api-{sdk_version}-py3-none-any.whl"
    )
    archive = build_release(WORKSPACE, wheel.resolve(), args.output.resolve())
    print(archive)


if __name__ == "__main__":
    main()
