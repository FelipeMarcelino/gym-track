"""Resolving a name the user typed into a catalog row (§16).

Four stages in §16's order, first hit wins: the user's own aliases, the global
ones, the canonical names, then fuzzy matching. Everything here is
deterministic — no model is consulted this sprint, which is what makes the
golden table in `tests/domain/fixtures/` meaningful as a contract.

The interesting behaviour is the refusal. Below the high-confidence threshold
nothing is written, and between two candidates that are too close together the
resolver asks rather than picking: choosing the first of two equals is
insertion order dressed up as a decision, and §16 calls a confidently wrong
exercise the worst failure this system has.
"""

from __future__ import annotations

import logging
from uuid import UUID

from rapidfuzz import fuzz

from app.application.ports.exercise_catalog import CatalogEntry, ExerciseCatalogPort
from app.domain.exercises.normalization import normalize_for_match
from app.domain.exercises.resolution import (
    AMBIGUITY_MARGIN,
    HIGH_CONFIDENCE,
    MEDIUM_CONFIDENCE,
    ExerciseResolution,
    ResolutionCandidate,
    ResolutionMethod,
)

logger = logging.getLogger(__name__)


class ExerciseResolver:
    def __init__(
        self,
        catalog: ExerciseCatalogPort,
        *,
        high_confidence: float = HIGH_CONFIDENCE,
        medium_confidence: float = MEDIUM_CONFIDENCE,
        ambiguity_margin: float = AMBIGUITY_MARGIN,
        max_candidates: int = 5,
    ) -> None:
        self._catalog = catalog
        self._high_confidence = high_confidence
        self._medium_confidence = medium_confidence
        self._ambiguity_margin = ambiguity_margin
        self._max_candidates = max_candidates

    async def resolve_entry(
        self, raw_name: str, *, user_id: UUID
    ) -> tuple[ExerciseResolution, CatalogEntry | None]:
        """`resolve`, plus the catalog row it landed on.

        Callers that go on to build something need the row's `is_bodyweight`
        and `uses_implements` (Q49), and the resolution itself cannot carry
        them: it is a domain type, and `CatalogEntry` belongs to the port. The
        entry is None exactly when nothing resolved.
        """
        resolution = await self.resolve(raw_name, user_id=user_id)
        if resolution.exercise_id is None:
            return resolution, None

        for searchable in await self._catalog.all_searchable():
            if searchable.entry.exercise_id == resolution.exercise_id:
                return resolution, searchable.entry
        return resolution, None  # pragma: no cover - a resolved id is in the catalog

    async def resolve(self, raw_name: str, *, user_id: UUID) -> ExerciseResolution:
        """Stages 1-4 in the normative order, first hit wins."""
        normalized = normalize_for_match(raw_name)
        if not normalized:
            # Nothing to match. Fuzzy scoring an empty string against every
            # term produces noise, and the honest answer is that we did not
            # understand rather than a candidate list built from nothing.
            return ExerciseResolution(raw_name=raw_name, requires_clarification=True)

        # Awaited one at a time, and only when the stage before it came back
        # empty. Building this as a tuple ran all three every time: each is a
        # round trip, the canonical one loads the whole catalog, and a failure
        # in a stage nobody needed would discard a hit that already succeeded.
        entry = await self._catalog.by_user_alias(normalized, user_id)
        if entry is not None:
            return self._exact(raw_name, entry, ResolutionMethod.USER_ALIAS)

        entry = await self._catalog.by_global_alias(normalized)
        if entry is not None:
            return self._exact(raw_name, entry, ResolutionMethod.GLOBAL_ALIAS)

        entry = await self._catalog.by_canonical_name(normalized)
        if entry is not None:
            return self._exact(raw_name, entry, ResolutionMethod.CANONICAL)

        return await self._fuzzy(raw_name, normalized)

    def _exact(
        self, raw_name: str, entry: CatalogEntry, method: ResolutionMethod
    ) -> ExerciseResolution:
        return ExerciseResolution(
            raw_name=raw_name,
            exercise_id=entry.exercise_id,
            canonical_name=entry.canonical_name,
            method=method,
            confidence=1.0,
        )

    async def _fuzzy(self, raw_name: str, normalized: str) -> ExerciseResolution:
        ranked = await self._ranked_candidates(normalized)

        if not ranked or ranked[0].score < self._medium_confidence:
            # Nothing close enough to be worth showing. Offering the best of a
            # bad list invites the user to confirm something we invented.
            return ExerciseResolution(raw_name=raw_name, requires_clarification=True)

        best = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        too_close = runner_up is not None and best.score - runner_up.score <= self._ambiguity_margin

        if best.score >= self._high_confidence and not too_close:
            return ExerciseResolution(
                raw_name=raw_name,
                exercise_id=best.exercise_id,
                canonical_name=best.canonical_name,
                method=ResolutionMethod.FUZZY,
                confidence=best.score,
                candidates=tuple(ranked),
            )

        if best.score >= self._high_confidence:
            # Confident about the neighbourhood, not about which one. Asking is
            # cheap; writing the wrong exercise is not.
            logger.info(
                "exercise resolution is ambiguous",
                extra={"raw_name": raw_name, "candidates": len(ranked)},
            )
            return ExerciseResolution(
                raw_name=raw_name,
                candidates=tuple(ranked),
                confidence=best.score,
                requires_clarification=True,
            )

        # The middle band: probably this, not sure enough to write it. The
        # caller may ask; Sprint 3's LLM stage takes this case.
        return ExerciseResolution(
            raw_name=raw_name,
            candidates=tuple(ranked),
            confidence=best.score,
            requires_clarification=False,
        )

    async def _ranked_candidates(self, normalized: str) -> list[ResolutionCandidate]:
        """Best score per exercise, best first.

        Scored against every term rather than the canonical name alone: "rdl"
        and "levantamento terra romeno" are the same exercise reached by very
        different strings, and only one of them looks like the row.
        """
        candidates: list[ResolutionCandidate] = []
        for searchable in await self._catalog.all_searchable():
            # `token_sort_ratio` rather than `WRatio`, which the plan named.
            # WRatio falls back to a partial ratio when the lengths differ, so
            # a query that merely *contains* a catalog name scores as though it
            # were that name: "agachamento bulgaro" hit "Agachamento livre" at
            # exactly 0.90 and "supino com corda" hit "Supino reto" at 0.90 --
            # both written without asking. Those are two different exercises,
            # and §16 calls a confidently wrong one the worst failure here.
            # Sorting the tokens keeps word order from mattering ("terra
            # levantamento") while still charging for the words that differ.
            best_term = max(
                (fuzz.token_sort_ratio(normalized, term) for term in searchable.normalized_terms),
                default=0.0,
            )
            candidates.append(
                ResolutionCandidate(
                    exercise_id=searchable.entry.exercise_id,
                    canonical_name=searchable.entry.canonical_name,
                    score=best_term / 100.0,
                )
            )

        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        return [
            candidate
            for candidate in candidates[: self._max_candidates]
            if candidate.score >= self._medium_confidence
        ]
