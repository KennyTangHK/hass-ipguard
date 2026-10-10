"""Exercise the real SDK against plain and CURVE TCP servers on loopback only."""

import asyncio
import json
import logging
import sys
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
import zmq
import zmq.asyncio
from ipguard_api import (
    AndroidProfile,
    AuthenticationError,
    AuthRevokedError,
    ConnectionFailureError,
    ExistingInstallationError,
    InvalidCodeError,
    InvalidInputError,
    IpguardClient,
    OutcomeUnknownError,
    RelayUnavailableError,
    Session,
    SessionStatus,
    validate_identifier,
)
from ipguard_api.protocol import envelope, packet_body, packet_event

pytestmark = pytest.mark.skipif(
    not zmq.has("curve"), reason="Runtime has no CURVE support"
)


class FakeCloud:
    def __init__(self) -> None:
        self.context = zmq.asyncio.Context()
        self.public, self.secret = zmq.curve_keypair()
        self.activation = self.context.socket(zmq.ROUTER)
        self.secure = self.context.socket(zmq.ROUTER)
        for socket in (self.activation, self.secure):
            socket.linger = 0
            socket.setsockopt(zmq.ROUTER_HANDOVER, 1)
        self.changed = asyncio.Condition()
        self.secure.curve_publickey, self.secure.curve_secretkey = (
            self.public,
            self.secret,
        )
        self.secure.curve_server = True
        self.activation_port = self.activation.bind_to_random_port("tcp://127.0.0.1")
        self.secure_port = self.secure.bind_to_random_port("tcp://127.0.0.1")
        self.activation_endpoint = f"tcp://127.0.0.1:{self.activation_port}"
        self.secure_endpoint = f"tcp://127.0.0.1:{self.secure_port}"
        self.records: list[tuple[str, list[bytes], dict]] = []
        self.handshakes: list[dict[str, str | None]] = []
        self.failures: list[Exception] = []
        self.existing = False
        self.reject_code: object = 1
        self.drop_ack = False
        self.drop_auth = False
        self.revoke_startup = False
        self.registration = "normal"
        self.routes: set[bytes] = set()
        self.tasks = [
            asyncio.create_task(self.read(self.activation, "activation")),
            asyncio.create_task(self.read(self.secure, "secure")),
        ]

    def client(self, **options) -> IpguardClient:
        return IpguardClient(
            timeout=options.pop("timeout", 1.0),
            activation_endpoint=self.activation_endpoint,
            secure_endpoint=self.secure_endpoint,
            reconnect_delay=0.03,
            max_reconnect_delay=0.06,
            **options,
        )

    def saved_session(self, **options) -> Session:
        return Session(
            identity=str(uuid4()),
            identifier=validate_identifier("user@example.invalid"),
            label="Personal phone",
            verification_code="012345",
            server_public_key=self.public.decode(),
            sip_password="synthetic-password",
            activation_endpoint=self.activation_endpoint,
            secure_endpoint=self.secure_endpoint,
            **options,
        )

    def inventory(self, *, allowed=True, pending=False, legacy=False) -> dict:
        boards = [
            {
                "identity": "synthetic-board",
                "name": "Test entrance",
                "device_id": 101,
                "relay_name": ["Front door", "Local gate", "Garage"],
                "relay_active": [False, True, True],
                "relay_access_mobile_v2": [
                    {"type": 0 if allowed else 1, "mode": 0},
                    {"type": 1, "mode": 1},
                    {"type": "2", "mode": 2},
                ],
                "waitConfirmBadgeDigital": pending,
            }
        ]
        return (
            {"boards": boards}
            if legacy
            else {"sites": {"test-site": {"name": "Test building", "boards": boards}}}
        )

    async def send(
        self, socket, route, event, body=None, token=None, sender="fixture-service"
    ) -> None:
        await socket.send_multipart(
            [route, json.dumps(envelope(sender, event, body or {}, token)).encode()]
        )

    async def push(self, event, body=None) -> None:
        for route in self.routes:
            await self.send(self.secure, route, event, body)

    async def configuration(self, route) -> None:
        await self.send(
            self.secure,
            route,
            "srv_sp_update",
            {"data": {"1": {"sip_pwd": "synthetic-password"}}},
            "opaque_Node.js_CLI_token",
        )
        await self.send(self.secure, route, "bc_sp_relays", self.inventory())

    async def read(self, socket, channel) -> None:
        try:
            while True:
                message = await socket.recv_multipart(copy=False)
                metadata: dict[str, str | None] = {}
                for key in ("Socket-Type", "Identity", "User-Agent"):
                    try:
                        metadata[key] = message[-1].get(key)
                    except zmq.ZMQError:
                        metadata[key] = None
                self.handshakes.append(metadata)
                frames = [bytes(frame) for frame in message]
                route, payload = frames[0], frames[-1]
                packet = json.loads(payload)
                self.records.append((channel, frames, packet))
                async with self.changed:
                    self.changed.notify_all()
                assert route.decode() == packet["header"]["sender"]
                event, body = packet_event(packet), packet_body(packet)
                if channel == "secure":
                    self.routes.add(route)
                if event == "hello":
                    assert body["version"] == "3.4.8"
                    data = body["data"]
                    assert (
                        data["os_version"] == "14" and data["connectionInfo"] == "WiFi"
                    )
                    assert data["language"] == "en_GB" and data["without_sip"] is True
                    if channel == "secure":
                        await socket.send_multipart([route, b"not-json"])
                        await socket.send_multipart(
                            [
                                route,
                                b'{"action":"register","data":{"token":"opaque-control"}}',
                            ]
                        )
                        if data["username"]:
                            if self.revoke_startup:
                                await self.send(socket, route, "srv_sp_delete")
                            else:
                                await self.send(
                                    socket, route, "bc_sp_relays", self.inventory()
                                )
                        else:
                            await self.send(socket, route, "srv_sp_delete")
                            await self.send(socket, route, "bc_sp_delete_app")
                elif event == "sp_srv_check_auth":
                    assert channel == "activation" and len(frames) == 2
                    if not self.drop_auth:
                        await self.send(
                            socket,
                            route,
                            "srv_sp_check_auth_result",
                            {"result": 1},
                            body["token"],
                        )
                elif event == "sp_srv_validate_auth":
                    assert body["code"] == "012345"
                    await self.send(
                        socket,
                        route,
                        "srv_sp_validate_auth_result",
                        {"result": self.reject_code},
                        body["token"],
                    )
                elif event == "sp_srv_ask_key":
                    await self.send(
                        socket,
                        route,
                        "srv_sp_ask_key_result",
                        {"result": 1, "key": self.public.decode()},
                        body["token"],
                    )
                elif event in ("sp_srv_check_username", "sp_srv_override_username"):
                    assert (
                        body["username"] == "Personal phone"
                        and body["model"] == "Pixel 7"
                    )
                    assert body["code"] == "012345" and channel == "secure"
                    if event == "sp_srv_check_username" and self.existing:
                        await self.send(
                            socket,
                            route,
                            "srv_sp_check_username_result",
                            {"result": 0},
                            body["token"],
                        )
                    elif self.registration == "update-only":
                        await self.configuration(route)
                    elif self.registration == "config-first":
                        await self.configuration(route)
                        await self.send(
                            socket,
                            route,
                            "srv_sp_check_username_result",
                            {"result": 2},
                            body["token"],
                        )
                    else:
                        await self.send(
                            socket,
                            route,
                            "srv_sp_check_username_result",
                            {"result": 1},
                            body["token"],
                        )
                        await self.configuration(route)
                elif event == "sp_srv_update_ack":
                    assert body["token"] == "opaque_Node.js_CLI_token"
                elif event == "sp_brd_open_door":
                    assert (
                        channel == "secure"
                        and len(frames) == 3
                        and frames[1] == b"synthetic-board"
                    )
                    if not self.drop_ack:
                        await self.send(
                            socket,
                            route,
                            "brd_sp_open_door_ack",
                            {"result": 1},
                            "wrong-token",
                            "synthetic-board",
                        )
                        await self.send(
                            socket,
                            route,
                            "brd_sp_open_door_ack",
                            {"result": 1},
                            body["token"],
                            "wrong-board",
                        )
                        await self.send(
                            socket,
                            route,
                            "brd_sp_open_door_ack",
                            {"result": 1},
                            body["token"],
                            "synthetic-board",
                        )
                else:
                    raise AssertionError("Unexpected outbound service event")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.failures.append(error)

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.activation.close(linger=0)
        self.secure.close(linger=0)
        self.context.term()

    def events(self) -> list[str]:
        return [packet_event(packet) for _, _, packet in self.records]

    async def wait_for_event(self, event: str, count: int = 1) -> None:
        async with asyncio.timeout(1), self.changed:
            await self.changed.wait_for(lambda: self.events().count(event) >= count)

    async def restart_secure(self) -> None:
        task = self.tasks.pop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.secure.close(linger=0)
        await asyncio.sleep(0.04)
        self.secure = self.context.socket(zmq.ROUTER)
        self.secure.linger = 0
        self.secure.setsockopt(zmq.ROUTER_HANDOVER, 1)
        self.secure.curve_publickey, self.secure.curve_secretkey = (
            self.public,
            self.secret,
        )
        self.secure.curve_server = True
        self.secure.bind(self.secure_endpoint)
        self.tasks.append(asyncio.create_task(self.read(self.secure, "secure")))


