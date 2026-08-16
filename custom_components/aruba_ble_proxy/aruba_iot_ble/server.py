from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from websockets.exceptions import ConnectionClosed
from websockets.asyncio.server import serve

from .aruba_proto import ArubaTelemetryDecoder, ArubaTelemetryMessage
from .models import ArubaBleEvent

LOGGER = logging.getLogger(__name__)
EventHandler = Callable[[ArubaBleEvent], Awaitable[None]]
MessageHandler = Callable[[ArubaTelemetryMessage], Awaitable[None]]
SourceDisconnectHandler = Callable[[str], None]
SEND_TIMEOUT = 2.0
MAX_MESSAGE_SIZE = 1024 * 1024
MAX_MESSAGE_QUEUE = 16
MAX_CONNECTIONS = 128
MAX_SOURCES = 1024
MAX_DECODE_ERRORS = 3
MAX_TEXT_MESSAGES = 3
DEFAULT_ENDPOINT_PATH = "/aruba-ble-proxy"
_MAC_RE = re.compile(r"^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$")


@dataclass
class ReceiverStats:
    connections_opened: int = 0
    connections_closed: int = 0
    binary_messages: int = 0
    text_messages: int = 0
    invalid_tokens: int = 0
    decode_errors: int = 0
    rejected_connections: int = 0
    rejected_paths: int = 0
    rejected_sources: int = 0
    source_replacements: int = 0
    last_peer: str | None = None


class ArubaBleReceiver:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 7443,
        access_token: str | None = None,
        endpoint_path: str = DEFAULT_ENDPOINT_PATH,
        event_handler: EventHandler | None = None,
        message_handler: MessageHandler | None = None,
        source_disconnect_handler: SourceDisconnectHandler | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.decoder = ArubaTelemetryDecoder(access_token=access_token)
        self.endpoint_path = _normalize_endpoint_path(endpoint_path)
        self.event_handler = event_handler or log_event
        self.message_handler = message_handler
        self.source_disconnect_handler = source_disconnect_handler
        self.stats = ReceiverStats()
        self._connections_by_source: dict[str, object] = {}
        # Aruba clusters may multiplex reports from several AP radios over one
        # transport WebSocket. Keep every reporter source associated with the
        # physical connection so southbound actions can be routed back through
        # that same socket.
        self._sources_by_connection: dict[int, set[str]] = {}
        self._active_connections: set[int] = set()
        self._send_locks: dict[str, asyncio.Lock] = {}

    async def run(self) -> None:
        LOGGER.info("Starting Aruba BLE receiver on %s:%s", self.host, self.port)
        async with serve(
            self._handle_connection,
            self.host,
            self.port,
            max_size=MAX_MESSAGE_SIZE,
            max_queue=MAX_MESSAGE_QUEUE,
        ):
            await asyncio.Future()

    async def _handle_connection(self, websocket) -> None:
        peer = websocket.remote_address
        peer_name = _peer_name(peer)
        self.stats.connections_opened += 1
        self.stats.last_peer = peer_name
        connection_id = id(websocket)
        if len(self._active_connections) >= MAX_CONNECTIONS:
            self.stats.rejected_connections += 1
            await websocket.close(code=1013, reason="connection limit reached")
            self.stats.connections_closed += 1
            return
        self._active_connections.add(connection_id)

        request_path = _websocket_request_path(websocket)
        if request_path is not None and request_path != self.endpoint_path:
            self.stats.rejected_paths += 1
            await websocket.close(code=1008, reason="invalid endpoint path")
            self._active_connections.discard(connection_id)
            self.stats.connections_closed += 1
            return
        LOGGER.info("Aruba WebSocket connected: %s", peer)

        connection_decode_errors = 0
        connection_text_messages = 0
        try:
            try:
                async for message in websocket:
                    if isinstance(message, str):
                        self.stats.text_messages += 1
                        connection_text_messages += 1
                        LOGGER.debug("Ignoring text WebSocket message from %s: %s", peer, message)
                        if connection_text_messages >= MAX_TEXT_MESSAGES:
                            await websocket.close(code=1003, reason="binary telemetry required")
                            return
                        continue

                    self.stats.binary_messages += 1
                    try:
                        decoded = self.decoder.decode_message(message)
                    except PermissionError:
                        self.stats.invalid_tokens += 1
                        LOGGER.warning("Closing Aruba WebSocket with invalid token from %s", peer)
                        await websocket.close(code=1008, reason="invalid token")
                        return
                    except Exception:
                        self.stats.decode_errors += 1
                        connection_decode_errors += 1
                        if connection_decode_errors == 1:
                            LOGGER.exception("Failed to decode Aruba telemetry from %s", peer)
                        else:
                            LOGGER.warning(
                                "Repeated invalid Aruba telemetry from %s (%d/%d)",
                                peer,
                                connection_decode_errors,
                                MAX_DECODE_ERRORS,
                            )
                        if connection_decode_errors >= MAX_DECODE_ERRORS:
                            await websocket.close(code=1008, reason="invalid telemetry")
                            return
                        continue

                    if not await self._bind_connection_source(
                        websocket,
                        decoded.reporter.source,
                    ):
                        return
                    if self.message_handler is not None:
                        await self.message_handler(decoded)
                    for event in decoded.events:
                        await self.event_handler(event)
            except ConnectionClosed as err:
                LOGGER.info("Aruba WebSocket closed from %s: %s", peer, err)
        finally:
            disconnected_sources = self._forget_connection(websocket)
            self._active_connections.discard(connection_id)
            for source in disconnected_sources:
                self._notify_source_disconnected(source)
            self.stats.connections_closed += 1
            LOGGER.info("Aruba WebSocket disconnected: %s", peer)

    async def async_send_to_source(self, source: str, payload: bytes) -> None:
        normalized_source = source.upper()
        websocket = self._connections_by_source.get(normalized_source)
        if websocket is None:
            raise KeyError(f"No active Aruba WebSocket for source {source}")
        lock = self._send_locks.get(normalized_source)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[normalized_source] = lock
        async with lock:
            await asyncio.wait_for(websocket.send(payload), timeout=SEND_TIMEOUT)

    def connected_sources(self) -> list[str]:
        return sorted(self._connections_by_source)

    async def _bind_connection_source(self, websocket, source: str) -> bool:
        normalized_source = _normalize_source(source)
        if normalized_source is None:
            await websocket.close(code=1008, reason="invalid reporter source")
            return False

        connection_id = id(websocket)
        previous = self._connections_by_source.get(normalized_source)
        if previous is None and len(self._connections_by_source) >= MAX_SOURCES:
            self.stats.rejected_sources += 1
            await websocket.close(code=1013, reason="source limit reached")
            return False
        if previous is not None and previous is not websocket:
            self.stats.source_replacements += 1
            previous_sources = self._sources_by_connection.get(id(previous))
            if previous_sources is not None:
                previous_sources.discard(normalized_source)
                if not previous_sources:
                    self._sources_by_connection.pop(id(previous), None)

        self._connections_by_source[normalized_source] = websocket
        self._sources_by_connection.setdefault(connection_id, set()).add(
            normalized_source
        )
        return True

    def _forget_connection(self, websocket) -> list[str]:
        disconnected_sources = []
        sources = self._sources_by_connection.pop(id(websocket), set())
        for source in sources:
            if self._connections_by_source.get(source) is not websocket:
                continue
            self._connections_by_source.pop(source, None)
            self._send_locks.pop(source, None)
            disconnected_sources.append(source)
        return sorted(disconnected_sources)

    def _notify_source_disconnected(self, source: str) -> None:
        if self.source_disconnect_handler is None:
            return
        try:
            self.source_disconnect_handler(source)
        except Exception:
            LOGGER.exception("Aruba source disconnect handler failed for %s", source)


