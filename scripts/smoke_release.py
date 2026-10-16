"""Exercise the release ZIP against real HA and a loopback-only cloud fixture.

Run in a disposable container with --network none. No provider account is used.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib
import importlib.metadata
import json
import subprocess
import sys
from contextlib import suppress
from importlib.abc import MetaPathFinder
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4
from zipfile import ZipFile

import zmq
import zmq.asyncio
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry, entity_registry

IDENTIFIER = "resident@example.invalid"
CODE = "000123"
BOARD = "fixture-board"


class BlockExternalSdk(MetaPathFinder):
    """Fail if the release accidentally depends on an installed SDK package."""

    def find_spec(self, fullname: str, *args: object, **kwargs: object) -> None:
        if fullname == "ipguard_api" or fullname.startswith("ipguard_api."):
            raise ImportError("The release must use its own generated SDK copy.")


class CloudFixture:
    def __init__(
        self, *, existing: bool = False, saved: dict[str, Any] | None = None
    ) -> None:
        self.context = zmq.asyncio.Context()
        self.plain = self.context.socket(zmq.ROUTER)
        self.secure = self.context.socket(zmq.ROUTER)
        for socket in [self.plain, self.secure]:
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.ROUTER_HANDOVER, 1)
        if saved is None:
            self.public, self.secret = zmq.curve_keypair()
        else:
            self.public, self.secret = (
                saved["public"].encode(),
                saved["secret"].encode(),
            )
        self.secure.curve_publickey = self.public
        self.secure.curve_secretkey = self.secret
        self.secure.curve_server = True
        self.plain.bind(saved["activation_endpoint"] if saved else "tcp://127.0.0.1:*")
        self.secure.bind(saved["secure_endpoint"] if saved else "tcp://127.0.0.1:*")
        self.activation_endpoint = self.plain.getsockopt_string(zmq.LAST_ENDPOINT)
        self.secure_endpoint = self.secure.getsockopt_string(zmq.LAST_ENDPOINT)
        self.tasks: list[asyncio.Task[None]] = []
        self.packets: list[dict[str, Any]] = []
        self.routing_id: bytes | None = None
        self.acknowledge = True
        self.permission = True
        self.existing = existing
        self.call_routes: dict[str, str] = {}
        self.hangups: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def settings(self) -> dict[str, str]:
        return {
            "public": self.public.decode(),
            "secret": self.secret.decode(),
            "activation_endpoint": self.activation_endpoint,
            "secure_endpoint": self.secure_endpoint,
        }

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self.serve(self.plain, secure=False)),
            asyncio.create_task(self.serve(self.secure, secure=True)),
        ]

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.plain.close()
        self.secure.close()
        self.context.term()

    async def send(
        self,
        socket: zmq.asyncio.Socket,
        routing_id: bytes,
        event: str,
        body: dict[str, Any],
        token: str | None = None,
        sender: str = "fixture-server",
    ) -> None:
        packet = {
            "header": {"event": event, "sender": sender, "timestamp": 1},
            "body": {"token": token or str(uuid4()), **body},
        }
        await socket.send_multipart([routing_id, json.dumps(packet).encode()])

    async def inventory(self, routing_id: bytes) -> None:
        await self.send(
            self.secure,
            routing_id,
            "bc_sp_relays",
            {
                "sites": {
                    "fixture-site": {
                        "name": "Home",
                        "boards": [
                            {
                                "identity": BOARD,
                                "name": "Front entrance",
                                "relay_name": ["Front door", "Nearby door"],
                                "relay_access_mobile_v2": [
                                    {"type": 0 if self.permission else 1, "mode": 0},
                                    {"type": 1, "mode": 1},
                                ],
                            }
                        ],
                    },
                }
            },
        )

    async def incoming_call(self, token: str, remote_identity: str = BOARD) -> None:
        assert self.routing_id is not None
        self.call_routes[token] = remote_identity
        await self.send(
            self.secure,
            self.routing_id,
            "ps_sp_sdp_offer",
            {
                "remoteIdentity": remote_identity,
                "sdp": base64.b64encode(
                    b"v=0\r\na=ice-pwd:fixture-private-media-secret\r\n"
                ).decode(),
            },
            token,
            "fixture-call-service",
        )

    async def serve(self, socket: zmq.asyncio.Socket, *, secure: bool) -> None:
        while True:
            frames = await socket.recv_multipart()
            routing_id = frames[0]
            packet = json.loads(frames[-1])
            self.packets.append(packet)
            header, body = packet["header"], packet["body"]
            event, token = header["event"], body["token"]
            assert routing_id.decode() == header["sender"]
            if event == "sp_brd_hangup_call":
                assert secure and len(frames) == 3
                assert frames[1].decode() == self.call_routes[token]
                assert set(body) == {"token", "cause"}
                assert type(body["cause"]) is int and body["cause"] == 0
                self.hangups.put_nowait(packet)
                continue
            if event == "sp_brd_open_door":
                assert secure and len(frames) == 3 and frames[1].decode() == BOARD
                assert self.permission and body["door"] == 1
                if self.acknowledge:
                    await self.send(
                        socket,
                        routing_id,
                        "brd_sp_open_door_ack",
                        {"result": 1, "ble_frame_status": "_teOK_"},
                        token,
                        BOARD,
                    )
                continue
            assert len(frames) == 2
            if event == "hello":
                assert body["type"] == 0 and body["version"] == "3.4.8"
                data = body["data"]
                assert data["without_sip"] is True
                assert data["fcm_token"] == ""
                assert data["os_version"] == "14" and data["connectionInfo"] == "WiFi"
                if secure:
                    self.routing_id = routing_id
                    assert data["email"] == IDENTIFIER and data["code"] == CODE
                    await socket.send_multipart(
                        [
                            routing_id,
                            b'{"action":"register","data":{"token":"fixture"}}',
                        ]
                    )
                    await socket.send_multipart([routing_id, b"unrelated control"])
                    if data["username"] == "":
                        await self.send(socket, routing_id, "srv_sp_delete", {})
                    await self.inventory(routing_id)
                continue
            if event == "sp_srv_update_ack":
                continue
            assert body["email"] == IDENTIFIER
            if event == "sp_srv_check_auth":
                await self.send(
                    socket, routing_id, "srv_sp_check_auth_result", {"result": 1}, token
                )
            elif event == "sp_srv_validate_auth":
                await self.send(
                    socket,
                    routing_id,
                    "srv_sp_validate_auth_result",
                    {"result": int(body["code"] == CODE)},
                    token,
                )
            elif event == "sp_srv_ask_key":
                assert body["code"] == CODE
                await self.send(
                    socket,
                    routing_id,
                    "srv_sp_ask_key_result",
                    {"result": 1, "key": self.public.decode()},
                    token,
                )
            elif event in ("sp_srv_check_username", "sp_srv_override_username"):
                assert (
                    secure
                    and body["username"] == "Pixel 7"
                    and body["model"] == "Pixel 7"
                )
                if self.existing and event == "sp_srv_check_username":
                    await self.send(
                        socket,
                        routing_id,
                        "srv_sp_check_username_result",
                        {"result": 0},
                        token,
                    )
                    continue
                await self.send(
                    socket,
                    routing_id,
                    "srv_sp_check_username_result",
                    {"result": 1},
                    token,
                )
                await self.send(
                    socket,
                    routing_id,
                    "srv_sp_update",
                    {"data": [{}, {"sip_pwd": "fixture-password"}]},
                )
            else:
                raise AssertionError(f"Unexpected fixture event: {event}")

    def count(self, event: str) -> int:
        return sum(packet["header"]["event"] == event for packet in self.packets)


async def wait_for_state(hass: HomeAssistant, entity_id: str, state: str) -> None:
    event = asyncio.Event()

    @callback
    def receive(change: Any) -> None:
        if change.data.get("entity_id") == entity_id:
            current = hass.states.get(entity_id)
            if current is not None and current.state == state:
                event.set()

    unsubscribe = hass.bus.async_listen("state_changed", receive)
    try:
        current = hass.states.get(entity_id)
        if current is not None and current.state == state:
            return
        async with asyncio.timeout(5):
            await event.wait()
    finally:
        unsubscribe()


def entry_event_states(hass: HomeAssistant, entry_id: str) -> list[State]:
    """Select this account's events without counting unrelated core entities."""
    registry = entity_registry.async_get(hass)
    return [
        state
        for state in hass.states.async_all("event")
        if (record := registry.async_get(state.entity_id)) is not None
        and record.config_entry_id == entry_id
    ]


