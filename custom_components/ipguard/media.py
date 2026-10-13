"""Scoped local companion pairing and the private call signalling bridge."""

import asyncio
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

from aiohttp import ClientSession, WSMsgType, encode_basic_auth, web
from aiohttp.streams import StreamReader
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.http import HomeAssistantView
from yarl import URL

from .api import IpguardClient, IpguardError, MediaSnapshot, MediaUnavailableError
from .const import (
    CONF_COMPANION_CREDENTIAL,
    CONF_RTSP_URL,
    CONF_VIDEO_ENABLED,
    DOMAIN,
    SCRYPTED_PLUGIN,
)

BRIDGE_VERSION = 1
_MAX_BODY = 100_000
_MAX_SDP = 65_536
_PAIR_SECONDS = 60
_ACTIVE_STATES = frozenset({"pending", "starting", "answer_sent", "streaming"})
_MANAGER_KEY = "media_manager"


class CompanionError(Exception):
    """A classified local failure with no arbitrary transport error details."""


def server_url(value: object, *, https: bool) -> URL:
    """Validate a server origin without accepting credentials or request paths."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise CompanionError("invalid_url")
    try:
        url = URL(value)
        valid = (
            url.scheme in ({"https"} if https else {"http", "https"})
            and url.host is not None
            and url.host not in {"0.0.0.0", "::"}
            and url.port is not None
            and 0 < url.port <= 65535
            and url.user is None
            and url.password is None
            and url.path in {"", "/"}
            and not url.query_string
            and not url.fragment
            and not any(ord(char) < 33 for char in value)
        )
    except ValueError, TypeError:
        raise CompanionError("invalid_url") from None
    if not valid:
        raise CompanionError("invalid_url")
    return url.with_path("")


def rtsp_url(value: object, expected_host: str) -> str:
    """Accept only the selected companion's network RTSP source."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise CompanionError("invalid_rtsp")
    try:
        url = URL(value)
        valid = (
            url.scheme == "rtsp"
            and url.host == expected_host
            and url.port is not None
            and 0 < url.port <= 65535
            and url.user is None
            and url.password is None
            and url.path not in {"", "/"}
            and not url.query_string
            and not url.fragment
            and not any(ord(char) < 33 for char in value)
        )
    except ValueError, TypeError:
        raise CompanionError("invalid_rtsp") from None
    if not valid:
        raise CompanionError("invalid_rtsp")
    return str(url)


@dataclass(slots=True)
class Pairing:
    """A single-use platform-authenticated pairing transaction."""

    host: str
    ha_url: str
    code: str = field(repr=False)
    credential: str = field(repr=False)
    deadline: float
    source: str | None = field(default=None, repr=False)


@dataclass(slots=True)
class MediaBridge:
    """Keep private media state separate from public entity projections."""

    client: IpguardClient = field(repr=False)
    credential: str = field(repr=False)
    source: str = field(repr=False)
    snapshot: MediaSnapshot = field(repr=False)
    active: bool = True
    revision: int = 0
    connecting: bool = False
    active_claim: tuple[str, str] | None = field(default=None, repr=False)
    unsubscribe_sdk: Callable[[], None] | None = field(default=None, repr=False)
    listeners: list[Callable[[], None]] = field(default_factory=list, repr=False)
    streams: dict[web.WebSocketResponse, asyncio.Queue[dict[str, object]]] = field(
        default_factory=dict, repr=False
    )
    handlers: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    closers: set[asyncio.Task[bool]] = field(default_factory=set, repr=False)

    @property
    def available(self) -> bool:
        return (
            self.active and bool(self.streams) and self.snapshot.state in _ACTIVE_STATES
        )

    def authorized(self, header: str) -> bool:
        return self.active and secrets.compare_digest(
            header.encode(), f"Bearer {self.credential}".encode()
        )

    def state(self) -> dict[str, object]:
        return {
            "version": BRIDGE_VERSION,
            "state": self.snapshot.state if self.active else "disabled",
            "call_id": self.snapshot.call_id if self.active else None,
            "revision": self.revision,
        }

    @callback
    def receive(self, snapshot: MediaSnapshot) -> None:
        if not self.active:
            return
        self.snapshot = snapshot
        if self.active_claim is not None and (
            snapshot.call_id != self.active_claim[0]
            or snapshot.state not in {"starting", "answer_sent", "streaming"}
        ):
            self.active_claim = None
        self.revision += 1
        payload = self.state()
        for queue in self.streams.values():
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(payload)
        for listener in tuple(self.listeners):
            listener()

    @callback
    def connection_changed(self) -> None:
        for listener in tuple(self.listeners):
            listener()

    @callback
    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return unsubscribe

    @callback
    def revoke(self) -> None:
        """Invalidate credentials synchronously before any reload or re-pair."""
        if not self.active:
            return
        self.active = False
        self.client.set_media_enabled(False)
        self.active_claim = None
        for stream in tuple(self.streams):
            task = asyncio.create_task(stream.close())
            self.closers.add(task)
            task.add_done_callback(self.closers.discard)
        for listener in tuple(self.listeners):
            listener()

    async def async_close(self) -> None:
        self.revoke()
        if self.unsubscribe_sdk is not None:
            self.unsubscribe_sdk()
            self.unsubscribe_sdk = None
        await asyncio.gather(
            *(stream.close() for stream in tuple(self.streams)),
            return_exceptions=True,
        )
        await asyncio.gather(*tuple(self.closers), return_exceptions=True)
        current = asyncio.current_task()
        await asyncio.gather(
            *(task for task in tuple(self.handlers) if task is not current),
            return_exceptions=True,
        )
        self.listeners.clear()


