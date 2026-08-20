"""Which handler a batch goes to (§11.3).

Trivial this sprint by design: the strict-syntax marker decides, because the
only thing that can produce a workout is WS-10's adapter. Sprint 3 replaces the
body with an IntentRouter reading the batch properly.

The signature takes the batch's texts and returns a task type, which is what
makes that a substitution rather than a rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.application.services.strict_syntax import matches
from app.domain.results import TaskType


def route(texts: Sequence[str]) -> TaskType:
    """LOG_WORKOUT when any fragment carries the marker, CONVERSATION otherwise.

    Any fragment, because the batch is one interaction: somebody who says "bom
    dia" and then logs a set has logged a set.
    """
    return TaskType.LOG_WORKOUT if any(matches(text) for text in texts) else TaskType.CONVERSATION
