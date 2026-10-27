"""The integration consumes only reviewed PyPI artifacts, including offline builds."""

import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
import tomlkit
from test_release import release_fixture

from scripts import sdk_package


@pytest.fixture
def published(tmp_path):
    root, wheel = release_fixture(tmp_path)
    return root, wheel.parent


def test_fetch_uses_only_locked_urls_and_reuses_verified_cache(
    published, monkeypatch, tmp_path
):
    root, existing = published
    sdk = sdk_package.read_sdk(root)
    calls = []

    class Response(io.BytesIO):
        def __init__(self, url):
            self.url = url
            super().__init__((existing / url.rsplit("/", 1)[-1]).read_bytes())

        def geturl(self):
            return self.url

    def download(url, timeout):
        assert timeout == 30
        calls.append(url)
        return Response(url)

    monkeypatch.setattr(sdk_package, "urlopen", download)
    out = tmp_path / "downloaded"
    assert sdk_package.fetch_sdk(root, out) == sdk
    assert calls == [sdk.wheel.url, sdk.sdist.url]
    sdk_package.fetch_sdk(root, out)
    assert len(calls) == 2
    assert sorted(path.name for path in out.iterdir()) == sorted(
        [sdk.wheel.filename, sdk.sdist.filename]
    )


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_tampered_registry_artifact_fails_before_parsing_or_network(
    published, monkeypatch, artifact
):
    root, directory = published
    sdk = sdk_package.read_sdk(root)
    selected = getattr(sdk, artifact)
    path = directory / selected.filename
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    monkeypatch.setattr(
        sdk_package,
        "urlopen",
        lambda *args, **kwargs: pytest.fail(
            "Corrupt cached bytes must fail, not be silently replaced"
        ),
    )
    with pytest.raises(ValueError, match="hash"):
        sdk_package.validate_sdk_artifacts(root, directory)
    with pytest.raises(ValueError, match="hash"):
        sdk_package.fetch_sdk(root, directory)


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://example.invalid/ipguard_api-0.1.0.tar.gz"),
        ("url", "http://files.pythonhosted.org/packages/ipguard_api-0.1.0.tar.gz"),
        (
            "url",
            "https://user:password@files.pythonhosted.org/packages/ipguard_api-0.1.0.tar.gz",
        ),
        ("url", "https://files.pythonhosted.org/packages/ipguard_api-9.0.0.tar.gz"),
        ("hash", "sha256:short"),
        ("hash", "md5:" + "a" * 32),
        ("size", 0),
        ("size", True),
        ("size", 100 * 1024 * 1024),
    ],
)
def test_sdk_lock_rejects_untrusted_or_unverifiable_artifacts(published, field, value):
    root, _ = published
    path = root / "uv.lock"
    lock = tomlkit.parse(path.read_text())
    sdk = next(
        package for package in lock["package"] if package["name"] == "ipguard-api"
    )
    sdk["sdist"][field] = value
    path.write_text(tomlkit.dumps(lock))
    with pytest.raises(ValueError):
        sdk_package.read_sdk(root)


def test_runtime_and_registry_pins_are_authoritative(published):
    root, _ = published
    path = root / "pyproject.toml"
    path.write_text(
        path.read_text()
        + '\n[tool.uv.sources]\nipguard-api={path="../ipguard-api",editable=true}\n'
    )
    with pytest.raises(ValueError, match="source overrides"):
        sdk_package.read_sdk(root)


@pytest.mark.parametrize("mode", ["redirect", "short", "changed"])
def test_bad_download_never_creates_a_release_file(
    published, monkeypatch, tmp_path, mode
):
    root, existing = published
    sdk = sdk_package.read_sdk(root)
    data = (existing / sdk.wheel.filename).read_bytes()

    class Response(io.BytesIO):
        def geturl(self):
            return (
                "https://example.invalid/file" if mode == "redirect" else sdk.wheel.url
            )

    if mode == "short":
        data = data[:-1]
    elif mode == "changed":
        data = bytes([data[0] ^ 1]) + data[1:]
    monkeypatch.setattr(sdk_package, "urlopen", lambda *args, **kwargs: Response(data))
    out = tmp_path / "downloaded"
    with pytest.raises(ValueError, match="redirected|Downloaded SDK bytes"):
        sdk_package.fetch_sdk(root, out)
    assert not list(out.iterdir())


def test_download_retry_is_bounded_and_does_not_write_partial_files(
    published, monkeypatch, tmp_path
):
    root, _ = published
    calls = []

    def fail(url, timeout):
        calls.append(url)
        raise HTTPError(url, 503, "synthetic failure", {}, None)

    monkeypatch.setattr(sdk_package, "urlopen", fail)
    sleeps = []
    monkeypatch.setattr(sdk_package.time, "sleep", sleeps.append)
    out = tmp_path / "downloaded"
    with pytest.raises(ValueError, match="could not be downloaded"):
        sdk_package.fetch_sdk(root, out)
    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert not list(out.iterdir())


def test_installed_sdk_is_a_registry_package():
    from importlib.metadata import distribution

    import ipguard_api

    installed = distribution("ipguard-api")
    root = Path(__file__).parents[1]
    sdk = sdk_package.read_sdk(root)
    assert installed.version == sdk.version
    assert (
        Path(ipguard_api.__file__).resolve()
        == Path(installed.locate_file("ipguard_api/__init__.py")).resolve()
    )
    direct_url = installed.read_text("direct_url.json")
    assert not direct_url or not json.loads(direct_url).get("dir_info", {}).get(
        "editable"
    )
