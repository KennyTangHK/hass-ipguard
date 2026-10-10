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
from pathlib import Path
from zipfile import BadZipFile

from release_metadata import (
    ROOT,
    ReleaseMetadata,
    checksum_text,
    read_metadata,
    release_notes,
    validate_assets,
)

type Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class Asset:
    """Expected immutable bytes for one publication file."""

    path: Path
    size: int
    digest: str


def run_command(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    """Keep GitHub responses and subprocess diagnostics out of workflow logs."""
    try:
        return subprocess.run(
            arguments, capture_output=True, text=True, check=False, timeout=300
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
    """Small GitHub CLI boundary with strict response handling and no shell."""

    def __init__(self, repository: str, runner: Runner = run_command) -> None:
        if REPOSITORY.fullmatch(repository) is None:
            raise ValueError("Expected a GitHub owner/repository name.")
        self.repository, self.runner = repository, runner

    def command(self, *arguments: str) -> None:
        if self.runner(["gh", *arguments]).returncode != 0:
            raise ValueError("GitHub release command failed; the draft is retained.")

    def get(self, endpoint: str, *, missing_ok: bool = False) -> object:
        response = self.runner(
            [
                "gh",
                "api",
                "--method",
                "GET",
                "--include",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2026-03-10",
                f"repos/{self.repository}/{endpoint}",
            ]
        )
        parts = re.split(r"\r?\n\r?\n", response.stdout, maxsplit=1)
        status = re.match(r"HTTP/[0-9.]+\s+([0-9]{3})\b", parts[0])
        if status is None:
            raise ValueError("GitHub response has no valid HTTP status.")
        code = int(status[1])
        if code == 404 and missing_ok:
            return None
        if code != 200 or response.returncode != 0 or len(parts) != 2:
            raise ValueError("GitHub API request failed; publication stopped.")
        try:
            return json.loads(parts[1])
        except ValueError:
            raise ValueError("GitHub returned invalid JSON.") from None

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
        """Find drafts through the authenticated list, which includes unpublished releases."""
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

    def assets(self, release_id: int) -> list[object]:
        result: list[object] = []
        for page in range(1, 101):
            items = self.get(f"releases/{release_id}/assets?per_page=100&page={page}")
            if not isinstance(items, list) or len(items) > 100:
                raise ValueError("GitHub returned malformed asset pagination.")
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("GitHub asset pagination exceeded its limit.")


def publish(
    github: GitHub,
    metadata: ReleaseMetadata,
    tag: str,
    assets: Mapping[str, Asset],
    notes: Path,
    commit: str,
) -> None:
    """Leave every failure as a draft, without removing unexpected assets."""
    if tag != f"v{metadata.version}" or set(assets) != set(metadata.publication_assets):
        raise ValueError("Publication tag or asset set does not match the metadata.")
    if github.tag_commit(tag) != commit:
        raise ValueError("Remote release tag no longer names the tested commit.")
    release_id = github.draft(tag)
    flags = (
        "--repo",
        github.repository,
        "--verify-tag",
        f"--prerelease={str(metadata.prerelease).lower()}",
        f"--latest={str(metadata.latest).lower()}",
        "--title",
        f"IPGUARD {metadata.version}",
        "--notes-file",
        str(notes),
    )
    if release_id is None:
        github.command("release", "create", tag, "--draft", *flags)
        release_id = github.draft(tag)
        if release_id is None:
            raise ValueError("Created release draft could not be read.")
    validate_remote_assets(github.assets(release_id), assets, complete=False)
    if github.tag_commit(tag) != commit:
        raise ValueError("Remote release tag changed before asset upload.")
    github.command(
        "release",
        "upload",
        tag,
        *(str(asset.path) for asset in assets.values()),
        "--repo",
        github.repository,
        "--clobber",
    )
    if github.draft(tag) != release_id:
        raise ValueError("Release draft changed during asset upload.")
    validate_remote_assets(github.assets(release_id), assets, complete=True)
    if github.tag_commit(tag) != commit:
        raise ValueError("Remote release tag changed before publication.")
    github.command("release", "edit", tag, "--draft=false", *flags)


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
        metadata = read_metadata(ROOT, args.tag)
        response = run_command(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
        if response.returncode != 0:
            raise ValueError("Could not inspect the release checkout.")
        commit = validate_commit(
            response.stdout.strip(), args.event_sha, args.tested_sha
        )
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
    ) as error:
        print(f"Release publication failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
