"""Message shapes established by the IPGUARD 3.4.8 Android package."""

import json
import time
from collections.abc import Mapping
from datetime import datetime
from uuid import uuid4

from .errors import InvalidInputError, ProtocolError
from .models import AndroidProfile, AuthIdentifier, Board, Relay, Session, SessionStatus
from .validation import (
    validate_identifier,
    validate_installation_label,
    validate_routing_identity,
    validate_verification_code,
)

type Packet = dict[str, object]
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_FRAMES = 16
MAX_INVENTORY_SITES = 256
MAX_INVENTORY_BOARDS = 1024


def envelope(
    identity: str, event: str, body: Mapping[str, object], token: str | None = None
) -> Packet:
    return {
        "header": {
            "event": event,
            "sender": identity,
            "timestamp": time.time_ns() // 1_000_000,
        },
        "body": {**body, "token": str(uuid4()) if token is None else token},
    }


def hello(identity: str, identifier: AuthIdentifier, session: Session | None) -> Packet:
    # Fixed handset metadata also replaces obsolete saved implementation labels.
    profile = AndroidProfile()
    offset = datetime.now().astimezone().utcoffset()
    data: dict[str, object] = {
        "fcm_token": "",
        "without_sip": True,
        "os_version": profile.os_version,
        "language": profile.language,
        "timezone": int(offset.total_seconds()) if offset else 0,
        "weektypes": [[], [], [], [], [], [], []],
        "connectionInfo": profile.connection_info,
    }
    if session is not None:
        label = validate_installation_label(session.label)
        data.update(
            {
                "username": label if session.status is SessionStatus.ACTIVATED else "",
                "phone": identifier.value if identifier.kind.value == "phone" else "",
                "email": identifier.value if identifier.kind.value == "email" else "",
                "akey": "",
                "code": validate_verification_code(session.verification_code),
            }
        )
    return envelope(
        identity, "hello", {"type": 0, "version": profile.app_version, "data": data}
    )


def identifier_fields(identifier: AuthIdentifier) -> dict[str, object]:
    return {identifier.kind.value: identifier.value}


def packet_event(packet: Packet) -> str:
    header = packet.get("header")
    return str(header.get("event", "")) if isinstance(header, dict) else ""


def packet_body(packet: Packet) -> dict[str, object]:
    body = packet.get("body")
    return body if isinstance(body, dict) else {}


def packet_sender(packet: Packet) -> object:
    header = packet.get("header")
    return header.get("sender") if isinstance(header, dict) else None


def assert_android_metadata(packet: Packet) -> None:
    """Allow only the observed envelope and implemented Android request shapes."""
    header, body, profile = packet.get("header"), packet.get("body"), AndroidProfile()
    if (
        set(packet) != {"header", "body"}
        or not isinstance(header, dict)
        or set(header) != {"event", "sender", "timestamp"}
        or not isinstance(body, dict)
        or not isinstance(header.get("event"), str)
        or not isinstance(header.get("sender"), str)
        or type(header.get("timestamp")) is not int
        or header["timestamp"] < 0
        or not isinstance(body.get("token"), str)
        or not body["token"]
    ):
        raise InvalidInputError("Unsupported Android message envelope.")
    validate_routing_identity(header["sender"])
    event = header["event"]
    if event == "hello":
        data = body.get("data")
        basic = {
            "fcm_token",
            "without_sip",
            "os_version",
            "language",
            "timezone",
            "weektypes",
            "connectionInfo",
        }
        authenticated = {"username", "phone", "email", "akey", "code"}
        if (
            set(body) != {"type", "version", "data", "token"}
            or type(body.get("type")) is not int
            or body["type"] != 0
            or not isinstance(data, dict)
            or set(data) not in (basic, basic | authenticated)
            or body.get("version") != profile.app_version
        ):
            raise InvalidInputError("Incompatible Android greeting metadata.")
        expected = {
            "os_version": profile.os_version,
            "language": profile.language,
            "connectionInfo": profile.connection_info,
            "fcm_token": "",
            "without_sip": True,
            "weektypes": [[], [], [], [], [], [], []],
        }
        if (
            any(data.get(key) != value for key, value in expected.items())
            or data.get("without_sip") is not True
            or type(data.get("timezone")) is not int
            or not -86400 <= data["timezone"] <= 86400
        ):
            raise InvalidInputError("Incompatible Android greeting metadata.")
        if set(data) == basic | authenticated:
            if (
                not all(isinstance(data[key], str) for key in authenticated)
                or data["akey"] != ""
                or bool(data["phone"]) == bool(data["email"])
            ):
                raise InvalidInputError("Incompatible Android greeting metadata.")
            if data["username"]:
                validate_installation_label(data["username"])
            validate_verification_code(data["code"])
            _assert_identifier_fields(
                {key: data[key] for key in ("phone", "email") if data[key]}
            )
        return
    allowed = {
        "sp_srv_check_auth": {"token"},
        "sp_srv_validate_auth": {"token", "code"},
        "sp_srv_ask_key": {"token", "code"},
        "sp_srv_check_username": {"token", "code", "username", "model"},
        "sp_srv_override_username": {"token", "code", "username", "model"},
        "sp_srv_update_ack": {"token"},
        "sp_brd_open_door": {"token", "door"},
    }
    expected_keys = allowed.get(event)
    if expected_keys is None:
        raise InvalidInputError("Unsupported Android message event.")
    if event.startswith("sp_srv_") and event != "sp_srv_update_ack":
        identifiers = {key: body[key] for key in ("phone", "email") if key in body}
        _assert_identifier_fields(identifiers)
        expected_keys = expected_keys | set(identifiers)
    if set(body) != expected_keys:
        raise InvalidInputError("Unsupported Android message fields.")
    if "code" in body:
        validate_verification_code(body["code"])
    if event in ("sp_srv_check_username", "sp_srv_override_username"):
        label = body.get("username")
        if not isinstance(label, str) or body.get("model") != profile.model:
            raise InvalidInputError("Incompatible Android registration metadata.")
        validate_installation_label(label)
    if event == "sp_brd_open_door" and (
        type(body["door"]) is not int or not 1 <= body["door"] <= 3
    ):
        raise InvalidInputError("Unsupported Android relay number.")


