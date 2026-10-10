# Android metadata contract

Compatibility profile for the IPGUARD Android app (`com.noralsy.portaphone`). Exact handset and application version values are defined in [`AndroidProfile`](https://github.com/KennyTangHK/hass-ipguard/blob/master/ipguard-api/src/ipguard_api/models.py); [message construction and validation](https://github.com/KennyTangHK/hass-ipguard/blob/master/ipguard-api/src/ipguard_api/protocol.py) use that profile.

This contract describes the Android app's application messages and HTTP metadata. The integration implements the direct ZeroMQ route. It sends **no HTTP requests to IPGUARD**, so it currently sends no HTTP User-Agent. The HTTP profile below documents the Android app's other routes; it is not a new fallback implementation.

## Direct MQ messages

The Android app uses a DEALER socket with the installation UUID as its routing identity. Its JeroMQ handshake supplies `Socket-Type: DEALER` and `Identity`; there is no User-Agent or client implementation name. Loopback tests verify these peer metadata values for the SDK's plaintext and CURVE connections. Transport negotiation and network fingerprints are not claimed to be byte-identical to Android.

Every application envelope contains `header.event`, `header.sender` and a millisecond `header.timestamp`, plus a fresh opaque `body.token`. Account identifiers, codes, routing identities, keys and incoming acknowledgement tokens remain authorization/protocol data; they are not subject to branding substring filters.

| Request | Android application fields used by the SDK |
| --- | --- |
| Initial `hello` | `type: 0`, `version` from `AndroidProfile.app_version`; data includes `fcm_token`, `without_sip`, `os_version`, `language`, `timezone`, `weektypes`, `connectionInfo` |
| Secure `hello` | Same fields, plus `username`, `phone`, `email`, `akey` and `code` |
| `sp_srv_check_auth` | One selected phone or email identifier and token |
| `sp_srv_validate_auth`, `sp_srv_ask_key` | Selected identifier, six-digit code and token |
| `sp_srv_check_username`, `sp_srv_override_username` | Selected identifier, code, personal installation `username`, handset `model` and token |
| `sp_srv_update_ack` | The incoming opaque update token |
| `sp_brd_open_door` | Token and one-based `door`, with the board identity in its separate routing frame |

The handset profile is a permitted mock Android device: `Pixel 7`, `WiFi` and Java-style locale `en_GB`, with OS and application versions supplied by `AndroidProfile`. Timezone is an offset in seconds, computed from the host timezone as Android does from its device timezone. Empty Firebase token, disabled SIP and seven empty weekday arrays match the app's initial defaults. The integration does not fabricate a Firebase token or copy a real phone's push token.

The installation label is user-entered in Android; it is distinct from `Build.MODEL`. Use a personal name or Android handset name. The SDK rejects altered handset metadata, unexpected outbound fields and implementation labels at the transport boundary. The integration's package version stays local; messages use the Android application profile.

## Android HTTP, OAuth and Socket.IO

The Android app uses **OkHttp**. Its default HTTP header has this form, with the version supplied by the Android HTTP library:

```text
User-Agent: okhttp/<http-library-version>
```

`BridgeInterceptor` inserts that value when no override is set. It also supplies the host, normal HTTP/1.1 `Connection: Keep-Alive`, and `Accept-Encoding: gzip` when appropriate. Request bodies supply their content type and byte content length, or chunked encoding for unknown lengths. HTTP/2 negotiation can change which transport headers are actually sent.

| Android HTTP operation | Observed metadata |
| --- | --- |
| HTTPS `/oauth2/create` | `PUT`; `Content-Type: application/json; charset=utf-8`; body has `platform: "android"`, installation `identity`, and the real Firebase token |
| HTTPS `/oauth2/token`, client credentials | `POST`; same content type; body has `grant_type: "client_credentials"`, `client_id` equal to installation identity, `client_secret` equal to the Firebase token, and `scopes: ["app-1"]` |
| HTTPS `/oauth2/token`, refresh | `POST`; same content type; `grant_type: "refresh_token"`, client identity/token and refresh token |
| Native Socket.IO fallback | Explicit `Authorization: Bearer <access_token>`; path `/waQxY3uRqZFiOQ/`; Engine.IO query `EIO=4&transport=websocket`; default OkHttp User-Agent |
| WebSocket upgrade | `Upgrade: websocket`, `Connection: Upgrade`, random `Sec-WebSocket-Key`, `Sec-WebSocket-Version: 13`, and `Sec-WebSocket-Extensions: permessage-deflate` |

Native OAuth calls use certificate pins. The Socket.IO setup does not explicitly configure that pinner. These paths use no OAuth User-Agent override, custom model header, `Accept-Language`, or project-specific header. React Native support-page requests also use the same OkHttp User-Agent, with their own `Accept: application/json` and form content types; those are separate from door control.

## Compatibility limits

Device-dependent values differ between real handsets. This contract covers application fields, default headers and supported mock values. It does not establish identical TLS/transport fingerprints, current vendor acceptance, or physical door outcomes. Shared direct-enrollment security limitations remain documented in [protocol notes](PROTOCOL.md).
