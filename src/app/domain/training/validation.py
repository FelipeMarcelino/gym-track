"""ActivityValidator: deciding whether a draft can be recorded (§14.1).

Four outcomes, and the difference between two of them is the whole point:

* **MISSING_ESSENTIAL_DATA** — the input is incomplete, so ask for the missing
  field by name. "Supino 80 kg" needs repetitions (Q46), and inventing them is
  the one thing this system must never do.
* **INVALID** — the input is broken. Negative repetitions are not an absence;
  no answer from the user completes them.

Precedence follows from that: a range violation wins over a missing field. A
draft with reps = -3 and no load is INVALID, not MISSING_ESSENTIAL_DATA, because
asking someone for a load they never mentioned while ignoring the impossible
number they did mention is the wrong reply.

Deterministic and pure (DEC-001): no clock, no database, no model. The same
draft always produces the same outcome, which is what lets Sprint 3's extractor
be judged against it rather than the other way round.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.domain.training.activities import ActivityDraft, ActivityField
from app.domain.training.schema_registry import ActivitySchema, ActivitySchemaRegistry


class ValidationStatus(StrEnum):
    VALID = "valid"
    VALID_WITH_WARNINGS = "valid_with_warnings"
    MISSING_ESSENTIAL_DATA = "missing_essential_data"
    INVALID = "invalid"


class IssueCode(StrEnum):
    MISSING = "missing"
    OUT_OF_RANGE = "out_of_range"
    NEGATIVE = "negative"
    UNEXPECTED = "unexpected"
    #: NaN or an infinity. Separate from OUT_OF_RANGE because it is not a
    #: number the user typed: it is a value some upstream parser produced, and
    #: the fix is there rather than in what they send next.
    NOT_FINITE = "not_finite"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    field: ActivityField
    code: IssueCode
    #: pt-BR and deterministic. `confirmations.py` (D8) turns these into what
    #: the user reads, so the text is part of the domain rather than of a
    #: presentation layer that does not exist yet.
    #:
    #: Phrased as "campo: problema" rather than as a sentence, because a
    #: sentence has to agree in gender and number with the field it names --
    #: "esforço negativa" and "faltou repetições" are both wrong, and a message
    #: table that has to be re-read every time a field is added is a message
    #: table that eventually is not.
    message: str


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    status: ValidationStatus
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def missing_fields(self) -> tuple[ActivityField, ...]:
        """What to ask the user for, in a stable order."""
        return tuple(issue.field for issue in self.issues if issue.code is IssueCode.MISSING)

    @property
    def is_persistable(self) -> bool:
        """Whether this activity may be written.

        A warning does not stop a set from being recorded: the user did the
        work, and refusing it over an extra field they mentioned would lose
        real training.
        """
        return self.status in (ValidationStatus.VALID, ValidationStatus.VALID_WITH_WARNINGS)


_FIELD_NAMES_PT_BR: dict[ActivityField, str] = {
    ActivityField.REPETITIONS: "repetições",
    ActivityField.LOAD: "carga",
    ActivityField.LOAD_MODE: "modo de carga",
    ActivityField.DISTANCE: "distância",
    ActivityField.DURATION: "duração",
    ActivityField.EFFORT: "esforço",
}


def field_name(activity_field: ActivityField) -> str:
    return _FIELD_NAMES_PT_BR[activity_field]


class ActivityValidator:
    def __init__(self, registry: ActivitySchemaRegistry | None = None) -> None:
        self._registry = registry or ActivitySchemaRegistry()

    def validate(self, draft: ActivityDraft) -> ValidationOutcome:
        schema = self._registry.schema_for(draft.activity_type)
        stated = draft.stated_fields()

        broken = self._range_issues(draft, schema)
        if broken:
            # Checked first and returned alone: a broken value is not an
            # incomplete one, and mixing the two produces a reply that asks for
            # something while ignoring something impossible.
            return ValidationOutcome(status=ValidationStatus.INVALID, issues=broken)

        missing = self._missing_issues(schema, stated)
        if missing:
            return ValidationOutcome(status=ValidationStatus.MISSING_ESSENTIAL_DATA, issues=missing)

        unexpected = self._unexpected_issues(schema, stated)
        if unexpected:
            return ValidationOutcome(status=ValidationStatus.VALID_WITH_WARNINGS, issues=unexpected)

        return ValidationOutcome(status=ValidationStatus.VALID)

    def _range_issues(
        self, draft: ActivityDraft, schema: ActivitySchema
    ) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []

        for activity_field in sorted(draft.stated_fields(), key=lambda item: item.value):
            value = draft.value_of(activity_field)
            if value is None:
                continue

            if not value.is_finite():
                # Checked before any ordering comparison: `Decimal("NaN") < 0`
                # raises InvalidOperation, and a validator that can raise has
                # no contract at all -- it would abort the whole batch instead
                # of reporting one broken activity.
                issues.append(
                    ValidationIssue(
                        field=activity_field,
                        code=IssueCode.NOT_FINITE,
                        message=f"{field_name(activity_field)}: valor não utilizável",
                    )
                )
                continue

            if value < Decimal(0):
                issues.append(
                    ValidationIssue(
                        field=activity_field,
                        code=IssueCode.NEGATIVE,
                        message=f"{field_name(activity_field)}: valor negativo",
                    )
                )
                continue

            allowed = schema.ranges.get(activity_field)
            if allowed is not None and not allowed.contains(value):
                issues.append(
                    ValidationIssue(
                        field=activity_field,
                        code=IssueCode.OUT_OF_RANGE,
                        message=(
                            f"{field_name(activity_field)}: fora da faixa esperada "
                            f"({allowed.minimum} a {allowed.maximum})"
                        ),
                    )
                )

        return tuple(issues)

    def _missing_issues(
        self, schema: ActivitySchema, stated: frozenset[ActivityField]
    ) -> tuple[ValidationIssue, ...]:
        issues = [
            ValidationIssue(
                field=activity_field,
                code=IssueCode.MISSING,
                message=f"informe {field_name(activity_field)}",
            )
            for activity_field in sorted(schema.essential - stated, key=lambda item: item.value)
        ]

        for group in schema.at_least_one_of:
            if group & stated:
                continue
            # Every member is named, because the user may supply any of them and
            # a message mentioning only the first would look like a demand.
            issues.extend(
                ValidationIssue(
                    field=activity_field,
                    code=IssueCode.MISSING,
                    message=(
                        "informe "
                        + " ou ".join(
                            field_name(member)
                            for member in sorted(group, key=lambda item: item.value)
                        )
                    ),
                )
                for activity_field in sorted(group, key=lambda item: item.value)
            )

        return tuple(issues)

    def _unexpected_issues(
        self, schema: ActivitySchema, stated: frozenset[ActivityField]
    ) -> tuple[ValidationIssue, ...]:
        """Fields that mean nothing for this activity type.

        Warned about rather than dropped: the input said something, and a value
        that disappears without a trace is how a user learns not to trust what
        the system recorded.
        """
        return tuple(
            ValidationIssue(
                field=activity_field,
                code=IssueCode.UNEXPECTED,
                message=(f"{field_name(activity_field)}: não se aplica a este tipo de atividade"),
            )
            for activity_field in sorted(stated, key=lambda item: item.value)
            if not schema.accepts(activity_field)
        )
