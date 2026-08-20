"""Turning structured input into something committable (Q49, Q56, Q57).

Every earlier workstream meets here: WS-7 resolves the name, WS-8's inheritance
fills the gaps the sentence left implicit, WS-3 parses the units, WS-4 reads the
effort, WS-2 says whether the result is worth writing, WS-3 derives the metrics.

The rule that shapes the code is Q57: a failure is scoped to the activity that
caused it. The user did the bench press, and losing it because the next sentence
was unintelligible is the failure this exists to prevent — so each activity is
built independently, and anything that goes wrong becomes a `DeferredItem`
about that activity rather than an exception about the message.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.commands.workout import (
    ActivityCommand,
    BuildOutcome,
    DeferralReason,
    DeferredItem,
    GroupCommand,
    LogWorkoutCommand,
    SetCommand,
    operation_id_for,
)
from app.application.ports.exercise_catalog import CatalogEntry
from app.application.services.exercise_resolver import ExerciseResolver
from app.domain.training.activities import ActivityDraft, ActivityField, ActivityType, LoadMode
from app.domain.training.effort import EffortNormalizer
from app.domain.training.inheritance import InheritedSet, inherit_within_block
from app.domain.training.input_contract import StructuredActivityInput, StructuredWorkoutInput
from app.domain.training.metrics import derive_all
from app.domain.training.units import (
    Quantity,
    UnitParseError,
    parse_distance,
    parse_duration,
    parse_load,
    to_kilograms,
    to_meters,
    to_seconds,
)
from app.domain.training.validation import ActivityValidator, IssueCode, ValidationOutcome
from app.infrastructure.postgres.exercise_catalog import PostgresExerciseCatalog

logger = logging.getLogger(__name__)


class _ActivityDeferred(Exception):  # noqa: N818 - control flow within one activity
    """Raised and caught inside `_build_activity`, never seen by a caller.

    An exception rather than a returned union because it can happen four levels
    down (a load that will not parse in the third set of the second activity),
    and threading an error value back up through the unit parsing would obscure
    the parsing itself.
    """

    def __init__(self, item: DeferredItem) -> None:
        super().__init__(item.reason)
        self.item = item


class WorkoutCommandBuilder:
    def __init__(
        self,
        *,
        resolver: ExerciseResolver,
        validator: ActivityValidator,
        effort: EffortNormalizer,
    ) -> None:
        self._resolver = resolver
        self._validator = validator
        self._effort = effort

    async def build(
        self,
        structured: StructuredWorkoutInput,
        *,
        user_id: UUID,
        conversation_id: UUID,
        message_batch_id: UUID,
        source_message_ids: Sequence[UUID],
    ) -> BuildOutcome:
        """Resolve, inherit, convert, validate, and keep only what commits."""
        activities: list[ActivityCommand] = []
        deferred: list[DeferredItem] = []

        for structured_activity in structured.activities:
            try:
                activities.append(await self._build_activity(structured_activity, user_id=user_id))
            except _ActivityDeferred as postponed:
                deferred.append(postponed.item)

        if not activities:
            # Nothing committable. The deferrals are the entire result, and the
            # caller turns them into a question rather than a confirmation.
            return BuildOutcome(command=None, deferred=tuple(deferred))

        return BuildOutcome(
            command=LogWorkoutCommand(
                operation_id=operation_id_for(message_batch_id),
                user_id=user_id,
                conversation_id=conversation_id,
                message_batch_id=message_batch_id,
                source_message_ids=tuple(source_message_ids),
                activities=tuple(activities),
                groups=self._groups_still_in_use(structured, activities),
            ),
            deferred=tuple(deferred),
        )

    # -- one activity ------------------------------------------------------

    async def _build_activity(
        self, structured: StructuredActivityInput, *, user_id: UUID
    ) -> ActivityCommand:
        resolution, entry = await self._resolver.resolve_entry(structured.raw_name, user_id=user_id)
        if entry is None:
            raise _ActivityDeferred(
                DeferredItem(
                    raw_name=structured.raw_name,
                    reason=(
                        DeferralReason.AMBIGUOUS_EXERCISE
                        if resolution.candidates
                        else DeferralReason.UNRESOLVED_EXERCISE
                    ),
                    candidates=resolution.candidates,
                )
            )

        if not structured.sets:
            # "Fiz supino hoje" is a real thing to say and not a workout yet.
            # A block with no sets records that an exercise happened and
            # nothing about it, which is worse than asking (Q46).
            raise _ActivityDeferred(
                DeferredItem(
                    raw_name=structured.raw_name,
                    reason=DeferralReason.MISSING_ESSENTIAL_DATA,
                    missing_field=ActivityField.REPETITIONS,
                )
            )

        # The producer may say, but the catalog knows: a guessed activity type
        # changes which fields are essential, and turns a run into a set.
        activity_type = structured.activity_type or entry.activity_type

        sets = tuple(
            self._build_set(
                inherited,
                index=index,
                entry=entry,
                activity_type=activity_type,
                raw_name=structured.raw_name,
            )
            for index, inherited in enumerate(inherit_within_block(structured.sets))
        )

        return ActivityCommand(
            exercise_id=entry.exercise_id,
            canonical_name=entry.canonical_name,
            activity_type=activity_type,
            effort=self._effort.normalize(structured.effort),
            sets=sets,
            group_ref=structured.group_ref,
        )

    def _build_set(
        self,
        inherited: InheritedSet,
        *,
        index: int,
        entry: CatalogEntry,
        activity_type: ActivityType,
        raw_name: str,
    ) -> SetCommand:
        load_kg = self._quantity(inherited.load.value, parse_load, to_kilograms, raw_name)
        distance_m = self._quantity(inherited.distance.value, parse_distance, to_meters, raw_name)
        duration_s = self._quantity(inherited.duration.value, parse_duration, to_seconds, raw_name)
        load_mode = self._load_mode(inherited, entry=entry, load_kg=load_kg)
        effort = self._effort.normalize(inherited.effort)

        draft = ActivityDraft(
            activity_type=activity_type,
            repetitions=inherited.repetitions.value,
            load_kg=load_kg,
            load_mode=load_mode,
            distance_m=distance_m,
            duration_s=duration_s,
            effort_rpe=effort.rpe if effort else None,
            set_type=inherited.set_type,
        )
        self._require_persistable(self._validator.validate(draft), raw_name)

        return SetCommand(
            set_index=index,
            set_type=inherited.set_type,
            repetitions=inherited.repetitions.value,
            repetitions_provenance=inherited.repetitions.provenance,
            load_kg=load_kg,
            load_mode=load_mode,
            load_provenance=inherited.load.provenance,
            raw_load_text=inherited.load.value,
            distance_m=distance_m,
            distance_provenance=inherited.distance.provenance,
            duration_s=duration_s,
            duration_provenance=inherited.duration.provenance,
            effort=effort,
            metrics=derive_all(draft),
            notes=inherited.notes,
        )

    # -- the small decisions -----------------------------------------------

    def _quantity(
        self,
        raw: str | None,
        parse: Callable[[str], Quantity],
        convert: Callable[[Quantity], Decimal],
        raw_name: str,
    ) -> Decimal | None:
        """Parse a stated measure, or defer the activity that stated it.

        A value nobody can read is not a value to guess at: "umas pesadas" has
        no kilograms in it, and inventing some writes a number the user never
        said and cannot see is wrong.
        """
        if raw is None:
            return None
        try:
            return convert(parse(raw))
        except UnitParseError:
            logger.info("unreadable measurement", extra={"raw_name": raw_name, "raw": raw})
            raise _ActivityDeferred(
                DeferredItem(raw_name=raw_name, reason=DeferralReason.INVALID_VALUE)
            ) from None

    def _load_mode(
        self, inherited: InheritedSet, *, entry: CatalogEntry, load_kg: Decimal | None
    ) -> LoadMode | None:
        """Q49, decided from the catalog unless the user was explicit.

        "20kg de halteres" is 20 per hand, and the answer comes from the
        equipment row rather than from pattern-matching the exercise's name. A
        user who says otherwise has said something the catalog cannot know, so
        a stated mode always wins.
        """
        if inherited.load_mode.value is not None:
            return inherited.load_mode.value
        if entry.is_bodyweight:
            return LoadMode.BODYWEIGHT_PLUS if load_kg is not None else LoadMode.BODYWEIGHT
        if load_kg is None:
            # No load and no reason to claim one: a mode without a weight
            # describes nothing.
            return None
        return LoadMode.PER_IMPLEMENT if entry.uses_implements else LoadMode.TOTAL

    def _require_persistable(self, outcome: ValidationOutcome, raw_name: str) -> None:
        if outcome.is_persistable:
            return

        missing = outcome.missing_fields
        if missing:
            raise _ActivityDeferred(
                DeferredItem(
                    raw_name=raw_name,
                    reason=DeferralReason.MISSING_ESSENTIAL_DATA,
                    missing_field=missing[0],
                )
            )
        logger.info(
            "activity refused by validation",
            extra={
                "raw_name": raw_name,
                "codes": [issue.code.value for issue in outcome.issues],
            },
        )
        raise _ActivityDeferred(
            DeferredItem(
                raw_name=raw_name,
                reason=DeferralReason.INVALID_VALUE,
                missing_field=next(
                    (
                        issue.field
                        for issue in outcome.issues
                        if issue.code is not IssueCode.MISSING
                    ),
                    None,
                ),
            )
        )

    def _groups_still_in_use(
        self, structured: StructuredWorkoutInput, activities: Sequence[ActivityCommand]
    ) -> tuple[GroupCommand, ...]:
        """Only groups something committed belongs to.

        A superset whose members were all deferred describes nothing, and
        writing it would leave a group of zero exercises in the history.
        """
        referenced = {activity.group_ref for activity in activities if activity.group_ref}
        return tuple(
            GroupCommand(ref=group.ref, group_type=group.group_type, rounds=group.rounds)
            for group in structured.groups
            if group.ref in referenced
        )


class SessionScopedCommandBuilder:
    """A builder that gets a fresh catalog per call.

    `PostgresExerciseCatalog` caches its searchable listing for the life of the
    session it was given, which is right for one request and wrong for a
    process: an alias a user teaches us in Sprint 3 has to be visible on their
    next message, not after the next deploy. Opening a short session here is
    what keeps that true without pushing session management into the handler.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def build(
        self,
        structured: StructuredWorkoutInput,
        *,
        user_id: UUID,
        conversation_id: UUID,
        message_batch_id: UUID,
        source_message_ids: Sequence[UUID],
    ) -> BuildOutcome:
        async with self._session_factory() as session:
            builder = WorkoutCommandBuilder(
                resolver=ExerciseResolver(PostgresExerciseCatalog(session)),
                validator=ActivityValidator(),
                effort=EffortNormalizer(),
            )
            return await builder.build(
                structured,
                user_id=user_id,
                conversation_id=conversation_id,
                message_batch_id=message_batch_id,
                source_message_ids=source_message_ids,
            )
