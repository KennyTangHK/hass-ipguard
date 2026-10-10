"""Home Assistant fixtures with synthetic SDK models and no cloud networking."""

from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from ipguard_api import (
    AuthIdentifier,
    Board,
    IdentifierKind,
    IpguardClient,
    Relay,
    Session,
)


@pytest.fixture(autouse=True)
def allow_custom_integration(enable_custom_integrations: None) -> None:
    """Let the official fixture loader find the custom integration."""


@pytest.fixture
def session() -> Session:
    return Session(
        identity=str(UUID(int=1)),
        identifier=AuthIdentifier(IdentifierKind.EMAIL, "person@example.invalid"),
        label="Kenny",
        verification_code="012345",
        server_public_key="a" * 40,
        sip_password="synthetic-secret",
    )


@pytest.fixture
def relay() -> Relay:
    return Relay(Board("synthetic-board", name="Entrance"), 1, "Front door", True)


@pytest.fixture
def client(session: Session, relay: Relay) -> Mock:
    mock = Mock(spec=IpguardClient)
    mock.connected = True
    mock.session = session
    mock.async_request_verification = AsyncMock()
    mock.async_verify_code = AsyncMock()
    mock.async_complete_registration = AsyncMock(return_value=session)
    mock.async_connect = AsyncMock()
    mock.async_get_relays = AsyncMock(return_value=(relay,))
    mock.async_release = AsyncMock()
    mock.async_close = AsyncMock()
    mock.unsubscribe = Mock()
    mock.subscribe = Mock(return_value=mock.unsubscribe)
    mock.unsubscribe_calls = Mock()
    mock.subscribe_calls = Mock(return_value=mock.unsubscribe_calls)
    return mock
