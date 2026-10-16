# IPGUARD for Home Assistant

[![HACS Integration](https://img.shields.io/badge/HACS-Integration-41BDF5.svg)](https://www.hacs.dev/)
[![Latest release](https://img.shields.io/github/v/release/KennyTangHK/hass-ipguard?include_prereleases&label=release)](https://github.com/KennyTangHK/hass-ipguard/releases)
[![Home Assistant integration](https://img.shields.io/badge/Home%20Assistant-Integration-41BDF5.svg)](https://www.home-assistant.io/)
[![Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Receive incoming intercom calls as Home Assistant doorbell events and open your building's entrance using your existing IPGUARD account. Release buttons and the incoming-call event share one IPGUARD device for each configured account.

> **Beta:** this integration is intended for testing and is not a stable release.

## Disclaimer and license

This is an independent community project. It is not affiliated with, sponsored by, or endorsed by IPGUARD, NACD, or Noralsy, and does not claim Home Assistant Core acceptance or an official quality tier.

Use it lawfully, only with your own authorized account and buildings or doors you have permission to access and control. Do not use it for unauthorized access or abuse.

**Use at your own risk.** To the extent permitted by applicable law, the software is provided without warranty, and its authors and contributors accept no responsibility or liability for its use. The warranty and liability limitations in the [Apache-2.0 license](LICENSE) apply.

## Features

- Incoming call doorbell events over the existing cloud connection.
- One device per account, grouping the incoming-call event and all permitted cloud release buttons.
- Guided setup with phone or email verification and explicit consent before replacing a mobile installation.
- Saved authorization across restarts, automatic reconnection, and availability updates when access changes.
- One transmission per press; uncertain results are never automatically retried.

Incoming calls emit a `ring` event. **This experimental build also attempts to refuse each fresh incoming call automatically** to test whether the provider starts a carrier call sooner. There is no setting to disable the trial in this build. Refusal may cancel the call instead; faster fallback has not been verified. Track the test and the decision to keep or remove this behavior in [TODO #2](https://github.com/KennyTangHK/hass-ipguard/issues/2).

**Video calling and camera streaming are not supported.** Audio conversation, call answering, manual call controls, Firebase, Bluetooth and SIP are also unsupported. Automatic refusal does not answer the call or open a door. Buttons report command acknowledgement; they do not measure whether a door is open or locked.

## Requirements

- A Home Assistant installation compatible with the selected release. HACS checks the minimum requirement during installation.
- An IPGUARD account with remote access to the intended entrance and access to its SMS or email verification codes.
- Internet access, including outbound TCP to `zmq.campiserveur.com` on ports **5579** and **5577**.

## Installation

### HACS (recommended)

Start with [HACS](https://www.hacs.dev/) installed and set up in Home Assistant.

[![Open this repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=KennyTangHK&repository=hass-ipguard)

IPGUARD is listed in HACS as `KennyTangHK/hass-ipguard`. Use the button above or find it through the [HACS dashboard](https://hacs.dev/docs/use/repositories/dashboard/):

1. Open **HACS** and search for **IPGUARD**.
2. Select the integration from **KennyTangHK/hass-ipguard**. To install a beta, enable beta/pre-release versions in its menu or Download/Redownload dialog.
3. Download the integration and **restart Home Assistant**.
4. Go to **Settings → Devices & services → Add integration** and search for **IPGUARD**.

Use HACS to install future updates, then restart Home Assistant; your saved account authorization is retained.

For installation without HACS, follow the [manual installation guide](docs/USAGE.md#manual-installation).

## Account setup

Enter your registered email or an international phone number:

| Country | Example |
| --- | --- |
| United Kingdom | `+447700900123` |
| France | `+33612345678` |

For UK and French numbers, omit the domestic leading zero after the country code. Spaces and `00` prefixes are accepted.

Choose a personal installation name or an Android device name, such as `Pixel 7`, then enter the six-digit verification code. Keep any leading zeros in the code.

If an existing mobile installation is detected, setup asks separately whether to replace it. Replacement defaults to decline; approving it can disable the previous mobile installation. Setup checks access without opening a door.

## Using the integration

Add a release button to a dashboard or call `button.press` in an automation. Button names identify the intercom board and relay. Buttons become unavailable when the cloud connection is lost or release permission is removed. An account without eligible remote relays can finish setup without creating buttons.

Each account also has an **incoming call** doorbell event entity. Use its **Ring** trigger to create notifications or other automations; see the [doorbell automation example](docs/USAGE.md#incoming-call-notifications). Known callers include board and site details. Call delivery with an empty mobile push token still needs confirmation on your account.

Use the official app or your provider's normal phone-call service to answer calls. This integration provides notifications, release buttons and the temporary automatic-refusal experiment; see [unsupported call features](docs/USAGE.md#unsupported-call-features).

If a press reports an unknown outcome, the command may have reached the board without a matching acknowledgement. Check the entrance before choosing to press again.

## Help and documentation

- [Usage and troubleshooting](docs/USAGE.md) — manual installation, logging, updates and removal.
- [Report an issue](https://github.com/KennyTangHK/hass-ipguard/issues) — describe the problem without sharing verification codes or Home Assistant's `.storage` files.
- [Development](DEVELOPMENT.md) — Python setup, tests, packaging and releases.
- [Protocol notes](docs/PROTOCOL.md) — supported behavior and compatibility limits.
- [Python API library](ipguard-api/README.md) — the independently packaged protocol client.
