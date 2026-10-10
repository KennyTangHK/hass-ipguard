"""Keep all protocol tests confined to loopback, including native ZeroMQ I/O."""

from collections.abc import Iterator
from typing import Any

import pytest
import zmq


@pytest.fixture(autouse=True)
def prevent_provider_traffic(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pytest's ordinary socket guard cannot intercept libzmq's native sockets."""
    connect = zmq.Socket.connect
    bind = zmq.Socket.bind

    def check(endpoint: str) -> None:
        if not endpoint.startswith(("inproc://", "tcp://127.0.0.1:", "tcp://[::1]:")):
            raise AssertionError("Protocol tests may only use loopback endpoints.")

    def local_connect(socket: zmq.Socket[Any], endpoint: str) -> None:
        check(endpoint)
        connect(socket, endpoint)

    def local_bind(socket: zmq.Socket[Any], endpoint: str) -> None:
        check(endpoint)
        bind(socket, endpoint)

    monkeypatch.setattr(zmq.Socket, "connect", local_connect)
    monkeypatch.setattr(zmq.Socket, "bind", local_bind)
    yield
