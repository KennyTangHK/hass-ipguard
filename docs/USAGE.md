# Installation and usage

## Requirements

Use a Home Assistant installation compatible with the selected release; HACS checks its minimum requirement during installation. An active IPGUARD account must already have access to the intended building. You need its registered phone number or email address and access to SMS/email verification codes. This integration cannot grant access to additional buildings or bypass relay permissions.

Home Assistant needs outbound TCP access to `zmq.campiserveur.com` on ports **5579** for enrollment and **5577** for the authenticated cloud connection. The inspected Android app enrolls over ordinary TCP, then uses ZeroMQ CURVE for the authenticated connection. Enrollment is not protected by HTTPS. This is the app's observed direct protocol.

The release ZIP contains the Python SDK. Home Assistant installs the required native PyZMQ dependency from PyPI if necessary, so the first startup needs internet access to the package index. It is not a fully offline installation bundle.

## Install with HACS

Follow the [HACS installation guide in the main README](../README.md#installation). Search for IPGUARD in the HACS catalogue or use the Open in HACS button, download a release, restart Home Assistant, then add IPGUARD through Devices & services.

Use HACS to download later releases and updates. Your saved account authorization remains in Home Assistant's config-entry storage.

## Manual installation

1. Obtain `ipguard-<version>.zip` from the selected [GitHub release](https://github.com/KennyTangHK/hass-ipguard/releases) or a local build's `dist` folder. This is the manual archive; `ipguard.zip` is formatted for HACS.
2. Extract it into your Home Assistant configuration directory, commonly `/config`. The result must be `/config/custom_components/ipguard/manifest.json`; avoid a second `custom_components` directory nested inside the first.
3. Restart Home Assistant.
4. Open **Settings → Devices & services → Add integration**, then choose **IPGUARD**.

No YAML configuration or separate background service is required. Install the generated ZIP rather than copying the development source directory: the source manifest expects the independent SDK package, while the ZIP contains it and uses the published native dependency.

When upgrading, replace the complete `custom_components/ipguard` folder with the new release and restart. Keep Home Assistant's config-entry storage; it contains the saved account authorization.

## Set up your account

Enter the registered email or an international phone number such as `+447700900123` (UK) or `+33612345678` (France). Omit the domestic leading zero after the UK or French country code. Spaces and `00` prefixes are accepted. Use a personal installation label or an Android device name, such as `Kenny's phone` or `Pixel 7`. The label is the Android app's user-entered installation name; it is distinct from the handset model and the board name.

Submitting the account form requests a verification code. Enter all six digits, including leading zeros. Merely opening the form, restarting Home Assistant or reconnecting does not request another code. If the code is rejected, verify the identifier and use the explicit resend action when necessary.

If IPGUARD reports an existing mobile installation, setup shows a separate replacement confirmation. Replacement is optional and defaults to decline. Approving it can disable the previous mobile installation. Cancel if you want to preserve it; the integration never replaces it automatically.

Setup verifies the saved authorization and relay inventory without opening a door. An account with no eligible remote relay can finish setup without buttons. Confirm pending digital badges in the official app and check account permissions there.

The integration sends Android-compatible handset metadata. Its local display name is not sent to IPGUARD as an installation label.

## Incoming call notifications

Each account has an **incoming call** event entity with the doorbell device class, including accounts without remotely releasable relays. It emits `ring` when a valid incoming offer arrives over the account's existing ZeroMQ connection. It does not register with Firebase, answer or acknowledge the call, negotiate media, or release a door.

Use the entity's **Ring** trigger in an automation. For example, replace the entity ID below with the one shown in **Settings → Devices & services → IPGUARD → Entities**:

```yaml
alias: IPGUARD incoming call
triggers:
  - trigger: event.received
    target:
      entity_id: event.your_account_incoming_call
    options:
      event_type:
        - ring
actions:
  - action: persistent_notification.create
    data:
      title: Intercom
      message: Someone is calling from the intercom.
```

The event state is the last ring's timestamp, rather than a ringing on/off status. When the caller matches the latest cached inventory, attributes include `board_id`, `board_name`, `site_id` and `site_name`; unknown callers still ring without those details. Call tokens and media credentials are not exposed. Repeated offers are suppressed. Restoring the last timestamp after restart does not trigger a new notification.

The entity becomes unavailable when disconnected or authorization is revoked. Calls while disconnected cannot be recovered. Calls received while the current secure connection is getting ready are buffered briefly in memory and discarded on connection failure or unload. Live delivery with the integration's empty mobile push token has not yet been verified for a real account.

## Release buttons

Each permitted remote relay appears as a button attached to its intercom board. Use **button.press** in the interface or an automation. You can rename entities in Home Assistant without changing their board/relay identity.

Buttons become unavailable when the cloud connection is lost, the installation is revoked, the relay disappears or remote release permission is removed. Bluetooth-only and pending-confirmation relays are omitted. Availability does not prove that the physical intercom is online.

A matching acknowledgement confirms that the board acknowledged the command. It does not prove that the door moved or remained open. If acknowledgement times out after possible transmission, the result is unknown. The integration never automatically retries a release command. Inspect the entrance before choosing to press again.

## Troubleshooting

- **Cannot connect:** check DNS and outbound TCP ports 5579/5577. Home Assistant retries cloud connectivity using saved authorization. A network failure does not send another verification code.
- **Invalid code or authorization:** enter the code as text, preserving leading zeros. A revoked activated installation requires explicit reauthentication through Devices & services.
- **Existing installation:** decide on the dedicated confirmation screen whether replacing the prior mobile installation is acceptable.
- **No buttons:** check the official app's badge confirmation and remote access permissions. Access that only works over Bluetooth does not create a cloud button.
- **No incoming notifications:** check that the doorbell entity is available and the automation uses its Ring trigger. A local test does not establish provider delivery for your account; no Firebase fallback is included.
- **Unknown release outcome:** there was no matching acknowledgement. Do not assume the command failed or that the door is closed.
- **Dependency installation failed:** verify PyPI access and a supported Python/architecture. The generated bundle includes the SDK but relies on the native PyZMQ dependency.

To enable sanitized protocol logs temporarily, add this to your existing Home Assistant logging configuration and restart:

```yaml
logger:
  logs:
    custom_components.ipguard: debug
    ipguard_api: debug
    custom_components.ipguard._vendor.ipguard_api: debug
```

Logs describe protocol stages and connection categories. Do not share Home Assistant's `.storage` files or serialized SDK sessions: they contain the account identifier and authorization credentials. This integration stores necessary authorization in config-entry data, which is not an encrypted credential vault.

## Remove

Delete the IPGUARD integration entry from **Settings → Devices & services**. That stops its connection and removes its Home Assistant configuration. It does not revoke the installation at the provider or restore an installation previously replaced. Manage mobile installations through the official app or service if needed. After deleting all entries, remove `custom_components/ipguard` and restart Home Assistant.