@pytest_asyncio.fixture
async def cloud():
    fixture = FakeCloud()
    try:
        yield fixture
    finally:
        await fixture.close()
        assert not fixture.failures


async def enroll(client: IpguardClient, identifier="user@example.invalid") -> Session:
    await client.async_request_verification(identifier, "Personal phone")
    await client.async_verify_code("012345")
    return await client.async_complete_registration()


@pytest.mark.parametrize("identifier", ["user@example.invalid", "+447700900123"])
@pytest.mark.parametrize("registration", ["normal", "update-only", "config-first"])
async def test_real_enrollment_curve_inventory_and_release(
    cloud, identifier, registration, caplog
):
    cloud.registration = registration
    caplog.set_level(logging.DEBUG, logger="ipguard_api")
    async with cloud.client() as client:
        session = await enroll(client, identifier)
        assert session.status is SessionStatus.ACTIVATED and session.sip_password
        assert client.connected
        relays = await client.async_get_relays()
        assert [item.remote_allowed for item in relays] == [True, False, True]
        await client.async_release("synthetic-board", 1)
        assert cloud.events().count("sp_brd_open_door") == 1
        assert "sp_srv_override_username" not in cloud.events()
        assert "012345" not in caplog.text and "user@example.invalid" not in caplog.text
        assert (
            session.identity not in caplog.text
            and session.server_public_key not in caplog.text
        )
        assert "opaque_Node.js_CLI_token" not in caplog.text
        hellos = [p for _, _, p in cloud.records if packet_event(p) == "hello"]
        assert hellos[-1]["body"]["data"]["username"] == ""


