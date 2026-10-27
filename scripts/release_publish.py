"""Publish validated assets only while the remote tag still names the tested commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import sleep, time
from urllib.parse import quote
from zipfile import BadZipFile

from scripts.prepare_release import prepare_release
from scripts.release_metadata import (
    ROOT,
    ReleaseMetadata,
    checksum_text,
    read_metadata,
    release_notes,
    validate_assets,
)
from scripts.sdk_package import read_sdk

type Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
API_RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)
VISIBILITY_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0)
MAX_RETRY_DELAY = 30.0
MAX_RETRY_WAIT = 60.0


class GitHubApiError(ValueError):
    """Classify failures without retaining response bodies or credentials."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float = 0.0,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class Asset:
    """Expected immutable bytes for one publication file."""

    path: Path
    size: int
    digest: str


def run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Keep GitHub responses and subprocess diagnostics out of workflow logs."""
    timeout = (
        30
        if (
            arguments[:5] == ["gh", "api", "--method", "GET", "--include"]
            or (arguments[:2] == ["gh", "api"] and arguments[-1] == "graphql")
        )
        else 300
    )
    try:
        return subprocess.run(
            arguments, capture_output=True, text=True, check=False, timeout=timeout
        )
    except OSError, subprocess.TimeoutExpired:
        raise ValueError("Release command could not complete.") from None


def expected_assets(dist: Path, metadata: ReleaseMetadata) -> dict[str, Asset]:
    """Bind the final nine publication files to their sizes and SHA256 digests."""
    archives = validate_assets(dist, metadata)
    if (dist / "SHA256SUMS").read_text() != checksum_text(archives):
        raise ValueError("Aggregate checksums do not match the release archives.")
    result: dict[str, Asset] = {}
    for name in metadata.publication_assets:
        path = dist / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("A publication asset is missing or not a regular file.")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        result[name] = Asset(path.resolve(), path.stat().st_size, f"sha256:{digest}")
    return result


def validate_remote_assets(
    assets: list[object], expected: Mapping[str, Asset], *, complete: bool
) -> None:
    """Permit a partial draft; publishing requires every expected file and byte."""
    names: set[str] = set()
    identities: set[int] = set()
    for item in assets:
        if not isinstance(item, dict):
            raise ValueError("GitHub returned malformed release assets.")
        name, identity = item.get("name"), item.get("id")
        if (
            not isinstance(name, str)
            or name not in expected
            or name in names
            or type(identity) is not int
            or identity <= 0
            or identity in identities
        ):
            raise ValueError("Draft contains unexpected or duplicate release assets.")
        names.add(name)
        identities.add(identity)
        if complete:
            file = expected[name]
            if (
                item.get("state") != "uploaded"
                or type(item.get("size")) is not int
                or item["size"] != file.size
                or item.get("digest") != file.digest
            ):
                raise ValueError("An uploaded asset does not match the validated file.")
    if complete and names != set(expected):
        raise ValueError("GitHub is missing required release assets.")


def validate_commit(head: str, event_sha: str, tested_sha: str) -> str:
    """Require checkout, triggering event, and tested artifact to name one commit."""
    if any(SHA.fullmatch(value) is None for value in (head, event_sha, tested_sha)):
        raise ValueError("Release commit identifiers must be full commit SHAs.")
    if head != event_sha or head != tested_sha:
        raise ValueError("Release checkout does not match the event and tested commit.")
    return head


class GitHub:
    """Use authenticated REST requests and pin mutations to one release ID."""

    def __init__(
        self,
        repository: str,
        runner: Runner = run_command,
        *,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if REPOSITORY.fullmatch(repository) is None:
            raise ValueError("Expected a GitHub owner/repository name.")
        self.repository, self.runner = repository, runner
        self.sleeper = sleeper if sleeper is not None else sleep

    def _pause(self, delay: float, stage: str) -> None:
        if delay > MAX_RETRY_DELAY:
            raise ValueError("GitHub requested a retry beyond the bounded wait budget.")
        print(f"Waiting {delay:g}s for GitHub {stage}.")
        self.sleeper(delay)

    def _request(
        self,
        method: str,
        endpoint: str,
        parameters: tuple[str, ...] = (),
        *,
        accepted: tuple[int, ...] = (200,),
        missing_ok: bool = False,
    ) -> object:
        if endpoint == "graphql" or endpoint.startswith("releases?"):
            stage = "draft lookup"
        elif endpoint.startswith("git/"):
            stage = "tag verification"
        elif endpoint.startswith("https://uploads.github.com/"):
            stage = "asset upload"
        elif method == "POST":
            stage = "draft creation"
        elif method == "PATCH":
            stage = "publication"
        elif method == "DELETE":
            stage = "asset replacement"
        else:
            stage = "release lookup"
        url = (
            endpoint
            if endpoint == "graphql"
            or endpoint.startswith("https://uploads.github.com/")
            else f"repos/{self.repository}/{endpoint}"
        )
        try:
            response = self.runner(
                [
                    "gh",
                    "api",
                    "--method",
                    method,
                    "--include",
                    "-H",
                    "Accept: application/vnd.github+json",
                    "-H",
                    "X-GitHub-Api-Version: 2026-03-10",
                    "-H",
                    "Cache-Control: no-cache",
                    *parameters,
                    url,
                ]
            )
        except ValueError:
            raise GitHubApiError(
                f"GitHub {stage} request could not complete; its result is unconfirmed.",
                retryable=True,
            ) from None
        parts = re.split(r"\r?\n\r?\n", response.stdout, maxsplit=1)
        status = re.match(r"HTTP/[0-9.]+\s+([0-9]{3})\b", parts[0])
        if status is None:
            raise GitHubApiError(
                f"GitHub {stage} response has no valid HTTP status.",
                retryable=response.returncode != 0,
                ambiguous=method != "GET",
            )
        code = int(status[1])
        headers = {
            name.lower(): value.strip()
            for line in parts[0].splitlines()[1:]
            if ":" in line
            for name, value in [line.split(":", 1)]
        }
        if code == 404 and missing_ok:
            return None
        if code not in accepted or response.returncode != 0:
            rate_limited = code in (403, 429) and (
                "retry-after" in headers or headers.get("x-ratelimit-remaining") == "0"
            )
            # Unspecified secondary rate limits require at least a minute.
            # Our shorter bounded budget stops instead of retrying too early.
            delay = 60.0 if code == 429 and not rate_limited else 0.0
            try:
                if "retry-after" in headers:
                    value = headers["retry-after"]
                    delay = (
                        float(int(value))
                        if value.isdecimal()
                        else max(0.0, parsedate_to_datetime(value).timestamp() - time())
                    )
                if headers.get("x-ratelimit-remaining") == "0":
                    delay = max(
                        delay, float(int(headers["x-ratelimit-reset"])) - time()
                    )
            except ValueError, KeyError, OverflowError, TypeError:
                raise GitHubApiError(
                    f"GitHub {stage} returned invalid retry metadata (HTTP {code})."
                ) from None
            raise GitHubApiError(
                f"GitHub API request failed during {stage} "
                f"(HTTP {code}); publication stopped.",
                status=code,
                retryable=(
                    code >= 500
                    or code == 429
                    or rate_limited
                    or (code in accepted and response.returncode != 0)
                ),
                retry_after=max(0.0, delay),
                ambiguous=method != "GET" and code in accepted,
            )
        if code == 204:
            return None
        if len(parts) != 2:
            raise GitHubApiError(
                f"GitHub {stage} returned an incomplete API response (HTTP {code}).",
                status=code,
                ambiguous=method != "GET",
            )
        try:
            return json.loads(parts[1])
        except ValueError:
            raise GitHubApiError(
                f"GitHub {stage} returned invalid JSON (HTTP {code}).",
                status=code,
                ambiguous=method != "GET",
            ) from None

    def get(
        self,
        endpoint: str,
        *,
        missing_ok: bool = False,
        retry_missing: bool = False,
        method: str = "GET",
        parameters: tuple[str, ...] = (),
    ) -> object:
        waited = 0.0
        for attempt in range(len(API_RETRY_DELAYS) + 1):
            try:
                return self._request(
                    method, endpoint, parameters, missing_ok=missing_ok
                )
            except GitHubApiError as error:
                if not (
                    error.retryable or (retry_missing and error.status == 404)
                ) or attempt == len(API_RETRY_DELAYS):
                    raise
                delay = max(API_RETRY_DELAYS[attempt], error.retry_after)
                if waited + delay > MAX_RETRY_WAIT:
                    raise ValueError(
                        "GitHub retries exceeded the bounded wait budget."
                    ) from None
                self._pause(delay, "API response")
                waited += delay
        raise AssertionError("Unreachable retry state")

    def tag_commit(self, tag: str) -> str:
        reference = self.get(f"git/ref/tags/{tag}")
        if (
            not isinstance(reference, dict)
            or reference.get("ref") != f"refs/tags/{tag}"
        ):
            raise ValueError("GitHub returned an unexpected tag reference.")
        target = reference.get("object")
        visited: set[str] = set()
        for _ in range(16):
            if not isinstance(target, dict):
                break
            kind, sha = target.get("type"), target.get("sha")
            if not isinstance(sha, str) or SHA.fullmatch(sha) is None or sha in visited:
                break
            if kind == "commit":
                return sha
            if kind != "tag":
                break
            visited.add(sha)
            annotated = self.get(f"git/tags/{sha}")
            if not isinstance(annotated, dict) or annotated.get("sha") != sha:
                break
            target = annotated.get("object")
        raise ValueError("Remote tag could not be resolved to a commit.")

    def draft(self, tag: str) -> int | None:
        """Discover pre-existing drafts; never depend on this list after creation."""
        match: int | None = None
        identities: set[int] = set()
        for page in range(1, 101):
            releases = self.get(f"releases?per_page=100&page={page}")
            if not isinstance(releases, list) or len(releases) > 100:
                raise ValueError("GitHub returned malformed release pagination.")
            for release in releases:
                if not isinstance(release, dict):
                    raise ValueError("GitHub returned malformed release metadata.")
                name, draft, identity = (
                    release.get("tag_name"),
                    release.get("draft"),
                    release.get("id"),
                )
                if (
                    not isinstance(name, str)
                    or not name
                    or type(draft) is not bool
                    or type(identity) is not int
                    or identity <= 0
                    or identity in identities
                ):
                    raise ValueError("GitHub returned malformed or duplicate releases.")
                identities.add(identity)
                if name != tag:
                    continue
                if match is not None:
                    raise ValueError("Multiple releases use the publication tag.")
                if not draft:
                    raise ValueError(
                        "Release is published or has unexpected draft metadata."
                    )
                match = identity
            if len(releases) < 100:
                return match
        raise ValueError("GitHub release pagination exceeded its limit.")

    def resolve_draft(self, tag: str) -> int | None:
        """Use the CLI's draft lookup route when the REST list has no match."""
        if (identity := self.draft(tag)) is not None:
            return identity
        owner, name = self.repository.split("/", 1)
        value = self.get(
            "graphql",
            method="POST",
            parameters=(
                "-f",
                "query=query($owner:String!,$name:String!,$tag:String!){"
                "repository(owner:$owner,name:$name){release(tagName:$tag){"
                "databaseId isDraft tagName}}}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-f",
                f"tag={tag}",
            ),
        )
        if not isinstance(value, dict) or value.get("errors"):
            raise ValueError("GitHub draft lookup failed or returned malformed data.")
        data = value.get("data")
        repository = data.get("repository") if isinstance(data, dict) else None
        if not isinstance(repository, dict) or "release" not in repository:
            raise ValueError("GitHub draft lookup returned malformed repository data.")
        release = repository["release"]
        if release is None:
            return None
        if not isinstance(release, dict):
            raise ValueError("GitHub draft lookup returned malformed release data.")
        result = self._release(
            {
                "id": release.get("databaseId"),
                "tag_name": release.get("tagName"),
                "draft": release.get("isDraft"),
            },
            tag,
        )
        if result["draft"] is not True:
            raise ValueError("Release is already published; no draft will be created.")
        release_id = result["id"]
        assert isinstance(release_id, int)
        self.require_draft(release_id, tag)
        return release_id

    @staticmethod
    def _release(
        value: object, tag: str, release_id: int | None = None
    ) -> dict[str, object]:
        if (
            not isinstance(value, dict)
            or type(value.get("id")) is not int
            or value["id"] <= 0
            or value.get("tag_name") != tag
            or type(value.get("draft")) is not bool
            or (release_id is not None and value["id"] != release_id)
        ):
            raise ValueError("GitHub returned unexpected release identity or metadata.")
        return value

    def require_draft(self, release_id: int, tag: str) -> dict[str, object]:
        value = self._release(
            self.get(f"releases/{release_id}", retry_missing=True), tag, release_id
        )
        if value["draft"] is not True:
            raise ValueError(
                "Release is already published; no further draft writes are allowed."
            )
        return value

    def create_draft(
        self, tag: str, metadata: ReleaseMetadata, notes: Path, commit: str
    ) -> int:
        try:
            value = self._request(
                "POST",
                "releases",
                (
                    "-f",
                    f"tag_name={tag}",
                    "-f",
                    f"target_commitish={commit}",
                    "-f",
                    f"name=IPGUARD {metadata.version}",
                    "-F",
                    "draft=true",
                    "-F",
                    f"prerelease={str(metadata.prerelease).lower()}",
                    "-F",
                    f"body=@{notes}",
                ),
                accepted=(201,),
            )
        except GitHubApiError as error:
            if not (error.retryable or error.ambiguous or error.status in (409, 422)):
                raise
            # A lost response may already have created a draft. Never repeat POST.
            for attempt in range(len(VISIBILITY_DELAYS) + 1):
                if error.retry_after:
                    self._pause(error.retry_after, "creation reconciliation")
                    error.retry_after = 0.0
                release_id = self.resolve_draft(tag)
                if release_id is not None:
                    self.require_draft(release_id, tag)
                    return release_id
                if attempt < len(VISIBILITY_DELAYS):
                    self._pause(VISIBILITY_DELAYS[attempt], "created draft visibility")
            raise ValueError(
                "Draft creation could not be confirmed; no second creation was attempted."
            ) from None
        result = self._release(value, tag)
        if result["draft"] is not True:
            raise ValueError("Created release is not an unpublished draft.")
        identity = result["id"]
        assert isinstance(identity, int)
        return identity

    def assets(self, release_id: int) -> list[object]:
        result: list[object] = []
        for page in range(1, 101):
            items = self.get(
                f"releases/{release_id}/assets?per_page=100&page={page}",
                retry_missing=True,
            )
            if not isinstance(items, list) or len(items) > 100:
                raise ValueError("GitHub returned malformed asset pagination.")
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("GitHub asset pagination exceeded its limit.")

    @staticmethod
    def _matches(item: Mapping[str, object], file: Asset) -> bool:
        return (
            item.get("state") == "uploaded"
            and type(item.get("size")) is int
            and item["size"] == file.size
            and item.get("digest") == file.digest
        )

    def wait_assets(
        self, release_id: int, expected: Mapping[str, Asset], *, name: str | None = None
    ) -> None:
        wanted = set(expected) if name is None else {name}
        for attempt in range(len(VISIBILITY_DELAYS) + 1):
            items = self.assets(release_id)
            validate_remote_assets(items, expected, complete=False)
            selected = {item["name"]: item for item in items if isinstance(item, dict)}
            pending = wanted - selected.keys()
            for asset_name in wanted & selected.keys():
                item, file = selected[asset_name], expected[asset_name]
                if item.get("state") == "starter":
                    pending.add(asset_name)
                elif item.get("state") == "uploaded" and item.get("digest") is None:
                    if type(item.get("size")) is not int or item["size"] != file.size:
                        raise ValueError(
                            "An uploaded asset does not match the validated file."
                        )
                    pending.add(asset_name)
                elif not self._matches(item, file):
                    raise ValueError(
                        "An uploaded asset does not match the validated file."
                    )
            if not pending:
                return
            if attempt < len(VISIBILITY_DELAYS):
                self._pause(VISIBILITY_DELAYS[attempt], "uploaded asset visibility")
        raise ValueError(
            "Uploaded assets could not be verified within the bounded wait; the draft is retained."
        )

    def upload_assets(
        self,
        release_id: int,
        tag: str,
        expected: Mapping[str, Asset],
        existing: list[object],
    ) -> None:
        known = {item["name"]: item for item in existing if isinstance(item, dict)}
        for name, file in expected.items():
            item = known.get(name)
            if item is not None and self._matches(item, file):
                continue
            if item is not None:
                self.require_draft(release_id, tag)
                self._request(
                    "DELETE",
                    f"releases/assets/{item['id']}",
                    accepted=(204,),
                    missing_ok=True,
                )
            self.require_draft(release_id, tag)
            try:
                value = self._request(
                    "POST",
                    f"https://uploads.github.com/repos/{self.repository}/releases/{release_id}/assets?name={quote(name, safe='')}",
                    (
                        "-H",
                        "Content-Type: application/octet-stream",
                        "--input",
                        str(file.path),
                    ),
                    accepted=(201,),
                )
            except GitHubApiError as error:
                if not (
                    error.retryable or error.ambiguous or error.status in (409, 422)
                ):
                    raise
                # Confirm possibly accepted bytes instead of repeating an upload.
                if error.retry_after:
                    self._pause(error.retry_after, "upload reconciliation")
                self.require_draft(release_id, tag)
                self.wait_assets(release_id, expected, name=name)
                continue
            if (
                not isinstance(value, dict)
                or value.get("name") != name
                or type(value.get("id")) is not int
                or value["id"] <= 0
            ):
                raise ValueError("GitHub returned unexpected uploaded asset metadata.")

    def publish_draft(
        self,
        release_id: int,
        tag: str,
        metadata: ReleaseMetadata,
        notes: Path,
        commit: str,
        expected: Mapping[str, Asset],
    ) -> None:
        self.require_draft(release_id, tag)
        if self.tag_commit(tag) != commit:
            raise ValueError("Remote release tag changed before publication.")
        reconciled = False
        try:
            value = self._request(
                "PATCH",
                f"releases/{release_id}",
                (
                    "-F",
                    "draft=false",
                    "-F",
                    f"prerelease={str(metadata.prerelease).lower()}",
                    "-f",
                    f"make_latest={'legacy' if metadata.latest else 'false'}",
                    "-f",
                    f"name=IPGUARD {metadata.version}",
                    "-F",
                    f"body=@{notes}",
                ),
            )
        except GitHubApiError as error:
            if not (error.retryable or error.ambiguous):
                raise
            reconciled = True
            if error.retry_after:
                self._pause(error.retry_after, "publication reconciliation")
            for attempt in range(len(VISIBILITY_DELAYS) + 1):
                value = self.get(f"releases/{release_id}", retry_missing=True)
                result = self._release(value, tag, release_id)
                if result["draft"] is False:
                    break
                if attempt < len(VISIBILITY_DELAYS):
                    self._pause(VISIBILITY_DELAYS[attempt], "publication result")
            else:
                raise ValueError(
                    "Publication result could not be confirmed; inspect the GitHub release before retrying."
                ) from None
        result = self._release(value, tag, release_id)
        if (
            result["draft"] is not False
            or result.get("prerelease") is not metadata.prerelease
        ):
            raise ValueError(
                "Publication result could not be confirmed; inspect the GitHub release before retrying."
            )
        if reconciled:
            try:
                validate_remote_assets(self.assets(release_id), expected, complete=True)
                if self.tag_commit(tag) != commit:
                    raise ValueError("The release tag changed during reconciliation.")
            except ValueError:
                raise ValueError(
                    "Published release contents or tag could not be confirmed; inspect the GitHub release before retrying."
                ) from None


