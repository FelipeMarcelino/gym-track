"""What the user is told, in pt-BR (§25, Q46, Q56, D8).

Pure functions of a result object. Every sentence here is a fact about what the
database now contains, which is the only way a confirmation is worth anything:
§25 requires one for every successful registration, and a reply that says "ok"
tells the user nothing about what we understood them to mean.

Nothing in this module invents a value. If a number appears in a message, it
came from the result; if an exercise is named, it is the name that was written.
That is what makes these worth testing rather than reviewing.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.training.deferrals import DeferralReason, DeferredItem
from app.domain.training.validation import field_name
from app.domain.training.workout_log import WorkoutLoggedResult


def _sets_phrase(count: int) -> str:
    return "1 série" if count == 1 else f"{count} séries"


def workout_confirmation(result: WorkoutLoggedResult) -> str:
    """What was recorded, named so the user can catch a mistake."""
    written = ", ".join(
        f"{exercise.canonical_name} ({_sets_phrase(exercise.set_count)})"
        for exercise in result.exercises
    )
    if result.replayed:
        # A redelivery. Their message still deserves an answer, but telling
        # them we recorded it again would be a lie about the database.
        return f"Esse treino já estava registrado: {written}."
    return f"Registrei {written}."


def clarification_request(deferred: Sequence[DeferredItem]) -> str:
    """What is still needed, phrased as something answerable (Q46, Q56)."""
    return " ".join(_question(item) for item in deferred)


def partial_confirmation(result: WorkoutLoggedResult, deferred: Sequence[DeferredItem]) -> str:
    """Both at once (Q56, Q57).

    Sending only the question would look like nothing was saved, and sending
    only the confirmation would silently drop what the user said.
    """
    return f"{workout_confirmation(result)} {clarification_request(deferred)}"


def _question(item: DeferredItem) -> str:
    match item.reason:
        case DeferralReason.AMBIGUOUS_EXERCISE:
            options = " ou ".join(candidate.canonical_name for candidate in item.candidates)
            return f'Por "{item.raw_name}" você quis dizer {options}?'
        case DeferralReason.UNRESOLVED_EXERCISE:
            return f'Não conheço "{item.raw_name}". Como esse exercício se chama?'
        case DeferralReason.MISSING_ESSENTIAL_DATA:
            missing = field_name(item.missing_field) if item.missing_field else "os detalhes"
            return f'Faltou {missing} em "{item.raw_name}". Quanto foi?'
        case DeferralReason.INVALID_VALUE:
            missing = field_name(item.missing_field) if item.missing_field else "um valor"
            return f'Não consegui ler {missing} em "{item.raw_name}". Pode repetir?'
