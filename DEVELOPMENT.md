# Development

The repository contains two independently packaged libraries: `ha-ipguard` at the root, with the HACS-required `custom_components/ipguard` layout, and `ipguard-api` in its own child project. Each has its own package metadata, anonymous Apache-2.0 license and wheel/source distribution. `uv` shares development dependencies. Buttons and notifications need no separate runtime service. Video calling, camera streaming and Scrypted integration are unsupported.

## Environment

Use the interpreter selected by [`.python-version`](.python-version). Runtime requirements and development dependencies are declared in [`pyproject.toml`](pyproject.toml) and [`uv.lock`](uv.lock); the CI tool version is set in the [checks workflow](.github/workflows/checks.yml). The virtual environment also contains Home Assistant's own pinned `uv` dependency; do not independently upgrade that package.

Run from the repository root:

```sh
uv run --no-project --python "$(cat .python-version)" python scripts/prepare_release.py
uv sync --locked --all-packages
uv run --no-sync python --version
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync pytest
```

`.venv` and `.cache/uv` are local and ignored. Both source packages are editable workspace dependencies. Do not install into a real Home Assistant instance to run these checks. SDK transport tests use plaintext/CURVE loopback servers; Home Assistant tests use isolated fixtures and synthetic accounts. No test may contact the provider, send a real SMS, replace an installation or release a physical door.

The root `conftest.py` is pytest configuration shared by both test directories. Its automatic fixture patches native ZeroMQ connection and binding methods to allow only loopback or in-process endpoints. Pytest's ordinary Python socket guard cannot intercept libzmq, so this additional guard is required to prevent tests from contacting the provider. It is development-only and excluded from wheels, source distributions and release ZIPs.

## Architecture and invariants

The SDK owns identifier validation, challenge state, registration, Android metadata guards, ZeroMQ sockets, response correlation, pushed relay inventories, incoming-call validation/deduplication and sanitized callbacks, connection recovery, typed failures and the guarded opt-in auto-hangup experiment. It must preserve the pending-enrollment reset-notice fix and tolerate unrelated SIP/control envelopes. The integration owns explicit setup/consent forms, config entries, runtime subscriptions, one service device per account, release buttons and the account doorbell event. Entity getters use cached data.

Device identifiers use the config-entry ID, keeping accounts independent even when they share a physical board. Buttons and the incoming-call event declare the same account device through normal entity setup; new relay pushes attach to it without another connection or enrollment. This is the sole supported layout. There is no registry regrouping, config-entry migration or cleanup for prior feature layouts. Saved credentials, entity IDs, custom settings and device grouping must persist across normal reloads and restarts.