def _assert_identifier_fields(fields: Mapping[str, object]) -> None:
    if len(fields) != 1:
        raise InvalidInputError("Unsupported Android account fields.")
    kind, value = next(iter(fields.items()))
    if not isinstance(value, str):
        raise InvalidInputError("Unsupported Android account fields.")
    identifier = validate_identifier(value)
    if identifier.kind.value != kind or identifier.value != value:
        raise InvalidInputError("Unsupported Android account fields.")


def decode_frames(frames: list[bytes]) -> Packet | None:
    if len(frames) > MAX_MESSAGE_FRAMES or sum(map(len, frames)) > MAX_MESSAGE_BYTES:
        raise ProtocolError("Service message exceeds supported bounds.")
    for frame in reversed(frames):
        try:
            value = json.loads(frame)
            if isinstance(value, str):
                value = json.loads(value)
        except UnicodeDecodeError, ValueError:
            continue
        except RecursionError:
            raise ProtocolError(
                "Service message nesting exceeds supported bounds."
            ) from None
        if not isinstance(value, dict):
            continue
        if value.get("action") == "register":
            continue
        header, body = value.get("header"), value.get("body")
        if (
            isinstance(header, dict)
            and isinstance(header.get("event"), str)
            and isinstance(body, dict)
        ):
            return value
    # SIP action/register controls and unrelated/noisy frames are not auth replies.
    return None


def _decoded(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError, RecursionError:
            raise ProtocolError("Invalid service data encoding.") from None
    return value


def configuration_password(packet: Packet) -> str | None:
    data = _decoded(packet_body(packet).get("data"))
    item: object = None
    if isinstance(data, list):
        item = data[1] if len(data) > 1 else None
    elif isinstance(data, dict):
        item = data.get("1")
    elif data is not None:
        raise ProtocolError("Unsupported account configuration shape.")
    password = item.get("sip_pwd") if isinstance(item, dict) else None
    return password if isinstance(password, str) and password else None


def relay_inventory(body: Mapping[str, object]) -> tuple[Relay, ...]:
    source = _decoded(body.get("boards") if "boards" in body else body.get("sites"))
    if isinstance(source, list):
        sites: dict[str, object] = {"0": {"name": "", "boards": source}}
    elif isinstance(source, dict):
        sites = source
    else:
        raise ProtocolError(
            "Door inventory has no supported boards or sites structure."
        )
    relays: list[Relay] = []
    seen: set[tuple[str, int]] = set()
    if len(sites) > MAX_INVENTORY_SITES:
        raise ProtocolError("Door inventory exceeds supported bounds.")
    board_count = 0
    for site_id, site in sites.items():
        if not isinstance(site, dict) or not isinstance(site.get("boards"), list):
            continue
        board_count += len(site["boards"])
        if board_count > MAX_INVENTORY_BOARDS:
            raise ProtocolError("Door inventory exceeds supported bounds.")
        for item in site["boards"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("identity"), str)
                or not item["identity"]
            ):
                continue
            try:
                validate_routing_identity(item["identity"])
            except InvalidInputError:
                raise ProtocolError("Invalid service board identity.") from None
            board = Board(
                identity=item["identity"],
                name=_name(item.get("name")),
                site_id=str(site_id),
                site_name=_name(site.get("name")),
                device_id=_scalar(item.get("device_id")),
                device_type=_scalar(item.get("device_type")),
            )
            access, names = item.get("relay_access_mobile_v2"), item.get("relay_name")
            if not isinstance(access, list):
                continue
            names = names if isinstance(names, list) else []
            pending = bool(item.get("waitConfirmBadgeDigital"))
            for index, permission in enumerate(access[:3]):
                if not isinstance(permission, dict):
                    continue
                identity = (board.identity, index + 1)
                if identity in seen:
                    raise ProtocolError("Ambiguous board relay inventory.")
                seen.add(identity)
                kind = _scalar(permission.get("type"))
                relays.append(
                    Relay(
                        board=board,
                        number=index + 1,
                        name=_name(names[index])
                        if index < len(names)
                        else f"Relay {index + 1}",
                        remote_allowed=not pending and kind in (0, 2, "0", "2"),
                        pending_confirmation=pending,
                        access_type=kind,
                        access_mode=_scalar(permission.get("mode")),
                    )
                )
    return tuple(relays)


def _name(value: object) -> str:
    return value if isinstance(value, str) else ""


def _scalar(value: object) -> int | str | None:
    return (
        value if isinstance(value, (int, str)) and not isinstance(value, bool) else None
    )


def validate_endpoint(endpoint: str) -> None:
    from urllib.parse import urlsplit

    try:
        value = urlsplit(endpoint)
        valid = (
            value.scheme == "tcp"
            and value.hostname
            and value.port
            and not value.path
            and not value.query
            and not value.fragment
            and not value.username
        )
    except ValueError:
        valid = False
    if not valid:
        raise InvalidInputError("Expected a ZeroMQ TCP service endpoint.")
