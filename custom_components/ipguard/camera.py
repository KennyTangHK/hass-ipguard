"""An optional account camera consuming the companion's local RTSP output."""

import asyncio
import logging
from base64 import b64decode
from collections.abc import Callable
from threading import Event, Thread

from homeassistant.components.camera import Camera
from homeassistant.components.camera.const import CameraEntityFeature
from homeassistant.components.stream import Stream
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .media import MediaBridge
from .runtime import IpguardConfigEntry, IpguardCoordinator, IpguardRuntimeData

_PLACEHOLDER = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _MediaLogFilter(logging.Filter):
    """Protect this camera's private source in the framework's stream messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = (
            "Local IPGUARD video transport failed"
            if record.levelno >= logging.WARNING
            else "Local IPGUARD video transport status"
        )
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _run_private_media_worker(
    worker: Callable[[], None], logger: logging.Logger
) -> None:
    """Discard only this worker thread's native logs before Python publication."""
    from av.logging import Capture

    stop = Event()
    with Capture(local=True) as records:

        def discard() -> None:
            while not stop.wait(0.01):
                records.clear()

        drainer = Thread(target=discard, name="IPGUARD media log cleanup", daemon=True)
        drainer.start()
        try:
            worker()
        except Exception:
            logger.error("Local IPGUARD video transport failed")
        finally:
            stop.set()
            drainer.join()
            records.clear()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IpguardConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    if runtime.media is not None:
        async_add_entities(
            [IpguardIncomingCamera(entry.entry_id, runtime, runtime.media)]
        )


class IpguardIncomingCamera(CoordinatorEntity[IpguardCoordinator], Camera):
    """A stable camera; only its RTSP consumer may cause media demand."""

    _attr_has_entity_name = True
    _attr_translation_key = "incoming_video"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self, entry_id: str, runtime: IpguardRuntimeData, bridge: MediaBridge
    ) -> None:
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, runtime.coordinator)
        self._runtime = runtime
        self._bridge = bridge
        self._attr_unique_id = f"{entry_id}_incoming_video"
        self.content_type = "image/png"
        self.stream_options = {"rtsp_transport": "tcp"}
        self._guarded_stream: Stream | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._stream_logger: logging.Logger | None = None
        self._log_filter: _MediaLogFilter | None = None

    @property
    def available(self) -> bool:
        return (
            self.coordinator.connected
            and not self._runtime.closing
            and self._bridge.available
        )

    async def async_added_to_hass(self) -> None:
        stream_logger = logging.getLogger(
            f"homeassistant.components.stream.stream.{self.entity_id}"
        )
        media_filter = _MediaLogFilter()
        stream_logger.addFilter(media_filter)
        self._stream_logger = stream_logger
        self._log_filter = media_filter
        self.async_on_remove(self._begin_cleanup)
        await super().async_added_to_hass()
        self.async_on_remove(self._bridge.subscribe(self.async_write_ha_state))

    async def stream_source(self) -> str | None:
        """Read the cached RTSP source without claiming or answering a call."""
        if not self.available:
            return None
        return self._bridge.source

    async def async_refresh_providers(self, *, write_state: bool = True) -> None:
        """Keep video on the audited RTSP/HLS path instead of another gateway."""

    async def async_create_stream(self) -> Stream | None:
        """Wrap the owned framework worker before it can open the RTSP source."""
        stream = await super().async_create_stream()
        if stream is None or stream is self._guarded_stream:
            return stream
        worker = stream._run_worker

        def guarded_worker() -> None:
            _run_private_media_worker(worker, stream._logger)

        worker_attribute = "_run_worker"
        setattr(stream, worker_attribute, guarded_worker)
        self._guarded_stream = stream
        return stream

    async def async_will_remove_from_hass(self) -> None:
        """Stop even a preloaded owned stream before removing privacy filters."""
        self._begin_cleanup()
        assert self._cleanup_task is not None
        await asyncio.shield(self._cleanup_task)
        await super().async_will_remove_from_hass()

    def _begin_cleanup(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._async_close_stream())

    async def _async_close_stream(self) -> None:
        if self.stream is not None:
            await self.stream.stop()
            await self.stream._stop()
        if self._stream_logger is not None and self._log_filter is not None:
            self._stream_logger.removeFilter(self._log_filter)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes:
        """Never open a provider peer merely to refresh a dashboard thumbnail."""
        return _PLACEHOLDER

    async def async_update(self) -> None:
        """Cached media and inventory pushes own availability."""