def check_account_device(hass: HomeAssistant, entry_id: str) -> None:
    """Keep this account's release buttons and ring under one service device."""
    devices = device_registry.async_entries_for_config_entry(
        device_registry.async_get(hass), entry_id
    )
    assert len(devices) == 1
    device = devices[0]
    assert device.identifiers == {("ipguard", entry_id)}
    assert device.entry_type is device_registry.DeviceEntryType.SERVICE
    entities = entity_registry.async_entries_for_config_entry(
        entity_registry.async_get(hass), entry_id
    )
    controls = [
        entity
        for entity in entities
        if entity.platform == "ipguard" and entity.domain in {"button", "event"}
    ]
    assert {entity.domain for entity in controls} == {"button", "event"}
    assert {entity.device_id for entity in controls} == {device.id}


async def check_incoming_calls(
    hass: HomeAssistant, fixture: CloudFixture, entry_id: str
) -> None:
    """Exercise safe rings and the one-attempt refusal trial through CURVE."""
    check_account_device(hass, entry_id)
    states = entry_event_states(hass, entry_id)
    assert len(states) == 1, {
        "event_entity_ids": [state.entity_id for state in states],
        "event_count": len(states),
        "registered_event_entity_ids": [
            record.entity_id
            for record in entity_registry.async_get(hass).entities.values()
            if record.entity_id.startswith("event.")
        ],
    }
    entity_id = states[0].entity_id
    assert states[0].state == "unknown"
    assert states[0].attributes["device_class"] == "doorbell"
    assert states[0].attributes["event_types"] == ["ring"]
    received = asyncio.Event()
    rings: list[str] = []

    @callback
    def receive(change: Any) -> None:
        if change.data.get("entity_id") != entity_id:
            return
        state = hass.states.get(entity_id)
        if state is not None and state.attributes.get("event_type") == "ring":
            rings.append(state.state)
            received.set()

    unsubscribe = hass.bus.async_listen("state_changed", receive)
    try:
        await hass.async_block_till_done()
        sent_before = len(fixture.packets)
        await fixture.incoming_call("fixture-private-call-token")
        async with asyncio.timeout(5):
            await received.wait()
        await hass.async_block_till_done()
        assert len(rings) == 1
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["board_id"] == BOARD
        assert state.attributes["board_name"] == "Front entrance"
        assert state.attributes["site_id"] == "fixture-site"
        assert state.attributes["site_name"] == "Home"
        for secret in ("fixture-private-call-token", "fixture-private-media-secret"):
            assert secret not in repr(state.as_dict())
        assert fixture.count("sp_brd_open_door") == 0
        hangup = await asyncio.wait_for(fixture.hangups.get(), 5)
        assert hangup["body"] == {"token": "fixture-private-call-token", "cause": 0}
        assert len(fixture.packets) == sent_before + 1
        await fixture.incoming_call("fixture-private-call-token")
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        assert len(rings) == 1
        assert len(fixture.packets) == sent_before + 1
        assert fixture.hangups.empty()
        received.clear()
        await fixture.incoming_call("fixture-private-next-token", "unknown-board")
        async with asyncio.timeout(5):
            await received.wait()
        await hass.async_block_till_done()
        assert len(rings) == 2 and rings[0] != rings[1]
        hangup = await asyncio.wait_for(fixture.hangups.get(), 5)
        assert hangup["body"] == {"token": "fixture-private-next-token", "cause": 0}
        state = hass.states.get(entity_id)
        assert state is not None
        assert not any(
            key in state.attributes
            for key in ("board_id", "board_name", "site_id", "site_name")
        )
        await fixture.incoming_call("fixture-private-call-token")
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        assert len(rings) == 2
        assert len(fixture.packets) == sent_before + 2
        assert fixture.count("sp_brd_hangup_call") == 2
        assert fixture.hangups.empty()
        assert fixture.count("sp_brd_open_door") == 0
    finally:
        unsubscribe()