@dataclass(slots=True)
class MediaManager:
    """Own loaded bridges and uncommitted pairing transactions."""

    bridges: dict[str, MediaBridge] = field(default_factory=dict, repr=False)
    pairing: dict[str, Pairing] = field(default_factory=dict, repr=False)

    @callback
    def revoke(self, entry_id: str) -> None:
        if (bridge := self.bridges.get(entry_id)) is not None:
            bridge.revoke()

    def create_bridge(
        self, entry: ConfigEntry, client: IpguardClient
    ) -> MediaBridge | None:
        options = entry.options
        credential = options.get(CONF_COMPANION_CREDENTIAL)
        source = options.get(CONF_RTSP_URL)
        if (
            options.get(CONF_VIDEO_ENABLED) is not True
            or not isinstance(credential, str)
            or not credential
            or len(credential) > 128
            or not isinstance(source, str)
            or not source
        ):
            return None
        try:
            host = URL(source).host
            if host is None:
                return None
            source = rtsp_url(source, host)
        except CompanionError, ValueError:
            return None
        client.set_media_enabled(True)
        bridge = MediaBridge(client, credential, source, client.media_snapshot)
        bridge.unsubscribe_sdk = client.subscribe_media(bridge.receive)
        self.bridges[entry.entry_id] = bridge
        return bridge


def manager(hass: HomeAssistant) -> MediaManager:
    data = hass.data.setdefault(DOMAIN, {})
    if _MANAGER_KEY not in data:
        data[_MANAGER_KEY] = MediaManager()
    result: MediaManager = data[_MANAGER_KEY]
    return result


def register_views(hass: HomeAssistant) -> None:
    owner = manager(hass)
    hass.http.register_view(PairView(owner))
    hass.http.register_view(MediaView(owner))
    hass.http.register_view(MediaEventsView(owner))


