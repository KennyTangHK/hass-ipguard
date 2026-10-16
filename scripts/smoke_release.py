"""Exercise the release ZIP against real HA and a loopback-only cloud fixture.

Run in a disposable container with --network none. No provider account is used.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
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
from aiohttp import ClientSession
from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry, entity_registry

IDENTIFIER = "resident@example.invalid"
CODE = "000123"
BOARD = "fixture-board"
BASELINE_SHA256 = "11ce6985efa96f6873c8bf9f4e290465261d2a82d114a70bbd6bed46159a7578"


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


async def check_incoming_calls(
    hass: HomeAssistant, fixture: CloudFixture, entry_id: str
) -> None:
    """Exercise the packaged event through CURVE without transmitting a response."""
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
        assert len(fixture.packets) == sent_before
        await fixture.incoming_call("fixture-private-call-token")
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
        assert len(rings) == 1
        received.clear()
        await fixture.incoming_call("fixture-private-next-token", "unknown-board")
        async with asyncio.timeout(5):
            await received.wait()
        await hass.async_block_till_done()
        assert len(rings) == 2 and rings[0] != rings[1]
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
        assert len(fixture.packets) == sent_before
        assert fixture.count("sp_brd_open_door") == 0
    finally:
        unsubscribe()


async def check_retired_video(
    hass: HomeAssistant, fixture: CloudFixture, entry: Any, http_port: int
) -> None:
    """Migrate old video settings without changing the saved account or entities."""
    component = importlib.import_module("custom_components.ipguard")
    registry = entity_registry.async_get(hass)
    session_before = dict(entry.data["session"])
    entities_before = {
        record.entity_id: record.unique_id
        for record in registry.entities.values()
        if record.config_entry_id == entry.entry_id
    }
    assert await hass.config_entries.async_unload(entry.entry_id)
    credential = "fixture-retired-companion-credential"
    hass.config_entries.async_update_entry(
        entry,
        minor_version=1,
        options={
            "video_enabled": True,
            "scrypted_url": "https://obsolete.example.invalid",
            "ha_url": "http://obsolete.example.invalid",
            "companion_credential": credential,
            "rtsp_url": "rtsp://obsolete.example.invalid/private-source",
            "verify_ssl": True,
            "fixture_option": "preserved",
        },
    )
    camera = registry.async_get_or_create(
        "camera",
        "ipguard",
        f"{entry.entry_id}_incoming_video",
        config_entry=entry,
        suggested_object_id="retired_incoming_video",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.minor_version == 2
    assert dict(entry.options) == {"fixture_option": "preserved"}
    assert entry.data["session"] == session_before
    assert registry.async_get(camera.entity_id) is None
    assert {
        record.entity_id: record.unique_id
        for record in registry.entities.values()
        if record.config_entry_id == entry.entry_id
    } == entities_before
    assert not hass.states.async_all("camera")
    assert component.__file__ is not None
    assert not {"camera.py", "media.py", "video_setup.py"} & {
        path.name for path in Path(component.__file__).parent.iterdir()
    }
    assert not any(
        "/api/ipguard/media/" in str(route.resource)
        for route in hass.http.app.router.routes()
    )
    async with ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{http_port}/api/ipguard/media/{entry.entry_id}/state",
            headers={"Authorization": f"Bearer {credential}"},
            json={"version": 1},
        ) as response:
            assert response.status in {401, 404}
    assert fixture.count("sp_ps_sdp_answer") == 0
    assert fixture.count("sp_brd_open_door") == 0


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
    if saved is None:
        probe = await asyncio.start_server(
            lambda _reader, writer: writer.close(), "127.0.0.1", 0
        )
        assert probe.sockets is not None
        http_port = int(probe.sockets[0].getsockname()[1])
        probe.close()
        await probe.wait_closed()
    else:
        restored_port = saved["http_port"]
        assert type(restored_port) is int and 0 < restored_port <= 65535
        http_port = restored_port
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
                    "http": {"server_host": "127.0.0.1", "server_port": http_port},
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
            events = entry_event_states(hass, entry.entry_id)
            assert len(events) == 1 and events[0].state == "unknown"
            await check_incoming_calls(hass, fixture, entry.entry_id)
            await check_retired_video(hass, fixture, entry, http_port)
            assert fixture.count("sp_srv_check_auth") == 0
            assert fixture.count("sp_srv_check_username") == 0
            assert fixture.count("sp_srv_override_username") == 0
            assert fixture.count("sp_brd_open_door") == 0
            assert await hass.config_entries.async_unload(entry.entry_id)
            print(
                json.dumps(
                    {
                        "upgrade_restart": True,
                        "same_credentials": True,
                        "same_entity_ids": True,
                        "no_sms_or_actuation": True,
                        "new_doorbell_event": True,
                        "passive_incoming_call": True,
                        "retired_video_removed": True,
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
                        "session": entry.data["session"],
                        "http_port": http_port,
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
            print(json.dumps({"baseline_state_saved": True, "no_actuation": True}))
            return
        await check_incoming_calls(hass, fixture, entry.entry_id)
        await check_retired_video(hass, fixture, entry, http_port)
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
                    "passive_incoming_call": True,
                    "duplicate_call_suppression": True,
                    "retired_video_removed": True,
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
    parser.add_argument("--upgrade-from", type=Path)
    parser.add_argument(
        "--phase", choices=("normal", "save", "restore"), default="normal"
    )
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument(
        "--registration", choices=("normal", "replace", "decline"), default="normal"
    )
    args = parser.parse_args()
    if args.upgrade_from is not None:
        baseline = args.upgrade_from.resolve()
        assert hashlib.sha256(baseline.read_bytes()).hexdigest() == BASELINE_SHA256
        with TemporaryDirectory(prefix="ipguard-upgrade-check-") as directory:
            config = Path(directory)
            for archive, phase in [
                (baseline, "save"),
                (args.archive.resolve(), "restore"),
            ]:
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        str(archive),
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
