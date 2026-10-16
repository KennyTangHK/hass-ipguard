"""Authorized enrollment, pushed relay discovery and single-attempt release."""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import wraps
from struct import error as StructError
from time import monotonic
from types import TracebackType
from typing import Concatenate
from uuid import UUID, uuid4

from zmq.utils import z85

from .errors import (
    AuthenticationError,
    AuthRevokedError,
    ConnectionFailureError,
    ExistingInstallationError,
    InvalidCodeError,
    InvalidInputError,
    IpguardError,
    MediaUnavailableError,
    OutcomeUnknownError,
    RelayUnavailableError,
    ReleaseRejectedError,
)
from .models import (
    DEFAULT_ACTIVATION_ENDPOINT,
    DEFAULT_SECURE_ENDPOINT,
    AndroidProfile,
    AuthIdentifier,
    Board,
    ClientSnapshot,
    IncomingCall,
    MediaOffer,
    MediaSnapshot,
    Relay,
    Session,
    SessionStatus,
)
from .protocol import (
    Packet,
    _CallOffer,
    configuration_password,
    door_inventory,
    envelope,
    hello,
    identifier_fields,
    media_answer,
    packet_body,
    packet_event,
    packet_sender,
    parse_call_offer,
)
from .transport import ZmqTransport
from .validation import (
    validate_duration,
    validate_identifier,
    validate_installation_label,
    validate_routing_identity,
    validate_verification_code,
)

type Listener = Callable[[ClientSnapshot], None]
type CallListener = Callable[[IncomingCall], None]
type MediaListener = Callable[[MediaSnapshot], None]
MAX_RECENT_CALLS = 256
RECENT_CALL_TTL = 15 * 60
MEDIA_OFFER_TTL = 30.0
MEDIA_SETUP_TTL = 120.0
MEDIA_MAX_LIFETIME = 10 * 60.0
_CALL_EVENTS = frozenset(
    {"ps_sp_sdp_offer", "ps_sp_hangup_call", "brd_sp_hangup_call", "ps_sp_ring_ack"}
)
_LOGGER = logging.getLogger(__name__)
_LOGGED_EVENTS = frozenset(
    {
        "srv_sp_delete",
        "bc_sp_delete_app",
        "bc_sp_relays",
        "srv_sp_update",
        "srv_sp_check_auth_result",
        "srv_sp_validate_auth_result",
        "srv_sp_ask_key_result",
        "srv_sp_check_username_result",
        "brd_sp_open_door_ack",
    }
)


@dataclass(slots=True, repr=False)
class _MediaCall:
    offer: _CallOffer
    transport: ZmqTransport
    call_id: str
    expires_at: float
    maximum_deadline: float
    state: str = "pending"
    claim_id: str | None = None


