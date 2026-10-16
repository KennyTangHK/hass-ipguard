# API usage

Construct the asynchronous client inside an active event loop and always close it or use its async context manager. Construction and cached properties do not send messages. Enrollment and release happen only when explicitly called.

## Enrollment

```python
from ipguard_api import ExistingInstallationError, IpguardClient


async def enroll(identifier: str, label: str, read_code):
    async with IpguardClient() as client:
        await client.async_request_verification(identifier, label)
        code = await read_code()
        await client.async_verify_code(code)
        try:
            return await client.async_complete_registration()
        except ExistingInstallationError:
            # Ask the user before explicitly requesting replacement.
            raise
```

Use the account's registered email or international phone number. Phones are normalized to the Android API's `00` format. Codes remain strings of six ASCII digits, including leading zeros. The installation label must be a personal name or Android device name; the handset model is separately fixed to `Pixel 7`.

`async_request_verification` requests an SMS/email and resets challenge credentials while preserving the identity for the same pending account. Calling it again is an explicit resend. `async_verify_code` validates the code and obtains a server public key. `async_complete_registration` performs secure registration, requires fresh configuration and relay inventory, and returns an activated `Session`.

If registration would replace an installation, it raises `ExistingInstallationError`. After explicit consent, call `async_complete_registration(replace_existing=True)`. A connection interruption while awaiting consent must not send another code or replacement command without that call. Enrollment and discovery never test a physical release.

## Saved authorization and updates

Persist `Session.to_dict()` in private storage. It contains credentials; its `repr` omits sensitive fields. Restore it with `Session.from_dict(data)`; restoration alone does not authenticate.

```python
from ipguard_api import IpguardClient, Session


async def observe(saved_data, on_snapshot):
    session = Session.from_dict(saved_data)
    async with IpguardClient(session=session) as client:
        unsubscribe = client.subscribe(on_snapshot)
        try:
            await client.async_connect(session)
            return await client.async_get_relays()
        finally:
            unsubscribe()
```

Normal startup calls `async_connect` without requesting a code or replacing an installation. Keep the context open while consuming cloud pushes. `subscribe` registers a synchronous event-loop callback and returns an unsubscribe function. A `ClientSnapshot` contains cached relays, connection/revocation flags, classified error and current session. Persist changed credentials through explicit serialization if needed. Never block inside a callback.

`client.connected`, `client.session` and `client.snapshot` read cached values. Background recovery uses bounded exponential backoff and the saved session. Revocation stops reconnecting and requires user-driven enrollment. `async_close` closes sockets, cancels recovery and clears callbacks; repeated cleanup is supported.

## Incoming calls

Use `subscribe_calls` before connecting and keep the client open. It registers a synchronous event-loop callback and returns an unsubscribe function:

```python
from ipguard_api import IncomingCall, IpguardClient, Session


async def listen(saved_data, stop):
    def on_call(call: IncomingCall) -> None:
        print(call.board.name if call.board else "Incoming intercom call")

    session = Session.from_dict(saved_data)
    async with IpguardClient(session=session) as client:
        unsubscribe = client.subscribe_calls(on_call)
        try:
            await client.async_connect(session)
            await stop.wait()
        finally:
            unsubscribe()
```

`IncomingCall` contains only an optional `Board` matched by exact identity against the latest cached inventory. Unknown or ambiguous callers still produce a callback with `board=None`. Optional board/site labels and the site identity are omitted if malformed, contain control characters or exceed 256 UTF-8 bytes. Call tokens, routing senders, SDP and media credentials are never exposed in callback data. Media descriptions are validated and discarded. Offers must come from the current authenticated transport for an activated installation; malformed offers are ignored without interrupting relay updates.

Callbacks may arrive before `async_connect` completes, including during reconnect. Callers should briefly buffer sanitized notifications if their own interface is not ready. With the default constructor, receipt does not acknowledge an offer, answer, pick up, hang up or negotiate media. It does not use Firebase. Live server delivery with an empty mobile push token remains a field check.

Deduplication stores at most 256 token digests. Repeated offers refresh a 15-minute idle expiry; the cache survives reconnects for the same installation and clears on explicit close or an installation change. No disconnected call history is fetched or replayed. Incoming payloads require bounded routing identities, an opaque token of at most 1,024 UTF-8 bytes and valid Base64 SDP of at most 256 KiB beginning with `v=0`.

Video calling and camera streaming are not supported. The SDK has no offer-retention, answer or media-session API. It does not send ring acknowledgements or pickup commands.

### Temporary automatic hangup experiment

`IpguardClient(auto_hangup=True)` opts into one immediate `sp_brd_hangup_call` attempt with numeric `cause: 0` after publishing each fresh, validated incoming-call notification. **This may cancel the call, including ordinary telephone fallback. Faster carrier fallback has not been established.** The Home Assistant integration temporarily enables this experiment without a configuration option.

The original token and caller route are retained privately only for the bounded send attempt; the media description is discarded. The active connection and installation are checked again immediately before transmission, and an attempt more than five seconds old is skipped. Busy, stale, expired, disconnected or superseded attempts are skipped. A newer call invalidates an older pending attempt; if its cancellation is still settling, the newer attempt is skipped too rather than queued. Exact correlated provider termination and shutdown cancel pending work. No acknowledgement is awaited, and an uncertain send is not retried. Existing token deduplication also suppresses repeat attempts across reconnects for the same installation.

Sanitized DEBUG messages report `IPGUARD auto hangup outcome=attempted`, `sent`, `uncertain` or `skipped`. `sent` confirms a local transport write only; it does not establish provider acceptance or carrier-call routing. These fixture-tested mechanics need a supervised live comparison before deciding whether to retain the experiment.

## Release one relay

Select a current `Relay` with `remote_allowed=True` and no pending confirmation. Call `await client.async_release(relay.board_identity, relay.number)` only for a deliberate user action. Relay numbers are one-based; the Android app supports 1–3.

The SDK checks current permission and rejects concurrent release for the same relay. It does not queue actions for later connectivity. Success means a matching board acknowledgement, without a physical open/closed claim.

`OutcomeUnknownError` means the command may already have reached the board: report uncertainty and never replay it automatically. `ReleaseRejectedError` reports a matching rejection, `RelayUnavailableError` covers absent/unpermitted relays, `AuthRevokedError` requires enrollment, and `ConnectionFailureError` reports ordinary connectivity failures. All derive from `IpguardError`; authentication failures derive from `AuthenticationError`.

## Transport and logging

Defaults are `tcp://zmq.campiserveur.com:5579` for activation and `tcp://zmq.campiserveur.com:5577` for secure messaging. The constructor accepts test endpoints, `timeout`, `reconnect_delay` and `max_reconnect_delay`. Saved sessions retain their endpoints. Do not disable CURVE authentication to work around handshake failures.

Enable `ipguard_api` at DEBUG for sanitized stage/event information. Outgoing metadata uses Android app values or mock Android handset details from the [handset profile](../src/ipguard_api/models.py), validated by the [protocol implementation](../src/ipguard_api/protocol.py). Raw packets, identifiers, UUIDs, codes, passwords, keys, tokens and arbitrary server text must not be logged.

The supported timeout range is 0.001–300 seconds. Reconnect delays are 0.001–3,600 seconds, with the maximum at least the initial delay. Routing identities are valid UTF-8, at most 255 bytes, without NUL. Service messages are limited to 16 frames and 8 MiB aggregate bytes; inventory updates allow at most 256 sites and 1,024 board entries and fail as a whole when oversized. These bounds do not change saved session serialization.
