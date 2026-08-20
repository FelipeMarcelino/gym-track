"""WS-9: which handler a batch goes to.

Deliberately trivial this sprint: the strict-syntax marker decides. Sprint 3
replaces the body with an IntentRouter, and the signature is chosen so that is
a substitution rather than a rewrite.
"""

from __future__ import annotations

import pytest

from app.domain.results import TaskType
from app.graphs.main.routing import route


@pytest.mark.parametrize(
    "texts",
    [
        ("#log supino 80kg 10",),
        ("bom dia", "#log supino 80kg 10"),
        ("#LOG supino 80kg 10",),
    ],
)
def test_a_marked_line_anywhere_in_the_batch_is_a_workout(texts: tuple[str, ...]) -> None:
    """The batch is one interaction: a fragment carrying the marker means the
    user logged something, whatever else they said around it."""
    assert route(texts) is TaskType.LOG_WORKOUT


@pytest.mark.parametrize(
    "texts", [("bom dia",), (), ("#logger supino",), ("obrigado", "até amanhã")]
)
def test_everything_else_is_a_conversation(texts: tuple[str, ...]) -> None:
    """Including `#logger`, which is an ordinary hashtag: routing it here would
    hand a greeting to a parser that reads it as training."""
    assert route(texts) is TaskType.CONVERSATION
