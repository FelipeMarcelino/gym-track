"""WS-9: what the user is told, in pt-BR, deterministically (§25, D8).

Pure functions of a result object, so the reply is a fact about what was
written rather than a sentence somebody generated. §25 requires a confirmation
for every successful registration, and the rule that shapes every test here is
that the message must not contain a value its input did not.
"""

from __future__ import annotations

from uuid import uuid4

from app.domain.exercises.resolution import ResolutionCandidate
from app.domain.training.activities import ActivityField
from app.domain.training.confirmations import (
    clarification_request,
    partial_confirmation,
    workout_confirmation,
)
from app.domain.training.deferrals import DeferralReason, DeferredItem
from app.domain.training.workout_log import LoggedExercise, WorkoutLoggedResult

SESSION = uuid4()


def _result(*exercises: LoggedExercise) -> WorkoutLoggedResult:
    return WorkoutLoggedResult(
        training_session_id=SESSION, session_opened=True, exercises=exercises
    )


def _logged(name: str, sets: int, block: int = 0) -> LoggedExercise:
    return LoggedExercise(
        session_exercise_id=uuid4(), canonical_name=name, block_index=block, set_count=sets
    )


def test_the_confirmation_names_the_exercise_and_the_count() -> None:
    """§25: a registration the user cannot see is a registration they cannot
    trust, and "ok" does not tell them what we understood."""
    message = workout_confirmation(_result(_logged("Supino reto", 3)))

    assert "Supino reto" in message
    assert "3" in message


def test_several_exercises_are_all_named() -> None:
    message = workout_confirmation(
        _result(_logged("Supino reto", 3), _logged("Remada curvada", 4, block=1))
    )

    assert "Supino reto" in message
    assert "Remada curvada" in message


def test_a_replay_does_not_claim_to_have_written_anything_new() -> None:
    """A redelivery must not tell the user we recorded their workout twice."""
    replayed = WorkoutLoggedResult(
        training_session_id=SESSION,
        session_opened=False,
        exercises=(_logged("Supino reto", 3),),
        replayed=True,
    )

    assert "já" in workout_confirmation(replayed).casefold()


def test_the_clarification_names_what_is_missing() -> None:
    """Q46: the question has to be answerable. "Não entendi" is not."""
    message = clarification_request(
        (
            DeferredItem(
                raw_name="supino",
                reason=DeferralReason.MISSING_ESSENTIAL_DATA,
                missing_field=ActivityField.REPETITIONS,
            ),
        )
    )

    assert "repetições" in message
    assert "supino" in message


def test_an_ambiguous_name_is_asked_about_with_its_options() -> None:
    """Q56: the user picks. Choosing for them is the failure §16 refuses."""
    message = clarification_request(
        (
            DeferredItem(
                raw_name="supino clinado",
                reason=DeferralReason.AMBIGUOUS_EXERCISE,
                candidates=(
                    ResolutionCandidate(uuid4(), "Supino inclinado", 0.93),
                    ResolutionCandidate(uuid4(), "Supino declinado", 0.93),
                ),
            ),
        )
    )

    assert "Supino inclinado" in message
    assert "Supino declinado" in message


def test_an_unresolvable_name_is_quoted_back_rather_than_guessed() -> None:
    message = clarification_request(
        (DeferredItem(raw_name="aquele do peito", reason=DeferralReason.UNRESOLVED_EXERCISE),)
    )

    assert "aquele do peito" in message


def test_a_partial_result_says_both_things() -> None:
    """Q56 and Q57 in one message: what was recorded, and what is still open.
    Sending only the question would look like nothing was saved."""
    message = partial_confirmation(
        _result(_logged("Supino reto", 3)),
        (
            DeferredItem(
                raw_name="agachamento",
                reason=DeferralReason.MISSING_ESSENTIAL_DATA,
                missing_field=ActivityField.REPETITIONS,
            ),
        ),
    )

    assert "Supino reto" in message
    assert "agachamento" in message
    assert "repetições" in message


def test_no_message_invents_a_number_or_a_name() -> None:
    """The mechanical form of D8: every message is a function of its input, so
    every name and count in it comes from that input."""
    result = _result(_logged("Supino reto", 3))
    deferred = (
        DeferredItem(
            raw_name="agachamento",
            reason=DeferralReason.MISSING_ESSENTIAL_DATA,
            missing_field=ActivityField.REPETITIONS,
        ),
    )

    for message in (
        workout_confirmation(result),
        clarification_request(deferred),
        partial_confirmation(result, deferred),
    ):
        digits = {character for character in message if character.isdigit()}
        assert digits <= {"3"}, f"a number nobody reported appeared in {message!r}"
