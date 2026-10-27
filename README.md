# IPGUARD for Home Assistant

[![HACS Integration](https://img.shields.io/badge/HACS-Integration-41BDF5.svg)](https://www.hacs.dev/)
[![Latest release](https://img.shields.io/github/v/release/KennyTangHK/hass-ipguard?include_prereleases&label=release)](https://github.com/KennyTangHK/hass-ipguard/releases)
[![Home Assistant integration](https://img.shields.io/badge/Home%20Assistant-Integration-41BDF5.svg)](https://www.home-assistant.io/)
[![Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Use your IPGUARD account to receive intercom calls as Home Assistant doorbell events. You can also release an entrance door. Each account has one IPGUARD device that contains its release buttons and incoming-call event.

> **Beta:** This integration is for testing. It is not a stable release.

## Installation

### HACS (recommended)

First, install and set up [HACS](https://www.hacs.dev/) in Home Assistant. HACS catalogue inclusion is pending, so add IPGUARD as a custom repository before installation:

1. In **HACS**, open the three-dot menu and select **Custom repositories**.
2. Enter `https://github.com/KennyTangHK/hass-ipguard`, select **Integration**, and click **Add**.
3. Search HACS for **IPGUARD**, select **IPGUARD (Beta)**, and download the integration.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration** and search for **IPGUARD**.

Home Assistant installs the required Python library from PyPI automatically. Use HACS to install updates. Restart Home Assistant after each update. Your account authorization stays in Home Assistant's config-entry storage.

For installation without HACS, see [Manual installation](docs/USAGE.md#manual-installation).

## Features

- Receive incoming calls as doorbell events over the cloud connection.
- Use **Auto hangup** to try to start carrier-call fallback sooner. This option is on by default.
- Use one device per account for the incoming-call event and permitted cloud release buttons.
- Set up an account with phone or email verification. The integration asks for your consent before it replaces a mobile installation.
- Keep account authorization across restarts. The integration reconnects and updates availability when access changes.
- Send at most one release command for each button press. The integration rejects unavailable or busy actions. It does not retry an uncertain command automatically.

An incoming call causes a `ring` event. When **Auto hangup** is on, the integration publishes the event and immediately tries once to hang up the internet call. This is intended to start carrier-call fallback sooner. The provider and account determine if and when fallback occurs. To change the option for an account, go to **Settings → Devices & services → IPGUARD → Configure**. When the option is off, the integration still sends incoming-call notifications.

Video calls and camera streaming are not supported. Audio conversation, call answering, manual call controls, Firebase, Bluetooth and SIP are also not supported. See [Unsupported call features](docs/USAGE.md#unsupported-call-features). Auto hangup does not answer a call or open a door. A button reports a command acknowledgement; it does not show the physical door state.

## Requirements

- A Home Assistant installation that is compatible with the selected release. HACS checks the minimum requirement during installation.
- An IPGUARD account with remote access to the entrance and access to its SMS or email verification codes.
- Internet access, including outbound TCP access to `zmq.campiserveur.com` on ports **5579** and **5577**.

## Account setup

Enter your registered email address or an international phone number. These values are examples:

| Account identifier | Example |
| --- | --- |
| UK phone number | `+447700900123` |
| Email address | `person@example.invalid` |

For UK and French numbers, omit the domestic leading zero after the country code. You can use spaces or a `00` prefix.

Enter a personal installation name or an Android device name, such as `Pixel 7`. Enter the six-digit verification code. Include any leading zeros.

If the service finds an existing mobile installation, it asks separately if you want to replace it. The default is to decline. Replacement can disable the previous mobile installation. Setup checks access without opening a door.

## Using the integration

Add a release button to a dashboard or use `button.press` in an automation. Button names identify the intercom board and relay. Buttons become unavailable if the cloud connection is lost or release permission is removed. An account can complete setup without buttons if it has no eligible remote relays.

Each account also has an **incoming call** doorbell event entity. Use its **Ring** trigger for notifications or other automations. See the [doorbell automation example](docs/USAGE.md#incoming-call-notifications). For known callers, the event includes board and site details.

When **Auto hangup** is on, you answer any fallback call through your provider's normal phone-call service. Turn the option off if you want to answer the internet call in the official app. See [Auto hangup](docs/USAGE.md#auto-hangup).

If a press has an unknown outcome, the command may have reached the board without a matching acknowledgement. Check the entrance before you press again.

## Help and documentation

- [Usage and troubleshooting](docs/USAGE.md): Manual installation, logging, updates and removal.
- [Report an issue](https://github.com/KennyTangHK/hass-ipguard/issues): Describe the problem. Do not share verification codes or Home Assistant `.storage` files.
- [Development](DEVELOPMENT.md): Python setup, tests, packaging and releases.
- [Python API library](https://github.com/KennyTangHK/ipguard-api).

## Disclaimer and license

This is an independent community project. It is not affiliated with, sponsored by or endorsed by IPGUARD, NACD or Noralsy. It does not claim Home Assistant Core acceptance or an official quality tier.

Use it only with your own authorized account and buildings or doors that you have permission to access and control. Obey applicable laws.

**Use at your own risk.** To the extent permitted by applicable law, the software is provided without warranty. Its authors and contributors accept no responsibility or liability for its use. The warranty and liability limits in the [Apache-2.0 license](LICENSE) apply.