async def check_saved_session_reload(
    hass: HomeAssistant, fixture: CloudFixture, entry: Any
) -> None:
    """Reload the current layout without changing authorization or entity IDs."""
    registry = entity_registry.async_get(hass)
    session_before = dict(entry.data["session"])
    entities_before = {
        record.entity_id: (record.unique_id, record.device_id)
        for record in entity_registry.async_entries_for_config_entry(
            registry, entry.entry_id
        )
    }
    sent_before = (
        fixture.count("sp_srv_check_auth"),
        fixture.count("sp_srv_check_username"),
        fixture.count("sp_srv_override_username"),
        fixture.count("sp_brd_hangup_call"),
        fixture.count("sp_brd_open_door"),
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["session"] == session_before
    check_account_device(hass, entry.entry_id)
    assert {
        record.entity_id: (record.unique_id, record.device_id)
        for record in entity_registry.async_entries_for_config_entry(
            registry, entry.entry_id
        )
    } == entities_before
    assert (
        fixture.count("sp_srv_check_auth"),
        fixture.count("sp_srv_check_username"),
        fixture.count("sp_srv_override_username"),
        fixture.count("sp_brd_hangup_call"),
        fixture.count("sp_brd_open_door"),
    ) == sent_before


async def check(
    archive: Path, config_dir: Path, registration: str, *, phase: str = "normal"
) -> None:
    with ZipFile(archive) as release:
        if "manifest.json" in release.namelist():
            release.extractall(config_dir / "custom_components/ipguard")
        else:
            release.extractall(config_dir)
    sys.path.insert(0, str(config_dir))
    sys.meta_path.insert(0, BlockExternalSdk())
    component = importlib.import_module("custom_components.ipguard")
    flow_module = importlib.import_module("custom_components.ipguard.config_flow")
    api = importlib.import_module("custom_components.ipguard.api")
    assert api.IpguardClient.__module__.startswith(
        "custom_components.ipguard._vendor.ipguard_api."
    )
    assert "ipguard_api" not in sys.modules
    assert zmq.has("curve")
    saved_path = config_dir / "fixture-state.json"
    saved = json.loads(saved_path.read_text()) if phase == "restore" else None
    fixture = CloudFixture(
        existing=registration != "normal", saved=saved["fixture"] if saved else None
    )
    fixture.start()
    clients: list[Any] = []

    def client_factory(**kwargs: Any) -> Any:
        client = api.IpguardClient(
            timeout=0.5,
            activation_endpoint=fixture.activation_endpoint,
            secure_endpoint=fixture.secure_endpoint,
            reconnect_delay=0.05,
            max_reconnect_delay=0.1,
            **kwargs,
        )
        clients.append(client)
        return client

    component.IpguardClient = client_factory  # type: ignore[attr-defined]
    flow_module.IpguardClient = client_factory  # type: ignore[attr-defined]
    hass = HomeAssistant(str(config_dir))
    loader.async_setup(hass)
    try:
        assert (
            await bootstrap.async_from_config_dict(
                {
                    "homeassistant": {
                        "latitude": 0,
                        "longitude": 0,
                        "elevation": 0,
                        "time_zone": "UTC",
                        "unit_system": "metric",
                        "country": "GB",
                    },
                    "persistent_notification": {},
                },
                hass,
            )
            is hass
        )
        await hass.async_start()
        await hass.async_block_till_done()
        if phase == "restore":
            entries = hass.config_entries.async_entries("ipguard")
            assert len(entries) == 1
            entry = entries[0]
            assert saved is not None
            assert entry.entry_id == saved["entry_id"]
            if entry.state is ConfigEntryState.NOT_LOADED:
                assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED
            states = hass.states.async_all("button")
            assert len(states) == 1 and states[0].state != "unavailable"
            registry = entity_registry.async_get(hass)
            record = registry.async_get(states[0].entity_id)
            assert record is not None
            assert states[0].entity_id == saved["entity_id"]
            assert record.unique_id == saved["unique_id"]
            assert entry.data["session"] == saved["session"]
            assert record.device_id == saved["device_id"]
            events = entry_event_states(hass, entry.entry_id)
            assert len(events) == 1 and events[0].state == "unknown"
            assert events[0].entity_id == saved["event_id"]
            await check_incoming_calls(hass, fixture, entry.entry_id)
            await check_saved_session_reload(hass, fixture, entry)
            assert fixture.count("sp_srv_check_auth") == 0
            assert fixture.count("sp_srv_check_username") == 0
            assert fixture.count("sp_srv_override_username") == 0
            assert fixture.count("sp_brd_open_door") == 0
            assert await hass.config_entries.async_unload(entry.entry_id)
            print(
                json.dumps(
                    {
                        "current_version_restart": True,
                        "same_credentials": True,
                        "same_entity_ids": True,
                        "one_account_device": True,
                        "no_sms_or_actuation": True,
                        "same_device_id": True,
                        "incoming_ring_event": True,
                        "auto_hangup_trial": True,
                    }
                )
            )
            return

        result = await hass.config_entries.flow.async_init(
            "ipguard", context={"source": "user"}
        )
        assert result["step_id"] == "user" and not fixture.packets
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "identifier": IDENTIFIER,
                "installation_name": "Pixel 7",
            },
        )
        assert result["step_id"] == "verification"
        assert fixture.count("sp_srv_check_auth") == 1
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"verification_code": "999999"}
        )
        assert (
            result["step_id"] == "verification"
            and result["errors"]
            and result["errors"]["base"] == "invalid_code"
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"verification_code": CODE}
        )
        if registration != "normal":
            assert result["step_id"] == "replace"
            assert fixture.count("sp_srv_override_username") == 0
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"replace_existing": registration == "replace"}
            )
            if registration == "decline":
                assert (
                    result["type"] == "abort"
                    and result["reason"] == "replacement_declined"
                )
                await hass.async_block_till_done()
                assert not hass.config_entries.async_entries("ipguard")
                assert fixture.count("sp_srv_override_username") == 0
                assert fixture.count("sp_brd_open_door") == 0
                assert all(not client.connected for client in clients)
                print(
                    json.dumps(
                        {
                            "registration": "decline",
                            "no_override": True,
                            "no_entry": True,
                            "cleanup": True,
                        }
                    )
                )
                return
            assert fixture.count("sp_srv_override_username") == 1
        assert result["type"] == "create_entry"
        entry = result["result"]
        await hass.async_block_till_done()
        if entry.state is ConfigEntryState.NOT_LOADED:
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        buttons = [state for state in hass.states.async_all("button")]
        assert len(buttons) == 1 and buttons[0].state != "unavailable"
        assert fixture.count("sp_brd_open_door") == 0
        assert fixture.count("sp_srv_check_auth") == 1
        entity_id = buttons[0].entity_id
        if phase == "save":
            record = entity_registry.async_get(hass).async_get(entity_id)
            assert record is not None
            saved_path.write_text(
                json.dumps(
                    {
                        "fixture": fixture.settings(),
                        "entry_id": entry.entry_id,
                        "entity_id": entity_id,
                        "unique_id": record.unique_id,
                        "device_id": record.device_id,
                        "event_id": entry_event_states(hass, entry.entry_id)[
                            0
                        ].entity_id,
                        "session": entry.data["session"],
                    }
                )
            )
            await hass.config_entries._store.async_save(
                {
                    "entries": [
                        item.as_dict() for item in hass.config_entries.async_entries()
                    ],
                }
            )
            await entity_registry.async_get(hass)._store.async_save(
                entity_registry.async_get(hass)._data_to_save()
            )
            await device_registry.async_get(hass)._store.async_save(
                device_registry.async_get(hass)._data_to_save()
            )
            assert fixture.count("sp_brd_open_door") == 0
            print(json.dumps({"current_state_saved": True, "no_actuation": True}))
            return
        await check_incoming_calls(hass, fixture, entry.entry_id)
        await check_saved_session_reload(hass, fixture, entry)
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        assert fixture.count("sp_brd_open_door") == 1
        fixture.acknowledge = False
        try:
            await hass.services.async_call(
                "button", "press", {"entity_id": entity_id}, blocking=True
            )
        except HomeAssistantError:
            pass
        else:
            raise AssertionError("Missing acknowledgement must fail explicitly.")
        assert fixture.count("sp_brd_open_door") == 2
        await asyncio.sleep(0.15)
        assert fixture.count("sp_brd_open_door") == 2
        fixture.permission = False
        assert fixture.routing_id is not None
        await fixture.inventory(fixture.routing_id)
        await wait_for_state(hass, entity_id, "unavailable")
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert all(not client.connected for client in clients)
        assert fixture.count("sp_srv_check_auth") == 1
        assert fixture.count("sp_brd_open_door") == 2
        print(
            json.dumps(
                {
                    "homeassistant": importlib.metadata.version("homeassistant"),
                    "pyzmq": importlib.metadata.version("pyzmq"),
                    "curve": True,
                    "bundle_sdk": True,
                    "enrollment": True,
                    "invalid_code_retry": True,
                    "saved_session_setup": True,
                    "eligible_buttons": len(buttons),
                    "one_account_device": True,
                    "incoming_ring_event": True,
                    "auto_hangup_trial": True,
                    "duplicate_call_suppression": True,
                    "release_acknowledged": True,
                    "no_release_replay": True,
                    "permission_push": True,
                    "unload": True,
                    "registration": registration,
                },
                sort_keys=True,
            )
        )
    finally:
        await hass.async_stop()
        for client in clients:
            await client.async_close()
        await fixture.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument(
        "--phase", choices=("normal", "save", "restore"), default="normal"
    )
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument(
        "--registration", choices=("normal", "replace", "decline"), default="normal"
    )
    args = parser.parse_args()
    if args.restart:
        with TemporaryDirectory(prefix="ipguard-restart-check-") as directory:
            config = Path(directory)
            for phase in ("save", "restore"):
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        str(args.archive.resolve()),
                        "--phase",
                        phase,
                        "--config-dir",
                        str(config),
                    ],
                    check=True,
                )
        return
    if args.config_dir is not None:
        asyncio.run(
            check(
                args.archive.resolve(),
                args.config_dir.resolve(),
                args.registration,
                phase=args.phase,
            )
        )
        return
    with TemporaryDirectory(prefix="ipguard-release-check-") as directory:
        asyncio.run(check(args.archive.resolve(), Path(directory), args.registration))


if __name__ == "__main__":
    main()
