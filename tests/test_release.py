"""Keep every public package constrained to reviewed, credential-free sources."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import shutil
import stat
import tarfile
import tomllib
from pathlib import Path
from types import ModuleType
from zipfile import ZipFile, ZipInfo

import pytest
import tomlkit

from scripts.packaging_policy import (
    load_policy,
    portable_readme,
    read_selection,
    source_bytes,
    validate_build_constraints,
)
from scripts.sdk_package import validate_sdk_artifacts


@pytest.fixture
def builder() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/build_release.py"
    spec = importlib.util.spec_from_file_location("build_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _selection(paths: list[str]) -> str:
    return "only-include = " + json.dumps(paths) + "\n"


def release_fixture(
    tmp_path: Path, version: str = "0.1.0", sdk_version: str = "0.1.0"
) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    sdk = workspace.parent / "sdk-fixture"
    component = "custom_components/ipguard/"
    files = {
        "custom_components/__init__.py": "",
        component + "manifest.json": json.dumps(
            {
                "domain": "ipguard",
                "version": version,
                "requirements": [f"ipguard-api=={sdk_version}"],
            }
        ),
        component + "api.py": "from ipguard_api import IpguardClient\n",
        component + "__init__.py": "from .api import IpguardClient\n",
        component + "button.py": "# button\n",
        component + "config_flow.py": "# config flow\n",
        component + "strings.json": "{}\n",
        component + "translations/en.json": "{}\n",
        component + "brand/icon.png": "synthetic icon",
        "docs/USAGE.md": "Installation instructions\n",
        "LICENSE": "Apache-2.0\n",
        "README.md": "Beta installation documentation\n",
    }
    for name, data in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)
    wheel_files = [name for name in files if name.startswith("custom_components/")]
    sdist_files = [*files, "pyproject.toml"]
    backend = '[build-system]\nrequires=["hatchling==1.32.4"]\nbuild-backend="hatchling.build"\n'
    (workspace / "pyproject.toml").write_text(
        backend + f'[project]\nname="ha-ipguard"\nversion="{version}"\n'
        f'dependencies=["ipguard-api=={sdk_version}"]\n'
        "[tool.hatch.build.targets.wheel]\n"
        + _selection(wheel_files)
        + "[tool.hatch.build.targets.sdist]\n"
        + _selection(sdist_files)
    )
    sdk_files = {
        "src/ipguard_api/__init__.py": "class IpguardClient: pass\n",
        "src/ipguard_api/models.py": "class Session: pass\n",
        "src/ipguard_api/py.typed": "",
        "LICENSE": "Apache-2.0\n",
        "README.md": "API library\n",
        "docs/USAGE.md": "API documentation\n",
        "tests/test_contract.py": "# SDK test\n",
    }
    for name, data in sdk_files.items():
        target = sdk / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)
    sdk_wheel = [name for name in sdk_files if name.startswith("src/")]
    (sdk / "pyproject.toml").write_text(
        backend + f'[project]\nname="ipguard-api"\nversion="{sdk_version}"\n'
        'dependencies=["pyzmq>=27.2.0,<28"]\n'
        "[tool.hatch.build.targets.wheel]\n"
        + _selection(sdk_wheel)
        + 'sources=["src"]\n[tool.hatch.build.targets.sdist]\n'
        + _selection([*sdk_files, "pyproject.toml", "uv.lock", "build-constraints.txt"])
    )
    (workspace / "hacs.json").write_text(
        json.dumps({"zip_release": True, "filename": "ipguard.zip"})
    )
    original = Path(__file__).parents[1]
    shutil.copyfile(
        original / "build-constraints.txt", workspace / "build-constraints.txt"
    )
    shutil.copyfile(original / "uv.lock", workspace / "uv.lock")
    shutil.copyfile(original / "build-constraints.txt", sdk / "build-constraints.txt")
    shutil.copyfile(original / "uv.lock", sdk / "uv.lock")
    output = tmp_path / "distributions"
    build_test_distributions(workspace, output)
    return workspace, output / f"ipguard_api-{sdk_version}-py3-none-any.whl"


def build_test_distributions(workspace: Path, output: Path) -> None:
    policy = load_policy(workspace)
    output.mkdir(parents=True, exist_ok=True)
    for project, name, wheel_selection, sdist_selection in (
        (workspace, "ha-ipguard", policy.integration_wheel, policy.integration_sdist),
        (
            workspace.parent / "sdk-fixture",
            "ipguard-api",
            read_selection(workspace.parent / "sdk-fixture", "wheel"),
            read_selection(workspace.parent / "sdk-fixture", "sdist"),
        ),
    ):
        version = tomllib.loads((project / "pyproject.toml").read_text())["project"][
            "version"
        ]
        stem = name.replace("-", "_") + "-" + version
        metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
        if name == "ipguard-api":
            metadata += "Requires-Dist: pyzmq<28,>=27.2.0\n"
        else:
            dependency = tomllib.loads((project / "pyproject.toml").read_text())[
                "project"
            ]["dependencies"][0]
            metadata += f"Requires-Dist: {dependency}\n"
        wheel_files = {
            path.removeprefix("src/") if name == "ipguard-api" else path: data
            for path, data in source_bytes(project, wheel_selection).items()
        } | {
            stem + ".dist-info/METADATA": metadata.encode(),
            stem + ".dist-info/WHEEL": b"Wheel-Version: 1.0\n",
            stem + ".dist-info/licenses/LICENSE": (project / "LICENSE").read_bytes(),
        }
        record = io.StringIO()
        writer = csv.writer(record)
        for path, data in wheel_files.items():
            digest = (
                base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                .decode()
                .rstrip("=")
            )
            writer.writerow((path, "sha256=" + digest, len(data)))
        writer.writerow((stem + ".dist-info/RECORD", "", ""))
        wheel_files[stem + ".dist-info/RECORD"] = record.getvalue().encode()
        with ZipFile(output / f"{stem}-py3-none-any.whl", "w") as wheel:
            for path, data in wheel_files.items():
                wheel.writestr(path, data)
        with tarfile.open(output / f"{stem}.tar.gz", "w:gz") as sdist:
            payload = source_bytes(project, sdist_selection) | {
                "PKG-INFO": metadata.encode()
            }
            for path, data in payload.items():
                member = tarfile.TarInfo(stem + "/" + path)
                member.size = len(data)
                sdist.addfile(member, io.BytesIO(data))

    pin_sdk_artifacts(workspace, output)


def pin_sdk_artifacts(workspace: Path, output: Path) -> None:
    """Model reviewed registry hashes, never read a real service in tests."""
    version = tomllib.loads((workspace / "pyproject.toml").read_text())["project"][
        "dependencies"
    ][0].split("==")[1]
    lock_path = workspace / "uv.lock"
    lock = tomlkit.parse(lock_path.read_text())
    ha = next(p for p in lock["package"] if p["name"] == "ha-ipguard")
    ha["version"] = tomllib.loads((workspace / "pyproject.toml").read_text())[
        "project"
    ]["version"]
    ha["metadata"]["requires-dist"] = [
        {"name": "ipguard-api", "specifier": f"=={version}"}
    ]
    sdk = next(p for p in lock["package"] if p["name"] == "ipguard-api")
    sdk["version"] = version
    sdk["source"] = {"registry": "https://pypi.org/simple"}

    def record(filename):
        data = (output / filename).read_bytes()
        return {
            "url": f"https://files.pythonhosted.org/packages/synthetic/{filename}",
            "hash": "sha256:" + hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }

    sdk["sdist"] = record(f"ipguard_api-{version}.tar.gz")
    sdk["wheels"] = [record(f"ipguard_api-{version}-py3-none-any.whl")]
    lock_path.write_text(tomlkit.dumps(lock))


def test_release_uses_pypi_sdk_without_bundled_code(
    tmp_path: Path, builder: ModuleType
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    cache = workspace / "custom_components/ipguard/__pycache__"
    cache.mkdir()
    (cache / "old.pyc").write_bytes(b"ignored")
    result = builder.build_release(workspace, wheel, tmp_path / "dist")
    with ZipFile(result) as archive:
        names = archive.namelist()
        prefix = "custom_components/ipguard/"
        manifest = json.loads(archive.read(prefix + "manifest.json"))
        assert manifest["requirements"] == ["ipguard-api==0.1.0"]
        assert (
            archive.read(prefix + "api.py")
            == b"from ipguard_api import IpguardClient\n"
        )
        assert not any("_vendor/" in name for name in names)
        assert all(name.startswith(prefix) for name in names)
        assert not any("__pycache__" in name for name in names)
        metadata = json.loads(archive.read(prefix + "release.json"))
        assert (
            metadata["sdk_wheel_sha256"]
            == hashlib.sha256(wheel.read_bytes()).hexdigest()
        )
        assert metadata["sdk_source"] == builder.SDK_SOURCE
        assert not (workspace / "ipguard-api").exists()
    assert (
        result.with_suffix(".zip.sha256").read_text().split()[0]
        == hashlib.sha256(result.read_bytes()).hexdigest()
    )
    assert (
        "from ipguard_api import"
        in (workspace / "custom_components/ipguard/api.py").read_text()
    )


@pytest.mark.parametrize(
    "name",
    [
        "custom_components/ipguard/session.json",
        "custom_components/ipguard/secrets.yaml",
        "custom_components/ipguard/session.json.secret",
        "custom_components/ipguard/_vendor/ipguard_api/old.py",
        "docs/account.json",
        "docs/private.md",
    ],
)
def test_unapproved_files_fail_before_release(
    tmp_path: Path, builder: ModuleType, name: str
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    stray = workspace / name
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text('{"password":"SYNTHETIC_PRIVATE_VALUE"}')
    with pytest.raises(ValueError, match="unapproved file"):
        builder.build_release(workspace, wheel, tmp_path / "dist")
    assert not (tmp_path / "dist").exists()


def test_source_symlink_fails_before_release(
    tmp_path: Path, builder: ModuleType
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    path = workspace / "custom_components/ipguard/api.py"
    path.unlink()
    path.symlink_to(workspace / "README.md")
    with pytest.raises(ValueError, match="symbolic links"):
        builder.build_release(workspace, wheel, tmp_path / "dist")


def test_missing_source_fails_before_release(
    tmp_path: Path, builder: ModuleType
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    (workspace / "custom_components/ipguard/button.py").unlink()
    with pytest.raises(ValueError, match="missing"):
        builder.build_release(workspace, wheel, tmp_path / "dist")


@pytest.mark.parametrize("member", ["ipguard_api/session.json", "../escaped.py"])
def test_unapproved_wheel_payload_fails(
    tmp_path: Path, builder: ModuleType, member: str
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    with ZipFile(wheel, "a") as archive:
        archive.writestr(member, "SYNTHETIC_PRIVATE_VALUE")
    pin_sdk_artifacts(workspace, wheel.parent)
    with pytest.raises(ValueError, match="allowlist|unsafe"):
        builder.build_release(workspace, wheel, tmp_path / "dist")


def test_wheel_symlink_fails(tmp_path: Path, builder: ModuleType) -> None:
    workspace, wheel = release_fixture(tmp_path)
    member = ZipInfo("ipguard_api/linked.py")
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(wheel, "a") as archive:
        archive.writestr(member, "models.py")
    pin_sdk_artifacts(workspace, wheel.parent)
    with pytest.raises(ValueError, match="invalid members"):
        builder.build_release(workspace, wheel, tmp_path / "dist")


@pytest.mark.parametrize(
    "bad_record",
    [b"", b"../escaped.py,,\n", b"ipguard_api/models.py,sha256=wrong,20\n"],
)
def test_wheel_record_must_match_real_payload(
    tmp_path: Path, builder: ModuleType, bad_record: bytes
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    with ZipFile(wheel) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    files["ipguard_api-0.1.0.dist-info/RECORD"] = bad_record
    with ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    pin_sdk_artifacts(workspace, wheel.parent)
    with pytest.raises(ValueError, match="Wheel RECORD"):
        builder.build_release(workspace, wheel, tmp_path / "dist")


def test_bundle_is_reproducible(tmp_path: Path, builder: ModuleType) -> None:
    workspace, wheel = release_fixture(tmp_path)
    first = builder.build_release(workspace, wheel, tmp_path / "first").read_bytes()
    second = builder.build_release(workspace, wheel, tmp_path / "second").read_bytes()
    assert first == second


def test_explicitly_reviewed_new_module_can_be_released(
    tmp_path: Path, builder: ModuleType
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    module = workspace / "../sdk-fixture/src/ipguard_api/added.py"
    module.write_text("ADDED = True\n")
    project = workspace / "../sdk-fixture/pyproject.toml"
    project.write_text(
        project.read_text().replace(
            '"src/ipguard_api/py.typed"',
            '"src/ipguard_api/added.py", "src/ipguard_api/py.typed"',
        )
    )
    build_test_distributions(workspace, wheel.parent)
    result = builder.build_release(workspace, wheel, tmp_path / "dist")
    with ZipFile(result) as archive:
        assert not any("ipguard_api/" in name for name in archive.namelist())
    with ZipFile(wheel) as sdk_archive:
        assert sdk_archive.read("ipguard_api/added.py") == b"ADDED = True\n"


def test_hacs_zip_extracts_directly_into_integration_directory(
    tmp_path: Path, builder: ModuleType
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    builder.build_release(workspace, wheel, tmp_path / "dist")
    target = tmp_path / "config/custom_components/ipguard"
    with ZipFile(tmp_path / "dist/ipguard.zip") as archive:
        assert "manifest.json" in archive.namelist()
        assert not any(
            name.startswith("custom_components/") for name in archive.namelist()
        )
        archive.extractall(target)
    assert not (target / "_vendor").exists()
    assert json.loads((target / "manifest.json").read_text())["requirements"] == [
        "ipguard-api==0.1.0"
    ]


def test_wrong_sdk_version_fails_before_release(
    tmp_path: Path, builder: ModuleType
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    lock = workspace / "uv.lock"
    data = tomlkit.parse(lock.read_text())
    next(p for p in data["package"] if p["name"] == "ipguard-api")["version"] = "0.2.0"
    lock.write_text(tomlkit.dumps(data))
    with pytest.raises(ValueError, match="metadata|pin"):
        builder.build_release(workspace, wheel, tmp_path / "dist")
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize("project_name", ["integration", "sdk"])
@pytest.mark.parametrize("change", ["hash", "version", "backend", "missing"])
def test_build_constraint_drift_fails(
    tmp_path: Path, change: str, project_name: str
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    project_root = (
        workspace if project_name == "integration" else workspace.parent / "sdk-fixture"
    )
    constraints = project_root / "build-constraints.txt"
    if change == "hash":
        constraints.write_text(
            constraints.read_text().replace("--hash=sha256:", "--hash=sha256:0", 1)
        )
    elif change == "version":
        constraints.write_text(
            constraints.read_text().replace("hatchling==1.32.4", "hatchling==1.32.3")
        )
    elif change == "backend":
        path = project_root / "pyproject.toml"
        path.write_text(
            path.read_text().replace("hatchling==1.32.4", "hatchling>=1.27")
        )
    else:
        constraints.write_text(
            "\n".join(
                line
                for line in constraints.read_text().splitlines()
                if not line.startswith("packaging==")
            )
        )
    if project_name == "sdk":
        build_test_distributions(workspace, wheel.parent)
    with pytest.raises(ValueError, match="Build constraints|build backend"):
        if project_name == "integration":
            validate_build_constraints(workspace)
        else:
            validate_sdk_artifacts(workspace, wheel.parent)


def test_build_does_not_need_or_read_a_sibling_checkout(
    tmp_path: Path, builder: ModuleType
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    shutil.rmtree(workspace.parent / "sdk-fixture")
    sibling = workspace.parent / "ipguard-api"
    sibling.mkdir()
    (sibling / "pyproject.toml").write_text("invalid source must never be read")
    assert builder.build_release(workspace, wheel, tmp_path / "dist").is_file()


def test_each_project_uses_its_own_build_lock(tmp_path: Path) -> None:
    workspace, wheel = release_fixture(tmp_path)
    sdk = workspace.parent / "sdk-fixture"
    lock = sdk / "uv.lock"
    package = next(
        package
        for package in tomllib.loads(lock.read_text())["package"]
        if package["name"] == "hatchling"
    )
    original_hash = package["sdist"]["hash"]
    synthetic_hash = "sha256:" + "0" * 64
    for path in (lock, sdk / "build-constraints.txt"):
        original = path.read_text()
        assert original_hash in original
        path.write_text(original.replace(original_hash, synthetic_hash))
    build_test_distributions(workspace, wheel.parent)
    validate_build_constraints(workspace)
    validate_sdk_artifacts(workspace, wheel.parent)


def test_portable_readme_links_development_guide(tmp_path: Path) -> None:
    workspace, _ = release_fixture(tmp_path)
    project = workspace / "pyproject.toml"
    project.write_text(
        project.read_text()
        + '\n[project.urls]\nRepository = "https://github.com/example/integration"\n'
    )
    (workspace / "README.md").write_text("[Development](DEVELOPMENT.md)\n")
    readme = portable_readme(workspace, "1.2.3").decode()
    assert "https://github.com/example/integration/blob/v1.2.3/DEVELOPMENT.md" in readme


def test_published_source_bytes_must_match_independent_wheel(
    tmp_path: Path, builder: ModuleType
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    original_wheel = wheel.read_bytes()
    (workspace.parent / "sdk-fixture/src/ipguard_api/models.py").write_text(
        "class ChangedSession: pass\n"
    )
    build_test_distributions(workspace, wheel.parent)
    wheel.write_bytes(original_wheel)
    pin_sdk_artifacts(workspace, wheel.parent)
    with pytest.raises(ValueError, match="approved source files"):
        builder.build_release(workspace, wheel, tmp_path / "dist")
    assert not (tmp_path / "dist").exists()


def test_foreign_sdk_source_project_is_rejected(
    tmp_path: Path, builder: ModuleType
) -> None:
    workspace, wheel = release_fixture(tmp_path)
    project = workspace.parent / "sdk-fixture/pyproject.toml"
    project.write_text(
        project.read_text().replace('name="ipguard-api"', 'name="foreign-api"')
    )
    build_test_distributions(workspace, wheel.parent)
    with pytest.raises(ValueError, match="source metadata"):
        builder.build_release(workspace, wheel, tmp_path / "dist")
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize(
    "bad_path", ["../session.json", "*.json", "/tmp/file", "a/../b", "a//b", "a\nb"]
)
def test_allowlist_rejects_unsafe_or_broad_selection(
    tmp_path: Path, bad_path: str
) -> None:
    workspace, _ = release_fixture(tmp_path)
    project = workspace / "pyproject.toml"
    project.write_text(
        project.read_text().replace(
            '"custom_components/__init__.py"', json.dumps(bad_path), 1
        )
    )
    with pytest.raises(ValueError, match="unsafe|globs"):
        load_policy(workspace)
