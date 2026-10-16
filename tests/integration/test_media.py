"""Private companion signalling, passive camera reads, and scoped pairing."""

import asyncio
import json
import logging
from dataclasses import replace
from threading import enumerate as threads
from time import monotonic
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import WSMsgType, WSServerHandshakeError
from homeassistant.components.camera import get_camera_from_entity_id
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from ipguard_api import MediaOffer, MediaSnapshot, MediaUnavailableError, Session
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ipguard.camera import (
    IpguardIncomingCamera,
    _run_private_media_worker,
)
from custom_components.ipguard.const import (
    CONF_COMPANION_CREDENTIAL,
    CONF_HA_URL,
    CONF_RTSP_URL,
    CONF_SCRYPTED_URL,
    CONF_SESSION,
    CONF_VERIFY_SSL,
    CONF_VIDEO_ENABLED,
    DOMAIN,
)
from custom_components.ipguard.media import (
    CompanionError,
    MediaBridge,
    Pairing,
    manager,
    pair_companion,
    read_bounded,
    rtsp_url,
    server_url,
)
from custom_components.ipguard.runtime import IpguardCoordinator, IpguardRuntimeData
from custom_components.ipguard.video_setup import ScryptedApp

TOKEN = "synthetic-companion-credential"
SOURCE = "rtsp://scrypted.example.invalid:43210/private-stream-token"
CALL = "synthetic-local-call"
CLAIM = "synthetic-local-claim"
SDP = "v=0\r\na=ice-pwd:private-media-credential\r\n"


def entry_for(hass: HomeAssistant, session: Session) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Kenny",
        unique_id="email:person@example.invalid",
        data={CONF_SESSION: session.to_dict()},
        options={
            CONF_VIDEO_ENABLED: True,
            CONF_COMPANION_CREDENTIAL: TOKEN,
            CONF_RTSP_URL: SOURCE,
        },
    )
    entry.add_to_hass(hass)
    return entry


def configure_media(client: Mock) -> None:
    client.media_snapshot = MediaSnapshot("pending", CALL)
    client.subscribe_media.return_value = Mock()
    client.async_claim_media = AsyncMock(
        return_value=MediaOffer(CALL, CLAIM, SDP, monotonic() + 30)
    )
    client.async_submit_media_answer = AsyncMock(return_value="sent")


