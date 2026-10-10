# Upgrade baseline

`ipguard-upgrade-baseline.zip` is a frozen integration bundle retained from an earlier published beta. It is used only by the offline Home Assistant upgrade/restart check with synthetic accounts and temporary configuration data.

The checksum pinned in [`scripts/smoke_release.py`](../../scripts/smoke_release.py) must match before the archive is extracted or executed. Keeping the fixture in the checkout lets the check run after older GitHub releases or tags are removed. It is excluded from wheels, source distributions and publication assets.

When replacing the baseline, review the complete archive, update the fixture and checksum together, and run the upgrade/restart check. The integration and bundled SDK licenses are retained inside the archive.
