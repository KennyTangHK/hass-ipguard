"""Fetch and verify the exact SDK distributions recorded in the PyPI lock."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
import time
import tomllib
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from scripts.packaging_policy import (
    read_selection_data,
    sdist_contents,
    validate_build_inputs,
    validate_wheel_payload,
    zip_contents,
)
from scripts.prepare_release import _local_packages, _pin, validate_sdk_source

ROOT = Path(__file__).resolve().parents[1]
SDK_REQUIREMENTS = ["pyzmq<28,>=27.2.0"]


@dataclass(frozen=True)
class Artifact:
    filename: str
    url: str
    sha256: str
    size: int

    def verify(self, path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise ValueError("A locked SDK artifact is missing or not a regular file.")
        if path.stat().st_size != self.size:
            raise ValueError("SDK artifact size differs from the PyPI lock.")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != self.sha256:
            raise ValueError("SDK artifact hash differs from the PyPI lock.")


@dataclass(frozen=True)
class SdkRelease:
    version: str
    wheel: Artifact
    sdist: Artifact


def _artifact(record: object, filename: str) -> Artifact:
    if not isinstance(record, dict):
        raise ValueError("The SDK lock must include its wheel and source archive.")
    url, digest, size = record.get("url"), record.get("hash"), record.get("size")
    if not isinstance(url, str):
        raise ValueError("SDK artifacts must use PyPI file URLs.")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "files.pythonhosted.org"
        or parsed.query
        or parsed.fragment
        or parsed.path.rsplit("/", 1)[-1] != filename
        or not parsed.path.startswith("/packages/")
    ):
        raise ValueError("SDK artifacts must use exact PyPI file URLs.")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
    ):
        raise ValueError("SDK artifacts require locked SHA-256 hashes.")
    if type(size) is not int or not 0 < size <= 32 * 1024 * 1024:
        raise ValueError("SDK artifact size is missing or outside the reviewed limit.")
    return Artifact(filename, url, digest.removeprefix("sha256:"), size)


def read_sdk(root: Path = ROOT) -> SdkRelease:
    project = tomllib.loads((root / "pyproject.toml").read_text())
    validate_sdk_source(project)
    version = _pin(project["project"].get("dependencies"))
    lock = tomllib.loads((root / "uv.lock").read_text())
    sdk = _local_packages(lock)["ipguard-api"]
    if sdk["version"] != version:
        raise ValueError("The PyPI lock must match the SDK dependency pin.")
    wheels = sdk.get("wheels")
    if not isinstance(wheels, list) or len(wheels) != 1:
        raise ValueError("The SDK must have one platform-independent Python wheel.")
    return SdkRelease(
        version,
        _artifact(wheels[0], f"ipguard_api-{version}-py3-none-any.whl"),
        _artifact(sdk.get("sdist"), f"ipguard_api-{version}.tar.gz"),
    )


def _download(artifact: Artifact) -> bytes:
    for attempt in range(3):
        try:
            with urlopen(artifact.url, timeout=30) as response:
                if response.geturl() != artifact.url:
                    raise ValueError(
                        "SDK download redirected away from its locked URL."
                    )
                data = bytes(response.read(artifact.size + 1))
            if (
                len(data) != artifact.size
                or hashlib.sha256(data).hexdigest() != artifact.sha256
            ):
                raise ValueError("Downloaded SDK bytes differ from the PyPI lock.")
            return data
        except HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise ValueError(
                    "The locked SDK artifact could not be downloaded."
                ) from None
        except URLError, TimeoutError:
            if attempt == 2:
                raise ValueError(
                    "The locked SDK artifact could not be downloaded."
                ) from None
        time.sleep(attempt + 1)
    raise AssertionError("Unreachable SDK download state.")


def fetch_sdk(root: Path, output: Path) -> SdkRelease:
    sdk = read_sdk(root)
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("SDK artifact directories cannot use symbolic links.")
    output.mkdir(parents=True, exist_ok=True)
    for artifact in (sdk.wheel, sdk.sdist):
        target = output / artifact.filename
        if target.exists() or target.is_symlink():
            artifact.verify(target)
            continue
        data = _download(artifact)
        with tempfile.NamedTemporaryFile(
            dir=output, prefix=".sdk-", delete=False
        ) as source:
            temporary = Path(source.name)
            try:
                source.write(data)
                source.flush()
                os.fsync(source.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            artifact.verify(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    validate_sdk_artifacts(root, output)
    return sdk


def _metadata(data: bytes, version: str) -> None:
    metadata = BytesParser().parsebytes(data)
    if metadata.get_all("Name") != ["ipguard-api"] or metadata.get_all("Version") != [
        version
    ]:
        raise ValueError("SDK distribution identity differs from its dependency pin.")
    if metadata.get_all("Requires-Dist", []) != SDK_REQUIREMENTS:
        raise ValueError("Review the SDK dependencies before using this release.")


def validate_sdk_artifacts(root: Path, directory: Path) -> SdkRelease:
    """Verify registry hashes, exact allowlists and independent SDK source bytes."""
    sdk = read_sdk(root)
    wheel, sdist = directory / sdk.wheel.filename, directory / sdk.sdist.filename
    sdk.wheel.verify(wheel)
    sdk.sdist.verify(sdist)
    files = sdist_contents(sdist, f"ipguard_api-{sdk.version}/")
    project: dict[str, Any] = tomllib.loads(files["pyproject.toml"].decode())
    if (
        project["project"]["name"] != "ipguard-api"
        or project["project"]["version"] != sdk.version
    ):
        raise ValueError("The SDK source metadata differs from the PyPI lock.")
    selected = read_selection_data(project, "sdist")
    wheel_selected = read_selection_data(project, "wheel")
    if set(files) != set(selected) | {"PKG-INFO"}:
        raise ValueError("SDK source archive differs from its explicit file allowlist.")
    if (
        not set(wheel_selected).issubset(selected)
        or "src/ipguard_api/__init__.py" not in wheel_selected
        or any(not name.startswith("src/ipguard_api/") for name in wheel_selected)
    ):
        raise ValueError(
            "The SDK wheel must contain only its complete reviewed package."
        )
    validate_build_inputs(
        project,
        tomllib.loads(files["uv.lock"].decode()),
        files["build-constraints.txt"].decode(),
    )
    validate_wheel_payload(
        wheel,
        "ipguard-api",
        sdk.version,
        {name.removeprefix("src/"): files[name] for name in wheel_selected},
        files["LICENSE"],
    )
    _metadata(files["PKG-INFO"], sdk.version)
    _metadata(
        zip_contents(wheel)[f"ipguard_api-{sdk.version}.dist-info/METADATA"],
        sdk.version,
    )
    return sdk


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        sdk = fetch_sdk(ROOT, args.output)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"SDK artifact validation failed: {error}\n")
    print(f"Verified PyPI SDK {sdk.version} wheel and source archive.")


if __name__ == "__main__":
    main()
