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

No YAML configuration or separate background service is required for release buttons and notifications. Optional video needs a separately running Scrypted server and the IPGUARD companion plugin. Install the generated ZIP rather than copying the development source directory: the source manifest expects the independent SDK package, while the ZIP contains it and uses the published native dependency.

When upgrading, replace the complete `custom_components/ipguard` folder with the new release and restart. Keep Home Assistant's config-entry storage; it contains the saved account authorization.

## Set up your account

Enter the registered email or an international phone number such as `+447700900123` (UK) or `+33612345678` (France). Omit the domestic leading zero after the UK or French country code. Spaces and `00` prefixes are accepted. Use a personal installation label or an Android device name, such as `Kenny's phone` or `Pixel 7`. The label is the Android app's user-entered installation name; it is distinct from the handset model and the board name.

Submitting the account form requests a verification code. Enter all six digits, including leading zeros. Merely opening the form, restarting Home Assistant or reconnecting does not request another code. If the code is rejected, verify the identifier and use the explicit resend action when necessary.

If IPGUARD reports an existing mobile installation, setup shows a separate replacement confirmation. Replacement is optional and defaults to decline. Approving it can disable the previous mobile installation. Cancel if you want to preserve it; the integration never replaces it automatically.

Setup verifies the saved authorization and relay inventory without opening a door. An account with no eligible remote relay can finish setup without buttons. Confirm pending digital badges in the official app and check account permissions there.

The integration sends Android-compatible handset metadata. Its local display name is not sent to IPGUARD as an installation label.

## Incoming call notifications

Each account has an **incoming call** event entity with the doorbell device class, including accounts without remotely releasable relays. It emits `ring` when a valid incoming offer arrives over the account's existing ZeroMQ connection. Receipt alone does not register with Firebase, answer or acknowledge the call, negotiate media, or release a door. With video enabled, the SDK also retains the matching offer temporarily for later live-stream demand.

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

## Incoming call video

Video is optional and requires both a Scrypted server and the separately installed `scrypted-ipguard` companion. Release buttons and doorbell notifications continue working without video. The companion uses the existing HA-owned account connection; do not enroll another IPGUARD installation in Scrypted.

1. Install and start Scrypted using its supported server or Home Assistant OS add-on deployment.
2. Install the IPGUARD companion plugin in Scrypted. For a local development installation, follow that plugin's own build/deployment guide.
3. In **Settings → Devices & services → IPGUARD**, open **Configure** and enable video. If Scrypted runs as a Home Assistant app, leave the URLs, username and password blank: setup finds the running app through HA's installed-app information and pairs through your existing HA access. No network discovery or separate app login is needed.
4. For a separate Scrypted server, enter its administrator login. You can leave its URL blank when the server is already configured in HA's Scrypted integration; otherwise enter its HTTPS address. Leave the HA URL blank to use HA's own address, or override it with an address reachable from Scrypted. Explicit URLs override automatic selection. When multiple servers are found, select one by its URL.
5. Complete pairing. Any supplied Scrypted password is used for that request and is not saved. Pairing exchanges a scoped local credential programmatically and creates an account-level incoming-call camera. Keep certificate verification enabled for direct HTTPS pairing unless you explicitly trust the server's self-signed certificate; the Home Assistant app uses HA's existing app access.

The camera is available only for a current incoming call. Opening its live stream causes the companion to answer the retained offer and receive video. It does not wait for a separate RTSP `PLAY` command or ask for another confirmation. A probe or snapshot client that requests the RTSP stream also counts as demand. Merely receiving a ring, pairing or checking availability does not answer the call; the camera uses a placeholder for thumbnails.

Continuous prebuffering is disabled. Multiple viewers share one receiving peer. Closing the browser may leave HA's media backend connected briefly; that is normal. Provider termination, connection loss and unloading close the receiving session. If a peer has ended, the old offer is not answered again on a retry. A call that expired before viewing cannot be recovered from the restored doorbell event.

The RTSP source is managed by the integration. This first camera uses Home Assistant's standard HLS playback; automatic HA WebRTC gateway selection is disabled for its audited privacy path. No separate Generic Camera, Scrypted HA custom component, Scrypted NVR card or copied SDP is needed. Keep private bridge credentials and RTSP URLs out of screenshots, entity attributes and logs. The plugin exposes video only: it does not provide microphone, audio pickup, refusal/hangup or door-release controls.

One installed companion currently pairs with one HA account/config entry. Unpair it in Scrypted before choosing another account. Normal restart preserves its source; explicit re-pairing rotates the private stream path to invalidate old consumers. Other HA accounts continue using their existing buttons and notifications.

Synthetic validation covers the local signalling and media route. Delivery of real calls, codec/NAT compatibility and acceptance of a delayed answer remain provider checks. Do not interpret an unavailable camera as proof that the intercom is idle or the call failed.

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
- **Video pairing failed:** confirm the companion is installed and the Scrypted app is running. For a separate server, check its HTTPS address, certificate setting and Scrypted login. Scrypted must be able to reach the selected HA address. Use a URL override if automatic selection is unavailable or finds multiple servers.
- **Video unavailable:** a valid live offer and connected companion are required. Old rings cannot recreate a session, and a failed or closed receiver does not automatically answer the old offer again.
- **Unknown release outcome:** there was no matching acknowledgement. Do not assume the command failed or that the door is closed.
- **Dependency installation failed:** verify PyPI access and a supported Python/architecture. The generated bundle includes the SDK but relies on the native PyZMQ dependency.

For an incoming-video problem, open the integration's menu under **Settings → Devices & services → IPGUARD** and choose **Enable debug logging**. Reproduce one fresh ring and explicitly open its live stream. Choose **Disable debug logging** afterwards to download the capture. In **Settings → System → Logs**, select Home Assistant Core and show raw logs to include debug entries.

You can also enable only these loggers immediately through **Developer tools → Actions**:

```yaml
action: logger.set_level
data:
  custom_components.ipguard: debug
  ipguard_api: debug
  custom_components.ipguard._vendor.ipguard_api: debug
```

The video logs show companion connection/disconnection, SDK call states, expiry and the local answer transmission result. `answer outcome=sent` means a local write, not provider acceptance. In Scrypted, open the IPGUARD camera's **Console** for receiving/RTSP progress, or its settings for the latest safe video status. A black thumbnail is the passive placeholder; test an explicit live stream during a new ring.

To keep sanitized protocol logging enabled across restarts, add this to your existing Home Assistant logging configuration:

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
