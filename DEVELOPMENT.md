# Development

This repository contains the `ha-ipguard` Python package and the `custom_components/ipguard` layout that HACS needs. The `ipguard-api` package is in a separate repository and is imported as `ipguard_api`. It has its own package metadata, Apache-2.0 license, dependencies, tests, builds and PyPI publication workflow. Buttons and notifications do not need a separate local service. Video calls and camera streaming are not supported.

## Environment

Use the interpreter specified in [`.python-version`](.python-version). See [`pyproject.toml`](pyproject.toml) and [`uv.lock`](uv.lock) for runtime and development dependencies. See the [checks workflow](.github/workflows/checks.yml) for the CI tool version. The virtual environment also contains Home Assistant's pinned `uv` dependency. Do not upgrade that package independently.

Clone [the integration repository](https://github.com/KennyTangHK/hass-ipguard). The SDK is installed from [PyPI](https://pypi.org/project/ipguard-api/) at the exact version in `pyproject.toml`. The lockfile records its wheel and source archive hashes. Development, CI and Home Assistant use this same published release. An SDK source checkout is not needed.

For work on the SDK itself, use its separate [development guide](https://github.com/KennyTangHK/ipguard-api/blob/master/DEVELOPMENT.md). Publish SDK changes before updating the integration dependency.

Run integration checks from `hass-ipguard`:

```sh
uv run --no-project --python "$(cat .python-version)" python scripts/prepare_release.py
uv sync --locked
uv run --no-sync python --version
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync pytest
```

`.venv` and `.cache/uv` are local and ignored. These commands run only integration and release-helper tests. The SDK repository runs its own tests before publication. Do not install into a real Home Assistant instance to run checks. SDK transport tests use plaintext/CURVE loopback servers. Home Assistant tests use isolated fixtures and synthetic accounts. Tests must not contact the provider, send a real SMS, replace an installation or release a physical door.

The root `conftest.py` guards this repository's tests. Its automatic fixture patches native ZeroMQ connection and binding methods. It permits only loopback or in-process endpoints. Pytest's Python socket guard cannot intercept libzmq. The additional guard prevents tests from contacting the provider. The SDK has its own test guard. These guards are only for development. They are not included in installed integration or SDK runtime code. The SDK source distribution includes its test guard to isolate its packaged tests.

## Architecture and invariants

The SDK is responsible for identifier validation, challenge state, registration and Android metadata guards. It manages ZeroMQ sockets, response correlation, pushed relay inventories and incoming-call validation and duplicate suppression. It also supplies sanitized callbacks, connection recovery, typed failures and guarded opt-in automatic hangup. Pending enrollment tolerates reset notices and unrelated control envelopes. A revoked activated installation needs reauthentication. The integration manages setup and consent forms, config entries and runtime subscriptions. It supplies one service device per account, release buttons and the account doorbell event. Entity getters use cached data.

Device identifiers use the config-entry ID. This keeps accounts independent, even when they share a physical board. Buttons and the incoming-call event declare the same account device through normal entity setup. New relay pushes attach to it without another connection or enrollment. Saved credentials, entity IDs, custom settings and device grouping must persist across normal current-version reloads and restarts.

Fresh validated incoming calls on the active connection produce sanitized ring notifications. This depends on notification readiness and bounded duplicate suppression. The SDK is passive by default. Home Assistant runtime clients use the saved per-account **Auto hangup** option, which is on by default. Enrollment clients remain passive. Users change the option under **Settings → Devices & services → IPGUARD → Configure**. An option update reloads the entry. When the option is on, clients schedule one immediate hangup with cause zero. They use the validated original call token and caller route. The receive loop does not wait for an acknowledgement. The attempt is intended to start carrier-call fallback sooner. When the option is off, clients still publish ring events. Attempt a hangup only while the call and connection are current. Cancel owned work on disconnect or unload. Do not queue, retry or replay uncertain writes. Do not log token or route fields.

The SDK validates and discards media descriptions. It does not retain them for answering or expose them through an API. Supported features are release buttons, incoming-call notifications and optional auto hangup. Development tests must use only loopback fixtures. Do not place calls to verify provider behavior.

Diagnostics use only fixed categories. Include `ipguard_api` in the manifest logger namespaces so integration debug capture includes the installed SDK. Do not log raw packets, call tokens, SDP, credentials, private URLs or arbitrary provider error text.

Relay numbers start at one and are stable within a board. Buttons are release actions. They do not show a lock state or contact sensor. A release must have current remote permission. It must not wait in a queue for connectivity or be replayed automatically. A timeout after possible delivery has an unknown result. Shutdown and cancelled setup flows must close sockets and stop background tasks.

All human-readable server metadata must use values from the Android app or the agreed mock Android profile. Validate personal installation names before transmission. Do not apply substring filters to identifiers, codes, keys, UUIDs or opaque correlation tokens. Logs must omit raw packets, serialized sessions, identifiers, passwords and arbitrary server-supplied error text.

Import the installed SDK through the integration's `api.py` adapter. The release keeps these imports and the manifest dependency unchanged. Do not copy SDK code into the integration or change `sys.path` or `sys.modules` in runtime code.

## Build an installable release

Use a clean `dist` directory. From `hass-ipguard`:

```sh
uv run --no-project --python "$(cat .python-version)" python scripts/prepare_release.py
uv run --no-sync python -m scripts.sdk_package --output dist
uv build --build-constraints build-constraints.txt --require-hashes
uv run --no-sync python scripts/build_release.py
uv run --no-sync python -m scripts.release_metadata --dist dist --write-checksums
```

The SDK helper downloads the exact wheel and source archive recorded in `uv.lock`. It checks each file's name, size and SHA-256 hash. It then checks the SDK wheel's files and license against the independently published source archive. Both archives must follow their exact file allowlists. The source archive's build pins and hashes must match its own lockfile. SDK sources are inspected as data; they are not executed or rebuilt.

The integration builder produces two archives and their checksums. `dist/ipguard.zip` contains flat integration files for HACS. `dist/ipguard-<version>.zip` contains `custom_components/ipguard` for manual installation. Both retain the normal SDK imports and the exact PyPI requirement. Home Assistant installs that requirement and its dependencies. `release.json` records the SDK version and both published archive hashes. Identical inputs produce identical ZIP contents.

Release preparation derives the integration version from the tag and synchronizes the manifest's SDK pin from `pyproject.toml`. It changes only the integration version in `uv.lock`. It does not change the registry SDK version, dependencies, URLs or hashes. An SDK pin that disagrees with the lockfile stops preparation. The publisher repeats these checks from the tested integration commit before it makes GitHub requests.

For a local tagged build, run `uv run --no-project --python "$(cat .python-version)" python scripts/prepare_release.py --tag "$release_tag"` before the build. Without a tag, preparation keeps the development version. HACS offers generated release archives. Neither ZIP includes SDK source or native binaries. Before adopting an SDK with different dependencies or archive contents, review the SDK validation and release checks.

The checks workflow runs on pull requests and default-branch pushes. The release workflow calls it before publication. HACS and Hassfest validation run alongside local checks. All must pass for a release. Local checks include actionlint, lint, formatting, strict typing, tests, builds and the isolated container release check. Superseded ordinary CI runs are cancelled. Tagged release runs are not cancelled. All runs build and validate release files. Only release calls upload the nine checked publication assets. These include exact copies of the SDK wheel and source distribution published on PyPI. Only the publishing job receives `contents: write`. Normal changes and pull requests receive read-only permissions. Custom packaging does not imply Home Assistant Core approval.

Release helpers are in `scripts/`. Their synthetic tests are in `tests/`. The standard mypy and pytest commands include them. Invoke the metadata and publishing helpers as `python -m scripts.release_metadata` and `python -m scripts.release_publish`. The publisher verifies the SDK pin and registry hashes before preparation, then checks downloaded SDK bytes before it accesses GitHub. The reusable checks reject a release request from a branch before they prepare metadata. After the publisher downloads the checked assets, it prepares metadata and validates their bytes once before it makes GitHub requests.

## Publish a release

Use `v<major>.<minor>.<patch>` tags. The integration version follows PEP 440 and Home Assistant/HACS's AwesomeVersion. These tags produce normal GitHub releases and do not need prerelease selection in HACS. Keep the beta notices in the README, HACS display name and release notes while the software is in beta. A normal version number does not change the project's maturity.

Publication uses GitHub's automatic date/version policy for Latest. It does not force every maintenance release to become Latest.

1. Review implementation changes. Run the applicable checks in each repository. Integration releases do not need manual edits to the integration package version or manifest version.
2. If an SDK update is needed, first publish it from the SDK repository. Update the exact dependency in `pyproject.toml`, then run `uv lock`. Review the new SDK version, URLs and hashes, and any dependency changes. Run `python scripts/prepare_release.py` to synchronize the manifest, then `uv sync --locked` and the checks. An integration-only release keeps the SDK pin and hashes.
3. Commit and push the changes to the default branch.
4. Select and push the new version tag. The **Publish release** workflow prepares metadata from that tag. It runs all checks, builds and publishes the assets automatically:

```sh
release_tag="v<major>.<minor>.<patch>"
git push origin master
git tag "$release_tag"
git push origin "$release_tag"
```

The workflow generates integration versions and SDK pins. It then checks consistency with the tag, independent SDK requirements and the exact tested commit. It finds existing drafts through authenticated release-list pagination. If the list has no match, it uses a read-only GraphQL lookup. It uses one release ID for creation, reads, uploads and publication. For a new draft, it uses the ID that GitHub returns. It does not require immediate list visibility. Requests retry transient read failures within bounded waits and respect server retry delays. They wait briefly for uploaded asset metadata. An uncertain creation response is reconciled without a second creation request. See the [GitHub release API](https://docs.github.com/en/rest/releases/releases#create-a-release) and [GitHub retry guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api).

The publisher reuses existing assets when their uploaded state, size and SHA-256 digest match. It deletes only expected draft assets that need replacement. Unexpected or duplicate assets stop publication. Before publication, it checks all nine final assets and the remote tag. It sets the GitHub prerelease and Latest flags from the version metadata. Writes recheck the pinned release's draft state. A failure before the final publication attempt leaves the draft unpublished. If the response to that attempt is lost, the publisher reads the release and rechecks all asset bytes and the tag. If it cannot confirm the result, it reports uncertainty. It does not overwrite published releases. The integration workflow does not need a manual ZIP upload or personal access token. It does not publish to PyPI. SDK PyPI publication belongs to the SDK repository.

### Retry a failed publication

Open **Actions → Publish release → the failed tagged run → Re-run all jobs**. The existing tag must still point to the tested commit. The workflow resumes a valid partial draft. It refuses a published release.

A rerun uses the original tagged commit and its workflow files. If the fix changes code or workflows, commit and push the fix. Then publish a new version tag. Keep existing release tags on their original commits.

The workflow also supports a manual dispatch against an existing tag:

```sh
release_tag="<existing-release-tag>"
gh workflow run release.yml --repo KennyTangHK/hass-ipguard --ref "$release_tag"
gh run watch --repo KennyTangHK/hass-ipguard
```

Run against the tag. A dispatch from a branch is rejected. Manual dispatch needs the workflow on the default branch and in the selected tagged revision. For older tagged runs, use **Re-run all jobs**. See [GitHub manual workflow runs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow) and the [GitHub CLI workflow reference](https://cli.github.com/manual/gh_workflow_run).

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

The local tag remains until you remove it with `git tag -d "$release_tag"`. Deleting a release removes its downloadable assets. It does not uninstall users' existing copies or revert repository commits. Use a new version to ship a correction. See [GitHub release management](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository) and the [GitHub CLI deletion reference](https://cli.github.com/manual/gh_release_delete).

For the next integration version, commit the implementation changes and select a new version tag. The workflow generates release metadata automatically. Adopt SDK updates through the pinned PyPI dependency and reviewed lockfile hashes. Update documentation when behavior or installation steps change. Invalid tags, inconsistent dependency pins, unverified registry hashes and altered artifacts cause failure before publication. Do not move an existing release tag. After a transient failure, rerun the same tag. Use a new tag for source or workflow fixes.

HACS catalogue inclusion is pending for `KennyTangHK/hass-ipguard`. Keep the repository public. Maintain its description, topics and release assets. Catalogue submission needs passing HACS and Hassfest checks and a published GitHub release. HACS maintainers review the listing request. The HACS validator reads the exact pushed GitHub revision, so it cannot fully validate unpushed local changes. See the [HACS inclusion guide](https://www.hacs.xyz/docs/publish/include/).

To repeat the Linux release check with Docker:

```sh
docker build -f scripts/Dockerfile.smoke -t ipguard-ha-smoke scripts
docker run --rm --network none \
  -v "$PWD/dist:/releases:ro" -v "$PWD/scripts:/checks:ro" \
  --entrypoint python ipguard-ha-smoke \
  /checks/smoke_release.py /releases/ipguard.zip
```

The image uses the official Home Assistant digest pinned in [`scripts/Dockerfile.smoke`](scripts/Dockerfile.smoke). It installs the native PyZMQ requirement. The check extracts the ZIP into temporary storage. It installs the verified SDK wheel from the mounted release files with network access disabled. It checks the installed SDK bytes and import path, then starts Home Assistant in that temporary directory. It exercises SDK enrollment and release against loopback plaintext/CURVE routers. The runtime has no external network. It mounts no credentials or real configuration directory. The check removes all temporary containers. Repeat the command with `--registration replace` and `--registration decline` after the ZIP argument. This checks both explicit replacement choices.

## Evidence and verification limits

Runtime checks validate permissions at transmission time and reject busy release writes. They limit malformed inputs and resources. They suppress exception chains that contain credentials. Source distributions, wheels and ZIPs use reviewed exact file selections. Unknown source files and altered archive payloads cause validation to fail. The published SDK wheel and source archive must match the registry hashes in `uv.lock`. The wheel files and license must also match the independent source archive. The integration build and published SDK source each retain complete build dependency pins and hashes checked against their own lockfiles.

The CI restart check starts the current archive in two separate processes with the same temporary configuration directory. It preserves config-entry data, installation UUID, account device and button and event entity IDs. The second process reconnects with saved authorization. It does not request a verification code, replace an installation or release a door. Run `scripts/smoke_release.py <archive> --restart` to repeat the check. Both processes communicate only with loopback fixtures.

Integration checks cover Home Assistant behavior, packaging, release metadata and publication. The SDK repository checks its API and transport behavior separately. Before you release changes to a project, run its Ruff lint and format checks, strict mypy and complete pytest suite. Also run workflow actionlint. Regression tests cover failure and cancellation cleanup, authorization changes before transmission, and credential-safe tracebacks and form responses. They also cover contaminated sources, archive tampering, SDK byte mismatches, partial drafts, moved tags and prerelease or normal release flags. Publication tests use fake GitHub responses.

Disposable Home Assistant container checks cover both ZIP layouts, normal enrollment and approved or declined replacement. They cover rejected-code retry, saved-session setup, eligible button creation and correlated release acknowledgement. They check that acknowledgement loss does not cause replay. They also check pushed permission removal, incoming ring delivery, auto hangup sequence and duplicate suppression, same-version reload and unload. Restart checks preserve config-entry credentials, installation UUIDs and entity IDs without reenrollment or actuation. These scenarios use synthetic loopback accounts, temporary config directories and native PyZMQ CURVE transport. They have no external network access. Generated archives must pass hash-constrained builds and exact source, archive and SDK byte validation.

Local fixtures validate message sequence, transport and adapter behavior against those fixtures. They cannot prove continued provider acceptance, incoming-call delivery, carrier routing or a physical release. Development validation must remain synthetic. A live account or physical check needs explicit user direction.

Direct enrollment, including retrieval of the server public key, uses plaintext TCP before the CURVE channel. Do not describe that stage as HTTPS or invent a secure fallback.