async def test_replacement_requires_opt_in_and_preserves_identity(cloud):
    cloud.existing = True
    async with cloud.client() as client:
        await client.async_request_verification(
            "user@example.invalid", "Personal phone"
        )
        await client.async_verify_code("012345")
        identity = client.session.identity
        with pytest.raises(ExistingInstallationError):
            await client.async_complete_registration()
        assert "sp_srv_override_username" not in cloud.events()
        await client.async_complete_registration(replace_existing=True)
        assert client.session.identity == identity
        assert cloud.events().count("sp_srv_override_username") == 1
        assert cloud.events().count("sp_srv_check_auth") == 1


async def test_explicit_resend_keeps_pending_and_saved_installation_identity(cloud):
    saved = cloud.saved_session()
    async with cloud.client(session=saved) as client:
        await client.async_request_verification(
            "user@example.invalid", "Personal phone"
        )
        await client.async_request_verification(
            "user@example.invalid", "Personal phone"
        )
        routes = [
            frames[0].decode()
            for _, frames, p in cloud.records
            if packet_event(p) == "sp_srv_check_auth"
        ]
        assert routes == [saved.identity, saved.identity]


@pytest.mark.parametrize("result", [0, True, False, -123])
async def test_invalid_and_unknown_verification_results(cloud, result):
    cloud.reject_code = result
    async with cloud.client() as client:
        await client.async_request_verification(
            "user@example.invalid", "Personal phone"
        )
        error = (
            InvalidCodeError
            if type(result) is int and result == 0
            else AuthenticationError
        )
        with pytest.raises(error):
            await client.async_verify_code("012345")
        assert "sp_srv_ask_key" not in cloud.events()


async def test_push_permission_removal_and_listener_unsubscribe(cloud):
    async with cloud.client() as client:
        await enroll(client)
        notices = []
        changed = asyncio.Event()

        def on_notice(snapshot):
            notices.append(snapshot)
            changed.set()

        unsubscribe = client.subscribe(on_notice)
        await cloud.push("bc_sp_relays", cloud.inventory(allowed=False))
        await asyncio.wait_for(changed.wait(), 1)
        assert not notices[-1].relays[0].remote_allowed
        with pytest.raises(RelayUnavailableError):
            await client.async_release("synthetic-board", 1)
        assert "sp_brd_open_door" not in cloud.events()
        unsubscribe()
        changed.clear()
        client.subscribe(lambda _: changed.set())
        await cloud.push("bc_sp_relays", cloud.inventory())
        await asyncio.wait_for(changed.wait(), 1)
        assert len(notices) == 1


