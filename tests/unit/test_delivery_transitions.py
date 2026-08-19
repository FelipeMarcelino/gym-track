"""WS-10: delivery state may only move forward (§25)."""

from __future__ import annotations

import pytest

from app.infrastructure.postgres.models import DeliveryState, OutboundMessage
from app.workers.dispatcher import (
    ALLOWED_TRANSITIONS,
    DISPATCH_SAFE,
    InvalidDeliveryTransitionError,
    transition,
)


def message(state: DeliveryState) -> OutboundMessage:
    row = OutboundMessage()
    row.delivery_state = state
    return row


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeliveryState.PENDING, DeliveryState.DISPATCHING),
        (DeliveryState.DISPATCHING, DeliveryState.DISPATCHED),
        (DeliveryState.DISPATCHED, DeliveryState.DELIVERED),
        (DeliveryState.DISPATCHING, DeliveryState.PENDING),
        (DeliveryState.DISPATCHING, DeliveryState.DISPATCHING),
    ],
)
def test_allowed_transitions_move_a_delivery_forward(
    current: DeliveryState, target: DeliveryState
) -> None:
    row = message(current)

    transition(row, target)

    assert row.delivery_state is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (DeliveryState.DELIVERED, DeliveryState.DISPATCHED),
        (DeliveryState.DELIVERED, DeliveryState.PENDING),
        (DeliveryState.DELIVERED, DeliveryState.FAILED),
        (DeliveryState.DISPATCHED, DeliveryState.DISPATCHING),
        (DeliveryState.DISPATCHED, DeliveryState.PENDING),
        (DeliveryState.FAILED, DeliveryState.DISPATCHING),
        (DeliveryState.FAILED, DeliveryState.PENDING),
    ],
)
def test_a_delivery_never_moves_backwards(current: DeliveryState, target: DeliveryState) -> None:
    """Backwards is how a delivered message gets sent to the user twice."""
    with pytest.raises(InvalidDeliveryTransitionError):
        transition(message(current), target)


def test_delivered_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS[DeliveryState.DELIVERED] == frozenset()


def test_failed_is_terminal() -> None:
    """A duplicate `response.ready` — from the at-least-once outbox or a DLQ
    replay — must not make the dispatcher call a provider that already rejected
    this message. Reviving it is an operator action, not a retry."""
    assert ALLOWED_TRANSITIONS[DeliveryState.FAILED] == frozenset()


def test_dispatch_safe_states_are_the_ones_a_retry_skips() -> None:
    """Q120: these are exactly the states that must not be sent again."""
    assert frozenset({DeliveryState.DISPATCHED, DeliveryState.DELIVERED}) == DISPATCH_SAFE


def test_every_state_has_a_declared_transition_set() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(DeliveryState)
