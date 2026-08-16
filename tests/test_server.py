import asyncio

import pytest

from custom_components.aruba_ble_proxy.server import ArubaBleReceiver
from custom_components.aruba_ble_proxy import server as server_module


class _Socket:
    def __init__(self, messages=(), *, path=None):
        self.messages = list(messages)
        self.remote_address = ("192.0.2.1", 12345)
        self.closed = []
        self.request = type("Request", (), {"path": path})() if path else None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def close(self, *, code, reason):
        self.closed.append((code, reason))


def test_receiver_replaces_previous_connection_for_same_source():
    async def run_test():
        receiver = ArubaBleReceiver(access_token="secret")
        old_socket = _Socket()
        new_socket = _Socket()

        assert await receiver._bind_connection_source(
            old_socket, "02:00:00:00:00:01"
        )
        assert await receiver._bind_connection_source(
            new_socket, "02:00:00:00:00:01"
        )

        assert old_socket.closed == []
        assert receiver._forget_connection(old_socket) == []
        assert receiver.connected_sources() == ["02:00:00:00:00:01"]
        assert receiver.stats.source_replacements == 1
        assert receiver._connections_by_source["02:00:00:00:00:01"] is new_socket

    asyncio.run(run_test())


def test_receiver_tracks_multiple_ap_sources_on_one_cluster_connection():
    async def run_test():
        receiver = ArubaBleReceiver(access_token="secret")
        websocket = _Socket()

        assert await receiver._bind_connection_source(
            websocket, "02:00:00:00:00:01"
        )
        assert await receiver._bind_connection_source(
            websocket, "02:00:00:00:00:02"
        )

        assert websocket.closed == []
        assert receiver.connected_sources() == [
            "02:00:00:00:00:01",
            "02:00:00:00:00:02",
        ]
        assert receiver._forget_connection(websocket) == [
            "02:00:00:00:00:01",
            "02:00:00:00:00:02",
        ]

    asyncio.run(run_test())


def test_receiver_bounds_distinct_reporter_sources(monkeypatch):
    async def run_test():
        receiver = ArubaBleReceiver(access_token="secret")
        websocket = _Socket()
        monkeypatch.setattr(server_module, "MAX_SOURCES", 1)

        assert await receiver._bind_connection_source(
            websocket, "02:00:00:00:00:01"
        )
        assert not await receiver._bind_connection_source(
            websocket, "02:00:00:00:00:02"
        )

        assert websocket.closed == [(1013, "source limit reached")]
        assert receiver.stats.rejected_sources == 1
        assert receiver.connected_sources() == ["02:00:00:00:00:01"]

    asyncio.run(run_test())


def test_receiver_source_disconnect_handler_is_invoked():
    disconnected = []
    receiver = ArubaBleReceiver(
        access_token="secret",
        source_disconnect_handler=disconnected.append,
    )

    receiver._notify_source_disconnected("02:00:00:00:00:01")

    assert disconnected == ["02:00:00:00:00:01"]


def test_receiver_send_to_source_times_out(monkeypatch):
    class WebSocket:
        async def send(self, payload):
            await asyncio.Future()

    async def run_test():
        receiver = ArubaBleReceiver(access_token="secret")
        assert await receiver._bind_connection_source(
            WebSocket(), "02:00:00:00:00:01"
        )
        monkeypatch.setattr(server_module, "SEND_TIMEOUT", 0.001)

        with pytest.raises(TimeoutError):
            await receiver.async_send_to_source("02:00:00:00:00:01", b"payload")

    asyncio.run(run_test())


def test_receiver_rejects_wrong_endpoint_path():
    async def run_test():
        receiver = ArubaBleReceiver(access_token="secret", endpoint_path="/expected")
        websocket = _Socket(path="/wrong")

        await receiver._handle_connection(websocket)

        assert websocket.closed == [(1008, "invalid endpoint path")]
        assert receiver.stats.rejected_paths == 1

    asyncio.run(run_test())


def test_receiver_closes_connection_after_repeated_decode_errors(monkeypatch):
    async def run_test():
        receiver = ArubaBleReceiver(access_token="secret")
        websocket = _Socket([b"bad", b"bad", b"bad"], path="/aruba-ble-proxy")

        def fail_decode(message):
            raise ValueError("invalid")

        monkeypatch.setattr(receiver.decoder, "decode_message", fail_decode)
        await receiver._handle_connection(websocket)

        assert websocket.closed == [(1008, "invalid telemetry")]
        assert receiver.stats.decode_errors == 3
        assert receiver.stats.connections_closed == 1

    asyncio.run(run_test())
