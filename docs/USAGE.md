# Installation and usage

## Requirements

Use a Home Assistant installation that is compatible with the selected release. HACS checks the minimum requirement during installation. You must have an active IPGUARD account with access to the building. You need the registered phone number or email address and access to the SMS or email verification codes. This integration cannot give you access to more buildings or bypass relay permissions.

Home Assistant needs outbound TCP access to `zmq.campiserveur.com` on port **5579** for enrollment and port **5577** for the authenticated cloud connection. Enrollment uses unencrypted TCP. ZeroMQ CURVE then protects the authenticated connection. Enrollment does not use HTTPS.

Home Assistant installs the required `ipguard-api` package and its dependencies from PyPI. The first startup needs internet access to the package index. The release ZIP is not a fully offline installation bundle.

## Install with HACS

Follow the [HACS installation instructions in the main README](../README.md#installation). Until catalogue inclusion is complete, add IPGUARD as a custom repository first. Download a release, restart Home Assistant, then add IPGUARD through Devices & services.

Use HACS to download updates. Your account authorization stays in Home Assistant's config-entry storage. Each account has one IPGUARD device that contains its release buttons and incoming-call event.

## Manual installation

1. Get `ipguard-<version>.zip` from the selected [GitHub release](https://github.com/KennyTangHK/hass-ipguard/releases) or from a local build's `dist` folder. This is the manual archive. `ipguard.zip` is for HACS.
2. Extract the archive into your Home Assistant configuration directory, usually `/config`. Make sure that the result is `/config/custom_components/ipguard/manifest.json`. Do not put a second `custom_components` directory inside the first.
3. Restart Home Assistant.
4. Open **Settings → Devices & services → Add integration**, then select **IPGUARD**.

You do not need YAML configuration or a separate background service. Install the generated ZIP. Home Assistant installs the required SDK from PyPI. Do not copy the development source directory for installation.

To update the integration, replace the complete `custom_components/ipguard` folder with the new release. Then restart Home Assistant. Keep Home Assistant's config-entry storage. It contains the saved account authorization.

## Set up your account

Enter your registered email address or an international phone number. The values `person@example.invalid` and `+447700900123` (UK) are examples. Use your own account details during setup. For UK and French numbers, omit the domestic leading zero after the country code. You can use spaces or a `00` prefix.

Enter a personal installation name or an Android device name, such as `Alex's phone` or `Pixel 7`. This name is the installation label that a user enters in the Android app. It is separate from the handset model and board name.

When you submit the account form, the integration requests a verification code. Enter all six digits, including leading zeros. Opening the form, restarting Home Assistant or reconnecting does not request another code. If the code is rejected, check the identifier. Use the explicit resend action if necessary.

If IPGUARD reports an existing mobile installation, setup shows a separate replacement confirmation. Replacement is optional and the default is to decline. Replacement can disable the previous mobile installation. Cancel if you want to keep it. The integration does not replace it automatically.

Setup checks the saved authorization and relay inventory without opening a door. An account with no eligible remote relay can complete setup without buttons. In the official app, confirm pending digital badges and check account permissions.

## Incoming call notifications

Each account has an **incoming call** event entity with the doorbell device class. This includes accounts without remotely releasable relays. When a valid incoming call arrives over the account's cloud connection, the entity emits `ring`. When **Auto hangup** is on, the integration then tries to hang up the internet call. This is intended to start carrier-call fallback sooner. The ring event works with either setting. The integration does not register with Firebase, answer the call, negotiate media or release a door.

Use the entity's **Ring** trigger in an automation. In this example, replace the entity ID with the ID shown in **Settings → Devices & services → IPGUARD → Entities**:

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

The event state is the timestamp of the last ring. It is not an on/off ringing status. If the caller matches the latest cached inventory, the attributes include `board_id`, `board_name`, `site_id` and `site_name`. Unknown callers also cause a ring, but without these details. Call tokens and media credentials are not exposed. The integration suppresses repeated offers. When Home Assistant restores the last timestamp after a restart, it does not cause a new notification.

The entity becomes unavailable when the connection is lost or authorization is revoked. Calls during a disconnection cannot be recovered. Delivery depends on the provider routing calls to the active connection. It can vary between accounts.

## Auto hangup

**Auto hangup** is on by default for each account. After the integration publishes the incoming-call notification, it immediately tries once to hang up the internet call. This is intended to start the provider's carrier-call fallback sooner. You answer any fallback call as a normal phone call. The provider and account determine if and when fallback occurs. In one supervised call, a user confirmed notification receipt and carrier fallback for one account. This does not guarantee the same result for other accounts.

To change the setting, go to **Settings → Devices & services → IPGUARD → Configure**. Turn it off to keep ring notifications and let the internet call continue in the official app. The setting is saved per account. It takes effect after the integration reloads.

A fresh validated call causes at most one immediate hangup attempt. Repeated offers are suppressed for a limited time. The integration skips the hangup if the call is no longer current or the command cannot be sent immediately. Disconnection or removal cancels pending work. The integration does not automatically retry or replay an uncertain write.

## Unsupported call features

Video calls and camera streaming are not supported. The integration has no camera entity. It does not provide Firebase push delivery, Bluetooth access or SIP calling.

The integration does not answer calls, negotiate media or provide audio conversation. It does not provide manual refusal or hangup controls. Release buttons, incoming-call notifications and auto hangup do not need a separate background service.

## Release buttons

Each permitted remote relay has a button on the account's IPGUARD device, together with the incoming-call event. Button names include the intercom board and relay. This lets you identify each door when an account has more than one board. Separate accounts have separate devices. Use **button.press** in the interface or an automation. Select the button entity for the door that you want to release. You can rename entities in Home Assistant without changing their board or relay identity.

Buttons become unavailable when the cloud connection is lost, the installation is revoked, the relay disappears or remote release permission is removed. Bluetooth-only relays and relays that need confirmation do not have buttons. An available button does not prove that the physical intercom is online.

A matching acknowledgement confirms that the board acknowledged the command. It does not prove that the door moved or stayed open. If an acknowledgement times out after possible transmission, the result is unknown. The integration does not automatically retry a release command. Check the entrance before you press again.

## Troubleshooting

- **Cannot connect:** Check DNS and outbound TCP ports 5579 and 5577. Home Assistant retries the cloud connection with saved authorization. A network failure does not send another verification code.
- **Invalid code or authorization:** Enter the code as text, including leading zeros. If an activated installation is revoked, authenticate again through Devices & services.
- **Existing installation:** On the confirmation screen, decide if you want to replace the previous mobile installation.
- **No buttons:** In the official app, check badge confirmation and remote access permissions. Bluetooth-only access does not create a cloud button.
- **No incoming notifications:** Check that the doorbell entity is available and that the automation uses its Ring trigger. Calls depend on the active cloud connection. There is no Firebase fallback.
- **Internet calls stop before you can answer:** If you want to answer in the official app, turn off **Auto hangup** in the account's **Configure** menu.
- **Unknown release outcome:** There was no matching acknowledgement. Do not assume that the command failed or that the door is closed.
- **Dependency installation failed:** Check PyPI access and use a supported Python version and architecture. Home Assistant must install the SDK and its native PyZMQ dependency.

For connection, release or incoming-notification problems, open the integration's menu under **Settings → Devices & services → IPGUARD**. Select **Enable debug logging**. Reproduce the problem, then select **Disable debug logging** to download the capture. In **Settings → System → Logs**, select Home Assistant Core and show raw logs to include debug entries.

You can also enable only these loggers immediately through **Developer tools → Actions**:

```yaml
action: logger.set_level
data:
  custom_components.ipguard: debug
  ipguard_api: debug
```

To keep sanitized protocol logging on across restarts, add this to your existing Home Assistant logging configuration:

```yaml
logger:
  logs:
    custom_components.ipguard: debug
    ipguard_api: debug
```

Logs describe protocol stages and connection categories. Auto-hangup DEBUG messages use the fixed outcomes `attempted`, `sent`, `uncertain` and `skipped`. `sent` means that the local transport accepted the write. It is not a carrier-call delivery receipt. Do not share Home Assistant `.storage` files or serialized SDK sessions. They contain the account identifier and authorization credentials. The integration stores necessary authorization in config-entry data. This is not an encrypted credential vault.

## Remove

Delete the IPGUARD integration entry from **Settings → Devices & services**. This stops its connection and removes its Home Assistant configuration. It does not revoke the installation at the provider or restore a previously replaced installation. If necessary, manage mobile installations through the official app or service. After you delete all entries, remove `custom_components/ipguard` and restart Home Assistant.