Incoming offers always produce a safe ring notification. The SDK remains passive by default; HA runtime clients explicitly enable `auto_hangup=True` for the temporary trial tracked in [TODO #2](https://github.com/KennyTangHK/hass-ipguard/issues/2). There is no Home Assistant option yet. The trial schedules one immediate cause-zero hangup using the validated original call token and caller route, without waiting for an acknowledgement in the receive loop. Deduplication, current-transport/session guards, stale/terminated-call invalidation and owned task cleanup prevent queued or repeated attempts after uncertainty. Do not log token/route fields.

Media descriptions are validated and discarded; they are not retained for answering or exposed through any API. No camera platform, media bridge, Scrypted discovery/pairing, SDP answer, pickup, microphone or automatic door release is implemented. Provider refusal acceptance and carrier-fallback behavior remain unknown; tests must use loopback fixtures only.

Diagnostics use fixed categories only. Include both standalone and bundled SDK logger namespaces in the manifest so integration debug capture works for either installation. Never log raw packets, call tokens, SDP, credentials, private URLs or arbitrary provider error text.

One-based relay numbers are stable within a board. Buttons are release actions, without a lock state or contact sensor. A release must have current remote permission, must not wait in a queue for connectivity, and must never be replayed automatically. A timeout after possible delivery is an unknown result. Shutdown and cancelled setup flows must close sockets and stop background tasks.

All human-readable server metadata must be values from the Android app or the agreed mock Android profile. Personal installation names are validated before transmission. Do not apply substring filters to identifiers, codes, keys, UUIDs or opaque correlation tokens. Logs must omit raw packets, serialized sessions, identifiers, passwords and arbitrary server-supplied error text.

The independent SDK is imported only through the integration's `api.py` adapter. Keep imports inside the SDK relative: the custom release places a generated copy under `_vendor/ipguard_api`. This preserves exception/type identity and avoids changing `sys.path` or registering aliases in `sys.modules`.

## Build an installable release

```sh
uv run --no-project --python "$(cat .python-version)" python scripts/prepare_release.py
uv build --all-packages --build-constraints build-constraints.txt --require-hashes
uv run --no-sync python scripts/build_release.py
```

The builder reads the integration and SDK versions from their package metadata and produces two archives with checksums: `dist/ipguard.zip` contains flat integration files for HACS, and `dist/ipguard-<version>.zip` contains `custom_components/ipguard` for manual extraction into a configuration directory. It checks the independent SDK wheel's name/version, includes its Apache-2.0 license, changes only the generated adapter to relative SDK imports, and sets the generated manifest's native requirement to the reviewed PyZMQ pin in [`scripts/build_release.py`](scripts/build_release.py). `release.json` records the SDK version and wheel digest. Identical inputs produce identical ZIP contents; generated files are not maintained by hand.

The source metadata is a valid development snapshot. Release workflows derive the integration version from the pushed tag and synchronize both SDK pins from the independent SDK package. The preparer updates only those metadata fields and local workspace versions in the lockfile, preserving external dependencies and hashes. The publisher repeats the same preparation from the checked commit and tag before comparing artifact bytes. For a local tagged build, run `uv run --no-project --python "$(cat .python-version)" python scripts/prepare_release.py --tag "$release_tag"` before building; without a tag, it synchronizes SDK pins and retains the development version. The source manifest's SDK pin supports a future PyPI publication. Install the bundled GitHub release through HACS, or use the manual archive. `hide_default_branch` prevents HACS from offering the source checkout without its built SDK. Native PyZMQ is installed by Home Assistant; no macOS binary is included in either archive. Before changing SDK dependencies, update the builder's dependency validation and release checks.

The checks workflow runs on pull requests and default-branch pushes, and the release workflow calls it before publication. HACS validation runs alongside local checks; both must pass for a release. Local checks include actionlint, lint, formatting, strict typing, tests, builds and the isolated container release check. Superseded ordinary CI runs are cancelled; tagged release runs are not. All runs build and validate release files, but only release calls upload the nine checked publication assets. Only the publishing job receives `contents: write`; normal changes and pull requests receive read-only permissions. Core submission still requires publishing the independent SDK and meeting upstream rules; bundled custom packaging does not imply Core approval.

Release helpers live in `scripts/` and their synthetic tests in `tests/`, so the standard mypy and pytest commands include them. Invoke the metadata and publishing helpers as `python -m scripts.release_metadata` and `python -m scripts.release_publish`. The reusable checks reject a release request from a branch before preparing metadata. After downloading the checked assets, the publisher prepares metadata and validates their bytes once before making GitHub requests.

## Publish a beta or stable release

Release versions follow PEP 440 and Home Assistant/HACS's AwesomeVersion. Tags have the form `v<major>.<minor>.<patch>b<serial>` for betas and `v<major>.<minor>.<patch>` for stable releases. Beta tags produce GitHub prereleases and require beta selection in HACS. Matching stable tags produce normal releases without the beta requirement. Stable publication uses GitHub's automatic date/version policy for Latest rather than forcing every maintenance release to become Latest.

| Channel | Integration version | Matching tag | GitHub result |
| --- | --- | --- | --- |
| Beta | `<major>.<minor>.<patch>b<serial>` | `v<major>.<minor>.<patch>b<serial>` | Prerelease; excluded from Latest |
| Stable | `<major>.<minor>.<patch>` | `v<major>.<minor>.<patch>` | Normal release; Latest follows GitHub's date/version policy |

1. Review implementation changes and run the relevant checks. Integration releases require no manual edits to the package version, manifest version or SDK pins.
2. When intentionally releasing a changed SDK, update only its version in [`ipguard-api/pyproject.toml`](ipguard-api/pyproject.toml), then run `uv run --no-project --python "$(cat .python-version)" python scripts/prepare_release.py` to synchronize dependent pins and local lock versions. An unchanged SDK retains its independent version. Changes to actual dependencies still require a reviewed `uv lock` update.
3. Commit and push the changes to the default branch.
4. Choose the new beta or stable tag and push it. The **Publish release** workflow prepares metadata from that tag, runs all checks, builds and publishes the assets automatically:

```sh
release_tag="v<major>.<minor>.<patch>b<serial>"
git push origin master
git tag "$release_tag"
git push origin "$release_tag"
```

For a stable release, choose `v<major>.<minor>.<patch>` without the beta suffix. When promoting the project out of beta, also remove beta-only labels from the README and HACS display name, and review the SDK's status separately. Create a new stable tag rather than changing the prerelease flag on a published beta.

The workflow generates integration versions and SDK pins before checking their consistency with the tag, independent SDK requirements, and the exact tested commit. It discovers existing drafts through authenticated release-list pagination, with a read-only GraphQL lookup when the list has no match, and pins creation, reads, uploads and publication to one release ID. Newly created drafts use the ID returned by GitHub rather than requiring immediate list visibility. Requests retry transient read failures within bounded waits, respect server retry delays, and briefly wait for uploaded asset metadata. An uncertain creation response is reconciled without sending a second creation request. [GitHub release API](https://docs.github.com/en/rest/releases/releases#create-a-release), [GitHub retry guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)

Existing assets with matching uploaded state, size and SHA-256 digest are reused. Only expected draft assets needing replacement are deleted; unexpected or duplicate assets stop publication. All nine final assets and the remote tag are checked before publishing with the metadata-derived beta/stable flags. Writes recheck the pinned release's draft state. Failures before the final publication attempt leave the draft unpublished. If that response is lost, the publisher reads the release and rechecks all asset bytes and the tag to confirm the result, reporting uncertainty if confirmation fails. Published releases are never overwritten. No manual ZIP upload, personal access token, or PyPI publishing step is required.

### Retry a failed publication

Open **Actions → Publish release → the failed tagged run → Re-run all jobs**. The existing tag must still point to the tested commit. A valid partial draft is resumed; a published release is refused.

Rerunning uses the original tagged commit, including its workflow files. If the fix changes code or workflows, commit and push the fix, then publish a new beta tag or recreate the failed tag only if it has never been published. Moving a published tag is not supported.

The workflow also supports a manual dispatch against an existing tag:

```sh
release_tag="<existing-release-tag>"
gh workflow run release.yml --repo KennyTangHK/hass-ipguard --ref "$release_tag"
gh run watch --repo KennyTangHK/hass-ipguard
```

Run against the tag, since dispatching from a branch is rejected. Manual dispatch requires the workflow to be present on the default branch and in the selected tagged revision; older tagged runs can use **Re-run all jobs**. [GitHub manual workflow runs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow), [GitHub CLI workflow reference](https://cli.github.com/manual/gh_workflow_run)

### Delete a published release

On GitHub, open **Releases**, select the release's delete button, then **Delete this release**. From the terminal, select and inspect the exact tag before deleting its release:

```sh
release_tag="<published-tag>"
gh release view "$release_tag" --repo KennyTangHK/hass-ipguard
gh release delete "$release_tag" --repo KennyTangHK/hass-ipguard
```

The command asks for confirmation and retains the Git tag. To delete the release and its remote tag together, use this command instead:

```sh
gh release delete "$release_tag" --repo KennyTangHK/hass-ipguard --cleanup-tag
```

The local tag remains until explicitly removed with `git tag -d "$release_tag"`. Deleting a release removes its downloadable assets; it does not uninstall users' existing copies or revert repository commits. Prefer a new version when shipping a correction. [GitHub release management](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository), [GitHub CLI deletion reference](https://cli.github.com/manual/gh_release_delete)

For the next integration version, commit the implementation changes and choose a new beta or stable tag; the workflow generates release metadata automatically. Change the SDK's own version only when releasing SDK changes, and use the preparer to synchronize its pins. Update documentation when behavior or installation steps change. Invalid tags, inconsistent prepared metadata and unverified artifacts fail before publication. Do not move an existing release tag; fix failed workflows and rerun the same tag's workflow, or publish a new version as appropriate.

The integration is listed in the HACS catalogue as `KennyTangHK/hass-ipguard`. Keep the repository public with its description/topics and release assets maintained. Catalogue inclusion is separate from Home Assistant Core approval. The HACS validator reads the exact pushed GitHub revision, so it cannot fully validate unpushed local changes. [HACS publisher guide](https://www.hacs.dev/docs/publish/)

To repeat the Linux release check with Docker:

```sh
docker build -f scripts/Dockerfile.smoke -t ipguard-ha-smoke scripts
docker run --rm --network none \
  -v "$PWD/dist:/releases:ro" -v "$PWD/scripts:/checks:ro" \
  --entrypoint python ipguard-ha-smoke \
  /checks/smoke_release.py /releases/ipguard.zip
```

The image uses the official Home Assistant digest pinned in [`scripts/Dockerfile.smoke`](scripts/Dockerfile.smoke) and installs the native PyZMQ requirement. The check extracts only the ZIP into temporary storage, blocks external SDK imports, starts Home Assistant in that temporary directory, and exercises real SDK enrollment and release against loopback plaintext/CURVE routers. The runtime has no external network and mounts no credentials or real configuration directory. All temporary containers are removed. Repeat the command with `--registration replace` and `--registration decline` after the ZIP argument to check both explicit replacement choices.

## Evidence and verification limits

Runtime hardening validates transmission-time permissions, rejects busy release writes, bounds malformed inputs/resources and suppresses credential-bearing exception chains. Source distributions, wheels and ZIPs use reviewed exact file selections; unknown source files and altered archive payloads fail validation. The copied SDK file set and bytes must equal its independent wheel, including the license. Both build backends and the complete isolated build dependency closure are pinned with hashes checked against `uv.lock`.

The CI restart check starts the current archive in two separate processes with the same temporary configuration directory. It preserves config-entry data, installation UUID, account device and button/event entity IDs. The second process reconnects using saved authorization without requesting a verification code, replacing an installation or releasing a door. Run `scripts/smoke_release.py <archive> --restart` to repeat it; both processes communicate only with loopback fixtures. No prior release or compatibility fixture is used.

Checks cover SDK, integration, packaging, release metadata and publication behavior. Run Ruff lint/format, strict mypy, the complete pytest suite and actionlint before releasing. Regressions cover failure/cancellation cleanup, pre-send authorization changes, complete credential-safe tracebacks and form responses, contaminated sources, archive tampering, SDK byte mismatches, partial drafts, moved tags and beta/stable releases. Publication tests use fake GitHub responses.

Disposable Home Assistant container checks cover both ZIP layouts, normal enrollment, approved replacement, declined replacement, rejected-code retry, saved-session setup, eligible button creation, correlated release acknowledgement, no replay after acknowledgement loss, pushed permission removal, incoming ring delivery, automatic-refusal sequencing and duplicate suppression, same-version reload and unload. Restart checks preserve config-entry credentials, installation UUIDs and entity IDs without reenrollment or actuation. These scenarios use synthetic loopback accounts, no external network and temporary config directories. Native PyZMQ CURVE has been validated on macOS arm64 and Linux arm64. Generated archives must pass hash-constrained builds and exact source/archive/SDK byte validation.

Protocol implementation follows the Android message contract configured by the SDK. Refer to [protocol notes](docs/PROTOCOL.md) for supported behavior and unknowns. Local fixtures prove message sequencing, transport and adapter behavior against those fixtures. They cannot prove continued vendor compatibility or a physical release at the user's building. Account-specific live enrollment and a supervised physical release remain field checks.

Direct enrollment is plaintext TCP before the CURVE channel. Do not describe that stage as HTTPS or invent a secure fallback. Future stable/Core publication needs an explicit review of this observed onboarding protocol, code expiry, account aliases and simultaneous installation behavior.
