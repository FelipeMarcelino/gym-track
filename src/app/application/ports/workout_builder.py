"""What the LOG_WORKOUT handler needs from a command builder (Q155).

Stated as a port because two things satisfy it: the builder itself, which takes
its collaborators directly and is what the tests use, and the session-scoped
wrapper the worker composes, which opens a database session per call so the
catalog cache lives exactly one request. The handler should not have to know
which one it was given.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from app.application.commands.workout import BuildOutcome
from app.domain.training.input_contract import StructuredWorkoutInput


class WorkoutCommandBuilderPort(Protocol):
    async def build(
        self,
        structured: StructuredWorkoutInput,
        *,
        user_id: UUID,
        conversation_id: UUID,
        message_batch_id: UUID,
        source_message_ids: Sequence[UUID],
    ) -> BuildOutcome: ...
