"""Exercise publication and its failure paths without accessing GitHub."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import release_publish
from release_metadata import ReleaseMetadata
from release_publish import (
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


def response(code: int, value: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        [],
        0 if code == 200 else 1,
        f"HTTP/2.0 {code} Result\nContent-Type: application/json\n\n{json.dumps(value)}",
        "",
    )


class FakeGh:
    def __init__(
        self, assets: dict[str, Asset], *, existing: bool = True, tag: str = TAG
    ) -> None:
        self.calls: list[list[str]] = []
        self.tag = tag
        self.existing, self.draft, self.uploaded = existing, True, False
        self.before: list[dict[str, object]] = remote(assets)[:2] if existing else []
        self.after: list[dict[str, object]] = remote(assets)
        self.commits = [COMMIT, COMMIT, COMMIT]
        self.annotated = False
        self.fail_upload = False
        self.api_status = 200

    def __call__(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if arguments[:2] == ["gh", "api"]:
            endpoint = arguments[-1].split("repos/owner/repository/", 1)[1]
            if self.api_status != 200:
                return response(self.api_status, {"message": "Synthetic failure"})
            if endpoint == f"git/ref/tags/{self.tag}":
                target = (
                    {"type": "tag", "sha": ANNOTATED}
                    if self.annotated
                    else {"type": "commit", "sha": self.commits.pop(0)}
                )
                return response(200, {"ref": f"refs/tags/{self.tag}", "object": target})
            if endpoint == f"git/tags/{ANNOTATED}":
                return response(
                    200,
                    {
                        "sha": ANNOTATED,
                        "object": {"type": "commit", "sha": self.commits.pop(0)},
                    },
                )
            if endpoint == f"releases/tags/{self.tag}":
                # GitHub's by-tag endpoint does not return unpublished drafts.
                return (
                    response(200, {"id": 7, "tag_name": self.tag, "draft": self.draft})
                    if self.existing and not self.draft
                    else response(404, {})
                )
            if endpoint == "releases?per_page=100&page=1":
                return response(
                    200,
                    [{"id": 7, "tag_name": self.tag, "draft": self.draft}]
                    if self.existing
                    else [],
                )
            if endpoint.startswith("releases/7/assets?"):
                return response(200, self.after if self.uploaded else self.before)
            raise AssertionError("Unexpected API endpoint")
        if arguments[:3] == ["gh", "release", "create"]:
            self.existing = True
        elif arguments[:3] == ["gh", "release", "upload"]:
            if self.fail_upload:
                return subprocess.CompletedProcess(
                    [], 1, "credential body", "secret token"
                )
            self.uploaded = True
        elif arguments[:3] == ["gh", "release", "edit"]:
            self.draft = False
        else:
            raise AssertionError("Unexpected GitHub command")
        return subprocess.CompletedProcess([], 0, "", "")


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("annotated", [True, False])
def test_publish_new_or_partial_draft_after_complete_verification(
    assets, tmp_path, existing, annotated
):
    fake = FakeGh(assets, existing=existing)
    fake.annotated = annotated
    publish(
        GitHub("owner/repository", fake),
        ReleaseMetadata("0.1.0b2", "0.1.0b2"),
        TAG,
        assets,
        tmp_path / "notes.md",
        COMMIT,
    )
    assert not fake.draft
    writes = [call for call in fake.calls if call[:2] == ["gh", "release"]]
    assert [call[2] for call in writes] == (
        ["upload", "edit"] if existing else ["create", "upload", "edit"]
    )
    assert "--prerelease=true" in writes[-1] and "--latest=false" in writes[-1]
    assert sum("git/ref/tags/" in call[-1] for call in fake.calls) == 3
    assert not any("releases/tags/" in call[-1] for call in fake.calls)


def test_stable_publication_sets_stable_latest_flags(assets, tmp_path):
    metadata = ReleaseMetadata("0.1.0", "0.1.0b2")
    stable_assets = {
        name: Asset(tmp_path / name, 10, "sha256:" + "0" * 64)
        for name in metadata.publication_assets
    }
    fake = FakeGh(stable_assets, tag="v0.1.0")
    publish(
        GitHub("owner/repository", fake),
        metadata,
        "v0.1.0",
        stable_assets,
        tmp_path / "notes.md",
        COMMIT,
    )
    assert "--prerelease=false" in fake.calls[-1] and "--latest=true" in fake.calls[-1]


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
        publish(
            GitHub("owner/repository", fake),
            ReleaseMetadata("0.1.0b2", "0.1.0b2"),
            TAG,
            assets,
            tmp_path / "notes.md",
            COMMIT,
        )
    assert not any(call[:2] == ["gh", "release"] for call in fake.calls)


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
    if mutation == "extra":
        fake.after.append({"id": 99, "name": "session.json"})
    elif mutation == "missing":
        fake.after.pop()
    elif mutation == "duplicate":
        fake.after.append(dict(fake.after[0], id=99))
    elif mutation == "missing_digest":
        fake.after[0].pop("digest")
    else:
        field, value = {
            "state": ("state", "starter"),
            "size": ("size", 0),
            "bool_size": ("size", True),
            "digest": ("digest", "sha256:" + "0" * 64),
            "null_digest": ("digest", None),
        }[mutation]
        fake.after[0][field] = value
    with pytest.raises(ValueError):
        publish(
            GitHub("owner/repository", fake),
            ReleaseMetadata("0.1.0b2", "0.1.0b2"),
            TAG,
            assets,
            tmp_path / "notes.md",
            COMMIT,
        )
    assert fake.draft
    assert not any(call[:3] == ["gh", "release", "edit"] for call in fake.calls)
    assert not any("delete" in call for call in fake.calls)


@pytest.mark.parametrize("changed_at", [0, 1, 2])
def test_tag_change_stops_before_next_write(assets, tmp_path, changed_at):
    fake = FakeGh(assets)
    fake.commits[changed_at] = OTHER_COMMIT
    with pytest.raises(ValueError, match="tag"):
        publish(
            GitHub("owner/repository", fake),
            ReleaseMetadata("0.1.0b2", "0.1.0b2"),
            TAG,
            assets,
            tmp_path / "notes.md",
            COMMIT,
        )
    assert fake.draft
    assert fake.uploaded is (changed_at == 2)


def test_upload_failure_does_not_log_response_or_publish(assets, tmp_path, capsys):
    fake = FakeGh(assets)
    fake.fail_upload = True
    with pytest.raises(ValueError, match="draft is retained") as error:
        publish(
            GitHub("owner/repository", fake),
            ReleaseMetadata("0.1.0b2", "0.1.0b2"),
            TAG,
            assets,
            tmp_path / "notes.md",
            COMMIT,
        )
    assert "secret" not in str(error.value) and "credential" not in str(error.value)
    assert capsys.readouterr().out == ""
    assert fake.draft


@pytest.mark.parametrize("code", [401, 403, 500])
def test_api_failure_is_not_treated_as_a_missing_draft(assets, tmp_path, code):
    fake = FakeGh(assets, existing=False)
    fake.api_status = code
    with pytest.raises(ValueError, match="API request failed"):
        publish(
            GitHub("owner/repository", fake),
            ReleaseMetadata("0.1.0b2", "0.1.0b2"),
            TAG,
            assets,
            tmp_path / "notes.md",
            COMMIT,
        )
    assert not any(call[:2] == ["gh", "release"] for call in fake.calls)


@pytest.mark.parametrize("mutation", ["published", "missing", "different_id"])
def test_draft_replacement_after_upload_stops_publication(assets, tmp_path, mutation):
    fake = FakeGh(assets)

    def runner(arguments):
        result = fake(arguments)
        if fake.uploaded and arguments[-1].endswith("releases?per_page=100&page=1"):
            if mutation == "missing":
                return response(200, [])
            return response(
                200,
                [
                    {
                        "id": 8 if mutation == "different_id" else 7,
                        "tag_name": TAG,
                        "draft": mutation != "published",
                    }
                ],
            )
        return result

    with pytest.raises(ValueError):
        publish(
            GitHub("owner/repository", runner),
            ReleaseMetadata("0.1.0b2", "0.1.0b2"),
            TAG,
            assets,
            tmp_path / "notes.md",
            COMMIT,
        )
    assert not any(call[:3] == ["gh", "release", "edit"] for call in fake.calls)


def test_created_draft_missing_from_release_list_stops_before_upload(assets, tmp_path):
    fake = FakeGh(assets, existing=False)

    def runner(arguments):
        result = fake(arguments)
        if arguments[-1].endswith("releases?per_page=100&page=1"):
            return response(200, [])
        return result

    with pytest.raises(ValueError, match="Created release draft could not be read"):
        publish(
            GitHub("owner/repository", runner),
            ReleaseMetadata("0.1.0b2", "0.1.0b2"),
            TAG,
            assets,
            tmp_path / "notes.md",
            COMMIT,
        )
    writes = [call for call in fake.calls if call[:2] == ["gh", "release"]]
    assert [call[2] for call in writes] == ["create"]
    assert fake.draft


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


def test_subprocess_boundary_uses_argv_and_captures_sensitive_diagnostics(monkeypatch):
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(
            arguments, 0, "private response", "private diagnostic"
        )

    monkeypatch.setattr(release_publish.subprocess, "run", run)
    arguments = ["gh", "release", "upload", TAG, "/path with spaces/ipguard.zip"]
    release_publish.run_command(arguments)
    assert calls == [
        (
            arguments,
            {"capture_output": True, "text": True, "check": False, "timeout": 300},
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