async def pair_companion(
    hass: HomeAssistant,
    session: ClientSession,
    entry_id: str,
    selected_url: URL,
    ha_url: URL,
    username: str,
    password: str,
    *,
    verify_ssl: bool,
    ingress: tuple[URL, dict[str, str]] | None = None,
) -> tuple[str, str]:
    """Exchange scoped credentials without displaying or copying generated secrets."""
    owner = manager(hass)
    assert selected_url.host is not None
    pairing = Pairing(
        selected_url.host,
        str(ha_url),
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(48),
        monotonic() + _PAIR_SECONDS,
    )
    owner.pairing[entry_id] = pairing
    try:
        endpoint, headers = (
            ingress
            if ingress is not None
            else (
                selected_url / "endpoint" / SCRYPTED_PLUGIN / "pair",
                {"Authorization": encode_basic_auth(username, password)},
            )
        )
        async with asyncio.timeout(_PAIR_SECONDS):
            async with session.post(
                endpoint,
                headers=headers,
                json={
                    "version": BRIDGE_VERSION,
                    "ha_url": str(ha_url),
                    "entry_id": entry_id,
                    "pairing_code": pairing.code,
                    "scrypted_host": selected_url.host,
                },
                ssl=verify_ssl,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise CompanionError("cannot_pair")
                raw = await read_bounded(response.content)
                if raw is None:
                    raise CompanionError("cannot_pair")
                result = json.loads(raw)
                if (
                    not isinstance(result, dict)
                    or result.get("version") != BRIDGE_VERSION
                    or result.get("paired") is not True
                    or pairing.source is None
                ):
                    raise CompanionError("cannot_pair")
        return pairing.credential, pairing.source
    except asyncio.CancelledError:
        raise
    except Exception:
        raise CompanionError("cannot_pair") from None
    finally:
        if owner.pairing.get(entry_id) is pairing:
            owner.pairing.pop(entry_id)


async def read_bounded(reader: StreamReader) -> bytes | None:
    """Read complete fragmented bodies with a fixed byte and time bound."""
    chunks: list[bytes] = []
    size = 0
    async with asyncio.timeout(10):
        while chunk := await reader.read(min(8192, _MAX_BODY + 1 - size)):
            size += len(chunk)
            if size > _MAX_BODY:
                return None
            chunks.append(chunk)
    return b"".join(chunks)


async def read_json(request: web.Request) -> dict[str, Any] | None:
    """Bound even chunked requests before parsing any sensitive fields."""
    try:
        raw = await read_bounded(request.content)
        if raw is None:
            return None
        value = json.loads(raw)
    except ValueError, OSError:
        return None
    return value if isinstance(value, dict) else None


def error_response(category: str, status: int) -> web.Response:
    return web.json_response(
        {"version": BRIDGE_VERSION, "error": category}, status=status
    )


class PairView(HomeAssistantView):
    """Consume an unpredictable one-use nonce delivered through Scrypted auth."""

    url = "/api/ipguard/pair/{entry_id}"
    name = "api:ipguard:pair"
    requires_auth = False

    def __init__(self, owner: MediaManager) -> None:
        self.owner = owner

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        value = await read_json(request)
        pending = self.owner.pairing.get(entry_id)
        if (
            value is None
            or set(value) != {"version", "pairing_code", "rtsp_url"}
            or type(value.get("version")) is not int
            or value["version"] != BRIDGE_VERSION
            or pending is None
            or pending.source is not None
            or pending.deadline <= monotonic()
            or not isinstance(value.get("pairing_code"), str)
            or not secrets.compare_digest(
                value["pairing_code"].encode(), pending.code.encode()
            )
        ):
            return error_response("unauthorized", 401)
        try:
            source = rtsp_url(value["rtsp_url"], pending.host)
        except CompanionError:
            return error_response("invalid_request", 400)
        pending.source = source
        return web.json_response(
            {
                "version": BRIDGE_VERSION,
                "credential": pending.credential,
                "bridge_url": f"{pending.ha_url}/api/ipguard/media/{entry_id}",
            }
        )


class MediaView(HomeAssistantView):
    """Authenticate before exposing offers or performing any call operation."""

    url = "/api/ipguard/media/{entry_id}/{operation}"
    name = "api:ipguard:media"
    requires_auth = False

    def __init__(self, owner: MediaManager) -> None:
        self.owner = owner

    async def post(
        self, request: web.Request, entry_id: str, operation: str
    ) -> web.Response:
        bridge = self.owner.bridges.get(entry_id)
        if bridge is None or not bridge.authorized(
            request.headers.get("Authorization", "")
        ):
            return error_response("unauthorized", 401)
        lifecycle_owner = next(iter(bridge.streams), None)
        value = await read_json(request)
        if not bridge.authorized(request.headers.get("Authorization", "")):
            return error_response("unauthorized", 401)
        if (
            value is None
            or type(value.get("version")) is not int
            or value["version"] != BRIDGE_VERSION
        ):
            return error_response("invalid_request", 400)
        keys = {"version"}
        if operation in {"claim", "answer", "streaming", "end"}:
            keys.add("call_id")
        if operation in {"answer", "streaming", "end"}:
            keys.add("claim_id")
        if operation == "answer":
            keys.add("sdp")
        if (
            operation not in {"state", "claim", "answer", "streaming", "end"}
            or set(value) != keys
            or any(
                not isinstance(value.get(key), str)
                or not value[key]
                or len(value[key]) > (_MAX_SDP if key == "sdp" else 128)
                for key in keys - {"version"}
            )
        ):
            return error_response("invalid_request", 400)
        try:
            if operation == "state":
                return web.json_response(bridge.state())
            if operation == "claim":
                if (
                    lifecycle_owner is None
                    or lifecycle_owner.closed
                    or lifecycle_owner not in bridge.streams
                ):
                    return error_response("unavailable", 409)
                offer = await bridge.client.async_claim_media(value["call_id"])
                if (
                    not bridge.active
                    or lifecycle_owner not in bridge.streams
                    or lifecycle_owner.closed
                ):
                    bridge.client.end_media(offer.call_id, offer.claim_id)
                    return error_response("unavailable", 409)
                bridge.active_claim = (offer.call_id, offer.claim_id)
                return web.json_response(
                    {
                        "version": BRIDGE_VERSION,
                        "call_id": offer.call_id,
                        "claim_id": offer.claim_id,
                        "sdp": offer.sdp,
                    }
                )
            if operation == "answer":
                outcome = await bridge.client.async_submit_media_answer(
                    value["call_id"], value["claim_id"], value["sdp"]
                )
                return web.json_response(
                    {"version": BRIDGE_VERSION, "outcome": outcome}
                )
            if operation == "streaming":
                bridge.client.mark_media_streaming(value["call_id"], value["claim_id"])
            else:
                bridge.client.end_media(value["call_id"], value["claim_id"])
                if bridge.active_claim == (value["call_id"], value["claim_id"]):
                    bridge.active_claim = None
        except MediaUnavailableError:
            return error_response("unavailable", 409)
        except IpguardError:
            return error_response("media_failed", 502)
        except Exception:
            return error_response("media_failed", 502)
        return web.json_response({"version": BRIDGE_VERSION, "ok": True})


class MediaEventsView(HomeAssistantView):
    """Deliver cached lifecycle updates without triggering media demand."""

    url = "/api/ipguard/media/{entry_id}/events"
    name = "api:ipguard:media:events"
    requires_auth = False

    def __init__(self, owner: MediaManager) -> None:
        self.owner = owner

    async def get(self, request: web.Request, entry_id: str) -> web.StreamResponse:
        bridge = self.owner.bridges.get(entry_id)
        if bridge is None or not bridge.authorized(
            request.headers.get("Authorization", "")
        ):
            return error_response("unauthorized", 401)
        if bridge.streams or bridge.connecting:
            return error_response("busy", 409)
        stream = web.WebSocketResponse(timeout=2, heartbeat=20, max_msg_size=1024)
        bridge.connecting = True
        try:
            await stream.prepare(request)
        except Exception:
            return error_response("invalid_request", 400)
        finally:
            bridge.connecting = False
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        bridge.streams[stream] = queue
        bridge.connection_changed()
        if not bridge.active:
            await stream.close()
            bridge.streams.pop(stream, None)
            bridge.connection_changed()
            return stream
        queue.put_nowait(bridge.state())
        handler = asyncio.current_task()
        if handler is not None:
            bridge.handlers.add(handler)

        async def send_updates() -> None:
            try:
                while bridge.active and not stream.closed:
                    await stream.send_json(await queue.get())
            except ConnectionError, RuntimeError:
                return

        sender = asyncio.create_task(send_updates())
        try:
            async for message in stream:
                if message.type not in {WSMsgType.PING, WSMsgType.PONG}:
                    break
        finally:
            sender.cancel()
            owned = bridge.streams.pop(stream, None) is not None
            if owned and not bridge.streams and bridge.active_claim is not None:
                claim, bridge.active_claim = bridge.active_claim, None
                bridge.client.end_media(*claim)
            if handler is not None:
                bridge.handlers.discard(handler)
            bridge.connection_changed()
            closer = asyncio.create_task(stream.close())
            bridge.closers.add(closer)
            closer.add_done_callback(bridge.closers.discard)
            await asyncio.shield(asyncio.gather(sender, closer, return_exceptions=True))
        return stream