async def test_release_timeout_and_concurrent_press_never_replay(cloud):
    cloud.drop_ack = True
    async with cloud.client(timeout=0.15) as client:
        await enroll(client)
        release = asyncio.create_task(client.async_release("synthetic-board", 1))
        await cloud.wait_for_event("sp_brd_open_door")
        with pytest.raises(RelayUnavailableError):
            await client.async_release("synthetic-board", 1)
        with pytest.raises(OutcomeUnknownError):
            await release
        assert cloud.events().count("sp_brd_open_door") == 1


async def test_saved_session_connects_without_sms_and_revocation_is_terminal(cloud):
    session = cloud.saved_session(
        android_profile=AndroidProfile(model="Node.js CLI", os_version="Node.js")
    )
    async with cloud.client() as client:
        await client.async_connect(session)
        assert client.session.android_profile == AndroidProfile()
        assert cloud.events() == ["hello"]
        notices = []
        revoked = asyncio.Event()

        def on_notice(snapshot):
            notices.append(snapshot)
            if snapshot.revoked:
                revoked.set()

        client.subscribe(on_notice)
        await cloud.push("srv_sp_delete")
        await asyncio.wait_for(revoked.wait(), 1)
        assert not client.connected and client.session.status is SessionStatus.DISABLED
        with pytest.raises(AuthRevokedError):
            await client.async_release("synthetic-board", 1)
        assert client._reconnect_task is None
        assert cloud.events() == ["hello"]


async def test_auth_timeout_and_cancel_release_all_reader_resources(cloud):
    cloud.drop_auth = True
    client = cloud.client(timeout=0.1)
    with pytest.raises(ConnectionFailureError):
        await client.async_request_verification(
            "user@example.invalid", "Personal phone"
        )
    await client.async_close()
    client = cloud.client(timeout=10)
    operation = asyncio.create_task(
        client.async_request_verification("user@example.invalid", "Personal phone")
    )
    await cloud.wait_for_event("sp_srv_check_auth", count=2)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert client._transport is None
    assert not [
        t
        for t in asyncio.all_tasks()
        if t.get_name() in {"ipguard-reader", "ipguard-monitor", "ipguard-reconnect"}
    ]


@pytest.mark.parametrize("failure", ["revocation", "protocol"])
async def test_reader_failure_cleans_resources_without_explicit_close(cloud, failure):
    async with cloud.client() as client:
        await client.async_connect(cloud.saved_session())
        transport = client._transport
        context = transport._context
        tasks = (transport._reader, transport._monitor_reader)
        failed = asyncio.Event()
        client.subscribe(
            lambda snapshot: failed.set() if snapshot.error is not None else None
        )
        if failure == "revocation":
            await cloud.push("srv_sp_delete")
        else:
            await cloud.push("bc_sp_relays", {"boards": [None] * 1025})
        await asyncio.wait_for(failed.wait(), 1)
        async with asyncio.timeout(1):
            await asyncio.gather(*tasks, return_exceptions=True)
        assert transport.closed and context.closed
        assert transport._context is None
        assert transport._socket is None and transport._monitor is None
        assert not transport._pending
        assert all(task.done() for task in tasks)
        assert not client.connected
        assert "sp_brd_open_door" not in cloud.events()


async def test_tcp_reconnect_restores_inventory_without_reenrollment_or_release(cloud):
    async with cloud.client() as client:
        session = await enroll(client)
        notices = []
        restored = asyncio.Event()

        def on_notice(snapshot):
            notices.append(snapshot)
            if snapshot.connected and any(not notice.connected for notice in notices):
                restored.set()

        client.subscribe(on_notice)
        await cloud.restart_secure()
        await asyncio.wait_for(restored.wait(), 2)
        assert client.session.identity == session.identity
        assert cloud.events().count("sp_srv_check_auth") == 1
        assert cloud.events().count("sp_srv_check_username") == 1
        assert "sp_brd_open_door" not in cloud.events()
        hellos = [
            p
            for channel, _, p in cloud.records
            if channel == "secure" and packet_event(p) == "hello"
        ]
        assert (
            len(hellos) >= 2
            and hellos[-1]["body"]["data"]["username"] == "Personal phone"
        )