async def test_camera_reads_and_setup_never_claim_media(
    hass: HomeAssistant,
    hass_client,
    client: Mock,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        runtime = entry.runtime_data
        assert runtime.media is not None
        camera = IpguardIncomingCamera(entry.entry_id, runtime, runtime.media)
        camera.hass = hass
        assert not camera.available
        http = await hass_client()
        stream = await http.ws_connect(
            f"/api/ipguard/media/{entry.entry_id}/events",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert (await stream.receive_json())["state"] == "pending"
        assert camera.available
        with patch(
            "homeassistant.components.camera.async_get_supported_provider"
        ) as provider:
            await camera.async_refresh_providers()
        provider.assert_not_called()
        assert await camera.stream_source() == SOURCE
        assert (await camera.async_camera_image()).startswith(b"\x89PNG")
        assert camera.extra_state_attributes is None
        assert len(hass.states.async_all("camera")) == 1
        exposed = repr(runtime) + repr(runtime.media) + caplog.text
        assert TOKEN not in exposed and SOURCE not in exposed and SDP not in exposed
        client.async_claim_media.assert_not_awaited()
        client.async_submit_media_answer.assert_not_awaited()
        client.async_request_verification.assert_not_awaited()
        client.async_release.assert_not_awaited()
        client.set_media_enabled.assert_called_once_with(True)
        assert await hass.config_entries.async_unload(entry.entry_id)
        client.set_media_enabled.assert_called_with(False)
        client.subscribe_media.return_value.assert_called_once()
        assert entry.entry_id not in manager(hass).bridges
        await stream.close()


async def test_camera_stream_logger_privacy_and_filter_cleanup(
    hass: HomeAssistant,
    hass_client,
    client: Mock,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entity_id = hass.states.async_all("camera")[0].entity_id
        logger = logging.getLogger(
            f"homeassistant.components.stream.stream.{entity_id}"
        )
        assert logger.filters
        caplog.set_level(logging.DEBUG)
        logger.debug("Started stream: %s", SOURCE)
        try:
            raise RuntimeError(SOURCE + TOKEN + SDP)
        except RuntimeError:
            logger.exception("Error from stream worker: %s", SOURCE)
        assert (
            SOURCE not in caplog.text
            and TOKEN not in caplog.text
            and SDP not in caplog.text
        )
        assert "Local IPGUARD video transport failed" in caplog.text
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert not logger.filters


async def test_real_framework_stream_is_guarded_and_preload_stops_before_redaction_removal(
    hass: HomeAssistant,
    hass_client,
    client: Mock,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        camera = get_camera_from_entity_id(
            hass, hass.states.async_all("camera")[0].entity_id
        )
        assert await camera.async_create_stream() is None
        http = await hass_client()
        events = await http.ws_connect(
            f"/api/ipguard/media/{entry.entry_id}/events",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        await events.receive_json()
        stream = await camera.async_create_stream()
        assert stream is not None
        assert stream.source == SOURCE
        assert await camera.async_create_stream() is stream
        assert stream._logger.filters
        stream.dynamic_stream_settings = replace(
            stream.dynamic_stream_settings, preload_stream=True
        )
        caplog.set_level(logging.DEBUG)

        async def stop_owned_worker() -> None:
            stream._logger.debug("Stopped stream: %s", SOURCE)

        with patch.object(
            stream, "_stop", new_callable=AsyncMock, side_effect=stop_owned_worker
        ) as stop:
            assert await hass.config_entries.async_unload(entry.entry_id)
            stop.assert_awaited_once()
        await events.close()
        assert SOURCE not in caplog.text
        assert not stream._logger.filters
        client.async_claim_media.assert_not_awaited()


async def test_concurrent_companion_connections_have_one_owner(
    hass: HomeAssistant, hass_client, client: Mock, session: Session
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        http = await hass_client()
        path = f"/api/ipguard/media/{entry.entry_id}/events"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        results = await asyncio.gather(
            http.ws_connect(path, headers=headers),
            http.ws_connect(path, headers=headers),
            return_exceptions=True,
        )
        denied = [
            value for value in results if isinstance(value, WSServerHandshakeError)
        ]
        accepted = [value for value in results if not isinstance(value, Exception)]
        assert len(denied) == len(accepted) == 1
        assert denied[0].status == 409
        bridge = entry.runtime_data.media
        assert len(bridge.streams) == 1
        assert len(bridge.handlers) == 1
        assert (await accepted[0].receive_json())["state"] == "pending"
        await accepted[0].close()
        results = await asyncio.gather(*tuple(bridge.handlers), return_exceptions=True)
        assert all(
            result is None or isinstance(result, asyncio.CancelledError)
            for result in results
        )
        await hass.async_block_till_done()
        assert not bridge.streams, (len(bridge.handlers), len(bridge.closers))
        assert not bridge.available
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_fragmented_and_oversized_bridge_body_reads() -> None:
    reader = Mock()
    reader.read = AsyncMock(side_effect=[b'{"ver', b'sion":1}', b""])
    assert await read_bounded(reader) == b'{"version":1}'
    reader.read = AsyncMock(side_effect=[b"x" * 100_000, b"y"])
    assert await read_bounded(reader) is None


async def test_native_rtsp_verbose_sdp_is_private_to_owned_worker(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, socket_enabled
) -> None:
    import av
    import pytest_socket

    pytest_socket.socket_allow_hosts(["127.0.0.1"])

    marker = "synthetic-native-private-sdp"
    sdp = (
        "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n"
        f"s={marker}\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
        "a=control:trackID=0\r\n"
    ).encode()

    async def rtsp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                lines = request.decode().split("\r\n")
                method = lines[0].split()[0]
                sequence = next(
                    line.split(":", 1)[1].strip()
                    for line in lines
                    if line.lower().startswith("cseq:")
                )
                response = f"RTSP/1.0 200 OK\r\nCSeq: {sequence}\r\n"
                body = b""
                if method == "OPTIONS":
                    response += "Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN\r\n"
                if method == "DESCRIBE":
                    body = sdp
                    response += f"Content-Type: application/sdp\r\nContent-Length: {len(body)}\r\n"
                if method == "SETUP":
                    response += "Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\nSession: synthetic\r\n"
                if method == "PLAY":
                    response += "Session: synthetic\r\n"
                writer.write(response.encode() + b"\r\n" + body)
                await writer.drain()
                if method == "PLAY":
                    return
        except asyncio.IncompleteReadError, ConnectionError:
            return
        finally:
            writer.close()
            await writer.wait_closed()

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="libav")
    previous = av.logging.get_level()
    av.logging.set_level(av.logging.VERBOSE)
    server = await asyncio.start_server(rtsp, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    source = f"rtsp://127.0.0.1:{port}/synthetic-private-path"

    def worker() -> None:
        with av.open(source, options={"rtsp_transport": "tcp"}, timeout=1):
            pass

    try:
        await hass.async_add_executor_job(
            _run_private_media_worker,
            worker,
            logging.getLogger("synthetic-private-stream"),
        )
        assert marker not in caplog.text and source not in caplog.text
        assert not any(
            thread.name == "IPGUARD media log cleanup" for thread in threads()
        )
        av.logging.log(av.logging.VERBOSE, "rtsp", "Unrelated camera message")
        assert "Unrelated camera message" in caplog.text
    finally:
        av.logging.set_level(previous)
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("state", ["idle", SDP + TOKEN + CALL])
async def test_camera_unavailable_without_waiting_for_a_future_call(
    hass: HomeAssistant,
    client: Mock,
    session: Session,
    state: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ipguard")
    entry = entry_for(hass, session)
    bridge = MediaBridge(client, TOKEN, SOURCE, MediaSnapshot(state))
    runtime = IpguardRuntimeData(client, IpguardCoordinator(hass, entry), media=bridge)
    runtime.coordinator.connected = True
    camera = IpguardIncomingCamera(entry.entry_id, runtime, bridge)
    camera.hass = hass
    assert not camera.available
    assert await camera.stream_source() is None
    assert await camera.async_create_stream() is None
    assert await camera.async_camera_image()
    expected_state = "idle" if state == "idle" else "unknown"
    assert any(
        f"companion_connected=False media_state={expected_state}" in message
        for message in caplog.messages
    )
    assert "IPGUARD video playback stream_ready=False" in caplog.messages
    assert all(
        secret not in caplog.text for secret in (TOKEN, SOURCE, SDP, CALL, CLAIM)
    )


async def test_http_bridge_scope_state_and_operations(
    hass: HomeAssistant,
    hass_client,
    client: Mock,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        http = await hass_client()
        path = f"/api/ipguard/media/{entry.entry_id}"
        response = await http.post(
            f"{path}/claim", json={"version": 1, "call_id": CALL}
        )
        assert response.status == 401
        client.async_claim_media.assert_not_awaited()
        headers = {"Authorization": f"Bearer {TOKEN}"}
        events = await http.ws_connect(f"{path}/events", headers=headers)
        await events.receive_json()
        response = await http.post(
            f"{path}/state", headers=headers, json={"version": 1}
        )
        assert (await response.json())["state"] == "pending"
        client.async_claim_media.assert_not_awaited()
        response = await http.post(
            f"{path}/claim", headers=headers, json={"version": 1, "call_id": CALL}
        )
        assert await response.json() == {
            "version": 1,
            "call_id": CALL,
            "claim_id": CLAIM,
            "sdp": SDP,
        }
        client.async_claim_media.assert_awaited_once_with(CALL)
        response = await http.post(
            f"{path}/answer",
            headers=headers,
            json={"version": 1, "call_id": CALL, "claim_id": CLAIM, "sdp": SDP},
        )
        assert await response.json() == {"version": 1, "outcome": "sent"}
        client.async_submit_media_answer.assert_awaited_once_with(CALL, CLAIM, SDP)
        for operation in ("streaming", "end"):
            response = await http.post(
                f"{path}/{operation}",
                headers=headers,
                json={"version": 1, "call_id": CALL, "claim_id": CLAIM},
            )
            assert response.status == 200
        client.mark_media_streaming.assert_called_once_with(CALL, CLAIM)
        client.end_media.assert_called_once_with(CALL, CLAIM)
        response = await http.post(
            "/api/ipguard/media/another-entry/state",
            headers=headers,
            json={"version": 1},
        )
        assert response.status == 401
        assert TOKEN not in caplog.text and SDP not in caplog.text
        assert await hass.config_entries.async_unload(entry.entry_id)
        await events.close()
        response = await http.post(
            f"{path}/state", headers=headers, json={"version": 1}
        )
        assert response.status == 401


async def test_bridge_errors_and_oversized_bodies_are_sanitized(
    hass: HomeAssistant,
    hass_client,
    client: Mock,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        http = await hass_client()
        path = f"/api/ipguard/media/{entry.entry_id}/claim"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        events = await http.ws_connect(
            f"/api/ipguard/media/{entry.entry_id}/events", headers=headers
        )
        await events.receive_json()
        for error, status, category in (
            (MediaUnavailableError(TOKEN + SDP), 409, "unavailable"),
            (RuntimeError(TOKEN + SDP), 502, "media_failed"),
        ):
            client.async_claim_media.side_effect = error
            response = await http.post(
                path, headers=headers, json={"version": 1, "call_id": CALL}
            )
            assert response.status == status
            assert await response.json() == {"version": 1, "error": category}
        for value in (
            {"version": True, "call_id": CALL},
            {"version": 1, "call_id": CALL, "extra": SDP},
            {"version": 1, "call_id": "x" * 100_001},
        ):
            response = await http.post(path, headers=headers, json=value)
            assert response.status == 400
        assert TOKEN not in caplog.text and SDP not in caplog.text
        assert await hass.config_entries.async_unload(entry.entry_id)
        await events.close()


async def test_events_push_state_without_media_and_close_on_unload(
    hass: HomeAssistant,
    hass_client,
    client: Mock,
    session: Session,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.ipguard")
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        http = await hass_client()
        stream = await http.ws_connect(
            f"/api/ipguard/media/{entry.entry_id}/events",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert (await stream.receive_json())["state"] == "pending"
        client.subscribe_media.call_args.args[0](MediaSnapshot("ended"))
        assert (await stream.receive_json())["state"] == "ended"
        client.async_claim_media.assert_not_awaited()
        bridge = entry.runtime_data.media
        assert len(bridge.handlers) == 1
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert (await stream.receive()).type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
        assert not bridge.streams, (len(bridge.handlers), len(bridge.closers))
        assert "IPGUARD video companion connected" in caplog.messages
        assert "IPGUARD video companion disconnected" in caplog.messages
        assert all(
            secret not in caplog.text for secret in (TOKEN, SOURCE, SDP, CALL, CLAIM)
        )


@pytest.mark.parametrize("state", ["pending", "starting", "answer_sent", "streaming"])
async def test_companion_disconnect_ends_only_its_claimed_media(
    hass: HomeAssistant,
    hass_client,
    client: Mock,
    session: Session,
    state: str,
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        http = await hass_client()
        path = f"/api/ipguard/media/{entry.entry_id}"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        events = await http.ws_connect(f"{path}/events", headers=headers)
        assert (await events.receive_json())["state"] == "pending"
        bridge = entry.runtime_data.media
        listener = client.subscribe_media.call_args.args[0]

        async def claim_media(call_id: str) -> MediaOffer:
            client.media_snapshot = MediaSnapshot(state, call_id)
            listener(client.media_snapshot)
            return MediaOffer(call_id, CLAIM, SDP, monotonic() + 30)

        def end_media(call_id: str, claim_id: str) -> None:
            assert (call_id, claim_id) == (CALL, CLAIM)
            client.media_snapshot = MediaSnapshot("ended", CALL)
            listener(client.media_snapshot)

        client.async_claim_media.side_effect = claim_media
        client.end_media.side_effect = end_media
        if state != "pending":
            response = await http.post(
                f"{path}/claim", headers=headers, json={"version": 1, "call_id": CALL}
            )
            assert response.status == 200
            assert (await events.receive_json())["state"] == state
            assert bridge.active_claim == (CALL, CLAIM)
            assert CLAIM not in repr(bridge)
        await events.close()
        await asyncio.gather(*tuple(bridge.handlers), return_exceptions=True)
        assert not bridge.streams
        assert bridge.active_claim is None
        assert bridge.snapshot.state == ("pending" if state == "pending" else "ended")
        if state == "pending":
            client.end_media.assert_not_called()
        else:
            client.end_media.assert_called_once_with(CALL, CLAIM)
        restarted = await http.ws_connect(f"{path}/events", headers=headers)
        assert (await restarted.receive_json())["state"] == bridge.snapshot.state
        client.async_submit_media_answer.assert_not_awaited()
        client.async_release.assert_not_awaited()
        await restarted.close()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_claim_requires_a_current_companion_lifecycle_owner(
    hass: HomeAssistant, hass_client, client: Mock, session: Session
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        http = await hass_client()
        response = await http.post(
            f"/api/ipguard/media/{entry.entry_id}/claim",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"version": 1, "call_id": CALL},
        )
        assert response.status == 409
        assert await response.json() == {"version": 1, "error": "unavailable"}
        client.async_claim_media.assert_not_awaited()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_stalled_claim_cannot_attach_to_a_reconnected_companion(
    hass: HomeAssistant, hass_client, client: Mock, session: Session
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        http = await hass_client()
        path = f"/api/ipguard/media/{entry.entry_id}"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        original = await http.ws_connect(f"{path}/events", headers=headers)
        await original.receive_json()
        reading = asyncio.Event()
        finish_reading = asyncio.Event()

        async def delayed_body(_request) -> dict[str, object]:
            reading.set()
            await finish_reading.wait()
            return {"version": 1, "call_id": CALL}

        async def post_claim():
            return await http.post(
                f"{path}/claim", headers=headers, json={"version": 1, "call_id": CALL}
            )

        with patch("custom_components.ipguard.media.read_json", delayed_body):
            pending = asyncio.create_task(post_claim())
            await reading.wait()
            await original.close()
            await asyncio.gather(
                *tuple(entry.runtime_data.media.handlers), return_exceptions=True
            )
            replacement = await http.ws_connect(f"{path}/events", headers=headers)
            assert (await replacement.receive_json())["state"] == "pending"
            finish_reading.set()
            response = await pending
            assert response.status == 409
            client.async_claim_media.assert_not_awaited()
            client.end_media.assert_not_called()
            await replacement.close()
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_claim_completed_after_owner_disconnect_is_ended_locally(
    hass: HomeAssistant, hass_client, client: Mock, session: Session
) -> None:
    configure_media(client)
    entry = entry_for(hass, session)
    with patch("custom_components.ipguard.IpguardClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        http = await hass_client()
        path = f"/api/ipguard/media/{entry.entry_id}"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        events = await http.ws_connect(f"{path}/events", headers=headers)
        await events.receive_json()
        claiming = asyncio.Event()
        finish_claim = asyncio.Event()

        async def delayed_claim(call_id: str) -> MediaOffer:
            claiming.set()
            await finish_claim.wait()
            return MediaOffer(call_id, CLAIM, SDP, monotonic() + 30)

        async def post_claim():
            return await http.post(
                f"{path}/claim", headers=headers, json={"version": 1, "call_id": CALL}
            )

        client.async_claim_media.side_effect = delayed_claim
        pending = asyncio.create_task(post_claim())
        await claiming.wait()
        await events.close()
        await asyncio.gather(
            *tuple(entry.runtime_data.media.handlers), return_exceptions=True
        )
        finish_claim.set()
        response = await pending
        assert response.status == 409
        client.end_media.assert_called_once_with(CALL, CLAIM)
        client.async_submit_media_answer.assert_not_awaited()
        assert entry.runtime_data.media.active_claim is None
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_pair_nonce_is_single_use_scoped_and_validates_network_source(
    hass: HomeAssistant, hass_client
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    http = await hass_client()
    owner = manager(hass)
    pairing = Pairing(
        "scrypted.example.invalid",
        "http://ha.example.invalid:8123",
        "one-use-code",
        TOKEN,
        monotonic() + 60,
    )
    owner.pairing["local-entry"] = pairing
    path = "/api/ipguard/pair/local-entry"
    body = {"version": 1, "pairing_code": "one-use-code", "rtsp_url": SOURCE}
    response = await http.post(path, json={**body, "pairing_code": "wrong-code"})
    assert response.status == 401
    response = await http.post(
        path, json={**body, "rtsp_url": "rtsp://elsewhere.example.invalid:1234/path"}
    )
    assert response.status == 400
    response = await http.post(path, json=body)
    assert await response.json() == {
        "version": 1,
        "credential": TOKEN,
        "bridge_url": "http://ha.example.invalid:8123/api/ipguard/media/local-entry",
    }
    assert pairing.source == SOURCE
    response = await http.post(path, json=body)
    assert response.status == 401
    owner.pairing["local-entry"] = Pairing(
        pairing.host, pairing.ha_url, "expired-code", TOKEN, monotonic() - 1
    )
    response = await http.post(path, json={**body, "pairing_code": "expired-code"})
    assert response.status == 401
    assert TOKEN not in repr(owner) and TOKEN not in repr(pairing)


async def test_programmatic_pair_does_not_store_transient_platform_password(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    entry = entry_for(hass, session)
    before_data = dict(entry.data)
    with (
        patch(
            "custom_components.ipguard.config_flow.pair_companion",
            new_callable=AsyncMock,
            return_value=(TOKEN, SOURCE),
        ) as paired,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_VIDEO_ENABLED: True}
        )
        assert result["step_id"] == "video"
        supplied = {
            CONF_SCRYPTED_URL: "https://scrypted.example.invalid:10443",
            CONF_HA_URL: "http://ha.example.invalid:8123",
            "username": "admin",
            "password": "transient-scrypted-password",
            CONF_VERIFY_SSL: True,
        }
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], supplied
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        paired.assert_awaited_once()
        assert entry.data == before_data
        assert entry.options[CONF_COMPANION_CREDENTIAL] == TOKEN
        assert entry.options[CONF_RTSP_URL] == SOURCE
        assert "password" not in entry.options and "username" not in entry.options
        assert "transient-scrypted-password" not in json.dumps(dict(entry.options))


@pytest.mark.parametrize(
    "supplied",
    [{}, {CONF_SCRYPTED_URL: "", CONF_HA_URL: "", "username": "", "password": ""}],
)
async def test_video_blank_fields_use_app_ingress_and_ha_address(
    hass: HomeAssistant, client: Mock, session: Session, supplied: dict[str, str]
) -> None:
    entry = entry_for(hass, session)
    app = ScryptedApp(
        server_url("https://172.30.32.1:10443", https=True),
        "/api/hassio_ingress/private-app-entry",
    )
    ingress = (
        server_url("http://supervisor", https=False)
        / "ingress"
        / "private-app-entry"
        / "endpoint"
        / "scrypted-ipguard"
        / "pair",
        {"Cookie": "ingress_session=private-transient-session"},
    )
    ha_url = server_url("http://homeassistant:8123", https=False)
    with (
        patch(
            "custom_components.ipguard.config_flow.async_find_scrypted_app",
            new_callable=AsyncMock,
            return_value=app,
        ) as discovered,
        patch(
            "custom_components.ipguard.config_flow.async_ingress_pairing",
            new_callable=AsyncMock,
            return_value=ingress,
        ) as ingress_pairing,
        patch(
            "custom_components.ipguard.config_flow.default_ha_url", return_value=ha_url
        ) as own_url,
        patch(
            "custom_components.ipguard.config_flow.pair_companion",
            new_callable=AsyncMock,
            return_value=(TOKEN, SOURCE),
        ) as paired,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_VIDEO_ENABLED: True}
        )
        assert result["step_id"] == "video"
        assert result["data_schema"]({}) == {CONF_VERIFY_SSL: True}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], supplied
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    discovered.assert_awaited_once_with(hass, None)
    ingress_pairing.assert_awaited_once_with(hass, app)
    own_url.assert_called_with(hass, app=True)
    assert paired.await_args.args[3:7] == (app.url, ha_url, "", "")
    assert paired.await_args.kwargs == {"verify_ssl": True, "ingress": ingress}
    assert entry.options[CONF_SCRYPTED_URL] == str(app.url)
    assert entry.options[CONF_HA_URL] == str(ha_url)
    stored = json.dumps(dict(entry.options))
    assert (
        "private-app-entry" not in stored and "private-transient-session" not in stored
    )
    assert "username" not in entry.options and "password" not in entry.options
    client.async_submit_media_answer.assert_not_awaited()
    client.async_release.assert_not_awaited()


async def test_video_explicit_urls_and_credentials_override_discovery(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    entry = entry_for(hass, session)
    supplied = {
        CONF_SCRYPTED_URL: "https://chosen.example.invalid:14443",
        CONF_HA_URL: "https://ha.example.invalid",
        "username": "chosen-admin",
        "password": "transient-password",
        CONF_VERIFY_SSL: False,
    }
    with (
        patch(
            "custom_components.ipguard.config_flow.async_find_scrypted_app",
            new_callable=AsyncMock,
        ) as discovered,
        patch(
            "custom_components.ipguard.config_flow.async_ingress_pairing",
            new_callable=AsyncMock,
        ) as ingress_pairing,
        patch(
            "custom_components.ipguard.config_flow.pair_companion",
            new_callable=AsyncMock,
            return_value=(TOKEN, SOURCE),
        ) as paired,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_VIDEO_ENABLED: True}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], supplied
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    discovered.assert_not_awaited()
    ingress_pairing.assert_not_awaited()
    assert paired.await_args.args[3:7] == (
        server_url(supplied[CONF_SCRYPTED_URL], https=True),
        server_url(supplied[CONF_HA_URL], https=False),
        "chosen-admin",
        "transient-password",
    )
    assert paired.await_args.kwargs == {"verify_ssl": False, "ingress": None}


async def test_video_reuses_unique_configured_server_address_but_not_its_login(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    entry = entry_for(hass, session)
    MockConfigEntry(
        domain="scrypted",
        data={
            "host": "known.example.invalid",
            "username": "another-user",
            "password": "another-integration-secret",
        },
    ).add_to_hass(hass)
    with (
        patch(
            "custom_components.ipguard.config_flow.async_find_scrypted_app",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "custom_components.ipguard.config_flow.default_ha_url",
            return_value=server_url("http://ha.example.invalid:8123", https=False),
        ),
        patch(
            "custom_components.ipguard.config_flow.pair_companion",
            new_callable=AsyncMock,
            return_value=(TOKEN, SOURCE),
        ) as paired,
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_VIDEO_ENABLED: True}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"username": "own-admin", "password": "own-transient-secret"},
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert str(paired.await_args.args[3]) == "https://known.example.invalid:10443"
    assert paired.await_args.args[5:7] == ("own-admin", "own-transient-secret")
    assert "another-integration-secret" not in json.dumps(dict(entry.options))


@pytest.mark.parametrize(
    "category", ["cannot_discover", "multiple_scrypted", "ha_url_unavailable"]
)
async def test_video_discovery_errors_are_actionable_without_pairing(
    hass: HomeAssistant, client: Mock, session: Session, category: str
) -> None:
    entry = entry_for(hass, session)
    with (
        patch(
            "custom_components.ipguard.config_flow.async_find_scrypted_app",
            new_callable=AsyncMock,
            side_effect=CompanionError(category),
        ),
        patch(
            "custom_components.ipguard.config_flow.pair_companion",
            new_callable=AsyncMock,
        ) as paired,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_VIDEO_ENABLED: True}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
    assert result["step_id"] == "video" and result["errors"] == {"base": category}
    paired.assert_not_awaited()


@pytest.mark.parametrize(
    "hosts,category",
    [
        ([], "scrypted_not_found"),
        (["first.example.invalid", "second.example.invalid"], "multiple_scrypted"),
        (["only.example.invalid"], "scrypted_credentials_required"),
    ],
)
async def test_video_does_not_guess_server_or_stored_credentials(
    hass: HomeAssistant, client: Mock, session: Session, hosts: list[str], category: str
) -> None:
    entry = entry_for(hass, session)
    for host in hosts:
        MockConfigEntry(
            domain="scrypted",
            data={"host": host, "username": "saved-user", "password": "saved-secret"},
        ).add_to_hass(hass)
    with (
        patch(
            "custom_components.ipguard.config_flow.async_find_scrypted_app",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "custom_components.ipguard.config_flow.default_ha_url",
            return_value=server_url("http://ha.example.invalid:8123", https=False),
        ),
        patch(
            "custom_components.ipguard.config_flow.pair_companion",
            new_callable=AsyncMock,
        ) as paired,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_VIDEO_ENABLED: True}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
    assert result["errors"] == {"base": category}
    paired.assert_not_awaited()


async def test_ingress_pair_uses_only_transient_cookie_and_scrypted_network_host(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    app_url = server_url("https://172.30.32.1:10443", https=True)
    ha_url = server_url("http://homeassistant:8123", https=False)
    endpoint = (
        server_url("http://supervisor", https=False)
        / "ingress"
        / "private-app-entry"
        / "endpoint"
        / "scrypted-ipguard"
        / "pair"
    )
    response = Mock(status=200)
    response.content.read = AsyncMock(side_effect=[b'{"version":1,"paired":true}', b""])
    request = Mock()
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)

    def post(url, **kwargs):
        assert url == endpoint
        assert kwargs["headers"] == {
            "Cookie": "ingress_session=private-transient-session"
        }
        assert kwargs["json"]["scrypted_host"] == "172.30.32.1"
        assert kwargs["json"]["ha_url"] == str(ha_url)
        manager(hass).pairing[
            "entry"
        ].source = "rtsp://172.30.32.1:43210/private-stream-token"
        return context

    request.post.side_effect = post
    credential, source = await pair_companion(
        hass,
        request,
        "entry",
        app_url,
        ha_url,
        "",
        "",
        verify_ssl=True,
        ingress=(endpoint, {"Cookie": "ingress_session=private-transient-session"}),
    )
    assert credential and source.startswith("rtsp://172.30.32.1:")
    assert not manager(hass).pairing
    assert (
        "private-transient-session" not in caplog.text and credential not in caplog.text
    )


async def test_disabling_video_immediately_revokes_old_pair(
    hass: HomeAssistant, client: Mock, session: Session
) -> None:
    entry = entry_for(hass, session)
    bridge = MediaBridge(client, TOKEN, SOURCE, MediaSnapshot("pending", CALL))
    manager(hass).bridges[entry.entry_id] = bridge
    with patch(
        "homeassistant.config_entries.ConfigEntries.async_reload", return_value=True
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_VIDEO_ENABLED: False}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert not bridge.authorized(f"Bearer {TOKEN}")
    assert CONF_COMPANION_CREDENTIAL not in entry.options
    assert CONF_RTSP_URL not in entry.options
    assert entry.data[CONF_SESSION] == session.to_dict()
    client.set_media_enabled.assert_called_once_with(False)


async def test_pair_transport_failures_and_cancel_discard_nonce(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    session = Mock()
    session.post.side_effect = RuntimeError(TOKEN + SDP)
    with pytest.raises(CompanionError, match="cannot_pair"):
        await pair_companion(
            hass,
            session,
            "entry",
            server_url("https://scrypted.example.invalid:10443", https=True),
            server_url("http://ha.example.invalid:8123", https=False),
            "admin",
            "transient-secret",
            verify_ssl=True,
        )
    assert not manager(hass).pairing
    session.post.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await pair_companion(
            hass,
            session,
            "entry",
            server_url("https://scrypted.example.invalid:10443", https=True),
            server_url("http://ha.example.invalid:8123", https=False),
            "admin",
            "transient-secret",
            verify_ssl=True,
        )
    assert not manager(hass).pairing
    assert TOKEN not in caplog.text and SDP not in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        "http://scrypted.example.invalid",
        "https://user:password@scrypted.example.invalid",
        "https://scrypted.example.invalid/path",
        "https://scrypted.example.invalid?secret=value",
        "file:///tmp/source",
        "https://0.0.0.0",
    ],
)
def test_server_url_rejects_credentials_paths_and_non_https(value: str) -> None:
    with pytest.raises(CompanionError):
        server_url(value, https=True)


@pytest.mark.parametrize(
    "value",
    [
        "http://scrypted.example.invalid/path",
        "rtsp://other.example.invalid/path",
        "rtsp://user:password@scrypted.example.invalid/path",
        "rtsp://scrypted.example.invalid/path?token=value",
        "rtsp://scrypted.example.invalid/",
    ],
)
def test_rtsp_source_validation(value: str) -> None:
    with pytest.raises(CompanionError):
        rtsp_url(value, "scrypted.example.invalid")