def _cancel_cleanup[**P, R](
    method: Callable[Concatenate[IpguardClient, P], Awaitable[R]],
) -> Callable[Concatenate[IpguardClient, P], Awaitable[R]]:
    @wraps(method)
    async def wrapped(client: IpguardClient, /, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await method(client, *args, **kwargs)
        except asyncio.CancelledError:
            await client.async_close()
            raise

    return wrapped


class IpguardClient:
    """Maintain one installation's cloud connection; getters perform no IO."""

    def __init__(
        self,
        timeout: float = 10.0,
        *,
        session: Session | None = None,
        activation_endpoint: str = DEFAULT_ACTIVATION_ENDPOINT,
        secure_endpoint: str = DEFAULT_SECURE_ENDPOINT,
        reconnect_delay: float = 1.0,
        max_reconnect_delay: float = 30.0,
    ) -> None:
        self.timeout = validate_duration(timeout, 300)
        reconnect_delay = validate_duration(reconnect_delay, 3600)
        max_reconnect_delay = validate_duration(max_reconnect_delay, 3600)
        if max_reconnect_delay < reconnect_delay:
            raise InvalidInputError(
                "The maximum reconnect delay must not be shorter than the initial delay."
            )
        self._session = (
            replace(session, android_profile=AndroidProfile()) if session else None
        )
        self._activation_endpoint = (
            session.activation_endpoint if session else activation_endpoint
        )
        self._secure_endpoint = session.secure_endpoint if session else secure_endpoint
        self._reconnect_delay, self._max_reconnect_delay = (
            reconnect_delay,
            max_reconnect_delay,
        )
        self._transport: ZmqTransport | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._listeners: set[Listener] = set()
        self._call_listeners: set[CallListener] = set()
        self._call_tokens: OrderedDict[bytes, float] = OrderedDict()
        self._call_boards: dict[str, Board] = {}
        self._media_enabled = False
        self._media_call: _MediaCall | None = None
        self._media_snapshot = MediaSnapshot("disabled")
        self._media_listeners: set[MediaListener] = set()
        self._media_expiry: asyncio.TimerHandle | None = None
        self._media_answer_lock = asyncio.Lock()
        self._media_answer_outcomes: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._relays: tuple[Relay, ...] = ()
        self._connected = False
        self._revoked = False
        self._error: IpguardError | None = None
        self._closed = False
        self._inventory_ready = asyncio.Event()
        self._configuration_ready = asyncio.Event()
        self._failure_ready = asyncio.Event()
        self._identifier: AuthIdentifier | None = None
        self._identity = ""
        self._label = ""
        self._verification_requested = False
        self._release_locks: dict[tuple[str, int], asyncio.Lock] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def session(self) -> Session | None:
        return self._session

    @property
    def snapshot(self) -> ClientSnapshot:
        return ClientSnapshot(
            self._relays, self._connected, self._revoked, self._error, self._session
        )

    def subscribe(self, callback: Listener) -> Callable[[], None]:
        """Register a synchronous event-loop callback; return its removal function."""
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)

    def subscribe_calls(self, callback: CallListener) -> Callable[[], None]:
        """Subscribe to sanitized incoming offers; no call response is sent.

        Callbacks run synchronously on the event loop. Notifications are not
        replayed to later subscribers. The removal function is idempotent.
        Duplicate tokens are remembered across reconnects for 15 minutes after
        their last receipt, subject to a limit of 256 recent tokens.
        """
        self._call_listeners.add(callback)
        return lambda: self._call_listeners.discard(callback)

    @property
    def media_snapshot(self) -> MediaSnapshot:
        """Read cached preview state without starting or answering a call."""
        self._expire_media()
        return self._media_snapshot

    def set_media_enabled(self, enabled: bool) -> None:
        """Opt into retaining future offers; enabling never restores old offers."""
        if not isinstance(enabled, bool):
            raise InvalidInputError("Media enablement must be a boolean.")
        if enabled == self._media_enabled:
            return
        self._media_enabled = enabled
        self._finish_media()
        self._media_snapshot = MediaSnapshot("idle" if enabled else "disabled")
        self._notify_media()

    def subscribe_media(self, callback: MediaListener) -> Callable[[], None]:
        """Subscribe to state changes; inspect the snapshot for current state."""
        self._media_listeners.add(callback)
        return lambda: self._media_listeners.discard(callback)

    def _notify_media(self) -> None:
        state = self._media_snapshot.state
        if state in {
            "disabled",
            "idle",
            "pending",
            "starting",
            "answer_sent",
            "streaming",
            "ended",
        }:
            _LOGGER.debug("IPGUARD video state=%s", state)
        for callback in tuple(self._media_listeners):
            try:
                callback(self._media_snapshot)
            except Exception:
                continue

    def _publish_media(self, call: _MediaCall) -> None:
        self._media_snapshot = MediaSnapshot(
            call.state, call.call_id, call.offer.notification.board
        )
        self._notify_media()

    def _finish_media(self) -> None:
        if self._media_expiry is not None:
            self._media_expiry.cancel()
            self._media_expiry = None
        call, self._media_call = self._media_call, None
        if call is not None:
            self._media_snapshot = MediaSnapshot(
                "ended" if self._media_enabled else "disabled",
                call.call_id if self._media_enabled else None,
                call.offer.notification.board if self._media_enabled else None,
            )
            self._notify_media()

    def _expire_media(self) -> None:
        if self._media_call is not None and self._media_call.expires_at <= monotonic():
            _LOGGER.debug("IPGUARD video call expired")
            self._finish_media()

    def _schedule_media_expiry(self, call: _MediaCall) -> None:
        if self._media_expiry is not None:
            self._media_expiry.cancel()
        self._media_expiry = asyncio.get_running_loop().call_later(
            max(0, call.expires_at - monotonic()), self._expire_media
        )

    def _current_media(self, call_id: str, claim_id: str | None = None) -> _MediaCall:
        self._expire_media()
        call = self._media_call
        if (
            not self._media_enabled
            or call is None
            or call.call_id != call_id
            or (claim_id is not None and call.claim_id != claim_id)
            or not self._connected
            or not self._call_transport_current(call.transport)
        ):
            raise MediaUnavailableError("The media call is no longer available.")
        return call

    async def async_claim_media(self, call_id: str) -> MediaOffer:
        """Claim one pending call on explicit media demand, without any send."""
        call = self._current_media(call_id)
        if call.state != "pending":
            raise MediaUnavailableError("The media call is already claimed.")
        call.state, call.claim_id = "starting", str(uuid4())
        call.expires_at = min(monotonic() + MEDIA_SETUP_TTL, call.maximum_deadline)
        self._schedule_media_expiry(call)
        self._publish_media(call)
        return MediaOffer(call.call_id, call.claim_id, call.offer.sdp, call.expires_at)

    def _remember_media_answer(self, call_id: str, claim_id: str, result: str) -> None:
        self._media_answer_outcomes[(call_id, claim_id)] = result
        if result in {"sent", "uncertain"}:
            _LOGGER.debug("IPGUARD video answer outcome=%s", result)
        if len(self._media_answer_outcomes) > MAX_RECENT_CALLS:
            self._media_answer_outcomes.popitem(last=False)

    async def async_submit_media_answer(
        self, call_id: str, claim_id: str, answer_sdp: str
    ) -> str:
        """Attempt this claim's answer once; uncertainty is never retransmitted."""
        async with self._media_answer_lock:
            previous = self._media_answer_outcomes.get((call_id, claim_id))
            if previous is not None:
                return previous
            call = self._current_media(call_id, claim_id)
            if call.state != "starting":
                raise MediaUnavailableError("The media call cannot accept an answer.")
            packet = media_answer(
                self._identity, call.offer.remote, call.offer.token, answer_sdp
            )
            attempted = False

            def guard() -> None:
                nonlocal attempted
                if (
                    self._current_media(call_id, claim_id) is not call
                    or call.state != "starting"
                ):
                    raise MediaUnavailableError(
                        "The media call is no longer available."
                    )
                attempted = True
                _LOGGER.debug("IPGUARD video answer transmission started")

            try:
                await call.transport.async_send(
                    packet, call.offer.sender, before_send=guard
                )
            except asyncio.CancelledError:
                if attempted:
                    self._remember_media_answer(call_id, claim_id, "uncertain")
                if self._media_call is call:
                    self._finish_media()
                raise
            except Exception:
                if attempted:
                    self._remember_media_answer(call_id, claim_id, "uncertain")
                    if self._media_call is call:
                        self._finish_media()
                    return "uncertain"
                _LOGGER.debug("IPGUARD video answer rejected before transmission")
                raise MediaUnavailableError("The media answer was not sent.") from None
            self._remember_media_answer(call_id, claim_id, "sent")
            if self._media_call is call:
                call.state = "answer_sent"
                call.expires_at = call.maximum_deadline
                self._schedule_media_expiry(call)
                self._publish_media(call)
            return "sent"

    def mark_media_streaming(self, call_id: str, claim_id: str) -> None:
        """Record verified local receiver readiness, without provider commands."""
        call = self._current_media(call_id, claim_id)
        if call.state not in {"answer_sent", "streaming"}:
            raise MediaUnavailableError("The media answer has not been sent.")
        if call.state != "streaming":
            call.state = "streaming"
            self._publish_media(call)

    def end_media(self, call_id: str, claim_id: str) -> None:
        """End a receiver claim locally; never send a provider hangup."""
        call = self._media_call
        if call is not None and call.call_id == call_id and call.claim_id == claim_id:
            _LOGGER.debug("IPGUARD video receiver closed locally")
            self._finish_media()

    def _notify(self) -> None:
        snapshot = self.snapshot
        for callback in tuple(self._listeners):
            try:
                callback(snapshot)
            except Exception:
                continue

    @_cancel_cleanup
    async def async_request_verification(self, identifier: str, label: str) -> None:
        auth = validate_identifier(identifier)
        label = validate_installation_label(label)
        await self._stop_connection()
        old = self._session
        previous_identifier, previous_identity = self._identifier, self._identity
        same_account = (
            old is not None
            and old.identifier.kind == auth.kind
            and old.identifier.value.casefold() == auth.value.casefold()
        )
        same_pending = (
            previous_identifier is not None
            and previous_identifier.kind == auth.kind
            and previous_identifier.value.casefold() == auth.value.casefold()
        )
        self._identity = (
            old.identity
            if same_account and old
            else (
                previous_identity
                if same_pending and previous_identity
                else str(uuid4())
            )
        )
        if self._identity != (old.identity if old else previous_identity):
            self._call_tokens.clear()
        self._call_boards.clear()
        self._identifier, self._label = auth, label
        self._session = None
        self._revoked, self._closed, self._error = False, False, None
        self._verification_requested = False
        self._relays = ()
        _LOGGER.debug("IPGUARD stage verification_request started")
        transport = await self._new_transport(secure=False)
        await transport.async_send(hello(self._identity, auth, None))
        await self._auth_request(
            "sp_srv_check_auth", identifier_fields(auth), "srv_sp_check_auth_result"
        )
        self._verification_requested = True

    @_cancel_cleanup
    async def async_verify_code(self, code: str) -> None:
        code = validate_verification_code(code)
        if not self._verification_requested or self._identifier is None:
            raise AuthenticationError(
                "Request a verification code before verifying it."
            )
        if (
            self._transport is None
            or self._transport.closed
            or self._transport.server_key is not None
        ):
            transport = await self._new_transport(secure=False)
            await transport.async_send(hello(self._identity, self._identifier, None))
        fields = {**identifier_fields(self._identifier), "code": code}
        _LOGGER.debug("IPGUARD stage verify_code started")
        await self._auth_request(
            "sp_srv_validate_auth", fields, "srv_sp_validate_auth_result"
        )
        body = await self._auth_request(
            "sp_srv_ask_key", fields, "srv_sp_ask_key_result"
        )
        key = body.get("key")
        if not isinstance(key, str) or not _valid_curve_key(key):
            raise AuthenticationError("IPGUARD returned an invalid server public key.")
        self._session = Session(
            identity=self._identity,
            identifier=self._identifier,
            label=self._label,
            verification_code=code,
            server_public_key=key,
            activation_endpoint=self._activation_endpoint,
            secure_endpoint=self._secure_endpoint,
            status=SessionStatus.PENDING,
        )

    @_cancel_cleanup
    async def async_complete_registration(
        self, *, replace_existing: bool = False
    ) -> Session:
        session = self._session
        if session is None or session.status is not SessionStatus.PENDING:
            raise AuthenticationError("Verify a code before completing registration.")
        if not isinstance(replace_existing, bool):
            raise InvalidInputError("Replacement consent must be an explicit boolean.")
        if (
            self._transport is None
            or self._transport.closed
            or self._transport.server_key is None
        ):
            transport = await self._new_transport(secure=True)
            await transport.async_send(
                hello(session.identity, session.identifier, session)
            )
        fields = {
            **identifier_fields(session.identifier),
            "code": session.verification_code,
            "username": validate_installation_label(session.label),
            "model": AndroidProfile().model,
        }
        _LOGGER.debug("IPGUARD stage registration started")
        result = await self._register("sp_srv_check_username", fields)
        if isinstance(result, int) and not isinstance(result, bool) and result == 0:
            if not replace_existing:
                raise ExistingInstallationError(
                    "Registration requires consent to replace an existing mobile installation."
                )
            result = await self._register("sp_srv_override_username", fields)
        if (
            not isinstance(result, int)
            or isinstance(result, bool)
            or result not in (1, 2)
        ):
            raise AuthenticationError("IPGUARD rejected installation registration.")
        await self._wait_ready(self._configuration_ready, "activation configuration")
        await self._wait_ready(self._inventory_ready, "relay inventory")
        assert self._session is not None
        self._session = replace(self._session, status=SessionStatus.ACTIVATED)
        self._connected, self._error = True, None
        self._notify()
        return self._session

    @_cancel_cleanup
    async def async_connect(self, session: Session) -> None:
        validate_installation_label(session.label)
        validate_verification_code(session.verification_code)
        auth = validate_identifier(session.identifier.value)
        try:
            UUID(session.identity)
        except ValueError, AttributeError:
            raise InvalidInputError("Invalid saved installation identity.") from None
        if (
            auth != session.identifier
            or not _valid_curve_key(session.server_public_key)
            or not session.sip_password
        ):
            raise AuthenticationError("Saved installation authorization is incomplete.")
        if session.status is SessionStatus.DISABLED:
            raise AuthRevokedError(
                "This installation requires a new user-driven enrollment."
            )
        if session.status is not SessionStatus.ACTIVATED:
            raise AuthenticationError("Saved installation enrollment is incomplete.")
        await self._stop_connection()
        if self._session is None or self._session.identity != session.identity:
            self._call_tokens.clear()
        self._call_boards.clear()
        self._closed, self._revoked, self._error = False, False, None
        self._session = replace(session, android_profile=AndroidProfile())
        self._identity, self._identifier, self._label = (
            session.identity,
            auth,
            session.label,
        )
        self._activation_endpoint, self._secure_endpoint = (
            session.activation_endpoint,
            session.secure_endpoint,
        )
        await self._connect_saved()

    async def _connect_saved(self) -> None:
        assert self._session is not None
        transport = await self._new_transport(secure=True)
        await transport.async_send(
            hello(self._session.identity, self._session.identifier, self._session)
        )
        try:
            await self._wait_ready(self._inventory_ready, "relay inventory")
        except IpguardError:
            self._finish_media()
            raise
        if self._revoked:
            raise AuthRevokedError("IPGUARD revoked this installation.")
        self._connected, self._error = True, None
        self._notify()

    async def async_get_relays(self) -> tuple[Relay, ...]:
        self._require_connected()
        return self._relays

    async def async_release(self, board_identity: str, relay: int) -> None:
        validate_routing_identity(board_identity)
        if (
            not isinstance(board_identity, str)
            or not board_identity
            or isinstance(relay, bool)
            or not isinstance(relay, int)
            or not 1 <= relay <= 3
        ):
            raise InvalidInputError(
                "Select a board identity and a one-based relay number from 1 to 3."
            )
        self._require_release_permission(board_identity, relay)
        lock = self._release_locks.setdefault((board_identity, relay), asyncio.Lock())
        if lock.locked():
            raise RelayUnavailableError(
                "A release request is already in progress for this relay."
            )
        try:
            async with lock:
                self._require_release_permission(board_identity, relay)
                transport, session = self._transport, self._session
                assert transport is not None and session is not None
                packet = envelope(session.identity, "sp_brd_open_door", {"door": relay})
                token = packet_body(packet)["token"]
                attempted = False

                def before_send() -> None:
                    nonlocal attempted
                    self._require_release_permission(board_identity, relay)
                    if self._transport is not transport:
                        raise ConnectionFailureError(
                            "The connection changed; no release was sent."
                        )
                    attempted = True

                _LOGGER.debug("IPGUARD stage release started")
                try:
                    reply = await transport.async_request(
                        packet,
                        lambda p: (
                            packet_event(p) == "brd_sp_open_door_ack"
                            and packet_body(p).get("token") == token
                            and packet_sender(p) == board_identity
                        ),
                        board_identity,
                        before_send=before_send,
                    )
                except ConnectionFailureError:
                    if not attempted:
                        raise
                    raise OutcomeUnknownError(
                        "Release outcome is unknown; the command was not retried."
                    ) from None
                body = packet_body(reply)
                if "result" in body and (
                    isinstance(body["result"], bool)
                    or not isinstance(body["result"], int)
                    or body["result"] != 1
                ):
                    raise ReleaseRejectedError(
                        "The board rejected the release request."
                    )
                status = body.get("ble_frame_status")
                if status and (
                    not isinstance(status, str) or not status.startswith("_teOK_")
                ):
                    raise ReleaseRejectedError(
                        "The board reported an unsuccessful release acknowledgement."
                    )
        finally:
            if self._release_locks.get((board_identity, relay)) is lock:
                self._release_locks.pop((board_identity, relay))

    def _require_release_permission(self, board_identity: str, relay: int) -> None:
        self._require_connected()
        selected = next(
            (
                item
                for item in self._relays
                if item.board_identity == board_identity and item.number == relay
            ),
            None,
        )
        if (
            selected is None
            or not selected.remote_allowed
            or selected.pending_confirmation
        ):
            raise RelayUnavailableError(
                "This relay is not currently permitted for remote release."
            )

    def _require_connected(self) -> None:
        if self._revoked:
            raise AuthRevokedError("IPGUARD revoked this activated installation.")
        if not self._connected or self._transport is None:
            raise ConnectionFailureError(
                "IPGUARD is not connected; no release was attempted."
            )

    async def _auth_request(
        self, event: str, fields: dict[str, object], response: str
    ) -> dict[str, object]:
        if self._transport is None:
            raise ConnectionFailureError("IPGUARD is not connected.")
        packet = envelope(self._identity, event, fields)
        token = packet_body(packet)["token"]
        reply = await self._transport.async_request(
            packet,
            lambda p: (
                packet_event(p) == response and packet_body(p).get("token") == token
            ),
        )
        body = packet_body(reply)
        result = body.get("result")
        if isinstance(result, bool) or not isinstance(result, int) or result != 1:
            if (
                event == "sp_srv_validate_auth"
                and isinstance(result, int)
                and not isinstance(result, bool)
                and result == 0
            ):
                raise InvalidCodeError("IPGUARD rejected the verification code.")
            raise AuthenticationError("IPGUARD rejected the authorization request.")
        return body

    async def _register(self, event: str, fields: dict[str, object]) -> object:
        assert self._transport is not None
        packet = envelope(self._identity, event, fields)
        token = packet_body(packet)["token"]
        reply = await self._transport.async_request(
            packet,
            lambda p: (
                (
                    packet_event(p) == "srv_sp_check_username_result"
                    and packet_body(p).get("token") == token
                )
                or (
                    packet_event(p) == "srv_sp_update"
                    and self._configuration_ready.is_set()
                )
            ),
        )
        return (
            2
            if packet_event(reply) == "srv_sp_update"
            else packet_body(reply).get("result")
        )

    async def _new_transport(self, *, secure: bool) -> ZmqTransport:
        self._finish_media()
        if self._transport is not None:
            await self._transport.async_close()
        self._connected = False
        self._inventory_ready.clear()
        self._error = None
        self._failure_ready.clear()
        self._configuration_ready.clear()
        key = self._session.server_public_key if secure and self._session else None

        async def receive(packet: Packet) -> None:
            if packet_event(packet) in _CALL_EVENTS:
                self._receive_call(packet, transport)
            else:
                await self._on_packet(packet)

        transport = ZmqTransport(
            self._identity,
            self._secure_endpoint if secure else self._activation_endpoint,
            self.timeout,
            receive,
            self._on_failure,
            key,
        )
        self._transport = transport
        await transport.async_start()
        return transport

    def _call_transport_current(self, transport: ZmqTransport) -> bool:
        session = self._session
        if (
            self._closed
            or self._revoked
            or self._error is not None
            or self._transport is not transport
            or transport.closed
            or session is None
            or session.status is not SessionStatus.ACTIVATED
            or transport.server_key is None
            or transport.server_key != session.server_public_key
            or transport.identity != session.identity
        ):
            return False
        return True

    def _receive_call(self, packet: Packet, transport: ZmqTransport) -> None:
        if not self._call_transport_current(transport):
            return
        event = packet_event(packet)
        if event != "ps_sp_sdp_offer":
            call = self._media_call
            if call is None:
                return
            body = packet_body(packet)
            expected_sender = (
                call.offer.remote
                if event == "brd_sp_hangup_call"
                else call.offer.sender
            )
            if (
                body.get("token") != call.offer.token
                or packet_sender(packet) != expected_sender
                or (
                    "remoteIdentity" in body
                    and body["remoteIdentity"] != call.offer.remote
                )
            ):
                return
            if event in {"ps_sp_hangup_call", "brd_sp_hangup_call"}:
                _LOGGER.debug("IPGUARD video provider termination received")
                self._finish_media()
            # A ring acknowledgement does not prove local media readiness.
            return
        parsed = parse_call_offer(packet, self._call_boards)
        if parsed is None:
            return
        notification, token_hash = parsed.notification, parsed.token_hash
        now = monotonic()
        while (
            self._call_tokens
            and next(iter(self._call_tokens.values())) <= now - RECENT_CALL_TTL
        ):
            self._call_tokens.popitem(last=False)
        if token_hash in self._call_tokens:
            self._call_tokens[token_hash] = now
            self._call_tokens.move_to_end(token_hash)
            current = self._media_call
            if (
                current is not None
                and current.offer.token_hash == token_hash
                and (
                    current.offer.sender != parsed.sender
                    or current.offer.remote != parsed.remote
                    or current.offer.sdp != parsed.sdp
                )
            ):
                _LOGGER.debug("IPGUARD ignored conflicting call update")
            return
        self._call_tokens[token_hash] = now
        if len(self._call_tokens) > MAX_RECENT_CALLS:
            self._call_tokens.popitem(last=False)
        if self._media_enabled:
            if self._media_call is not None:
                _LOGGER.debug("IPGUARD video call superseded")
            self._finish_media()
            self._media_call = media = _MediaCall(
                parsed,
                transport,
                str(uuid4()),
                now + MEDIA_OFFER_TTL,
                now + MEDIA_MAX_LIFETIME,
            )
            self._schedule_media_expiry(media)
            self._publish_media(media)
        for callback in tuple(self._call_listeners):
            try:
                callback(notification)
            except Exception:
                # Callback errors may contain private downstream data.
                continue

    async def _on_packet(self, packet: Packet) -> None:
        event, body = packet_event(packet), packet_body(packet)
        _LOGGER.debug(
            "IPGUARD received event %s", event if event in _LOGGED_EVENTS else "other"
        )
        if event in ("srv_sp_delete", "bc_sp_delete_app"):
            if self._session is None or self._session.status is SessionStatus.PENDING:
                return
            self._session = replace(self._session, status=SessionStatus.DISABLED)
            raise AuthRevokedError("IPGUARD revoked this activated installation.")
        if event == "bc_sp_relays":
            boards, self._relays = door_inventory(body)
            unique: dict[str, Board] = {}
            ambiguous: set[str] = set()
            for board in boards:
                if board.identity in unique:
                    ambiguous.add(board.identity)
                unique[board.identity] = board
            self._call_boards = {
                identity: board
                for identity, board in unique.items()
                if identity not in ambiguous
            }
            self._inventory_ready.set()
            self._notify()
        if event == "srv_sp_update" and isinstance(body.get("token"), str):
            password = configuration_password(packet)
            if password and self._session:
                self._session = replace(self._session, sip_password=password)
                self._configuration_ready.set()
            assert self._transport is not None
            await self._transport.async_send(
                envelope(self._identity, "sp_srv_update_ack", {}, str(body["token"]))
            )
            self._notify()

    async def _wait_ready(self, event: asyncio.Event, description: str) -> None:
        ready = asyncio.create_task(event.wait())
        failed = asyncio.create_task(self._failure_ready.wait())
        try:
            async with asyncio.timeout(self.timeout):
                await asyncio.wait((ready, failed), return_when=asyncio.FIRST_COMPLETED)
        except TimeoutError:
            raise ConnectionFailureError(
                f"Timed out waiting for {description}."
            ) from None
        finally:
            for task in (ready, failed):
                task.cancel()
            await asyncio.gather(ready, failed, return_exceptions=True)
        if self._error is not None:
            raise self._error

    def _on_failure(self, error: IpguardError) -> None:
        self._connected, self._error = False, error
        self._finish_media()
        self._failure_ready.set()
        self._revoked = isinstance(error, AuthRevokedError)
        _LOGGER.debug("IPGUARD connection stopped: %s", type(error).__name__)
        self._notify()
        if (
            not self._closed
            and not self._revoked
            and self._session
            and self._session.status is SessionStatus.ACTIVATED
            and self._reconnect_task is None
        ):
            self._reconnect_task = asyncio.create_task(
                self._reconnect(), name="ipguard-reconnect"
            )

    async def _reconnect(self) -> None:
        delay = self._reconnect_delay
        try:
            while not self._closed and not self._revoked:
                _LOGGER.debug("IPGUARD reconnect scheduled")
                await asyncio.sleep(delay)
                try:
                    await self._connect_saved()
                    return
                except AuthRevokedError:
                    return
                except IpguardError as error:
                    self._connected, self._error = False, error
                    self._notify()
                    delay = min(delay * 2, self._max_reconnect_delay)
        finally:
            self._reconnect_task = None

    async def _stop_connection(self) -> None:
        self._closed = True
        self._finish_media()
        task, self._reconnect_task = self._reconnect_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        transport, self._transport = self._transport, None
        if transport:
            await transport.async_close()
        self._connected = False

    async def async_close(self) -> None:
        await self._stop_connection()
        self._notify()
        self._listeners.clear()
        self._call_listeners.clear()
        self._media_listeners.clear()
        self._media_answer_outcomes.clear()
        self._media_snapshot = MediaSnapshot(
            "idle" if self._media_enabled else "disabled"
        )
        self._call_tokens.clear()
        self._call_boards.clear()
        self._verification_requested = False
        self._identifier, self._identity, self._label = None, "", ""
        if self._session and self._session.status is SessionStatus.PENDING:
            self._session = None

    async def __aenter__(self) -> IpguardClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.async_close()


def _valid_curve_key(key: str) -> bool:
    try:
        return len(key) == 40 and len(z85.decode(key.encode("ascii"))) == 32
    except KeyError, ValueError, UnicodeError, StructError, OverflowError:
        return False
