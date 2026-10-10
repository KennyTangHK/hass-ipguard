"""Exercise publication and its failure paths without accessing GitHub."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import release_publish
from scripts.release_metadata import ReleaseMetadata
from scripts.release_publish import (
    Asset,
    GitHub,
    expected_assets,
    publish,
    validate_commit,
    validate_remote_assets,
)

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
ANNOTATED = "c" * 40
TAG = "v0.1.0b2"
REPO = "owner/repository"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(release_publish, "sleep", lambda _: None)


@pytest.fixture
def assets(tmp_path: Path) -> dict[str, Asset]:
    metadata = ReleaseMetadata("0.1.0b2", "0.1.0b2")
    result = {}
    for name in metadata.publication_assets:
        path = tmp_path / name
        data = name.encode()
        path.write_bytes(data)
        result[name] = Asset(
            path, len(data), f"sha256:{hashlib.sha256(data).hexdigest()}"
        )
    return result


@pytest.fixture
def publisher_root(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "ipguard-api").mkdir(parents=True)
    (root / "custom_components/ipguard").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ha-ipguard"\nversion = "0.1.0b1"\n'
        'dependencies = ["ipguard-api==0.1.0b1"]\n'
    )
    (root / "ipguard-api/pyproject.toml").write_text(
        '[project]\nname = "ipguard-api"\nversion = "0.1.0b3"\n'
    )
    (root / "custom_components/ipguard/manifest.json").write_text(
        json.dumps(
            {
                "domain": "ipguard",
                "version": "0.1.0b1",
                "requirements": ["ipguard-api==0.1.0b1"],
            }
        )
        + "\n"
    )
    (root / "hacs.json").write_text(
        json.dumps({"filename": "ipguard.zip", "zip_release": True}) + "\n"
    )
    (root / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "ha-ipguard"\n'
        'version = "0.1.0b1"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "ipguard-api" }]\n\n'
        "[package.metadata]\n"
        'requires-dist = [{ name = "ipguard-api", editable = "ipguard-api" }]\n\n'
        '[[package]]\nname = "ipguard-api"\nversion = "0.1.0b1"\n'
        'source = { editable = "ipguard-api" }\n'
    )
    return root


def invoke_main(monkeypatch, root, *, tag=TAG, event=COMMIT, tested=COMMIT):
    monkeypatch.setattr(release_publish, "ROOT", root)
    monkeypatch.setattr(
        release_publish.sys,
        "argv",
        [
            "release_publish.py",
            "--tag",
            tag,
            "--repo",
            REPO,
            "--event-sha",
            event,
            "--tested-sha",
            tested,
            "--dist",
            str(root / "dist"),
            "--notes-file",
            str(root / "release-notes.md"),
        ],
    )
    release_publish.main()


def remote(assets: dict[str, Asset]) -> list[dict[str, object]]:
    return [
        {
            "id": index,
            "name": name,
            "state": "uploaded",
            "size": file.size,
            "digest": file.digest,
        }
        for index, (name, file) in enumerate(assets.items(), start=1)
    ]


def response(
    code: int, value: object = None, *, headers: str = ""
) -> subprocess.CompletedProcess[str]:
    body = "" if code == 204 else json.dumps(value)
    return subprocess.CompletedProcess(
        [],
        0 if 200 <= code < 300 else 1,
        f"HTTP/2.0 {code} Result\nContent-Type: application/json\n{headers}\n{body}",
        "",
    )


def graphql_payload(release: object) -> dict[str, object]:
    return {"data": {"repository": {"release": release}}}


def method(arguments: list[str]) -> str:
    assert arguments[:2] == ["gh", "api"], "Publication must use the authenticated API"
    return arguments[arguments.index("--method") + 1]


def endpoint(arguments: list[str]) -> str:
    return arguments[-1]


def fields(arguments: list[str]) -> dict[str, str]:
    return {
        arguments[index + 1].split("=", 1)[0]: arguments[index + 1].split("=", 1)[1]
        for index, value in enumerate(arguments[:-1])
        if value in {"-f", "-F", "--field", "--raw-field"}
    }


def writes(fake: FakeGh) -> list[list[str]]:
    return [
        call
        for call in fake.calls
        if method(call) != "GET" and endpoint(call) != "graphql"
    ]


class FakeGh:
    """An in-memory release with individually addressed release and asset APIs."""

    def __init__(
        self, assets: dict[str, Asset], *, existing: bool = True, tag: str = TAG
    ) -> None:
        self.calls: list[list[str]] = []
        self.tag = tag
        self.existing, self.draft, self.uploaded = existing, True, False
        self.before: list[dict[str, object]] = remote(assets)[:2] if existing else []
        self.after: list[dict[str, object]] = remote(assets)
        self.commits = [COMMIT, COMMIT, COMMIT, COMMIT]
        self.annotated = False
        self.fail_upload = False
        self.api_status = 200

    def release(self) -> dict[str, object]:
        return {
            "id": 7,
            "tag_name": self.tag,
            "draft": self.draft,
            "prerelease": "b" in self.tag,
        }

    def __call__(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        verb, target = method(arguments), endpoint(arguments)
        if self.api_status != 200:
            return response(self.api_status, {"message": "Synthetic failure"})
        if target == "graphql":
            assert verb == "POST"
            item = (
                {"databaseId": 7, "tagName": self.tag, "isDraft": self.draft}
                if self.existing
                else None
            )
            return response(200, graphql_payload(item))
        if target.startswith("https://uploads.github.com/"):
            assert verb == "POST"
            assert urlsplit(target).path == f"/repos/{REPO}/releases/7/assets"
            name = parse_qs(urlsplit(target).query)["name"][0]
            assert "--input" in arguments
            assert Path(arguments[arguments.index("--input") + 1]).name == name
            if self.fail_upload:
                return response(403, {"message": "credential body secret token"})
            item = next(item for item in self.after if item["name"] == name)
            # A successful upload response includes its server-side digest.
            uploaded = dict(item, id=100 + len(self.before))
            self.before.append(uploaded)
            self.uploaded = True
            return response(201, uploaded)
        assert target.startswith(f"repos/{REPO}/")
        target = target.removeprefix(f"repos/{REPO}/")
        if verb == "GET":
            if target == f"git/ref/tags/{self.tag}":
                target_object = (
                    {"type": "tag", "sha": ANNOTATED}
                    if self.annotated
                    else {"type": "commit", "sha": self.commits.pop(0)}
                )
                return response(
                    200, {"ref": f"refs/tags/{self.tag}", "object": target_object}
                )
            if target == f"git/tags/{ANNOTATED}":
                return response(
                    200,
                    {
                        "sha": ANNOTATED,
                        "object": {"type": "commit", "sha": self.commits.pop(0)},
                    },
                )
            if target == "releases?per_page=100&page=1":
                return response(200, [self.release()] if self.existing else [])
            if target == "releases/7":
                return (
                    response(200, self.release())
                    if self.existing
                    else response(404, {})
                )
            if target.startswith("releases/7/assets?"):
                complete = {item["name"] for item in self.after}.issubset(
                    {item["name"] for item in self.before}
                )
                return response(
                    200, self.after if self.uploaded and complete else self.before
                )
        elif verb == "POST" and target == "releases":
            self.existing = True
            return response(201, self.release())
        elif verb == "DELETE" and target.startswith("releases/assets/"):
            identity = int(target.rsplit("/", 1)[1])
            self.before[:] = [item for item in self.before if item["id"] != identity]
            return response(204)
        elif verb == "PATCH" and target == "releases/7":
            assert fields(arguments)["draft"] == "false"
            self.draft = False
            return response(200, self.release())
        raise AssertionError(f"Unexpected API operation: {verb} {target}")


def publish_fake(fake, assets, tmp_path, *, tag=TAG, metadata=None, runner=None):
    publish(
        GitHub(REPO, runner or fake),
        metadata or ReleaseMetadata("0.1.0b2", "0.1.0b2"),
        tag,
        assets,
        tmp_path / "notes.md",
        COMMIT,
    )


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("annotated", [True, False])
def test_publish_new_or_partial_draft_after_complete_verification(
    assets, tmp_path, existing, annotated
):
    fake = FakeGh(assets, existing=existing)
    fake.annotated = annotated
    publish_fake(fake, assets, tmp_path)
    assert not fake.draft
    mutations = writes(fake)
    assert sum(
        method(call) == "POST" and endpoint(call).endswith("/releases")
        for call in mutations
    ) == (0 if existing else 1)
    assert all(method(call) == "POST" for call in mutations[:-1])
    assert method(mutations[-1]) == "PATCH"
    assert fields(mutations[-1])["prerelease"] == "true"
    assert fields(mutations[-1])["make_latest"] == "false"
    assert endpoint(mutations[-1]) == f"repos/{REPO}/releases/7"
    assert sum("git/ref/tags/" in endpoint(call) for call in fake.calls) == 3
    assert not any("releases/tags/" in endpoint(call) for call in fake.calls)
    assert all(call[:2] == ["gh", "api"] for call in fake.calls)
    assert sum(endpoint(call) == "graphql" for call in fake.calls) == (
        0 if existing else 1
    )


def test_stable_publication_uses_github_latest_selection(assets, tmp_path):
    metadata = ReleaseMetadata("0.1.0", "0.1.0b2")
    stable_assets = {
        name: Asset(tmp_path / name, 10, "sha256:" + "0" * 64)
        for name in metadata.publication_assets
    }
    fake = FakeGh(stable_assets, tag="v0.1.0")
    publish_fake(fake, stable_assets, tmp_path, tag="v0.1.0", metadata=metadata)
    assert fields(writes(fake)[-1])["prerelease"] == "false"
    assert fields(writes(fake)[-1])["make_latest"] == "legacy"


@pytest.mark.parametrize(
    "mutation", ["extra", "duplicate", "duplicate_id", "published"]
)
def test_invalid_existing_draft_never_uploads_or_publishes(assets, tmp_path, mutation):
    fake = FakeGh(assets)
    if mutation == "extra":
        fake.before.append({"id": 99, "name": "credentials.json"})
    elif mutation == "duplicate":
        fake.before.append(dict(fake.before[0], id=99))
    elif mutation == "duplicate_id":
        fake.before[1]["id"] = fake.before[0]["id"]
    else:
        fake.draft = False
    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path)
    assert not writes(fake)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "missing",
        "state",
        "size",
        "bool_size",
        "digest",
        "null_digest",
        "missing_digest",
        "duplicate",
    ],
)
def test_uploaded_asset_mismatch_keeps_draft(assets, tmp_path, mutation):
    fake = FakeGh(assets)
    altered = [dict(item) for item in fake.after]
    if mutation == "extra":
        altered.append({"id": 99, "name": "session.json"})
    elif mutation == "missing":
        altered.pop()
    elif mutation == "duplicate":
        altered.append(dict(altered[0], id=99))
    elif mutation == "missing_digest":
        altered[0].pop("digest")
    else:
        field, value = {
            "state": ("state", "starter"),
            "size": ("size", 0),
            "bool_size": ("size", True),
            "digest": ("digest", "sha256:" + "0" * 64),
            "null_digest": ("digest", None),
        }[mutation]
        altered[0][field] = value

    def runner(arguments):
        result = fake(arguments)
        if (
            method(arguments) == "GET"
            and fake.uploaded
            and len(fake.before) == len(assets)
            and "/assets?" in endpoint(arguments)
        ):
            return response(200, altered)
        return result

    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert fake.draft
    assert not any(method(call) == "PATCH" for call in fake.calls)
    assert not any(method(call) == "DELETE" for call in fake.calls)


@pytest.mark.parametrize("changed_at", [0, 1, 2])
def test_tag_change_stops_before_next_write(assets, tmp_path, changed_at):
    fake = FakeGh(assets)
    fake.commits[changed_at] = OTHER_COMMIT
    with pytest.raises(ValueError, match="tag"):
        publish_fake(fake, assets, tmp_path)
    assert fake.draft
    assert fake.uploaded is (changed_at == 2)


def test_upload_failure_does_not_log_response_or_publish(assets, tmp_path, capsys):
    fake = FakeGh(assets)
    fake.fail_upload = True
    with pytest.raises(ValueError) as error:
        publish_fake(fake, assets, tmp_path)
    assert "secret" not in str(error.value) and "credential" not in str(error.value)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert fake.draft
    assert not any(method(call) == "PATCH" for call in fake.calls)


@pytest.mark.parametrize("code", [401, 403, 500])
def test_api_failure_is_not_treated_as_a_missing_draft(assets, tmp_path, code):
    fake = FakeGh(assets, existing=False)
    fake.api_status = code
    with pytest.raises(ValueError, match="API request failed"):
        publish_fake(fake, assets, tmp_path)
    assert not writes(fake)


@pytest.mark.parametrize(
    "mutation", ["published", "missing", "different_id", "different_tag"]
)
def test_draft_replacement_after_upload_stops_publication(assets, tmp_path, mutation):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        if (
            method(arguments) == "GET"
            and fake.uploaded
            and endpoint(arguments).endswith("releases/7")
        ):
            if mutation == "missing":
                return response(404, {})
            return response(
                200,
                {
                    "id": 8 if mutation == "different_id" else 7,
                    "tag_name": "v0.1.0b9" if mutation == "different_tag" else TAG,
                    "draft": mutation != "published",
                },
            )
        return result

    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert not any(method(call) == "PATCH" for call in fake.calls)


def test_created_draft_can_stay_absent_from_release_list(assets, tmp_path):
    fake = FakeGh(assets, existing=False)

    def runner(arguments):
        result = fake(arguments)
        if endpoint(arguments).endswith("releases?per_page=100&page=1"):
            return response(200, [])
        return result

    publish_fake(fake, assets, tmp_path, runner=runner)
    assert not fake.draft
    assert (
        sum(
            method(call) == "POST" and endpoint(call).endswith("/releases")
            for call in fake.calls
        )
        == 1
    )
    create_index = next(
        index
        for index, call in enumerate(fake.calls)
        if method(call) == "POST" and endpoint(call).endswith("/releases")
    )
    first_upload_index = next(
        index
        for index, call in enumerate(fake.calls)
        if endpoint(call).startswith("https://uploads.github.com/")
    )
    assert not any(
        "releases?" in endpoint(call)
        for call in fake.calls[create_index + 1 : first_upload_index]
    )


def test_wrong_publication_tag_or_asset_set_has_no_remote_calls(assets, tmp_path):
    fake = FakeGh(assets)
    for tag, files in (("v0.1.0b3", assets), (TAG, {})):
        with pytest.raises(ValueError, match="does not match the metadata"):
            publish(
                GitHub("owner/repository", fake),
                ReleaseMetadata("0.1.0b2", "0.1.0b2"),
                tag,
                files,
                tmp_path / "notes.md",
                COMMIT,
            )
    assert not fake.calls


def release_item(identity: int, tag: str, *, draft: bool = True) -> dict[str, object]:
    return {"id": identity, "tag_name": tag, "draft": draft}


def test_authenticated_release_list_finds_exact_draft_across_pages():
    calls = []
    first = [release_item(index, f"other-{index}") for index in range(1, 101)]
    first[0]["tag_name"] = f"{TAG}0"
    first[1]["tag_name"] = f"prefix-{TAG}"

    def runner(arguments):
        calls.append(arguments)
        return response(
            200,
            first if arguments[-1].endswith("&page=1") else [release_item(101, TAG)],
        )

    assert GitHub("owner/repository", runner).draft(TAG) == 101
    assert [call[-1] for call in calls] == [
        "repos/owner/repository/releases?per_page=100&page=1",
        "repos/owner/repository/releases?per_page=100&page=2",
    ]


def test_empty_release_list_means_no_existing_draft():
    def runner(arguments):
        return response(200, [])

    assert GitHub("owner/repository", runner).draft(TAG) is None


@pytest.mark.parametrize("across_pages", [False, True])
def test_duplicate_release_tag_is_rejected_even_after_a_match(across_pages):
    calls = []
    first = [release_item(1, TAG)]
    if across_pages:
        first.extend(release_item(index, f"other-{index}") for index in range(2, 101))
    else:
        first.append(release_item(101, TAG))

    def runner(arguments):
        calls.append(arguments)
        return response(
            200,
            first if arguments[-1].endswith("&page=1") else [release_item(101, TAG)],
        )

    with pytest.raises(ValueError, match="Multiple releases"):
        GitHub("owner/repository", runner).draft(TAG)
    assert len(calls) == (2 if across_pages else 1)


def test_duplicate_release_identity_across_pages_is_rejected():
    first = [release_item(index, f"other-{index}") for index in range(1, 101)]

    def runner(arguments):
        return response(
            200, first if arguments[-1].endswith("&page=1") else [release_item(1, TAG)]
        )

    with pytest.raises(ValueError, match="duplicate"):
        GitHub("owner/repository", runner).draft(TAG)


@pytest.mark.parametrize(
    "items",
    [
        {},
        [release_item(index, f"other-{index}") for index in range(1, 102)],
        [None],
        [[]],
        [{}],
        [{"id": 7, "tag_name": TAG}],
        [{"id": 7, "draft": True}],
        [release_item(7, "")],
        [{"id": 7, "tag_name": 1, "draft": True}],
        [{"id": 7, "tag_name": TAG, "draft": 1}],
        [{"id": 7, "tag_name": TAG, "draft": "true"}],
        [{"id": True, "tag_name": TAG, "draft": True}],
        [release_item(0, TAG)],
        [release_item(-1, TAG)],
        [{"id": "7", "tag_name": TAG, "draft": True}],
        [{"id": 7.0, "tag_name": TAG, "draft": True}],
    ],
)
def test_malformed_release_list_or_metadata_is_rejected(items):
    def runner(arguments):
        return response(200, items)

    with pytest.raises(ValueError, match="malformed"):
        GitHub("owner/repository", runner).draft(TAG)


def test_malformed_release_after_an_earlier_matching_draft_is_rejected():
    first = [release_item(1, TAG)]
    first.extend(release_item(index, f"other-{index}") for index in range(2, 101))

    def runner(arguments):
        return response(200, first if arguments[-1].endswith("&page=1") else [{}])

    with pytest.raises(ValueError, match="malformed"):
        GitHub("owner/repository", runner).draft(TAG)


def test_release_list_pagination_is_bounded():
    calls = []

    def runner(arguments):
        calls.append(arguments)
        page = int(arguments[-1].rsplit("=", 1)[1])
        return response(
            200,
            [
                release_item(identity, f"other-{identity}")
                for identity in range((page - 1) * 100 + 1, page * 100 + 1)
            ],
        )

    with pytest.raises(ValueError, match="exceeded its limit"):
        GitHub("owner/repository", runner).draft(TAG)
    assert len(calls) == 100


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_release_list_failure_is_not_treated_as_a_missing_draft(code):
    def runner(arguments):
        return response(code, {"message": "Synthetic failure"})

    with pytest.raises(ValueError, match="API request failed"):
        GitHub("owner/repository", runner).draft(TAG)


def test_asset_list_reads_all_pages():
    calls = []

    def runner(arguments):
        calls.append(arguments)
        return response(200, [{}] * (100 if arguments[-1].endswith("&page=1") else 2))

    assert len(GitHub("owner/repository", runner).assets(7)) == 102
    assert len(calls) == 2 and calls[-1][-1].endswith("page=2")


@pytest.mark.parametrize("items", [{}, [{}] * 101])
def test_malformed_asset_page_stops_publication(items):
    def runner(arguments):
        return response(200, items)

    with pytest.raises(ValueError, match="malformed asset pagination"):
        GitHub("owner/repository", runner).assets(7)


def test_asset_pagination_is_bounded():
    def runner(arguments):
        return response(200, [{}] * 100)

    with pytest.raises(ValueError, match="exceeded its limit"):
        GitHub("owner/repository", runner).assets(7)


@pytest.mark.parametrize(
    "target", [{"type": "tree", "sha": COMMIT}, {"type": "commit", "sha": "bad"}, None]
)
def test_remote_tag_must_resolve_to_a_commit(target):
    def runner(arguments):
        return response(200, {"ref": f"refs/tags/{TAG}", "object": target})

    with pytest.raises(ValueError, match="resolved to a commit"):
        GitHub("owner/repository", runner).tag_commit(TAG)


def test_annotated_tag_cycle_is_rejected():
    def runner(arguments):
        target = {"type": "tag", "sha": ANNOTATED}
        return response(
            200, {"ref": f"refs/tags/{TAG}", "object": target, "sha": ANNOTATED}
        )

    with pytest.raises(ValueError, match="resolved to a commit"):
        GitHub("owner/repository", runner).tag_commit(TAG)


@pytest.mark.parametrize(
    "event,tested", [(OTHER_COMMIT, COMMIT), (COMMIT, OTHER_COMMIT), ("bad", COMMIT)]
)
def test_checkout_event_and_tested_commit_must_match(event, tested):
    with pytest.raises(ValueError):
        validate_commit(COMMIT, event, tested)
    assert validate_commit(COMMIT, COMMIT, COMMIT) == COMMIT


def test_main_prepares_tag_and_sdk_pins_before_validating_assets(
    publisher_root, monkeypatch
):
    metadata = ReleaseMetadata("0.1.0b2", "0.1.0b3")
    assets = {
        name: Asset(publisher_root / "dist" / name, 10, "sha256:" + "0" * 64)
        for name in metadata.publication_assets
    }
    fake = FakeGh(assets)
    stages = []
    prepare_release = release_publish.prepare_release
    read_metadata = release_publish.read_metadata

    def run(arguments):
        assert arguments == [
            "git",
            "-C",
            str(publisher_root),
            "rev-parse",
            "HEAD",
        ]
        stages.append("commit")
        return subprocess.CompletedProcess(arguments, 0, COMMIT + "\n", "")

    def prepare(root, tag):
        stages.append("prepare")
        prepare_release(root, tag)

    def read(root, tag):
        stages.append("metadata")
        return read_metadata(root, tag)

    def validate(dist, prepared):
        stages.append("assets")
        assert dist == publisher_root / "dist" and prepared == metadata
        assert read_metadata(publisher_root, TAG) == metadata
        assert 'version = "0.1.0b2"' in (publisher_root / "uv.lock").read_text()
        assert 'version = "0.1.0b3"' in (publisher_root / "uv.lock").read_text()
        return assets

    def github(repository):
        stages.append("github")
        return GitHub(repository, fake)

    monkeypatch.setattr(release_publish, "run_command", run)
    monkeypatch.setattr(release_publish, "prepare_release", prepare)
    monkeypatch.setattr(release_publish, "read_metadata", read)
    monkeypatch.setattr(release_publish, "expected_assets", validate)
    monkeypatch.setattr(release_publish, "GitHub", github)
    invoke_main(monkeypatch, publisher_root)
    assert stages == ["commit", "prepare", "metadata", "assets", "github"]
    assert not fake.draft
    assert (publisher_root / "release-notes.md").read_text() == (
        release_publish.release_notes(metadata)
    )


@pytest.mark.parametrize(
    "event,tested", [(OTHER_COMMIT, COMMIT), (COMMIT, OTHER_COMMIT), ("bad", COMMIT)]
)
def test_main_commit_mismatch_cannot_prepare_or_access_github(
    publisher_root, monkeypatch, event, tested
):
    calls = []
    before = {
        path: path.read_bytes() for path in publisher_root.rglob("*") if path.is_file()
    }

    def run(arguments):
        calls.append(arguments)
        assert arguments[:2] == ["git", "-C"]
        return subprocess.CompletedProcess(arguments, 0, COMMIT + "\n", "")

    def unexpected(*args):
        pytest.fail("Commit mismatch must stop before preparation or GitHub access")

    monkeypatch.setattr(release_publish, "run_command", run)
    monkeypatch.setattr(release_publish, "prepare_release", unexpected)
    monkeypatch.setattr(release_publish, "GitHub", unexpected)
    with pytest.raises(SystemExit) as failure:
        invoke_main(monkeypatch, publisher_root, event=event, tested=tested)
    assert failure.value.code == 1
    assert len(calls) == 1
    assert all(path.read_bytes() == contents for path, contents in before.items())
    assert not (publisher_root / "release-notes.md").exists()


@pytest.mark.parametrize("tag", ["v0.1.0b0", "v01.1.0", "v0.1.0b2\nprivate-text"])
def test_main_invalid_tag_cannot_modify_metadata_or_access_github(
    publisher_root, monkeypatch, tag
):
    calls = []
    before = {
        path: path.read_bytes() for path in publisher_root.rglob("*") if path.is_file()
    }

    def run(arguments):
        calls.append(arguments)
        assert arguments[:2] == ["git", "-C"]
        return subprocess.CompletedProcess(arguments, 0, COMMIT + "\n", "")

    def unexpected(*args):
        pytest.fail("Invalid tag must stop before asset validation or GitHub access")

    monkeypatch.setattr(release_publish, "run_command", run)
    monkeypatch.setattr(release_publish, "expected_assets", unexpected)
    monkeypatch.setattr(release_publish, "GitHub", unexpected)
    with pytest.raises(SystemExit) as failure:
        invoke_main(monkeypatch, publisher_root, tag=tag)
    assert failure.value.code == 1
    assert len(calls) == 1
    assert all(path.read_bytes() == contents for path, contents in before.items())
    assert not (publisher_root / "release-notes.md").exists()


def test_main_asset_validation_failure_cannot_access_github(
    publisher_root, monkeypatch
):
    def run(arguments):
        assert arguments[:2] == ["git", "-C"]
        return subprocess.CompletedProcess(arguments, 0, COMMIT + "\n", "")

    def invalid_assets(dist, metadata):
        assert metadata == ReleaseMetadata("0.1.0b2", "0.1.0b3")
        raise ValueError("Synthetic invalid release assets.")

    def unexpected(*args):
        pytest.fail("Invalid assets must stop before GitHub access")

    monkeypatch.setattr(release_publish, "run_command", run)
    monkeypatch.setattr(release_publish, "expected_assets", invalid_assets)
    monkeypatch.setattr(release_publish, "GitHub", unexpected)
    with pytest.raises(SystemExit) as failure:
        invoke_main(monkeypatch, publisher_root)
    assert failure.value.code == 1
    assert not (publisher_root / "release-notes.md").exists()


def test_expected_publication_files_have_digests_and_required_checksums(
    assets, tmp_path, monkeypatch
):
    metadata = ReleaseMetadata("0.1.0b2", "0.1.0b2")
    archives = tuple(assets[name].path for name in metadata.assets)
    monkeypatch.setattr(release_publish, "validate_assets", lambda *args: archives)
    (tmp_path / "SHA256SUMS").write_text(release_publish.checksum_text(archives))
    expected = expected_assets(tmp_path, metadata)
    assert len(expected) == 9 and all(
        file.digest.startswith("sha256:") for file in expected.values()
    )
    (tmp_path / "ipguard.zip.sha256").unlink()
    with pytest.raises(ValueError, match="publication asset"):
        expected_assets(tmp_path, metadata)


def test_partial_draft_may_be_empty_but_complete_assets_are_required(assets):
    validate_remote_assets([], assets, complete=False)
    with pytest.raises(ValueError, match="missing required"):
        validate_remote_assets([], assets, complete=True)


def test_malformed_http_and_json_fail_closed():
    for stdout in ("not an HTTP response", "HTTP/2.0 200 OK\n\nnot JSON"):

        def runner(arguments, stdout=stdout):
            return subprocess.CompletedProcess([], 0, stdout, "")

        with pytest.raises(ValueError):
            GitHub("owner/repository", runner).get("releases/tags/test")


@pytest.mark.parametrize(
    "arguments,timeout",
    [
        (["gh", "api", "--method", "GET", "--include", "test"], 30),
        (["gh", "api", "--method", "POST", "--include", "graphql"], 30),
        (["gh", "api", "--method", "POST", "--include", "test"], 300),
        (["git", "rev-parse", "HEAD"], 300),
    ],
)
def test_subprocess_boundary_uses_argv_and_captures_sensitive_diagnostics(
    monkeypatch, arguments, timeout
):
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(
            arguments, 0, "private response", "private diagnostic"
        )

    monkeypatch.setattr(release_publish.subprocess, "run", run)
    release_publish.run_command(arguments)
    assert calls == [
        (
            arguments,
            {"capture_output": True, "text": True, "check": False, "timeout": timeout},
        )
    ]


def test_subprocess_timeout_does_not_expose_output(monkeypatch):
    def run(arguments, **kwargs):
        raise subprocess.TimeoutExpired(
            arguments, 300, output="secret token", stderr="credentials"
        )

    monkeypatch.setattr(release_publish.subprocess, "run", run)
    with pytest.raises(ValueError, match="could not complete") as error:
        release_publish.run_command(["gh", "api", "test"])
    assert "secret" not in str(error.value) and "credentials" not in str(error.value)


def test_creation_and_publication_send_metadata_and_notes_by_id(assets, tmp_path):
    fake = FakeGh(assets, existing=False)
    publish_fake(fake, assets, tmp_path)
    create = next(call for call in writes(fake) if endpoint(call).endswith("/releases"))
    create_fields = fields(create)
    assert create_fields["tag_name"] == TAG
    assert create_fields["target_commitish"] == COMMIT
    assert create_fields["name"] == "IPGUARD 0.1.0b2"
    assert create_fields["draft"] == "true"
    assert create_fields["prerelease"] == "true"
    assert create_fields["body"] == f"@{tmp_path / 'notes.md'}"
    final = writes(fake)[-1]
    assert endpoint(final) == f"repos/{REPO}/releases/7"
    assert fields(final)["name"] == create_fields["name"]
    assert fields(final)["body"] == create_fields["body"]
    assert fields(final)["draft"] == "false"
    for call in fake.calls:
        if endpoint(call).startswith("https://uploads.github.com/"):
            assert "Content-Type: application/octet-stream" in call


def test_matching_complete_draft_skips_every_asset_write(assets, tmp_path):
    fake = FakeGh(assets)
    fake.before = remote(assets)
    publish_fake(fake, assets, tmp_path)
    assert [method(call) for call in writes(fake)] == ["PATCH"]
    assert not fake.uploaded
    assert not fake.draft


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "starter"),
        ("size", 0),
        ("digest", None),
        ("digest", "sha256:" + "0" * 64),
    ],
)
def test_only_mismatched_expected_asset_is_replaced_by_asset_id(
    assets, tmp_path, field, value
):
    fake = FakeGh(assets)
    fake.before = remote(assets)
    fake.before[0][field] = value
    publish_fake(fake, assets, tmp_path)
    delete_calls = [call for call in writes(fake) if method(call) == "DELETE"]
    uploads = [call for call in writes(fake) if method(call) == "POST"]
    assert [endpoint(call) for call in delete_calls] == [
        f"repos/{REPO}/releases/assets/1"
    ]
    assert len(uploads) == 1
    assert parse_qs(urlsplit(endpoint(uploads[0])).query)["name"] == [
        next(iter(assets))
    ]
    assert not fake.draft


def test_each_existing_release_mutation_rechecks_same_draft_by_id(assets, tmp_path):
    fake = FakeGh(assets)
    fake.before[0]["digest"] = None
    publish_fake(fake, assets, tmp_path)
    since_mutation = []
    for call in fake.calls:
        if method(call) == "GET":
            since_mutation.append(call)
        else:
            assert any(
                endpoint(read) == f"repos/{REPO}/releases/7" for read in since_mutation
            )
            since_mutation = []


@pytest.mark.parametrize("lag", ["missing", "starter", "null_digest", "missing_digest"])
def test_uploaded_assets_eventually_become_visible_and_complete(assets, tmp_path, lag):
    fake = FakeGh(assets)
    lag_reads = 0

    def runner(arguments):
        nonlocal lag_reads
        result = fake(arguments)
        if (
            method(arguments) == "GET"
            and fake.uploaded
            and len(fake.before) == len(assets)
            and "/assets?" in endpoint(arguments)
        ):
            lag_reads += 1
            if lag_reads <= 2:
                delayed = [dict(item) for item in fake.after]
                if lag == "missing":
                    delayed.pop()
                elif lag == "starter":
                    delayed[-1]["state"] = "starter"
                elif lag == "null_digest":
                    delayed[-1]["digest"] = None
                else:
                    delayed[-1].pop("digest")
                return response(200, delayed)
        return result

    publish_fake(fake, assets, tmp_path, runner=runner)
    assert lag_reads >= 3
    assert not fake.draft


def test_known_created_release_id_can_lag_without_another_create(assets, tmp_path):
    fake = FakeGh(assets, existing=False)
    reads = 0

    def runner(arguments):
        nonlocal reads
        result = fake(arguments)
        if method(arguments) == "GET" and endpoint(arguments).endswith("releases/7"):
            reads += 1
            if reads <= 2:
                return response(404, {})
        return result

    publish_fake(fake, assets, tmp_path, runner=runner)
    assert reads >= 3
    assert not fake.draft
    assert (
        sum(
            method(call) == "POST" and endpoint(call).endswith("/releases")
            for call in fake.calls
        )
        == 1
    )


def test_known_release_id_lag_is_bounded_and_cannot_fallback_to_tag_lookup():
    calls = []

    def runner(arguments):
        calls.append(arguments)
        return response(404, {"message": "secret response"})

    with pytest.raises(ValueError):
        GitHub(REPO, runner).require_draft(7, TAG)
    assert 2 <= len(calls) <= 10
    assert all(
        method(call) == "GET" and endpoint(call) == f"repos/{REPO}/releases/7"
        for call in calls
    )


@pytest.mark.parametrize("failure", [503, 409, 422, "network"])
def test_ambiguous_creation_reconciles_by_authenticated_list_without_reposting(
    assets, tmp_path, failure
):
    fake = FakeGh(assets, existing=False)
    recovery_reads = 0

    def runner(arguments):
        nonlocal recovery_reads
        result = fake(arguments)
        if method(arguments) == "POST" and endpoint(arguments).endswith("/releases"):
            if failure == "network":
                return subprocess.CompletedProcess([], 1, "", "credential secret")
            return response(failure, {"message": "credential secret"})
        if fake.existing and "releases?" in endpoint(arguments):
            recovery_reads += 1
            if recovery_reads == 1:
                return response(200, [])
        if endpoint(arguments) == "graphql" and recovery_reads == 1:
            return response(200, graphql_payload(None))
        return result

    publish_fake(fake, assets, tmp_path, runner=runner)
    assert recovery_reads >= 2
    assert not fake.draft
    assert (
        sum(
            method(call) == "POST" and endpoint(call).endswith("/releases")
            for call in fake.calls
        )
        == 1
    )


def test_ambiguous_creation_with_no_visible_release_stops_without_reposting(
    assets, tmp_path, capsys
):
    fake = FakeGh(assets, existing=False)

    def runner(arguments):
        result = fake(arguments)
        if method(arguments) == "POST" and endpoint(arguments).endswith("/releases"):
            return response(503, {"message": "credential body secret"})
        if "releases?" in endpoint(arguments):
            return response(200, [])
        if endpoint(arguments) == "graphql":
            return response(200, graphql_payload(None))
        return result

    with pytest.raises(ValueError) as error:
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert "secret" not in str(error.value) and "credential" not in str(error.value)
    assert (
        sum(
            method(call) == "POST" and endpoint(call).endswith("/releases")
            for call in fake.calls
        )
        == 1
    )
    assert 2 <= sum("releases?" in endpoint(call) for call in fake.calls) <= 10
    assert not fake.uploaded and fake.draft
    output = capsys.readouterr().out
    assert "secret" not in output and "credential" not in output


@pytest.mark.parametrize(
    "mutation",
    ["published", "wrong_tag", "zero_id", "bool_id", "missing_id", "wrong_draft_type"],
)
def test_malformed_or_wrong_created_release_response_never_uploads(
    assets, tmp_path, mutation
):
    fake = FakeGh(assets, existing=False)

    def runner(arguments):
        result = fake(arguments)
        if method(arguments) == "POST" and endpoint(arguments).endswith("/releases"):
            item = fake.release()
            if mutation == "missing_id":
                item.pop("id")
            else:
                field, value = {
                    "published": ("draft", False),
                    "wrong_tag": ("tag_name", "v0.1.0b9"),
                    "zero_id": ("id", 0),
                    "bool_id": ("id", True),
                    "wrong_draft_type": ("draft", 1),
                }[mutation]
                item[field] = value
            return response(201, item)
        return result

    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert not fake.uploaded
    assert not any(method(call) == "PATCH" for call in fake.calls)


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {"id": 8, "tag_name": TAG, "draft": True},
        {"id": 7, "tag_name": "wrong", "draft": True},
        {"id": 7, "tag_name": TAG, "draft": False},
        {"id": 7, "tag_name": TAG, "draft": "true"},
    ],
)
def test_known_release_id_metadata_must_match_draft_and_tag(value):
    def runner(arguments):
        return response(200, value)

    with pytest.raises(ValueError):
        GitHub(REPO, runner).require_draft(7, TAG)


@pytest.mark.parametrize("failure", [429, 500, 502, 503, "network"])
def test_safe_get_retries_transient_errors_then_succeeds(failure):
    calls, delays = [], []

    def runner(arguments):
        calls.append(arguments)
        if len(calls) <= 2:
            if failure == "network":
                return subprocess.CompletedProcess([], 1, "", "private diagnostic")
            return response(
                failure,
                {"message": "private diagnostic"},
                headers="Retry-After: 2\n" if failure == 429 else "",
            )
        return response(200, {"ok": True})

    github = GitHub(REPO, runner, sleeper=delays.append)
    assert github.get("test") == {"ok": True}
    assert len(calls) == 3 and len(delays) == 2
    assert all(0 <= delay <= 60 for delay in delays)


@pytest.mark.parametrize("code", [401, 403, 404])
def test_nonretryable_get_failure_is_immediate_and_sanitized(code):
    calls, delays = [], []

    def runner(arguments):
        calls.append(arguments)
        return response(code, {"message": "secret credential"})

    with pytest.raises(ValueError) as error:
        GitHub(REPO, runner, sleeper=delays.append).get("test")
    assert len(calls) == 1 and not delays
    assert "secret" not in str(error.value) and "credential" not in str(error.value)


@pytest.mark.parametrize(
    "request_method,path,stage",
    [
        ("GET", f"git/ref/tags/{TAG}", "tag verification"),
        ("POST", "graphql", "draft lookup"),
        ("POST", "releases", "draft creation"),
        (
            "POST",
            f"https://uploads.github.com/repos/{REPO}/releases/42/assets?name=test.zip",
            "asset upload",
        ),
        ("DELETE", "releases/assets/42", "asset replacement"),
        ("PATCH", "releases/42", "publication"),
    ],
)
def test_failure_identifies_operation_without_exposing_response(
    request_method, path, stage
):
    def runner(arguments):
        return response(403, {"message": "secret credential"})

    with pytest.raises(ValueError) as error:
        GitHub(REPO, runner).get(path, method=request_method)
    message = str(error.value)
    assert stage in message and "HTTP 403" in message
    assert "secret" not in message and "credential" not in message
    assert path not in message


@pytest.mark.parametrize("failure", [429, 503, "network"])
def test_persistent_get_failure_and_retry_headers_are_bounded(failure):
    calls, delays = [], []

    def runner(arguments):
        calls.append(arguments)
        if failure == "network":
            return subprocess.CompletedProcess([], 1, "", "secret credential")
        return response(
            failure,
            {"message": "secret credential"},
            headers="Retry-After: 999999999999999999999999\nX-RateLimit-Remaining: 0\nX-RateLimit-Reset: 999999999999999999\n",
        )

    with pytest.raises(ValueError) as error:
        GitHub(REPO, runner, sleeper=delays.append).get("test")
    assert 1 <= len(calls) <= 10
    assert len(delays) == len(calls) - 1
    assert all(0 <= delay <= 60 for delay in delays)
    assert "secret" not in str(error.value) and "credential" not in str(error.value)


@pytest.mark.parametrize(
    "headers", ["Retry-After: 1\n", "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 1\n"]
)
def test_rate_limited_403_can_retry_when_headers_identify_rate_limit(headers):
    calls, delays = [], []

    def runner(arguments):
        calls.append(arguments)
        return (
            response(403, {}, headers=headers) if len(calls) == 1 else response(200, [])
        )

    assert GitHub(REPO, runner, sleeper=delays.append).get("test") == []
    assert len(calls) == 2 and len(delays) == 1


@pytest.mark.parametrize(
    "stdout",
    [
        "HTTP/2.0 200 Result\n\ninvalid JSON",
        "not an HTTP response",
        "HTTP/2.0 200 Result\n\n",
    ],
)
def test_malformed_success_response_is_not_retried(stdout):
    calls, delays = [], []

    def runner(arguments):
        calls.append(arguments)
        return subprocess.CompletedProcess([], 0, stdout, "secret credential")

    with pytest.raises(ValueError):
        GitHub(REPO, runner, sleeper=delays.append).get("test")
    assert len(calls) == 1 and not delays


@pytest.mark.parametrize("failure", [503, "network"])
def test_ambiguous_final_patch_checks_publication_by_id_without_repatching(
    assets, tmp_path, failure
):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        if method(arguments) == "PATCH":
            return (
                response(503, {"message": "secret"})
                if failure == 503
                else subprocess.CompletedProcess([], 1, "", "secret")
            )
        return result

    publish_fake(fake, assets, tmp_path, runner=runner)
    assert not fake.draft
    assert sum(method(call) == "PATCH" for call in fake.calls) == 1
    patch_index = next(
        index for index, call in enumerate(fake.calls) if method(call) == "PATCH"
    )
    recovery_reads = fake.calls[patch_index + 1 :]
    assert all(method(call) == "GET" for call in recovery_reads)
    assert any(endpoint(call) == f"repos/{REPO}/releases/7" for call in recovery_reads)
    assert any("/releases/7/assets?" in endpoint(call) for call in recovery_reads)
    assert endpoint(recovery_reads[-1]) == f"repos/{REPO}/git/ref/tags/{TAG}"
    assert sum("git/ref/tags/" in endpoint(call) for call in fake.calls) == 4


def test_unconfirmed_final_patch_does_not_claim_draft_is_retained(
    assets, tmp_path, capsys
):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        if method(arguments) == "PATCH":
            return response(503, {"message": "secret credential"})
        if (
            not fake.draft
            and method(arguments) == "GET"
            and endpoint(arguments).endswith("releases/7")
        ):
            return response(503, {"message": "secret credential"})
        return result

    with pytest.raises(ValueError) as error:
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert "draft is retained" not in str(error.value)
    assert "secret" not in str(error.value) and "credential" not in str(error.value)
    assert sum(method(call) == "PATCH" for call in fake.calls) == 1
    output = capsys.readouterr().out
    assert "secret" not in output and "credential" not in output


@pytest.mark.parametrize("mutation", ["wrong_id", "wrong_tag", "still_draft"])
def test_final_patch_response_must_describe_the_same_published_release(
    assets, tmp_path, mutation
):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        if method(arguments) == "PATCH":
            item = fake.release()
            field, value = {
                "wrong_id": ("id", 8),
                "wrong_tag": ("tag_name", "wrong"),
                "still_draft": ("draft", True),
            }[mutation]
            item[field] = value
            return response(200, item)
        return result

    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert sum(method(call) == "PATCH" for call in fake.calls) == 1


@pytest.mark.parametrize("mutation", ["different_id", "duplicate", "published"])
def test_visible_release_list_replacement_after_upload_stops_patch(
    assets, tmp_path, mutation
):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        if fake.uploaded and "releases?" in endpoint(arguments):
            item = fake.release()
            if mutation == "different_id":
                item["id"] = 8
            elif mutation == "published":
                item["draft"] = False
            return response(
                200, [item, dict(item, id=8)] if mutation == "duplicate" else [item]
            )
        return result

    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert not any(method(call) == "PATCH" for call in fake.calls)


@pytest.mark.parametrize("failure", [503, 409, 422, "network", "exception"])
def test_ambiguous_asset_upload_is_confirmed_without_a_second_upload(
    assets, tmp_path, failure
):
    fake = FakeGh(assets)
    fake.before = remote(assets)[1:]
    upload_count = 0

    def runner(arguments):
        nonlocal upload_count
        result = fake(arguments)
        if endpoint(arguments).startswith("https://uploads.github.com/"):
            upload_count += 1
            if failure == "network":
                return subprocess.CompletedProcess([], 1, "", "secret credential")
            if failure == "exception":
                raise ValueError("secret credential")
            return response(failure, {"message": "secret credential"})
        return result

    publish_fake(fake, assets, tmp_path, runner=runner)
    assert upload_count == 1
    assert not fake.draft


def test_unconfirmed_asset_upload_remains_bounded_and_never_reposts(
    assets, tmp_path, capsys
):
    fake = FakeGh(assets)
    fake.before = remote(assets)[1:]
    reads = 0

    def runner(arguments):
        nonlocal reads
        result = fake(arguments)
        if endpoint(arguments).startswith("https://uploads.github.com/"):
            fake.before.pop()
            return response(503, {"message": "secret credential"})
        if (
            fake.uploaded
            and method(arguments) == "GET"
            and "/assets?" in endpoint(arguments)
        ):
            reads += 1
        return result

    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert 2 <= reads <= 10
    assert (
        sum(
            endpoint(call).startswith("https://uploads.github.com/")
            for call in fake.calls
        )
        == 1
    )
    assert not any(method(call) == "PATCH" for call in fake.calls)
    assert fake.draft
    output = capsys.readouterr().out
    assert "secret" not in output and "credential" not in output


@pytest.mark.parametrize("code", [401, 403])
def test_denied_creation_is_not_reconciled_or_repeated(assets, tmp_path, code):
    fake = FakeGh(assets, existing=False)

    def runner(arguments):
        result = fake(arguments)
        if method(arguments) == "POST" and endpoint(arguments).endswith("/releases"):
            return response(code, {"message": "secret credential"})
        return result

    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert len(writes(fake)) == 1
    assert sum("releases?" in endpoint(call) for call in fake.calls) == 1
    assert not fake.uploaded


@pytest.mark.parametrize(
    "headers",
    [
        "Retry-After: nonsense\n",
        "Retry-After: -1\n",
        "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: secret\n",
        "Retry-After: 61\n",
    ],
)
def test_invalid_or_over_budget_retry_metadata_stops_without_waiting(headers):
    calls, delays = [], []

    def runner(arguments):
        calls.append(arguments)
        return response(429, {"message": "secret credential"}, headers=headers)

    with pytest.raises(ValueError) as error:
        GitHub(REPO, runner, sleeper=delays.append).get("test")
    assert len(calls) == 1 and not delays
    assert "secret" not in str(error.value) and "credential" not in str(error.value)


@pytest.mark.parametrize("failure", [503, "network"])
def test_persistent_get_transient_failure_exhausts_small_retry_count(failure):
    calls, delays = [], []

    def runner(arguments):
        calls.append(arguments)
        return (
            response(failure, {})
            if failure == 503
            else subprocess.CompletedProcess([], 1, "", "private")
        )

    with pytest.raises(ValueError):
        GitHub(REPO, runner, sleeper=delays.append).get("test")
    assert 2 <= len(calls) <= 10
    assert len(delays) == len(calls) - 1
    assert sum(delays) <= 120


def test_asset_upload_url_encodes_name_and_uses_separate_input_argument(tmp_path):
    name = "archive space&?.zip"
    path = tmp_path / name
    path.write_bytes(b"archive")
    expected = {
        name: Asset(path, 7, "sha256:" + hashlib.sha256(b"archive").hexdigest())
    }
    fake = FakeGh(expected)
    fake.before = []
    GitHub(REPO, fake).upload_assets(7, TAG, expected, [])
    upload = writes(fake)[0]
    assert endpoint(upload).endswith("assets?name=archive%20space%26%3F.zip")
    assert upload[upload.index("--input") + 1] == str(path)


def test_uncertain_final_patch_that_stays_draft_is_not_repeated(assets, tmp_path):
    fake = FakeGh(assets)
    after_patch_reads = 0

    def runner(arguments):
        nonlocal after_patch_reads
        result = fake(arguments)
        if method(arguments) == "PATCH":
            fake.draft = True
            return response(503, {"message": "secret credential"})
        if (
            any(method(call) == "PATCH" for call in fake.calls)
            and method(arguments) == "GET"
            and endpoint(arguments).endswith("releases/7")
        ):
            after_patch_reads += 1
        return result

    with pytest.raises(ValueError) as error:
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert 2 <= after_patch_reads <= 10
    assert sum(method(call) == "PATCH" for call in fake.calls) == 1
    assert "draft is retained" not in str(error.value)


def test_final_patch_wrong_prerelease_flag_is_unconfirmed(assets, tmp_path):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        if method(arguments) == "PATCH":
            return response(200, dict(fake.release(), prerelease=False))
        return result

    with pytest.raises(ValueError, match="could not be confirmed"):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert sum(method(call) == "PATCH" for call in fake.calls) == 1


@pytest.mark.parametrize("operation", ["create", "upload", "patch"])
@pytest.mark.parametrize("damage", ["invalid_json", "incomplete", "cli_failure"])
def test_accepted_mutation_with_uncertain_response_reconciles_without_repeating(
    assets, tmp_path, capsys, operation, damage
):
    fake = FakeGh(assets, existing=operation != "create")
    if operation == "upload":
        fake.before = remote(assets)[1:]
    damaged_calls = 0

    def runner(arguments):
        nonlocal damaged_calls
        result = fake(arguments)
        selected = (
            (
                operation == "create"
                and method(arguments) == "POST"
                and endpoint(arguments).endswith("/releases")
            )
            or (
                operation == "upload"
                and endpoint(arguments).startswith("https://uploads.github.com/")
            )
            or (operation == "patch" and method(arguments) == "PATCH")
        )
        if selected:
            damaged_calls += 1
            status = 200 if operation == "patch" else 201
            if damage == "cli_failure":
                result.returncode = 1
                result.stderr = "secret credential"
                return result
            output = f"HTTP/2.0 {status} Result" + (
                "\n\nsecret credential" if damage == "invalid_json" else ""
            )
            return subprocess.CompletedProcess([], 0, output, "secret credential")
        return result

    publish_fake(fake, assets, tmp_path, runner=runner)
    assert damaged_calls == 1
    assert not fake.draft
    output = capsys.readouterr().out
    assert "secret" not in output and "credential" not in output


def test_bare_rate_limit_429_does_not_retry_before_required_wait():
    calls, delays = [], []

    def runner(arguments):
        calls.append(arguments)
        return response(429, {"message": "secret credential"})

    with pytest.raises(ValueError):
        GitHub(REPO, runner, sleeper=delays.append).get("test")
    assert len(calls) == 1 and not delays


def test_existing_draft_hidden_from_rest_list_is_found_by_graphql(assets, tmp_path):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        return response(200, []) if "releases?" in endpoint(arguments) else result

    publish_fake(fake, assets, tmp_path, runner=runner)
    assert not fake.draft
    assert not any(endpoint(call).endswith("/releases") for call in writes(fake))
    reads = [call for call in fake.calls if endpoint(call) == "graphql"]
    assert len(reads) == 1
    assert method(reads[0]) == "POST"
    query_fields = fields(reads[0])
    assert query_fields["owner"] == "owner"
    assert query_fields["name"] == "repository"
    assert query_fields["tag"] == TAG
    assert "query" in query_fields and "release(tagName:" in query_fields["query"]


def test_null_graphql_release_and_empty_rest_list_allow_one_creation(assets, tmp_path):
    fake = FakeGh(assets, existing=False)
    publish_fake(fake, assets, tmp_path)
    assert not fake.draft
    assert sum(endpoint(call) == "graphql" for call in fake.calls) == 1
    assert sum(endpoint(call).endswith("/releases") for call in writes(fake)) == 1


@pytest.mark.parametrize(
    "item",
    [
        {"databaseId": 7, "isDraft": False, "tagName": TAG},
        {"databaseId": 7, "isDraft": True, "tagName": "wrong"},
        {"databaseId": 0, "isDraft": True, "tagName": TAG},
        {"databaseId": -1, "isDraft": True, "tagName": TAG},
        {"databaseId": True, "isDraft": True, "tagName": TAG},
        {"databaseId": "7", "isDraft": True, "tagName": TAG},
        {"databaseId": 7, "isDraft": "true", "tagName": TAG},
        {"databaseId": 7, "isDraft": 1, "tagName": TAG},
        {"databaseId": 7, "isDraft": True},
        {"databaseId": 7, "tagName": TAG},
        {"isDraft": True, "tagName": TAG},
        {},
        [],
        "secret credential",
    ],
)
def test_invalid_graphql_discovery_never_creates_or_mutates_a_release(
    assets, tmp_path, item
):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        if "releases?" in endpoint(arguments):
            return response(200, [])
        if endpoint(arguments) == "graphql":
            return response(200, graphql_payload(item))
        return result

    with pytest.raises(ValueError) as error:
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert not writes(fake)
    assert "secret" not in str(error.value) and "credential" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [],
        {"data": None},
        {"data": {}},
        {"data": {"repository": None}},
        {"data": {"repository": {}}},
        {"errors": [{"message": "secret credential"}]},
        {
            "data": {"repository": {"release": None}},
            "errors": [{"message": "secret credential"}],
        },
    ],
)
def test_malformed_or_error_graphql_payload_does_not_mean_release_absence(
    assets, tmp_path, capsys, payload
):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        if "releases?" in endpoint(arguments):
            return response(200, [])
        if endpoint(arguments) == "graphql":
            return response(200, payload)
        return result

    with pytest.raises(ValueError) as error:
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert not writes(fake)
    assert "secret" not in str(error.value) and "credential" not in str(error.value)
    output = capsys.readouterr().out
    assert "secret" not in output and "credential" not in output


def test_graphql_release_id_must_match_the_followup_rest_identity(assets, tmp_path):
    fake = FakeGh(assets)

    def runner(arguments):
        if endpoint(arguments).endswith("releases/8"):
            fake.calls.append(arguments)
            return response(200, fake.release())
        result = fake(arguments)
        if "releases?" in endpoint(arguments):
            return response(200, [])
        if endpoint(arguments) == "graphql":
            return response(
                200, graphql_payload({"databaseId": 8, "isDraft": True, "tagName": TAG})
            )
        return result

    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert not writes(fake)
    assert any(endpoint(call).endswith("releases/8") for call in fake.calls)


@pytest.mark.parametrize("failure", [503, 429, "network"])
def test_read_only_graphql_post_can_retry_then_find_the_hidden_draft(
    assets, tmp_path, failure
):
    fake = FakeGh(assets)
    graph_reads = 0

    def runner(arguments):
        nonlocal graph_reads
        result = fake(arguments)
        if "releases?" in endpoint(arguments):
            return response(200, [])
        if endpoint(arguments) == "graphql":
            graph_reads += 1
            if graph_reads <= 2:
                if failure == "network":
                    return subprocess.CompletedProcess([], 1, "", "secret credential")
                return response(
                    failure,
                    {"message": "secret credential"},
                    headers="Retry-After: 1\n" if failure == 429 else "",
                )
        return result

    publish_fake(fake, assets, tmp_path, runner=runner)
    assert graph_reads == 3
    assert not fake.draft
    assert not any(endpoint(call).endswith("/releases") for call in writes(fake))


@pytest.mark.parametrize("code", [401, 403, 404])
def test_graphql_api_permission_or_endpoint_failure_never_creates_a_release(
    assets, tmp_path, code
):
    fake = FakeGh(assets)
    graph_reads = 0

    def runner(arguments):
        nonlocal graph_reads
        result = fake(arguments)
        if "releases?" in endpoint(arguments):
            return response(200, [])
        if endpoint(arguments) == "graphql":
            graph_reads += 1
            return response(code, {"message": "secret credential"})
        return result

    with pytest.raises(ValueError):
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert graph_reads == 1
    assert not writes(fake)


@pytest.mark.parametrize("mutation", ["missing", "extra", "digest", "changed_tag"])
def test_recovered_patch_requires_fresh_published_bytes_and_tested_tag(
    assets, tmp_path, capsys, mutation
):
    fake = FakeGh(assets)
    patch_seen = False
    altered = [dict(item) for item in fake.after]
    if mutation == "missing":
        altered.pop()
    elif mutation == "extra":
        altered.append({"id": 99, "name": "secret-credential.json"})
    elif mutation == "digest":
        altered[0]["digest"] = "sha256:" + "0" * 64
    else:
        fake.commits[3] = OTHER_COMMIT

    def runner(arguments):
        nonlocal patch_seen
        result = fake(arguments)
        if method(arguments) == "PATCH":
            patch_seen = True
            return response(503, {"message": "secret credential"})
        if (
            patch_seen
            and method(arguments) == "GET"
            and "/assets?" in endpoint(arguments)
        ):
            return response(200, altered)
        return result

    with pytest.raises(ValueError, match="inspect the GitHub release") as error:
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert not fake.draft
    assert sum(method(call) == "PATCH" for call in fake.calls) == 1
    patch_index = next(
        index for index, call in enumerate(fake.calls) if method(call) == "PATCH"
    )
    assert all(method(call) == "GET" for call in fake.calls[patch_index + 1 :])
    assert any("/assets?" in endpoint(call) for call in fake.calls[patch_index + 1 :])
    assert "draft is retained" not in str(error.value)
    assert "secret" not in str(error.value) and "credential" not in str(error.value)
    output = capsys.readouterr().out
    assert "secret" not in output and "credential" not in output


def test_known_release_asset_endpoint_recovers_from_initial_404(assets):
    calls, delays = [], []
    expected = remote(assets)

    def runner(arguments):
        calls.append(arguments)
        return response(404, {}) if len(calls) == 1 else response(200, expected)

    assert GitHub(REPO, runner, sleeper=delays.append).assets(7) == expected
    assert len(calls) == 2 and len(delays) == 1
    assert 0 < sum(delays) <= 120
    assert all(
        method(call) == "GET"
        and endpoint(call) == f"repos/{REPO}/releases/7/assets?per_page=100&page=1"
        for call in calls
    )


def test_persistent_known_release_asset_404_stops_before_any_mutation(assets, tmp_path):
    fake = FakeGh(assets)
    asset_reads = 0

    def runner(arguments):
        nonlocal asset_reads
        result = fake(arguments)
        if method(arguments) == "GET" and "/assets?" in endpoint(arguments):
            asset_reads += 1
            return response(404, {"message": "secret credential"})
        return result

    with pytest.raises(ValueError) as error:
        publish_fake(fake, assets, tmp_path, runner=runner)
    assert asset_reads == 5
    assert not writes(fake)
    assert "secret" not in str(error.value) and "credential" not in str(error.value)