async def test_consent_retry_after_disconnect_uses_same_key_without_new_sms(cloud):
    cloud.existing = True
    async with cloud.client() as client:
        await client.async_request_verification(
            "user@example.invalid", "Personal phone"
        )
        await client.async_verify_code("012345")
        identity = client.session.identity
        with pytest.raises(ExistingInstallationError):
            await client.async_complete_registration()
        failed = asyncio.Event()
        client.subscribe(
            lambda snapshot: failed.set() if snapshot.error is not None else None
        )
        await cloud.restart_secure()
        await asyncio.wait_for(failed.wait(), 1)
        await client.async_complete_registration(replace_existing=True)
        assert client.session.identity == identity
        assert cloud.events().count("sp_srv_check_auth") == 1
        assert cloud.events().count("sp_srv_override_username") == 1


async def test_release_cancellation_is_not_replayed_and_keeps_shared_reader(cloud):
    cloud.drop_ack = True
    client = cloud.client()
    await enroll(client)
    release = asyncio.create_task(client.async_release("synthetic-board", 1))
    await cloud.wait_for_event("sp_brd_open_door")
    release.cancel()
    with pytest.raises(asyncio.CancelledError):
        await release
    assert client.connected and client._transport is not None
    assert not client._transport._pending
    assert cloud.events().count("sp_brd_open_door") == 1
    await client.async_close()
    assert not [
        t
        for t in asyncio.all_tasks()
        if t.get_name() in {"ipguard-reader", "ipguard-monitor", "ipguard-reconnect"}
    ]


async def test_disconnected_client_never_queues_a_future_release(cloud):
    async with cloud.client() as client:
        await client.async_connect(cloud.saved_session())
        await client._transport.async_close()
        client._on_failure(ConnectionFailureError("synthetic interruption"))
        with pytest.raises(ConnectionFailureError):
            await client.async_release("synthetic-board", 1)
        await client.async_close()
    assert "sp_brd_open_door" not in cloud.events()


async def test_unexpected_reader_exception_updates_availability_without_payload_logs(
    cloud, caplog
):
    caplog.set_level(logging.DEBUG, logger="ipguard_api")
    async with cloud.client() as client:
        await client.async_connect(cloud.saved_session())
        failed = asyncio.Event()
        client.subscribe(
            lambda snapshot: failed.set() if snapshot.error is not None else None
        )
        depth = sys.getrecursionlimit() + 100
        payload = (
            b'{"header":{"event":"bc_sp_relays"},"body":{"data":'
            + b"[" * depth
            + b'"synthetic-sensitive-payload"'
            + b"]" * depth
            + b"}}"
        )
        for route in cloud.routes:
            await cloud.secure.send_multipart([route, payload])
        await asyncio.wait_for(failed.wait(), 1)
        assert not client.connected
        assert isinstance(client.snapshot.error, ConnectionFailureError)
        assert "synthetic-sensitive-payload" not in caplog.text


@pytest.mark.parametrize(
    "label", ["CLI_app", "Node_js", "Node-js", "HomeAssistant", "custom client"]
)
async def test_bad_personal_labels_rejected_before_any_verification_request(
    cloud, label
):
    async with cloud.client() as client:
        with pytest.raises(InvalidInputError):
            await client.async_request_verification("user@example.invalid", label)
    assert not cloud.records


async def test_native_handshake_matches_android_dealer_metadata(cloud):
    async with cloud.client() as client:
        session = await enroll(client)
        assert cloud.handshakes
        assert {metadata["Socket-Type"] for metadata in cloud.handshakes} == {"DEALER"}
        assert {metadata["Identity"] for metadata in cloud.handshakes} == {
            session.identity
        }
        assert all(metadata["User-Agent"] is None for metadata in cloud.handshakes)
        assert all(
            frames[0] == session.identity.encode() for _, frames, _ in cloud.records
        )