def _peer_name(peer) -> str | None:
    if peer is None:
        return None
    if isinstance(peer, tuple):
        return ":".join(str(part) for part in peer)
    return str(peer)


def _normalize_endpoint_path(value: str) -> str:
    path = str(value).strip() or DEFAULT_ENDPOINT_PATH
    return path if path.startswith("/") else f"/{path}"


def _websocket_request_path(websocket) -> str | None:
    request = getattr(websocket, "request", None)
    path = getattr(request, "path", None)
    return str(path) if path is not None else None


def _normalize_source(value: str) -> str | None:
    compact = str(value).strip().replace("-", ":").upper()
    return compact if _MAC_RE.fullmatch(compact) else None


async def log_event(event: ArubaBleEvent) -> None:
    LOGGER.info(
        "BLE %s source=%s frame=%s rssi=%s name=%s uuids=%s mfg=%s service_data=%s",
        event.address,
        event.source,
        getattr(event.frame_type, "name", event.frame_type),
        event.rssi,
        event.advertisement.local_name,
        list(event.advertisement.service_uuids),
        sorted(event.advertisement.manufacturer_data),
        sorted(event.advertisement.service_data),
    )
    LOGGER.debug("BLE normalized event: %s", event_to_json(event))


class SummaryEventLogger:
    def __init__(self) -> None:
        self.event_count = 0
        self.addresses: set[str] = set()
        self.sources: set[str] = set()
        self.service_data_uuids: set[str] = set()
        self.manufacturer_ids: set[int] = set()
        self.local_names: set[str] = set()

    async def __call__(self, event: ArubaBleEvent) -> None:
        self.event_count += 1
        self.addresses.add(event.address)
        self.sources.add(event.source)
        self.service_data_uuids.update(event.advertisement.service_data)
        self.manufacturer_ids.update(event.advertisement.manufacturer_data)
        if event.advertisement.local_name:
            self.local_names.add(event.advertisement.local_name)

        if self.event_count == 1 or self.event_count % 50 == 0:
            LOGGER.info(
                "BLE summary events=%d addresses=%d sources=%d service_data=%s mfg=%s names=%s",
                self.event_count,
                len(self.addresses),
                len(self.sources),
                sorted(self.service_data_uuids),
                sorted(self.manufacturer_ids),
                sorted(self.local_names)[:10],
            )


def event_to_json(event: ArubaBleEvent) -> str:
    return json.dumps(
        {
            "source": event.source,
            "reporter": {
                "name": event.reporter.name,
                "mac": event.reporter.mac,
                "ipv4": event.reporter.ipv4,
                "hardware_type": event.reporter.hardware_type,
                "software_version": event.reporter.software_version,
            },
            "device": {
                "address": event.address,
                "frame_type": getattr(event.frame_type, "name", event.frame_type),
                "rssi": event.rssi,
                "address_type": getattr(event.address_type, "name", event.address_type),
                "apb_mac": event.apb_mac,
            },
            "advertisement": {
                "local_name": event.advertisement.local_name,
                "service_uuids": list(event.advertisement.service_uuids),
                "manufacturer_data": {
                    str(company_id): data.hex()
                    for company_id, data in event.advertisement.manufacturer_data.items()
                },
                "service_data": {
                    uuid: data.hex()
                    for uuid, data in event.advertisement.service_data.items()
                },
                "payload": event.payload[:64].hex() + ("..." if len(event.payload) > 64 else ""),
            },
        },
        sort_keys=True,
    )