def publish(
    github: GitHub,
    metadata: ReleaseMetadata,
    tag: str,
    assets: Mapping[str, Asset],
    notes: Path,
    commit: str,
) -> None:
    """Verify one pinned draft and its bytes before attempting publication."""
    if tag != f"v{metadata.version}" or set(assets) != set(metadata.publication_assets):
        raise ValueError("Publication tag or asset set does not match the metadata.")
    if github.tag_commit(tag) != commit:
        raise ValueError("Remote release tag no longer names the tested commit.")
    release_id = github.resolve_draft(tag)
    if release_id is None:
        release_id = github.create_draft(tag, metadata, notes, commit)
    github.require_draft(release_id, tag)
    before = github.assets(release_id)
    validate_remote_assets(before, assets, complete=False)
    if github.tag_commit(tag) != commit:
        raise ValueError("Remote release tag changed before asset upload.")
    github.upload_assets(release_id, tag, assets, before)
    github.require_draft(release_id, tag)
    # A stale list may omit the known ID. A visible replacement or duplicate is unsafe.
    if github.draft(tag) not in (None, release_id):
        raise ValueError("Release draft changed during asset upload.")
    github.wait_assets(release_id, assets)
    github.publish_draft(release_id, tag, metadata, notes, commit, assets)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--event-sha", required=True)
    parser.add_argument("--tested-sha", required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--notes-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        response = run_command(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
        if response.returncode != 0:
            raise ValueError("Could not inspect the release checkout.")
        commit = validate_commit(
            response.stdout.strip(), args.event_sha, args.tested_sha
        )
        read_sdk(ROOT)
        prepare_release(ROOT, args.tag)
        metadata = read_metadata(ROOT, args.tag)
        assets = expected_assets(args.dist, metadata)
        args.notes_file.write_text(release_notes(metadata))
        publish(GitHub(args.repo), metadata, args.tag, assets, args.notes_file, commit)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        tarfile.TarError,
        BadZipFile,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"Release publication failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