@pytest.mark.parametrize(
    "change",
    ["permission", "disappearance", "pending", "revocation", "disconnect", "transport"],
)
async def test_permission_and_connection_are_checked_at_transmission(
    cloud, monkeypatch, change
):
    async with cloud.client() as client:
        await enroll(client)
        transport = client._transport
        original_request = transport.async_request

        async def change_before_send(*args, **kwargs):
            if change == "permission":
                client._relays = tuple(
                    replace(r, remote_allowed=False) for r in client._relays
                )
            elif change == "disappearance":
                client._relays = ()
            elif change == "pending":
                client._relays = tuple(
                    replace(r, pending_confirmation=True) for r in client._relays
                )
            elif change == "revocation":
                client._revoked = True
            elif change == "disconnect":
                client._connected = False
            else:
                client._transport = None
            try:
                return await original_request(*args, **kwargs)
            finally:
                client._transport = transport

        monkeypatch.setattr(transport, "async_request", change_before_send)
        error = (
            AuthRevokedError
            if change == "revocation"
            else (
                ConnectionFailureError
                if change in ("disconnect", "transport")
                else RelayUnavailableError
            )
        )
        with pytest.raises(error):
            await client.async_release("synthetic-board", 1)
        assert not transport._pending and not client._release_locks
        assert "sp_brd_open_door" not in cloud.events()


@pytest.mark.parametrize(
    "change", ["permission", "disappearance", "pending", "revocation", "disconnect"]
)
async def test_busy_connection_rejects_release_without_delayed_transmission(
    cloud, change
):
    async with cloud.client() as client:
        await enroll(client)
        transport = client._transport
        async with transport._send_lock:
            with pytest.raises(RelayUnavailableError):
                await client.async_release("synthetic-board", 1)
            if change == "permission":
                client._relays = tuple(
                    replace(r, remote_allowed=False) for r in client._relays
                )
            elif change == "disappearance":
                client._relays = ()
            elif change == "pending":
                client._relays = tuple(
                    replace(r, pending_confirmation=True) for r in client._relays
                )
            elif change == "revocation":
                client._revoked = True
            else:
                client._connected = False
        await asyncio.sleep(0)
        assert not transport._pending and not client._release_locks
        assert "sp_brd_open_door" not in cloud.events()


async def test_server_write_lock_wait_is_bounded(cloud):
    async with cloud.client(timeout=0.05) as client:
        await enroll(client)
        transport = client._transport
        before = len(cloud.records)
        async with transport._send_lock:
            with pytest.raises(ConnectionFailureError):
                await transport.async_send(
                    envelope(
                        client.session.identity,
                        "sp_srv_update_ack",
                        {},
                        "opaque_Node.js_CLI_token",
                    )
                )
        assert len(cloud.records) == before


async def test_release_cancelled_before_send_keeps_reader_and_removes_guard(
    cloud, monkeypatch
):
    async with cloud.client() as client:
        await enroll(client)
        transport = client._transport
        original_request = transport.async_request
        entered = asyncio.Event()

        async def pause_before_send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(transport, "async_request", pause_before_send)
        operation = asyncio.create_task(client.async_release("synthetic-board", 1))
        await entered.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert client.connected and not transport.closed
        assert not transport._pending and not client._release_locks
        assert "sp_brd_open_door" not in cloud.events()
        monkeypatch.setattr(transport, "async_request", original_request)
        await client.async_release("synthetic-board", 1)
        assert cloud.events().count("sp_brd_open_door") == 1
        assert not client._release_locks


async def test_distinct_relay_can_release_while_other_ack_is_pending(cloud):
    cloud.drop_ack = True
    async with cloud.client(timeout=0.1) as client:
        await enroll(client)
        first = asyncio.create_task(client.async_release("synthetic-board", 1))
        await cloud.wait_for_event("sp_brd_open_door")
        cloud.drop_ack = False
        await client.async_release("synthetic-board", 3)
        with pytest.raises(OutcomeUnknownError):
            await first
        assert cloud.events().count("sp_brd_open_door") == 2
        assert not client._release_locks


async def test_wire_send_failure_after_attempt_is_unknown_once(cloud, monkeypatch):
    async with cloud.client() as client:
        await enroll(client)
        send = AsyncMock(side_effect=zmq.ZMQError(zmq.EAGAIN))
        monkeypatch.setattr(client._transport._socket, "send_multipart", send)
        with pytest.raises(OutcomeUnknownError):
            await client.async_release("synthetic-board", 1)
        send.assert_awaited_once()
        assert not client._release_locks and not client._transport._pending


async def test_closed_transport_failure_before_attempt_remains_connectivity_error(
    cloud,
):
    async with cloud.client() as client:
        await enroll(client)
        await client._transport.async_close()
        with pytest.raises(ConnectionFailureError):
            await client.async_release("synthetic-board", 1)
        assert not client._release_locks
        assert "sp_brd_open_door" not in cloud.events()
