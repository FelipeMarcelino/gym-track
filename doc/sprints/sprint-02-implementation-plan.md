# Sprint 2 — Task-level Implementation Plan

Companion to `doc/sprints/sprint-02-deterministic-workout-domain.md`. The sprint file states *what* ships and *why*; this states *where the code goes*, *what its signatures are*, *which migration carries the schema*, and *which test files fail when it is wrong*.

**Nothing here is normative.** Where this plan and the architecture spec disagree, the spec wins (or the spec gets an ADR). Where this plan and the sprint file disagree, the sprint file wins.

## How to read this

Each workstream lists: **Branch** (per `CLAUDE.md`), **Files** created (`+`) or modified (`~`), **Signatures** (bodies deliberately absent — this is a plan), **Migration** (revision, `down_revision`, what `downgrade()` undoes), **Tests** (file by file, each with its specific assertion), and **Done when** (the mechanical check that closes it).

## Decisions applied

| # | Applied as |
| --- | --- |
| D1 | `CATALOG_SEED` is a hand-written tuple of ~40 `SeedExercise` records in `app/domain/exercises/catalog_data.py` |
| D2 | `rapidfuzz>=3.10` added to `pyproject.toml` `[project].dependencies` (WS-7) |
| D3 | `HIGH_CONFIDENCE = 0.90`, `MEDIUM_CONFIDENCE = 0.70`, `AMBIGUITY_MARGIN = 0.02` in `app/domain/exercises/resolution.py` |
| D4 | Canonical columns are `load_kg NUMERIC(7,3)`, `distance_m NUMERIC(10,3)`, `duration_s NUMERIC(10,3)`; `Decimal` end to end, never `float` |
| D5 | `ONE_RM_VERSION = "1rm.epley.v1"` |
| D6 | No corrections module. `expected_version` columns ship (WS-6) but nothing writes them yet |
| D7 | `LOG_PREFIX = "#log"` in `app/application/services/strict_syntax.py` |
| D8 | `app/domain/training/confirmations.py` holds every user-visible string, in pt-BR, as pure functions of a result object |

## Migration and branch order

| WS | Branch | Migration | Depends on |
| --- | --- | --- | --- |
| WS-1 | `feat/ws-1-exercise-catalog` | `0005_exercise_catalog` | `0004` |
| WS-2 | `feat/ws-2-activity-model` | — | WS-1 (enums only) |
| WS-3 | `feat/ws-3-units-and-metrics` | — | WS-2 |
| WS-4 | `feat/ws-4-effort-normalization` | — | WS-2 |
| WS-5 | `feat/ws-5-training-sessions` | `0006_training_sessions` | `0005` |
| WS-6 | `feat/ws-6-workout-schema` | `0007_workout_domain` | `0006` |
| WS-7 | `feat/ws-7-exercise-resolver` | — | WS-1 |
| WS-8 | `feat/ws-8-input-contract` | — | WS-2, WS-3, WS-4, WS-7 |
| WS-9 | `feat/ws-9-workout-application-service` | — | WS-5, WS-6, WS-8 |
| WS-10 | `feat/ws-10-strict-syntax-adapter` | — | WS-8 |
| WS-11 | `feat/ws-11-cross-cutting-verification` | — | WS-9, WS-10 |
| WS-12 | `doc/ws-12-sprint-2-decision-records` | — | WS-3, WS-7, WS-10 |

WS-2, WS-3, WS-4 and WS-7 touch no schema and can be reviewed in parallel with WS-5/WS-6; the table is the safe serial order.

## The lists Sprint 1 left behind

Any workstream that adds a table **must** update all of these in the same PR, or the suite fails somewhere unrelated to the change:

1. `src/app/infrastructure/postgres/grants.py` — `SERVICE_GRANTS` per role **and** `ALL_TABLES`.
2. `tests/unit/test_persistence_contracts.py` — `EXPECTED_TABLES`, which asserts equality with `ALL_TABLES`.
3. `tests/conftest.py` — the `TRUNCATE` list in `clean_tables`. A table missing here leaks rows between tests and produces failures that look like logic bugs.
4. `src/app/infrastructure/postgres/models.py` — the model, so Alembic autogenerate and the enum-constraint filter see it.

Any workstream that adds a **process** must update: `ServiceName` in `app/config/settings.py`, `SERVICE_GRANTS` (keyed by `ServiceName`, so a missing key is a `KeyError` in provisioning), `.env.example`, `docker/compose.yaml` (role env vars **and** a service), and the role loop in `tests/conftest.py`. `PostgresSettings.every_service_has_a_role` fails startup otherwise — intended, but it fails *every* test, not just the new one.

---

# WS-1 — Exercise catalog schema and seed

**Branch:** `feat/ws-1-exercise-catalog`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `migrations/versions/0005_exercise_catalog.py` | The seven catalog tables |
| + | `src/app/domain/exercises/catalog.py` | Pure seed types — no SQLAlchemy |
| + | `src/app/domain/exercises/catalog_data.py` | `CATALOG_SEED`, the curated ~40 (D1) |
| + | `src/app/infrastructure/postgres/seeding.py` | Idempotent applier + `__main__` |
| ~ | `src/app/infrastructure/postgres/models.py` | Seven models, four enums |
| ~ | `src/app/infrastructure/postgres/grants.py` | Catalog grants + `ALL_TABLES` |
| ~ | `tests/unit/test_persistence_contracts.py` | `EXPECTED_TABLES` |
| ~ | `tests/conftest.py` | `TRUNCATE` list |
| ~ | `Makefile` | `seed` target |

## Schema (migration `0005_exercise_catalog`)

`revision = "0005_exercise_catalog"`, `down_revision = "0004_one_batch_per_message"`. Revision ids stay ≤32 chars — `alembic_version.version_num` is `varchar(32)` and a longer id fails at *stamp* time, after the DDL has run.

| Table | Columns beyond `Base` | Constraints |
| --- | --- | --- |
| `exercises` | `canonical_name str(160)`, `slug str(160)`, `activity_type`, `default_load_mode`, `is_bodyweight bool`, `locale str(16)`, `description Text?`, soft-delete | `UNIQUE(canonical_name)`, `UNIQUE(slug)` |
| `exercise_aliases` | `exercise_id FK`, `user_id FK?`, `alias str(160)`, `normalized_alias str(160)`, `source` (`SEED`/`USER_CONFIRMED`), soft-delete | partial `UNIQUE(normalized_alias) WHERE user_id IS NULL AND deleted_at IS NULL` → `uq_exercise_aliases_global`; partial `UNIQUE(user_id, normalized_alias) WHERE user_id IS NOT NULL AND deleted_at IS NULL` → `uq_exercise_aliases_user`; index on `normalized_alias` |
| `muscles` | `name str(80)`, `slug str(80)`, `muscle_group str(80)` | `UNIQUE(slug)` |
| `exercise_muscles` | `exercise_id FK`, `muscle_id FK`, `role` | `UNIQUE(exercise_id, muscle_id)` |
| `equipment` | `name str(80)`, `slug str(80)`, `is_implement bool` | `UNIQUE(slug)` |
| `exercise_equipment` | `exercise_id FK`, `equipment_id FK`, `is_primary bool` | `UNIQUE(exercise_id, equipment_id)` |
| `exercise_relations` | `from_exercise_id FK`, `to_exercise_id FK`, `relation_type` | `UNIQUE(from, to, relation_type)`, `CHECK (from_exercise_id <> to_exercise_id)` |

Two partial unique indexes rather than one composite: in PostgreSQL `NULL` is distinct from `NULL`, so a plain `UNIQUE(user_id, normalized_alias)` would let two *global* aliases share text — exactly the case the sprint file says must be refused, while a global and a user alias sharing text must be allowed.

`is_implement` on `equipment` is what makes Q49 mechanical later: the dumbbell row carries it, and WS-8 reads it rather than pattern-matching a name.

`downgrade()` drops in reverse FK order: relations, exercise_equipment, equipment, exercise_muscles, muscles, exercise_aliases, exercises.

Enums (via the existing `enum_column`): `MuscleRole` (`PRIMARY`/`SECONDARY`/`STABILIZER`, Q43), `ExerciseRelationType` (`VARIATION_OF`/`SUBSTITUTE_FOR`/`SIMILAR_MOVEMENT`/`PROGRESSION_OF`/`REGRESSION_OF`, Q44), `AliasSource`. `ActivityType` and `LoadMode` are imported from `app.domain.training.activities` (WS-2) — the database module depends on the domain, never the reverse. **WS-2 therefore lands first in the working tree even though its PR is second**; if inconvenient, WS-1 defines those two enums as its own first commit and WS-2 extends them.

## Seed

```python
# src/app/domain/exercises/catalog.py
@dataclass(frozen=True, slots=True)
class SeedMuscle:
    slug: str
    name: str
    group: str

@dataclass(frozen=True, slots=True)
class SeedEquipment:
    slug: str
    name: str
    is_implement: bool

@dataclass(frozen=True, slots=True)
class SeedExercise:
    slug: str
    canonical_name: str
    activity_type: ActivityType
    default_load_mode: LoadMode
    is_bodyweight: bool
    primary_muscles: tuple[str, ...]      # muscle slugs
    secondary_muscles: tuple[str, ...] = ()
    stabilizer_muscles: tuple[str, ...] = ()
    equipment: tuple[str, ...] = ()       # equipment slugs
    aliases_pt_br: tuple[str, ...] = ()
    aliases_en: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class SeedRelation:
    from_slug: str
    to_slug: str
    relation_type: ExerciseRelationType
```

```python
# src/app/domain/exercises/catalog_data.py
SEED_MUSCLES: Final[tuple[SeedMuscle, ...]]
SEED_EQUIPMENT: Final[tuple[SeedEquipment, ...]]
CATALOG_SEED: Final[tuple[SeedExercise, ...]]     # ~40, D1
SEED_RELATIONS: Final[tuple[SeedRelation, ...]]
```

Coverage target: bench press and its incline/decline/dumbbell variants, squat / leg press / leg extension / leg curl, deadlift / romanian deadlift, row variants, lat pulldown, pull-up, dip, overhead press, lateral raise, curl variants, triceps pushdown, calf raise, plank, push-up, plus the distance/timed entries (running, cycling, treadmill, stationary bike, jump rope) that keep `ActivityType` from being a single-member enum in practice.

```python
# src/app/infrastructure/postgres/seeding.py
async def seed_catalog(session: AsyncSession) -> SeedReport: ...
def seed_catalog_sync(connection: Connection) -> SeedReport: ...   # called by 0005

@dataclass(frozen=True, slots=True)
class SeedReport:
    exercises_inserted: int
    exercises_updated: int
    aliases_inserted: int

def main() -> None: ...                                            # `make seed`
```

Idempotence by `ON CONFLICT (slug) DO UPDATE` on `exercises`, `muscles`, `equipment`, and `ON CONFLICT DO NOTHING` on the join and alias tables — the same convergent shape as `provisioning.py`, for the reason recorded there: a migration runs once and is stamped forever, so an edited seed would reach fresh databases only. `0005` calls `seed_catalog_sync` so a clean clone has a catalog; `make seed` reconciles an existing one.

## Grants (Q145)

```python
ServiceName.WORKFLOW_WORKER: {
    "exercises": ("SELECT",),
    "exercise_aliases": ("SELECT", "INSERT"),   # stage-7 learned aliases, Sprint 3
    "muscles": ("SELECT",), "exercise_muscles": ("SELECT",),
    "equipment": ("SELECT",), "exercise_equipment": ("SELECT",),
    "exercise_relations": ("SELECT",),
}
```

No other service gets any catalog privilege. The API does not read exercises; the dispatcher does not know what one is.

## Tests

| File | Assertion |
| --- | --- |
| `tests/integration/test_catalog_seed.py` | `seed_catalog` twice → identical row counts and identical `(slug, canonical_name)` sets; second run reports `exercises_inserted == 0` |
| " | a second `exercises` row with an existing `canonical_name` raises `IntegrityError` |
| " | a global alias and a user alias with the same `normalized_alias` both insert; a second **global** alias with that text raises |
| " | a soft-deleted global alias frees its text for reuse (the partial index excludes it) |
| " | every seeded exercise has ≥1 `MuscleRole.PRIMARY` row — iterate the table, not the fixture |
| " | migration `0005` upgrades and downgrades cleanly against a real database |
| `tests/domain/test_catalog_data.py` | slugs unique across `CATALOG_SEED`; every referenced muscle/equipment slug exists; every relation's endpoints exist and differ |
| " | every alias is non-empty after `normalize_for_match` — i.e. no alias the resolver can never match (weaker `alias.strip() != ""` if WS-7 has not landed; WS-7 strengthens it) |
| " | `len(CATALOG_SEED) >= 35`, and at least one entry per `ActivityType` the sprint claims to support |
| `tests/unit/test_persistence_contracts.py` (~) | `EXPECTED_TABLES` extended; existing equality with `ALL_TABLES` does the rest |

**Done when:** `make check` green, `make seed` runs twice with no diff in row counts, `alembic downgrade -1` clean.

---

# WS-2 — Activity model and validator

**Branch:** `feat/ws-2-activity-model`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `src/app/domain/training/activities.py` | `ActivityType`, `LoadMode`, `SetType`, `ActivityField`, `ActivityDraft` |
| + | `src/app/domain/training/schema_registry.py` | `ActivitySchema`, `ActivitySchemaRegistry` |
| + | `src/app/domain/training/validation.py` | `ActivityValidator` and its outcome types |

Pure domain: no import of `sqlalchemy`, `pydantic`, or anything under `infrastructure/`. `tests/unit/test_project_layout.py` already enforces the first for the whole `domain/` package.

## Signatures

```python
# activities.py
class ActivityType(StrEnum):        # Q45
    STRENGTH = "strength"
    DISTANCE_ACTIVITY = "distance_activity"
    TIMED_ACTIVITY = "timed_activity"
    MIXED_ACTIVITY = "mixed_activity"
    MOBILITY = "mobility"
    OTHER = "other"

class LoadMode(StrEnum):            # §14.2, Q49
    TOTAL = "total"
    PER_SIDE = "per_side"
    PER_IMPLEMENT = "per_implement"
    BODYWEIGHT = "bodyweight"
    BODYWEIGHT_PLUS = "bodyweight_plus"
    BODYWEIGHT_MINUS = "bodyweight_minus"

class SetType(StrEnum):             # Q50
    WARMUP = "warmup"
    WORKING = "working"
    BACKOFF = "backoff"
    DROP_SET = "drop_set"
    FAILURE = "failure"
    AMRAP = "amrap"
    OTHER = "other"

class ActivityField(StrEnum):
    """Named so a validation outcome can point at a field by name (Q46)."""
    REPETITIONS = "repetitions"
    LOAD = "load"
    LOAD_MODE = "load_mode"
    DISTANCE = "distance"
    DURATION = "duration"
    EFFORT = "effort"

@dataclass(frozen=True, slots=True)
class ActivityDraft:
    """One set/activity in canonical units, before it is a command.

    Every measure is `Decimal | None`; `None` means *not stated*, and the
    validator's whole job is deciding whether that is fatal, a warning or fine.
    """
    activity_type: ActivityType
    repetitions: int | None = None
    load_kg: Decimal | None = None
    load_mode: LoadMode | None = None
    distance_m: Decimal | None = None
    duration_s: Decimal | None = None
    set_type: SetType = SetType.WORKING
```

```python
# schema_registry.py
@dataclass(frozen=True, slots=True)
class ValueRange:
    minimum: Decimal
    maximum: Decimal
    def contains(self, value: Decimal) -> bool: ...

@dataclass(frozen=True, slots=True)
class ActivitySchema:
    activity_type: ActivityType
    essential: frozenset[ActivityField]
    optional: frozenset[ActivityField]
    #: Each group needs at least one member present — Q47's "distance and/or duration".
    at_least_one_of: tuple[frozenset[ActivityField], ...] = ()
    ranges: Mapping[ActivityField, ValueRange] = field(default_factory=dict)

DEFAULT_ACTIVITY_SCHEMAS: Final[Mapping[ActivityType, ActivitySchema]]

class ActivitySchemaRegistry:
    def __init__(self, schemas: Mapping[ActivityType, ActivitySchema] | None = None) -> None: ...
    def schema_for(self, activity_type: ActivityType) -> ActivitySchema: ...
    def covered_types(self) -> frozenset[ActivityType]: ...
```

Registry content, as data:

| Type | essential | at_least_one_of | notable range |
| --- | --- | --- | --- |
| `STRENGTH` | `{REPETITIONS}` | — | reps 1–500, load 0–1000 kg |
| `DISTANCE_ACTIVITY` | — | `({DISTANCE, DURATION},)` | distance 1–500 000 m, duration 1–86 400 s |
| `TIMED_ACTIVITY` | `{DURATION}` | — | duration 1–86 400 s |
| `MIXED_ACTIVITY` | — | `({REPETITIONS, DISTANCE, DURATION},)` | union of the above |
| `MOBILITY` | — | `({DURATION, REPETITIONS},)` | duration 1–7 200 s |
| `OTHER` | — | `({REPETITIONS, DISTANCE, DURATION, LOAD},)` | permissive; "avoid invented values" (§14.1) means *require something*, not *accept anything* |

```python
# validation.py
class ValidationStatus(StrEnum):
    VALID = "valid"
    VALID_WITH_WARNINGS = "valid_with_warnings"
    MISSING_ESSENTIAL_DATA = "missing_essential_data"
    INVALID = "invalid"

@dataclass(frozen=True, slots=True)
class ValidationIssue:
    field: ActivityField
    code: str            # "missing", "out_of_range", "negative", "unexpected"
    message: str         # deterministic pt-BR, consumed by confirmations.py (D8)

@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    status: ValidationStatus
    issues: tuple[ValidationIssue, ...] = ()
    @property
    def missing_fields(self) -> tuple[ActivityField, ...]: ...
    @property
    def is_persistable(self) -> bool: ...   # VALID or VALID_WITH_WARNINGS

class ActivityValidator:
    def __init__(self, registry: ActivitySchemaRegistry | None = None) -> None: ...
    def validate(self, draft: ActivityDraft) -> ValidationOutcome: ...
```

Precedence, stated so tests can assert it: a range violation is `INVALID` even when an essential field is also missing — a negative rep count is a broken input, not an incomplete one, and asking the user for reps they already supplied would be the wrong reply.

## Tests

`tests/domain/test_activity_validator.py`, table-driven throughout (`parametrize` over `(draft, expected_status, expected_fields)`):

| Case | Expectation |
| --- | --- |
| `STRENGTH` with load, no reps | `MISSING_ESSENTIAL_DATA`, `missing_fields == (REPETITIONS,)` (Q46) |
| `STRENGTH` reps=10, no load | `VALID`, no issues (Q48, `10 flexões`) |
| `DISTANCE_ACTIVITY` distance only / duration only / both | `VALID` in all three (Q47) |
| `DISTANCE_ACTIVITY` neither | `MISSING_ESSENTIAL_DATA` naming both fields |
| reps = −3 / reps = 10 000 / load = −5 | `INVALID`, `code == "negative"` or `"out_of_range"` |
| reps = 0 | `INVALID` — a zero-rep set is not an incomplete set |
| `MOBILITY` duration only | `VALID` |
| `STRENGTH` with `distance_m` set | `VALID_WITH_WARNINGS`, `code == "unexpected"` — never silently dropped |
| `test_registry_covers_every_activity_type` | iterate `ActivityType`, assert membership in `covered_types()`; a new member without a schema fails here |
| `test_validator_never_returns_an_unnamed_issue` | every issue has a `field` and a non-empty `message` |

**Done when:** `mypy --strict` clean, and adding a member to `ActivityType` locally makes exactly one test fail (the coverage test) — verified by hand, not committed.

---

# WS-3 — Units and derived metrics

**Branch:** `feat/ws-3-units-and-metrics`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `src/app/domain/training/units.py` | Parsing and conversion to SI (D4) |
| + | `src/app/domain/training/metrics.py` | Versioned derived metrics (Q52) |
| + | `tests/contract/fixtures/derived_metrics.json` | The frozen metric table |

## Signatures

```python
# units.py
class Unit(StrEnum):
    KILOGRAM = "kg"; POUND = "lb"
    METER = "m"; KILOMETER = "km"; MILE = "mi"
    SECOND = "s"; MINUTE = "min"; HOUR = "h"

@dataclass(frozen=True, slots=True)
class Quantity:
    value: Decimal
    unit: Unit

class UnitParseError(ValueError):
    """The text is not a quantity. Never a fallback value — that is the point."""
    def __init__(self, raw: str, expected: str) -> None: ...

POUNDS_PER_KILOGRAM: Final[Decimal]
METERS_PER_MILE: Final[Decimal]

def parse_load(raw: str) -> Quantity: ...       # "80kg", "80 kg", "80", "176lb", "176 lbs"
def parse_distance(raw: str) -> Quantity: ...   # "5k", "5km", "5 km", "1500m", "3mi"
def parse_duration(raw: str) -> Quantity: ...   # "90s", "1:30", "1min30", "45min", "1h05"
def to_kilograms(quantity: Quantity) -> Decimal: ...
def to_meters(quantity: Quantity) -> Decimal: ...
def to_seconds(quantity: Quantity) -> Decimal: ...
```

A bare number is kilograms for load and **raises** for distance and duration: `5` could be 5 km or 5 minutes, and guessing is precisely the failure mode this sprint exists to prevent. `Decimal` throughout — `0.1 + 0.2` in a volume total a user compares against last week is a support ticket nobody can reproduce.

```python
# metrics.py
VOLUME_VERSION: Final = "volume.load_x_reps.v1"
PACE_VERSION: Final = "pace.seconds_per_km.v1"
SPEED_VERSION: Final = "speed.meters_per_second.v1"
ONE_RM_VERSION: Final = "1rm.epley.v1"                    # D5

METRIC_VERSIONS: Final[Mapping[str, str]]                 # metric name -> version

@dataclass(frozen=True, slots=True)
class DerivedMetric:
    name: str
    value: Decimal
    unit: str
    version: str

def volume(*, load_kg: Decimal | None, repetitions: int | None,
           load_mode: LoadMode | None, implements: int = 2) -> DerivedMetric | None: ...
def pace(*, distance_m: Decimal | None, duration_s: Decimal | None) -> DerivedMetric | None: ...
def speed(*, distance_m: Decimal | None, duration_s: Decimal | None) -> DerivedMetric | None: ...
def estimated_one_rm(*, load_kg: Decimal | None, repetitions: int | None) -> DerivedMetric | None: ...
def derive_all(draft: ActivityDraft) -> tuple[DerivedMetric, ...]: ...
```

Every function returns `None` when its inputs are insufficient — never `0`. A zero pace is a claim; `None` is the truth, and §19's later trend analysis would average a fabricated zero without noticing.

`volume` multiplies by `implements` only for `PER_IMPLEMENT` and by 2 only for `PER_SIDE`; `BODYWEIGHT` with no external load yields `None` rather than zero, because Sprint 2 does not know the user's body weight and §14.2 says it must not need to. `estimated_one_rm` returns `None` for `repetitions > 12` — Epley's error past that is larger than the signal, and a fabricated number stored with a version is worse than no number.

## Metric versioning, enforced

`tests/contract/fixtures/derived_metrics.json`:

```json
{
  "volume.load_x_reps.v1": [
    {"inputs": {"load_kg": "80", "repetitions": 10, "load_mode": "total"}, "value": "800.000", "unit": "kg"}
  ],
  "1rm.epley.v1": [
    {"inputs": {"load_kg": "100", "repetitions": 5}, "value": "116.667", "unit": "kg"}
  ]
}
```

`tests/contract/test_derived_metrics.py`:
- every case in the fixture reproduces exactly, including the version string;
- every version in `METRIC_VERSIONS` has ≥1 case in the fixture — a new metric cannot ship unfrozen;
- every version key in the fixture exists in `METRIC_VERSIONS` — a deleted version cannot be silently orphaned.

That is the mechanism behind "changing a formula without bumping its version fails a test": editing the arithmetic changes a frozen value, and the only green path is a new version key with its own frozen cases, leaving the old ones intact.

## Tests

| File | Assertion |
| --- | --- |
| `tests/domain/test_units.py` | golden conversion table, parametrized: `80kg`→80, `80 kg`→80, `80`→80, `176lb`→79.832, `5k`→5000, `5 km`→5000, `1500m`→1500, `3mi`→4828.032, `1:30`→90, `90s`→90, `45min`→2700, `1h05`→3900 |
| " | `parse_distance("5")` and `parse_duration("5")` raise `UnitParseError` naming what was expected |
| " | `parse_load("")`, `parse_load("oito quilos")`, `parse_load("80kgs extra")` raise; nothing returns a default |
| " | conversions are exact `Decimal`; `to_kilograms(parse_load("176lb"))` has no binary-float residue |
| `tests/domain/test_metrics.py` | `pace` and `speed` return `None` when either input is `None`, specifically **not** `Decimal(0)` |
| " | `volume` for `PER_IMPLEMENT` 20 kg × 10 × 2 = 400 kg; `PER_SIDE` doubles; `BODYWEIGHT` alone → `None` |
| " | `estimated_one_rm` returns `None` for reps 0, reps 13, load `None` |
| " | `derive_all` on a strength draft returns metrics whose `version` values are all in `METRIC_VERSIONS` |
| `tests/contract/test_derived_metrics.py` | the three fixture invariants above |

**Done when:** the fixture exists, `make check` green, and deleting a digit from any formula turns exactly one contract test red.

---

# WS-4 — Effort normalization (deterministic half)

**Branch:** `feat/ws-4-effort-normalization`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `src/app/domain/training/effort.py` | `EffortNormalizer` and its tables |

## Signatures

```python
EFFORT_VERSION: Final = "effort.deterministic.v1"

class EffortMethod(StrEnum):
    EXPLICIT_RPE = "explicit_rpe"
    RIR_MAPPED = "rir_mapped"
    PHRASE_TABLE = "phrase_table"
    UNNORMALIZED = "unnormalized"     # stored raw, RPE stays None (§14.3 deviation)

#: RIR -> RPE. Frozen: the sprint file requires this asserted value by value.
RIR_TO_RPE: Final[Mapping[int, Decimal]] = {
    0: Decimal("10"), 1: Decimal("9"), 2: Decimal("8"),
    3: Decimal("7"),  4: Decimal("6"), 5: Decimal("5"),
}

#: Curated pt-BR phrases. Small on purpose: every entry is a claim about what a
#: user meant, and a large table is a large number of unverified claims.
PT_BR_EFFORT_PHRASES: Final[Mapping[str, Decimal]] = {
    "leve": Decimal("5"), "tranquilo": Decimal("6"), "moderado": Decimal("7"),
    "puxado": Decimal("8"), "pesado": Decimal("8.5"),
    "quase falhei": Decimal("9.5"), "falhei": Decimal("10"),
    "ate a falha": Decimal("10"),
}

@dataclass(frozen=True, slots=True)
class NormalizedEffort:
    raw: str
    rpe: Decimal | None
    method: EffortMethod
    version: str = EFFORT_VERSION

class EffortNormalizer:
    def __init__(self, phrases: Mapping[str, Decimal] | None = None) -> None: ...
    def normalize(self, raw: str | None) -> NormalizedEffort | None: ...
```

`normalize(None)` returns `None` — "no effort was stated" and "an effort was stated that we could not read" are different facts and must stay different rows. Phrase lookup normalizes with the same casefold/accent-strip helper WS-7 uses (`app.domain.exercises.normalization.normalize_for_match`), so `"Quase Falhei"` and `"até a falha"` hit the table; if WS-7 has not merged, WS-4 defines the helper and WS-7 imports it.

Recognized explicit forms: `RPE 8`, `rpe8`, `8 rpe`, `RIR 2`, `rir2`, `2 rir`. Anything else goes to the phrase table; anything the phrase table does not contain is `UNNORMALIZED` with `rpe=None`.

## Persistence

Columns land in WS-6, twice, and the split is the point of §14.3:
- `session_exercises.raw_effort / normalized_rpe / effort_method / effort_version` — activity-level effort, stated once for the exercise.
- `exercise_sets.raw_effort / normalized_rpe / effort_method / effort_version` — written **only** when that set stated its own.

WS-8's builder must never copy the activity value into the sets. WS-4 ships the test that would catch it; WS-8 makes it pass end to end.

## Tests

`tests/domain/test_effort_normalizer.py`:

| Case | Expectation |
| --- | --- |
| `"RPE 8"`, `"rpe8"`, `"8 RPE"` | `rpe == 8`, `method == EXPLICIT_RPE` |
| `RIR_TO_RPE` | parametrized over all six pairs, asserted value by value |
| `"RIR 2"` | `rpe == 8`, `method == RIR_MAPPED` |
| every key of `PT_BR_EFFORT_PHRASES` | round-trips to its stated RPE with `method == PHRASE_TABLE` |
| `"Quase Falhei"`, `"até a falha"` | match despite case and accents |
| `"foi osso"` (unlisted) | `raw == "foi osso"`, `rpe is None`, `method == UNNORMALIZED` — the assertion is explicitly *nothing was invented* |
| `None` | returns `None`, not an `UNNORMALIZED` record |
| `"RPE 42"` | `rpe is None`, `UNNORMALIZED` — outside 1–10 is not a reading |
| every outcome | `version == EFFORT_VERSION` and `method` is set |

`tests/application/test_effort_is_not_broadcast.py` (written here, wired in WS-8): an activity with `effort="RPE 8"` and three sets with no effort of their own → effort on the activity, `None` on all three sets.

**Done when:** the RIR table test enumerates the mapping rather than sampling it.

---

# WS-5 — Training sessions

**Branch:** `feat/ws-5-training-sessions`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `migrations/versions/0006_training_sessions.py` | `training_sessions` and `audit_events` |
| + | `src/app/domain/training/sessions.py` | Pure expiry policy |
| + | `src/app/application/services/training_sessions.py` | `TrainingSessionManager` |
| + | `src/app/infrastructure/redis/session_hints.py` | Non-authoritative hints (§18) |
| + | `src/app/workers/session_expiration_worker.py` | The background sweep (Q31) |
| + | `src/app/entrypoints/session_expiration_worker.py` | Its process |
| ~ | `src/app/config/settings.py` | `ServiceName.SESSION_EXPIRATION_WORKER` |
| ~ | `models.py`, `grants.py` | `TrainingSession`, `AuditEvent`, `ActorType` + grants |
| ~ | `docker/compose.yaml`, `.env.example` | Role env vars + a service |
| ~ | `tests/conftest.py` | `TRUNCATE` list (roles come from the `ServiceName` loop) |
| ~ | `tests/unit/test_persistence_contracts.py`, `tests/unit/test_entrypoints.py` | New table, new entrypoint |

`WorkflowSettings.training_session_timeout` already exists (3 h default) — no settings change beyond the new `ServiceName`.

## Schema (migration `0006_training_sessions`)

`down_revision = "0005_exercise_catalog"`.

`training_sessions`: `user_id FK`, `conversation_id FK NULL`, `status` (`ACTIVE`/`CLOSED`), `started_at`, `last_activity_at`, `finished_at NULL`, `closed_by` (`LAZY`/`WORKER`/`EXPLICIT`, nullable), `expected_version int NOT NULL DEFAULT 1`, soft-delete.

`audit_events` (append-only, no soft delete) is created by this same migration: `actor_type str(32)` (`USER`/`SYSTEM`/`OPERATOR`), `actor_user_id FK NULL`, `action str(64)`, `entity_type str(64)`, `entity_id UUID`, `workflow_execution_id FK NULL`, `metadata JSONB`, `occurred_at`. Index `(entity_type, entity_id)`; index `(actor_user_id, occurred_at)`.

It lands here rather than with the workout schema because **sessions are the first thing this sprint mutates**, and a table that appears one migration after its first auditable event is a gap nobody notices until an audit asks about the missing week. §26 makes it append-only, so it joins `APPEND_ONLY_TABLES` and the existing invariant test starts covering it immediately: no role may UPDATE or DELETE.

It is not a second copy of `domain_events`. An event says what happened so consumers can react; an audit row says who caused it. They answer different questions when somebody asks why a set exists.

```python
sa.Index(
    "uq_training_sessions_one_active_per_user",
    "user_id", unique=True,
    postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL"),
)
sa.Index("ix_training_sessions_expiry", "status", "last_activity_at")
```

The partial unique index is the WS-7-of-Sprint-1 lesson applied here, exactly as the sprint file asks: two concurrent logs for one user must not open two sessions, and "must not" written in a service is a hope until the database agrees.

## Signatures

```python
# domain/training/sessions.py  — pure, no I/O
class SessionCloseReason(StrEnum):
    LAZY = "lazy"; WORKER = "worker"; EXPLICIT = "explicit"

def is_expired(*, last_activity_at: datetime, now: datetime, timeout: timedelta) -> bool: ...
def expiry_deadline(*, last_activity_at: datetime, timeout: timedelta) -> datetime: ...
```

```python
# application/services/training_sessions.py
Clock = Callable[[], datetime]

class TrainingSessionManager:
    def __init__(self, *, timeout: timedelta, clock: Clock = utc_now) -> None: ...

    async def active_session(self, session: AsyncSession, user_id: UUID) -> TrainingSession | None:
        """The user's open session, or None. Applies lazy expiry first (Q31)."""

    async def start_or_resume(
        self, session: AsyncSession, *, user_id: UUID, conversation_id: UUID,
    ) -> tuple[TrainingSession, bool]:
        """Returns (session, opened). Closes an expired one on the way through."""

    async def touch(self, session: AsyncSession, training_session: TrainingSession) -> None:
        """Refresh last_activity_at — PostgreSQL is authoritative (§18)."""

    async def close(
        self, session: AsyncSession, training_session: TrainingSession,
        *, reason: SessionCloseReason,
    ) -> None:
        """Close and record `training_session.finished` via record_domain_event."""

    async def close_expired(
        self, session: AsyncSession, *, limit: int = 100,
        candidates: Sequence[UUID] | None = None,
    ) -> list[UUID]:
        """Worker path. `candidates` is a *hint* — each is re-checked against
        `last_activity_at` under `FOR UPDATE SKIP LOCKED` before closing."""
```

`start_or_resume` handles the race the partial index creates: on `IntegrityError` against `uq_training_sessions_one_active_per_user` it re-reads and returns the winner rather than propagating. That is the only place in this sprint where an `IntegrityError` is caught rather than allowed to abort.

```python
# infrastructure/redis/session_hints.py
class RedisSessionHintStore:
    KEY_PREFIX: Final = "session:v1:expiry-hint:user:"
    def __init__(self, redis: Redis, *, timeout: timedelta) -> None: ...
    async def note_activity(self, user_id: UUID) -> None: ...
    async def expiry_candidates(self, *, limit: int = 100) -> list[UUID]: ...
```

Hints only ever *narrow* the worker's scan. `close_expired` never trusts one: the hint says "look here", `last_activity_at` says whether to act. §18 is explicit, and this is the seam where it would quietly stop being true.

```python
# workers/session_expiration_worker.py
class SessionExpirationWorker:
    def __init__(self, *, session_factory, manager: TrainingSessionManager,
                 hints: RedisSessionHintStore | None = None,
                 interval: timedelta = timedelta(minutes=1), batch: int = 100) -> None: ...
    async def run_once(self) -> list[UUID]: ...
    async def run_forever(self, stop: asyncio.Event) -> None: ...
```

The entrypoint follows `entrypoints/outbox_publisher.py`: `worker_runtime(...)`, the shared `shutdown_event()` from `entrypoints/runtime.py` (never its own handler — the WS-13 lesson), `run(main)`.

## Grants

```python
ServiceName.SESSION_EXPIRATION_WORKER: {
    "users": ("SELECT",),
    "training_sessions": ("SELECT", "UPDATE"),   # no INSERT: it never opens one
    "audit_events": ("SELECT", "INSERT"),        # it closes sessions, so it attributes them
    "domain_events": ("SELECT", "INSERT"),
    "outbox_events": ("SELECT", "INSERT"),
}
ServiceName.WORKFLOW_WORKER: {
    ...,
    "training_sessions": ("SELECT", "INSERT", "UPDATE"),
    "audit_events": ("SELECT", "INSERT"),
}
```

The expiration worker having no `INSERT` is a real constraint, not decoration: a bug that made the sweep *open* a session would be refused by PostgreSQL rather than discovered in a user's history.

## Tests

| File | Assertion |
| --- | --- |
| `tests/domain/test_session_expiry.py` | `is_expired` at `timeout − 1s`, exactly `timeout`, `timeout + 1s`; naive datetimes rejected |
| `tests/integration/test_training_sessions.py` | first log opens a session, `opened is True` |
| " | a second call inside the timeout returns the same id, `opened is False`, `last_activity_at` moved |
| " | a call after the timeout closes the old one (`status=CLOSED`, `closed_by=LAZY`, `finished_at` set) and opens a new one |
| " | the same expiry through `SessionExpirationWorker.run_once` → `closed_by=WORKER` |
| " | a `training_session.finished` domain event and its outbox row exist after each close |
| " | **each close writes an `audit_events` row**: the lazy path with `actor_type=USER` and `metadata.closed_by="lazy"`, the worker path with `actor_type=SYSTEM` and `"worker"` — a session that closed itself with nobody attributed is the audit gap this table exists to prevent |
| " | opening a session writes `training_session.started`, so a session's whole lifecycle is attributable rather than only its end |
| `tests/integration/test_persistence.py` (~) | no service role can UPDATE or DELETE `audit_events`, asserted with each role's own credentials |
| " | **a stale Redis hint for a user whose `last_activity_at` is recent closes nothing** (§18) |
| " | two concurrent `start_or_resume` calls for one user (`asyncio.gather`, two sessions, real Postgres) yield one row — assert `SELECT count(*)`, not return values |
| " | `performed_at` is "now", never taken from message content (Q32) — asserted in WS-9 where rows exist, stubbed here |
| `tests/integration/test_persistence.py` (~) | the session-expiration role is refused `INSERT` on `training_sessions` |
| `tests/unit/test_entrypoints.py` (~) | the new entrypoint installs no signal handler of its own and stops on the shared event |

**Done when:** `make up` starts six application containers, and the closure assertion holds through both the lazy path and the worker path.

---

# WS-6 — Workout domain schema

**Branch:** `feat/ws-6-workout-schema`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `migrations/versions/0007_workout_domain.py` | Four tables |
| + | `src/app/domain/training/provenance.py` | `Provenance`, `SourceRole`, `ExerciseGroupType` |
| ~ | `models.py` | `SessionExercise`, `ExerciseSet`, `ExerciseGroup`, `EntitySource` |
| ~ | `grants.py`, `tests/unit/test_persistence_contracts.py`, `tests/conftest.py` | The four lists |

## Domain enums

```python
class Provenance(StrEnum):        # §14.4
    EXPLICIT = "explicit"
    INHERITED = "inherited"

class SourceRole(StrEnum):        # §26.2
    CREATED_FROM = "created_from"
    UPDATED_FROM = "updated_from"
    CLARIFIED_BY = "clarified_by"

class ExerciseGroupType(StrEnum): # Q51
    SUPERSET = "superset"; TRISET = "triset"
    CIRCUIT = "circuit"; COMPLEX = "complex"
```

## Schema (migration `0007_workout_domain`)

`down_revision = "0006_training_sessions"`.

**`session_exercises`** — soft-deletable, `expected_version int NOT NULL DEFAULT 1`:

| Column | Notes |
| --- | --- |
| `training_session_id FK` | `ondelete="CASCADE"` |
| `exercise_id FK` | `NOT NULL` — an unresolved exercise is never persisted (WS-7) |
| `exercise_group_id FK NULL`, `position_in_group int NULL` | Q51 |
| `exercise_block_index int NOT NULL` | Q58 |
| `activity_type` | denormalized from the catalog at write time, so history survives a catalog edit |
| `raw_effort Text NULL`, `normalized_rpe NUMERIC(3,1) NULL`, `effort_method NULL`, `effort_version str(64) NULL` | §14.3 activity level |
| `performed_at timestamptz NOT NULL` | current interaction only (§7.3, Q32) |
| `notes Text NULL` | |

`UNIQUE(training_session_id, exercise_block_index)` → `uq_session_exercises_block`; index on the same pair.

**`exercise_sets`** — soft-deletable, `expected_version int NOT NULL DEFAULT 1`:

| Column | Notes |
| --- | --- |
| `session_exercise_id FK` | `ondelete="CASCADE"` |
| `set_index int NOT NULL` | never renumbered on soft delete |
| `set_type` | default `WORKING` |
| `repetitions int NULL`, `repetitions_provenance` | |
| `load_kg NUMERIC(7,3) NULL`, `load_mode NULL`, `load_provenance` | |
| `raw_load_text str(64) NULL` | §19 keeps reported load separate from effective load |
| `distance_m NUMERIC(10,3) NULL`, `distance_provenance` | |
| `duration_s NUMERIC(10,3) NULL`, `duration_provenance` | |
| `raw_effort / normalized_rpe / effort_method / effort_version` | set level only |
| `volume_kg NUMERIC(12,3) NULL`, `volume_metric_version str(64) NULL` | |
| `estimated_one_rm_kg NUMERIC(7,3) NULL`, `one_rm_metric_version str(64) NULL` | |
| `pace_s_per_km NUMERIC(10,3) NULL`, `pace_metric_version str(64) NULL` | |
| `speed_m_s NUMERIC(10,3) NULL`, `speed_metric_version str(64) NULL` | |

`UNIQUE(session_exercise_id, set_index)` → `uq_exercise_sets_index`; `CHECK ((volume_kg IS NULL) = (volume_metric_version IS NULL))` and the same paired check for each of the other three metrics — a derived value without its version is exactly what Q52 forbids, and the database is a cheaper place to enforce it than a review.

**`exercise_groups`**: `training_session_id FK`, `group_type`, `block_index int`, `rounds int NULL`, soft-deletable. `UNIQUE(training_session_id, block_index)`.

**`entity_sources`** (append-only, no soft delete): `entity_type str(64)`, `entity_id UUID`, `message_id FK NULL`, `message_batch_id FK NULL`, `source_role`. `CHECK (message_id IS NOT NULL OR message_batch_id IS NOT NULL)`; index `(entity_type, entity_id)`; index `(message_batch_id)`.

`downgrade()` drops entity_sources, exercise_sets, session_exercises, exercise_groups.

`audit_events` is **not** created here — see WS-5, where it lands with the first
thing that mutates.

## Block-index rule (Q58)

Implemented in WS-9's service, specified here because the schema is what makes it checkable:

```python
async def next_block_index(
    session: AsyncSession, *, training_session_id: UUID, exercise_id: UUID
) -> tuple[int, bool]:
    """(index, is_new_block). Reuses the highest block when it is the same
    exercise; otherwise max + 1. Soft-deleted blocks still consume an index."""
```

## Grants

`workflow-worker` gets `SELECT, INSERT, UPDATE` on `session_exercises`, `exercise_sets`, `exercise_groups`, and `SELECT, INSERT` on `entity_sources` — never `UPDATE`, because provenance is history. Its `audit_events` grant was already added in WS-5, with the table. No other service gets anything.

## Tests

`tests/integration/test_workout_schema.py`:

| Case | Assertion |
| --- | --- |
| consecutive sets of one exercise | one `session_exercises` row, `set_index` 0..n |
| A, B, A within a session | two blocks for A, `exercise_block_index` 0 and 2 — order preserved (Q58) |
| set ordering | `set_index` contiguous within a block, ordered by insertion |
| soft-deleted set | `deleted_at` set, remaining `set_index` unchanged, `select_active` excludes it |
| provenance | a row with `load_provenance = INHERITED` and `repetitions_provenance = EXPLICIT` persists and reads back |
| metric pairing | inserting `volume_kg` without `volume_metric_version` raises `IntegrityError` |
| `entity_sources` | every persisted set is reachable from its source message by joining on `(entity_type='exercise_set', entity_id)` — asserted as a query, not a Python walk |
| `entity_sources` | a row with neither `message_id` nor `message_batch_id` raises |
| migration | `0007` up and down clean |
| `tests/integration/test_persistence.py` (~) | no service has `UPDATE` on `entity_sources`; no service has `DELETE` on anything (the existing invariant test picks the new tables up from `ALL_TABLES` automatically) |

**Done when:** the A-B-A block test passes and `alembic downgrade -1` leaves `0006` intact.

---

# WS-7 — Exercise resolver, deterministic stages

**Branch:** `feat/ws-7-exercise-resolver`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `src/app/domain/exercises/normalization.py` | Matching normalization |
| + | `src/app/domain/exercises/resolution.py` | Result types, thresholds, scoring |
| + | `src/app/application/ports/exercise_catalog.py` | The port (Q155) |
| + | `src/app/application/services/exercise_resolver.py` | The staged resolver |
| + | `src/app/infrastructure/postgres/exercise_catalog.py` | `PostgresExerciseCatalog` |
| + | `tests/domain/fixtures/exercise_resolution.json` | The golden raw-name table |
| ~ | `pyproject.toml` | `rapidfuzz>=3.10` (D2) |

## Signatures

```python
# domain/exercises/normalization.py
def normalize_for_match(raw: str) -> str:
    """Casefold, strip accents (NFKD + combining-mark removal), collapse
    punctuation and whitespace to single spaces, trim. Deterministic and
    lossless in the only sense that matters: two spellings a human considers
    the same must land on the same string."""
```

```python
# domain/exercises/resolution.py
class ResolutionMethod(StrEnum):     # §16, order is normative
    USER_ALIAS = "user_alias"
    GLOBAL_ALIAS = "global_alias"
    CANONICAL = "canonical"
    FUZZY = "fuzzy"
    VECTOR = "vector"           # Sprint 8 — declared, never returned
    LLM = "llm"                 # Sprint 3 — declared, never returned
    USER_CONFIRMED = "user_confirmed"

#: Stages this sprint does not implement. A test asserts none is ever returned.
UNIMPLEMENTED_METHODS: Final[frozenset[ResolutionMethod]] = frozenset(
    {ResolutionMethod.VECTOR, ResolutionMethod.LLM, ResolutionMethod.USER_CONFIRMED}
)

HIGH_CONFIDENCE: Final = 0.90       # D3
MEDIUM_CONFIDENCE: Final = 0.70
AMBIGUITY_MARGIN: Final = 0.02

@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    exercise_id: UUID
    canonical_name: str
    score: float

@dataclass(frozen=True, slots=True)
class ExerciseResolution:
    raw_name: str
    exercise_id: UUID | None = None
    canonical_name: str | None = None
    method: ResolutionMethod | None = None
    confidence: float = 0.0
    candidates: tuple[ResolutionCandidate, ...] = ()
    requires_clarification: bool = False
    @property
    def resolved(self) -> bool: ...     # exercise_id is not None
    def __post_init__(self) -> None:
        """A resolution cannot be both resolved and requires_clarification, and
        cannot carry an exercise_id without a method."""
```

```python
# application/ports/exercise_catalog.py
class ExerciseCatalogPort(Protocol):
    async def by_user_alias(self, normalized: str, user_id: UUID) -> CatalogEntry | None: ...
    async def by_global_alias(self, normalized: str) -> CatalogEntry | None: ...
    async def by_canonical_name(self, normalized: str) -> CatalogEntry | None: ...
    async def all_searchable(self) -> Sequence[SearchableExercise]: ...

@dataclass(frozen=True, slots=True)
class CatalogEntry:
    exercise_id: UUID
    canonical_name: str
    activity_type: ActivityType
    default_load_mode: LoadMode
    is_bodyweight: bool
    uses_implements: bool          # from equipment.is_implement — Q49

@dataclass(frozen=True, slots=True)
class SearchableExercise:
    entry: CatalogEntry
    normalized_terms: tuple[str, ...]   # canonical name + every alias
```

```python
# application/services/exercise_resolver.py
class ExerciseResolver:
    def __init__(
        self, catalog: ExerciseCatalogPort, *,
        high_confidence: float = HIGH_CONFIDENCE,
        medium_confidence: float = MEDIUM_CONFIDENCE,
        ambiguity_margin: float = AMBIGUITY_MARGIN,
        max_candidates: int = 5,
    ) -> None: ...

    async def resolve(self, raw_name: str, *, user_id: UUID) -> ExerciseResolution:
        """Stages 1-4 in the normative order, first hit wins."""
```

Stage confidences: exact user alias, exact global alias and exact canonical match all return `1.0`. Fuzzy uses `rapidfuzz.fuzz.WRatio` over `normalized_terms`, scaled to 0..1. Routing (D3):

| Top score | Outcome |
| --- | --- |
| ≥ 0.90, second-best more than `AMBIGUITY_MARGIN` behind | resolved, `method=FUZZY` |
| ≥ 0.90, second-best within the margin | **not** resolved, `requires_clarification=True`, both candidates returned |
| 0.70 – 0.90 | not resolved, candidates returned, `requires_clarification=False` — the caller may ask, and Sprint 3's LLM stage will take it |
| < 0.70 | not resolved, no candidates, `requires_clarification=True` |

`PostgresExerciseCatalog.all_searchable` is cached per instance (the catalog is ~40 rows and immutable within a request); per resolver instance, not process-global, so a learned alias written in Sprint 3 is visible on the next message.

## Tests

| File | Assertion |
| --- | --- |
| `tests/domain/test_normalization.py` | `"Supino Reto"`, `"supino  reto"`, `"SUPINO-RETO"`, `"supíno reto"` all → `"supino reto"`; empty string and pure punctuation → `""` |
| `tests/application/test_exercise_resolver.py` | the golden table below, parametrized from the fixture, against a fake `ExerciseCatalogPort` built from `CATALOG_SEED` |
| " | **stage order**: user alias `"supino"` → A, global alias `"supino"` → B; resolving for that user returns A, for another user returns B |
| " | a fuzzy score below 0.70 resolves to **nothing** — `exercise_id is None`, `requires_clarification is True`. The sprint file names this the worst possible failure; the test asserts the absence, not a substitute |
| " | two exercises scoring within `AMBIGUITY_MARGIN` set `requires_clarification` and return both candidates in score order, rather than picking the first |
| " | a 0.70–0.90 match returns candidates with `requires_clarification is False` and `exercise_id is None` |
| " | `resolve` never returns a method in `UNIMPLEMENTED_METHODS`, over every fixture row |
| " | `ExerciseResolution(exercise_id=..., requires_clarification=True)` raises in `__post_init__` |
| `tests/integration/test_exercise_catalog.py` | `PostgresExerciseCatalog` against the seeded database: each of the four lookups returns the right row; `uses_implements` is `True` for a dumbbell exercise, `False` for a barbell one |

`tests/domain/fixtures/exercise_resolution.json` — the resolver's contract with real input, ~40 rows of `{"raw": ..., "canonical": ... | null, "method": ...}`: exact pt-BR names (`"supino reto"`), accented (`"remada curvada"`, `"flexão"`), common misspellings (`"supino retp"`, `"agacahmento"`), en names (`"bench press"`, `"squat"`), seeded abbreviations (`"rdl"`, `"leg press"`), and **explicit nulls** for names that must not resolve (`"aquele exercício do peito"`, `"treino de ontem"`).

**Done when:** the golden table passes and `rapidfuzz` is pinned in `uv.lock` — an unpinned scorer makes the frozen table meaningless.

---

# WS-8 — Input contract and command builder

**Branch:** `feat/ws-8-input-contract`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `src/app/domain/training/input_contract.py` | `StructuredWorkoutInput` |
| + | `src/app/domain/training/inheritance.py` | Fragment inheritance (§14.4) |
| + | `src/app/application/commands/workout.py` | Command types |
| + | `src/app/application/services/workout_command_builder.py` | The builder |
| + | `tests/contract/fixtures/structured_workout_input.json` | The frozen contract |

## The contract

Pydantic, `frozen=True`, `extra="forbid"` — this is the file Sprint 3's extractor is written against, so an unknown field must be an error rather than a silent ignore.

```python
SCHEMA_VERSION: Final = "workout-input.v1"

class StructuredSetInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repetitions: int | None = None
    load: str | None = None            # raw as stated: "80kg", "176lb", "20"
    load_mode: LoadMode | None = None  # None => derived from the catalog (Q49)
    set_type: SetType = SetType.WORKING
    distance: str | None = None        # "5km", "1500m"
    duration: str | None = None        # "1:30", "90s"
    effort: str | None = None
    notes: str | None = None

class StructuredActivityInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    raw_name: str = Field(min_length=1)
    activity_type: ActivityType | None = None   # None => taken from the catalog
    sets: tuple[StructuredSetInput, ...] = ()
    effort: str | None = None                   # activity level, §14.3
    group_ref: str | None = None

class StructuredGroupInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ref: str
    group_type: ExerciseGroupType
    rounds: int | None = None

class StructuredWorkoutInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["workout-input.v1"] = SCHEMA_VERSION
    activities: tuple[StructuredActivityInput, ...]
    groups: tuple[StructuredGroupInput, ...] = ()
    #: Free-text the producer could not structure. Kept so nothing is lost; the
    #: domain never parses it.
    unparsed: tuple[str, ...] = ()
```

Every field is one a language model can plausibly emit from a sentence — the sprint's second risk. Nothing requires an id, a UUID, or a unit conversion the model would have to perform.

## Fragment inheritance

```python
# domain/training/inheritance.py
@dataclass(frozen=True, slots=True)
class InheritedValue[T]:
    value: T | None
    provenance: Provenance

@dataclass(frozen=True, slots=True)
class InheritedSet:
    repetitions: InheritedValue[int]
    load: InheritedValue[str]
    load_mode: InheritedValue[LoadMode]
    distance: InheritedValue[str]
    duration: InheritedValue[str]
    set_type: SetType
    effort: str | None            # never inherited (§14.3)

def inherit_within_block(sets: Sequence[StructuredSetInput]) -> tuple[InheritedSet, ...]:
    """Carry a stated value forward to later sets of the same block, marking
    each carried value INHERITED (§14.4, Q54).

    Repetitions are never inherited: `80kg: 10, 9, 8` states three of them, and
    a missing rep count is the clarification case (Q46), not a gap to fill.
    Effort is never inherited (§14.3).
    """
```

## Commands

```python
# application/commands/workout.py
@dataclass(frozen=True, slots=True)
class SetCommand:
    set_index: int
    set_type: SetType
    repetitions: int | None
    repetitions_provenance: Provenance
    load_kg: Decimal | None
    load_mode: LoadMode | None
    load_provenance: Provenance
    raw_load_text: str | None
    distance_m: Decimal | None
    distance_provenance: Provenance
    duration_s: Decimal | None
    duration_provenance: Provenance
    effort: NormalizedEffort | None
    metrics: tuple[DerivedMetric, ...]

@dataclass(frozen=True, slots=True)
class ActivityCommand:
    exercise_id: UUID
    canonical_name: str
    activity_type: ActivityType
    effort: NormalizedEffort | None
    sets: tuple[SetCommand, ...]
    group_ref: str | None = None

@dataclass(frozen=True, slots=True)
class LogWorkoutCommand:
    operation_id: str
    user_id: UUID
    conversation_id: UUID
    message_batch_id: UUID
    source_message_ids: tuple[UUID, ...]
    activities: tuple[ActivityCommand, ...]
    groups: tuple[GroupCommand, ...] = ()
    def __post_init__(self) -> None:
        """Raise EmptyCommandError when there is nothing to commit."""

class EmptyCommandError(ValueError): ...

class DeferralReason(StrEnum):
    UNRESOLVED_EXERCISE = "unresolved_exercise"
    AMBIGUOUS_EXERCISE = "ambiguous_exercise"
    MISSING_ESSENTIAL_DATA = "missing_essential_data"
    INVALID_VALUE = "invalid_value"

@dataclass(frozen=True, slots=True)
class DeferredItem:
    raw_name: str
    reason: DeferralReason
    missing_field: ActivityField | None = None
    candidates: tuple[ResolutionCandidate, ...] = ()

@dataclass(frozen=True, slots=True)
class BuildOutcome:
    command: LogWorkoutCommand | None
    deferred: tuple[DeferredItem, ...]
```

```python
# application/services/workout_command_builder.py
class WorkoutCommandBuilder:
    def __init__(
        self, *, resolver: ExerciseResolver, validator: ActivityValidator,
        effort: EffortNormalizer,
    ) -> None: ...

    async def build(
        self, structured: StructuredWorkoutInput, *,
        user_id: UUID, conversation_id: UUID, message_batch_id: UUID,
        source_message_ids: Sequence[UUID], operation_id: str,
    ) -> BuildOutcome:
        """Resolve, inherit, normalize units and effort, validate, and keep only
        what is committable. Ambiguous or incomplete items are excluded from the
        command and reported (Q56); valid work is never discarded because of
        them (Q57)."""
```

Pipeline per activity, in order: resolve (WS-7) → activity type from the catalog when the input left it `None` → `inherit_within_block` → parse units (WS-3) → `load_mode` default from `CatalogEntry` (`uses_implements` → `PER_IMPLEMENT`; `is_bodyweight` → `BODYWEIGHT` / `BODYWEIGHT_PLUS` when external load is present; else `TOTAL`) → normalize effort (WS-4) → validate (WS-2) → derive metrics (WS-3). Any failure at any step produces a `DeferredItem` for **that activity only**.

`operation_id` is `f"log_workout:{message_batch_id}"` — derived from the batch, so a redelivery computes the same value without carrying state (WS-9 relies on this).

## Tests

| File | Assertion |
| --- | --- |
| `tests/contract/test_structured_workout_input.py` | the golden fixture parses and re-serializes byte-identical after a `model_dump(mode="json")` round-trip |
| " | a fixture with an added unknown field raises `ValidationError` (`extra="forbid"`) |
| " | `schema_version` other than `workout-input.v1` is refused |
| " | the fixture covers, in one file: a multi-set strength activity with inheritance, a bodyweight activity, a distance activity, a superset group, and an `unparsed` fragment |
| `tests/domain/test_inheritance.py` | `80kg: 10, 9, 8` → three sets, load `EXPLICIT` then `INHERITED` twice, repetitions `EXPLICIT` on all three |
| " | a restated load stops the chain and is `EXPLICIT` again |
| " | effort stated on set 1 does **not** appear on sets 2 and 3 |
| `tests/application/test_workout_command_builder.py` | one resolvable + one ambiguous exercise → command carries only the first; `deferred` names the second with `AMBIGUOUS_EXERCISE` and its candidates (Q56) |
| " | a strength activity with no reps → `deferred` with `MISSING_ESSENTIAL_DATA`, `missing_field == REPETITIONS`; `command is None` if it was the only activity |
| " | every activity deferred → `command is None`; constructing `LogWorkoutCommand` with no activities raises `EmptyCommandError` |
| " | a dumbbell exercise with `load="20kg"` and no stated mode → `load_mode == PER_IMPLEMENT` (Q49), asserted without the input mentioning it |
| " | a bodyweight exercise with no load → `load_mode == BODYWEIGHT`, `load_kg is None` |
| " | activity-level effort lands on `ActivityCommand.effort`, every `SetCommand.effort is None` (the WS-4 test, now wired) |
| " | every `SetCommand.metrics` entry carries a version from `METRIC_VERSIONS` |

**Done when:** the fixture is committed and Sprint 3 could write an extractor against it without opening any other file.

---

# WS-9 — Application service and the `LOG_WORKOUT` handler

**Branch:** `feat/ws-9-workout-application-service`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `src/app/application/services/workout_logging.py` | `WorkoutApplicationService` |
| + | `src/app/domain/training/confirmations.py` | Deterministic pt-BR templates (D8) |
| + | `src/app/graphs/main/routing.py` | Task-type routing |
| ~ | `src/app/graphs/main/handlers.py` | `LOG_WORKOUT` registered; registry becomes a factory |
| ~ | `src/app/workers/workflow_worker.py` | Router instead of a fixed task type; message ids in `TaskInput` |
| ~ | `src/app/entrypoints/workflow_worker.py` | Compose the service, pass the registry |
| ~ | `tests/unit/test_task_handlers.py` | The new registry shape |

## Signatures

```python
# application/services/workout_logging.py
@dataclass(frozen=True, slots=True)
class LoggedExercise:
    session_exercise_id: UUID
    canonical_name: str
    block_index: int
    set_count: int

@dataclass(frozen=True, slots=True)
class WorkoutLoggedResult:
    training_session_id: UUID
    session_opened: bool
    exercises: tuple[LoggedExercise, ...]
    #: True when this operation_id had already been processed: the rows exist
    #: from the first run and nothing was written now (§28).
    replayed: bool = False
    @property
    def set_count(self) -> int: ...

class WorkoutApplicationService:
    def __init__(
        self, *, session_factory: async_sessionmaker[AsyncSession],
        sessions: TrainingSessionManager, clock: Clock = utc_now,
    ) -> None: ...

    async def log_workout(self, command: LogWorkoutCommand) -> WorkoutLoggedResult:
        """One transaction (§15, Q57): the idempotency claim, the training
        session, session_exercises, exercise_sets, exercise_groups,
        entity_sources, domain_events and outbox_events."""
```

Transaction body, in order, inside a single `unit_of_work`:

1. `INSERT INTO processed_operations (operation_id, operation_type) VALUES (...) ON CONFLICT DO NOTHING` and check the row count. Zero means a redelivery: read back what the first run wrote (via `entity_sources` on `message_batch_id`) and return it with `replayed=True`. **Claiming first** is what makes this safe — a check-then-act would let two concurrent deliveries both pass the check.
2. `sessions.start_or_resume(...)` then `sessions.touch(...)`.
3. Per activity: `next_block_index(...)`, insert or reuse the `session_exercises` row, insert `exercise_sets` with provenance and metric versions.
4. Insert `entity_sources`: one per created `session_exercise` and per created `exercise_set`, pointing at the batch and at every source message, `source_role=CREATED_FROM` (§26.2).
5. Insert `audit_events` for **every** entity this transaction mutated, not only the created sets: `training_session.started` when step 2 opened one, `training_session.closed` when step 2 lazily closed a stale one (`metadata.closed_by="lazy"`), `exercise_group.created` when a group was built, and `workout.logged` per created `session_exercise` and `exercise_set`. `actor_type=USER` with `actor_user_id` from the batch, carrying the `workflow_execution_id` (§15, §26).

   Auditing only the leaves would leave the session a set belongs to unattributed, and the session is the row an operator asks about first.
6. `record_domain_event(... "workout.logged" ...)` on `Exchanges.DOMAIN_EVENTS`, payload naming the session, the exercises and the set count.

There is no step between the domain write, the audit write and the outbox write. That is the claim WS-11's failure-injection test exists to prove, and this ordering is what makes it provable.

```python
# domain/training/confirmations.py  — pure, D8
def workout_confirmation(result: WorkoutLoggedResult) -> str:
    """e.g. 'Registrei supino reto: 3 séries.' Names the exercise and the set
    count — §25 requires a confirmation for every successful registration."""

def clarification_request(deferred: Sequence[DeferredItem]) -> str:
    """Names the missing datum: 'Quantas repetições você fez no supino reto?'
    For an ambiguous exercise, lists the candidates. Never invents a value."""

def partial_confirmation(result: WorkoutLoggedResult, deferred: Sequence[DeferredItem]) -> str:
    """Both, in one message: what was recorded and what is still needed (Q56)."""
```

```python
# graphs/main/routing.py
def route(texts: Sequence[str]) -> TaskType:
    """LOG_WORKOUT when the strict-syntax adapter recognizes the input (WS-10),
    CONVERSATION otherwise. Sprint 3 replaces this with IntentRouter; the
    signature is chosen so that is a substitution, not a rewrite."""
```

```python
# graphs/main/handlers.py  (modified)
@dataclass(frozen=True, slots=True)
class TaskInput:
    task_type: TaskType
    message_batch_id: UUID
    user_id: UUID
    conversation_id: UUID
    texts: tuple[str, ...]
    message_ids: tuple[UUID, ...] = ()   # NEW — entity_sources needs them (§26.2)

def build_task_handlers(
    *, workout: WorkoutApplicationService | None = None,
    builder: WorkoutCommandBuilder | None = None,
) -> dict[TaskType, TaskHandler]:
    """The registry, composed with its dependencies. The module-level
    TASK_HANDLERS dict is kept for CONVERSATION so nothing that imported it
    breaks, but the worker now takes a registry."""

def make_log_workout_handler(
    workout: WorkoutApplicationService, builder: WorkoutCommandBuilder,
) -> TaskHandler: ...
```

The `LOG_WORKOUT` handler returns a `DomainResult` with `visibility=USER_VISIBLE`, one `OutboundText` at sequence 0 (confirmation, clarification or partial), and `facts` carrying `training_session_id`, `exercises` and `sets` so Sprint 3's `ResponseGuard` has something to check against.

`WorkflowWorker.__init__` changes from `task_type: TaskType` to `handlers: Mapping[TaskType, TaskHandler]` plus `router: Callable[[Sequence[str]], TaskType] = route`, and `_batch_texts` becomes `_batch_messages` returning `tuple[tuple[UUID, str], ...]`. The existing redelivery guard (`status is SUCCEEDED` → skip) stays exactly as it is; the `processed_operations` claim in the service is the second line of defence for the case where the execution row exists but the domain write is being retried.

## Tests

| File | Assertion |
| --- | --- |
| `tests/integration/test_workout_logging.py` | one exercise, three sets → 1 `training_sessions`, 1 `session_exercises`, 3 `exercise_sets`, ≥4 `entity_sources`, ≥4 `audit_events`, 1 `domain_events`, 1 `outbox_events`, in one transaction |
| `tests/integration/test_workout_logging.py` | every service role is refused UPDATE and DELETE on `audit_events`, asserted with that role's own credentials — the same protection `domain_events` has |
| " | **redelivery**: two calls with the same `operation_id` → `count(exercise_sets)` is 3 both times, `count(domain_events WHERE event_type='workout.logged')` is 1. Asserted on row counts, never on `result.replayed` alone |
| " | two concurrent calls with the same `operation_id` (`asyncio.gather`, real Postgres) → 3 sets total |
| " | a failure mid-transaction (patched to raise after the sets are added) leaves zero `training_sessions`, zero `exercise_sets`, zero `outbox_events` |
| " | load `EXPLICIT` on set 1 and `INHERITED` on sets 2 and 3, read back from the database (the sprint's fourth risk: assert provenance, not totals) |
| " | every `exercise_sets` row is reachable from its source message through `entity_sources` |
| `tests/unit/test_confirmations.py` | the confirmation names the canonical exercise name and the set count; the clarification names *repetições*; neither contains a value absent from its input |
| `tests/unit/test_task_handlers.py` (~) | `build_task_handlers` resolves `LOG_WORKOUT`; `resolve_handler` still raises `UnknownTaskTypeError` for an unknown type; a registry built without a workout service does **not** expose `LOG_WORKOUT` (a misconfigured worker must fail loudly, not acknowledge) |
| `tests/unit/test_routing.py` | `#log ...` → `LOG_WORKOUT`; anything else → `CONVERSATION` |
| CI | `mypy --strict` clean with the domain in place |

**Done when:** the redelivery test passes on row counts and `make check` is green.

---

# WS-10 — Strict-syntax input adapter (temporary)

**Branch:** `feat/ws-10-strict-syntax-adapter`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `src/app/application/services/strict_syntax.py` | The rigid parser |
| ~ | `README.md` | The syntax, and the sprint that deletes it |

## Signatures

```python
LOG_PREFIX: Final = "#log"          # D7

class StrictSyntaxError(ValueError):
    """The text starts with #log but does not parse. Never a silent skip: the
    user typed the marker, so they get told what was wrong."""
    def __init__(self, raw: str, problem: str) -> None: ...

def matches(text: str) -> bool:
    """True when the text begins with the prefix, case-insensitively."""

def parse(texts: Sequence[str]) -> StructuredWorkoutInput | None:
    """Parse every `#log` line in a batch into one StructuredWorkoutInput.
    Returns None when no line carries the prefix — the caller then falls through
    to the acknowledgement handler (item 36)."""
```

Grammar, and nothing beyond it:

```text
#log <exercise words> [<load>] [<reps> ...]
  <load>  = a unit-suffixed token parseable by parse_load: 80kg, 176lb
  <reps>  = bare integers, one per set
  effort  = optional trailing @<effort>: @RPE8, @RIR2
```

Everything is positional and every token type is distinguishable by shape. The parser does not know synonyms, does not infer a missing rep count, and does not accept `x` notation (`3x10`) — that is natural language, and ADR-013 draws the line there. `#log supino 80kg` produces one set with `load` and `repetitions=None`, which is the input WS-2 turns into a clarification: the adapter's job is to be *literal*, not to be *helpful*.

Multi-line: each `#log` line is one `StructuredActivityInput`; lines without the prefix in the same batch go into `unparsed`.

## README

A section titled "Strict-syntax logging (temporary)" stating the grammar, one worked example, and one sentence: *removed in Sprint 3 when the WorkoutExtractor lands; its contract test becomes the extractor's first eval case.*

## Tests

`tests/contract/test_strict_syntax.py`:

| Case | Expectation |
| --- | --- |
| `#log supino 80kg 10 9 8` | one activity, three sets, load on the first only (inheritance is WS-8's job, not the parser's) |
| `#log supino 80kg` | one set, `repetitions is None`, `load == "80kg"` |
| `#log flexao 10` | one set, `repetitions == 10`, `load is None` |
| `#log supino 80kg 10 @RPE8` | `effort == "RPE8"` on the set |
| two `#log` lines in one batch | two activities, order preserved |
| `bom dia` | `parse` returns `None` |
| `#log` alone / `#log 80kg 10` (no exercise) | raises `StrictSyntaxError` naming the problem |
| `#log supino 3x10` | raises — the adapter must not grow toward natural language |
| **no invention** | property-style: for every fixture line, every value in the produced `StructuredWorkoutInput` appears as a substring of the input line. The mechanical form of "never invents a value the syntax did not contain" |
| e2e reference | a non-matching message still produces the Sprint 1 acknowledgement — asserted in WS-11 |

**Done when:** the no-invention test exists and the README says when this file dies.

---

# WS-11 — Cross-cutting verification

**Branch:** `feat/ws-11-cross-cutting-verification`

## Files

| | Path | Purpose |
| --- | --- | --- |
| + | `tests/e2e/test_workout_logging.py` | The two end-to-end paths |
| + | `tests/e2e/test_workout_failure_injection.py` | §38 |
| ~ | `tests/e2e/conftest.py` | `Skeleton` gains the workout components |
| ~ | `scripts/demo.py` | The workout scenario |
| ~ | `README.md` | What `make demo` now proves |

## `Skeleton` additions

```python
@dataclass
class Skeleton:
    ...                                  # everything Sprint 1 already wires
    workout: WorkoutApplicationService
    builder: WorkoutCommandBuilder
    sessions: TrainingSessionManager
    async def logged_sets(self, user_id: UUID) -> list[Row]: ...
    async def training_sessions(self, user_id: UUID) -> list[Row]: ...
```

The worker is constructed with `build_task_handlers(workout=..., builder=...)`, so the e2e path exercises the real registry rather than a test-only one.

## Tests

`tests/e2e/test_workout_logging.py`:

| Case | Assertion |
| --- | --- |
| `#log supino 80kg 10 9 8` through the webhook | 1 batch, 1 workflow execution, 1 `training_sessions`, 1 `session_exercises`, 3 `exercise_sets`, and a dispatched reply naming the exercise — read from the database from outside the components |
| " | load `EXPLICIT` on set 1, `INHERITED` on sets 2 and 3 |
| " | the interaction `trace_id` is on the batch, the execution, the domain event and the outbound row (Q131 still holds with a real handler) |
| `#log supino 80kg` | zero `exercise_sets`, zero `session_exercises`, and a reply containing *repetições* |
| `bom dia` | the Sprint 1 acknowledgement, unchanged |

`tests/e2e/test_workout_failure_injection.py`:

| Case | Assertion |
| --- | --- |
| redelivery after commit | replay the same `InputBatchReady` through RabbitMQ; `count(exercise_sets)` unchanged, `count(domain_events WHERE event_type='workout.logged')` still 1 |
| duplicate outbox publication | publish the same `workout.logged` envelope twice; no second set, no second outbound message (dedupe on `event_id`) |
| crash between domain write and outbox write | patch `record_domain_event` to raise *after* the sets are added; assert **zero** sets, **zero** audit rows and **zero** outbox rows — the window does not exist because it is one transaction, and this test proves it rather than asserting it in prose |
| worker killed after commit, before ACK | (reuses Sprint 1's harness) one training session, three sets, one outbound message |

## `make demo`

`scripts/demo.py` gains a second scenario after the existing fragmented-message one, and a non-zero exit when either fails:

```python
WORKOUT_MESSAGE = "#log supino 80kg 10 9 8"
WORKOUT_QUERY = """
SELECT ts.id, se.exercise_block_index, count(es.id), e.canonical_name,
       bool_or(es.load_provenance = 'explicit'),
       bool_or(es.load_provenance = 'inherited')
FROM training_sessions ts
JOIN session_exercises se ON se.training_session_id = ts.id
JOIN exercises e ON e.id = se.exercise_id
JOIN exercise_sets es ON es.session_exercise_id = se.id
JOIN entity_sources src ON src.entity_id = es.id AND src.entity_type = 'exercise_set'
JOIN messages m ON m.id = src.message_id
WHERE m.external_message_id = %s
GROUP BY ts.id, se.exercise_block_index, e.canonical_name
"""
```

The query returns nothing unless the whole path completed *and* the provenance is right *and* the sets are reachable through `entity_sources` — the same shape as the Sprint 1 query, for the same reason: a demo that passes when the domain silently persisted nothing is worse than no demo.

**Done when:** `make demo` fails if the `workflow-worker` container is stopped, and fails if the sets are not persisted.

---

# WS-12 — Decision records

**Branch:** `doc/ws-12-sprint-2-decision-records`

## Files

| | Path |
| --- | --- |
| + | `doc/adr/adr-012-deterministic-exercise-resolution.md` |
| + | `doc/adr/adr-013-strict-syntax-adapter.md` |
| + | `doc/adr/adr-014-versioned-derived-metrics.md` |
| ~ | `doc/adr/README.md` — three index rows |
| ~ | `tests/unit/test_decision_records.py` — `EXPECTED_ADRS` |

Each follows `doc/adr/template.md`. `tests/unit/test_decision_records.py` already enforces the required sections, a `**Paid.**` cost line, a valid status and date, and that every referenced `DEC-` and `Q` number exists in the spec — so the traceability blocks below must be exact.

| ADR | Traceability | Decision, in one line |
| --- | --- | --- |
| ADR-012 | §16, §15 · DEC-001 · Q41, Q42 · Sprint 2, WS-7 | Resolution stops at deterministic stages 1–4 this sprint; thresholds 0.90/0.70 with a 0.02 ambiguity margin; below threshold resolves to *nothing* and asks |
| ADR-013 | §15, §11.3 · DEC-001 · Q56 · Sprint 2, WS-10 | The strict-syntax adapter is a temporary testing seam with a fixed grammar; it must never learn synonyms, `3x10` notation or free text, and it is deleted in Sprint 3 |
| ADR-014 | §14.1, §19 · DEC-001 · Q52 · Sprint 2, WS-3 | Derived metrics are versioned pure functions; every stored value carries the version that produced it, enforced by paired NOT NULL checks and a frozen fixture |

ADR-012's *How this is enforced* points at `tests/application/test_exercise_resolver.py` (the below-threshold test); ADR-013's at `tests/contract/test_strict_syntax.py` (the `3x10` rejection and the no-invention test); ADR-014's at `tests/contract/test_derived_metrics.py` and the `CHECK ((volume_kg IS NULL) = (volume_metric_version IS NULL))` constraints.

**Done when:** `pytest tests/unit/test_decision_records.py` passes with `EXPECTED_ADRS` extended to seven.

---

## Definition-of-Done coverage

| Criterion | Closed by |
| --- | --- |
| `make demo` logs a workout and fails if sets are absent | WS-11, `scripts/demo.py` |
| `make check` green with containers in CI | every WS |
| `supino 80kg 10 9 8` → 1 session, 1 block, 3 sets, EXPLICIT then INHERITED | WS-11 e2e; WS-9 integration |
| `supino 80kg` → no sets, clarification naming *repetições* | WS-11 e2e; WS-2 validator table |
| `10 flexões` → valid, no load | WS-2 validator table; WS-8 builder |
| 20 kg dumbbell → `PER_IMPLEMENT` unasked | WS-8 builder test (Q49) |
| Returning to an exercise creates a second block, order preserved | WS-6 schema test (A-B-A) |
| User alias beats global alias | WS-7 stage-order test |
| Below-threshold fuzzy resolves to nothing | WS-7 |
| Every set reachable through `entity_sources` | WS-6 query test; WS-9 integration |
| Redelivery creates no second set, on row counts | WS-9; WS-11 failure injection |
| Session opens, is reused, closed by both paths | WS-5 |
| A Redis hint cannot close a session Postgres considers active | WS-5 |
| Unrecognised effort stored raw, normalized to nothing | WS-4 |
| Every derived metric records its version | WS-3 contract test + WS-6 CHECK constraints |
| `StructuredWorkoutInput` frozen by a golden fixture | WS-8 |
| `mypy --strict` clean, `ruff` clean, no `sqlalchemy` under `domain/` | CI + `tests/unit/test_project_layout.py` |
| ADR-012/013/014 with §48 blocks | WS-12 |

## Sequencing risks specific to this plan

- **WS-1 needs two enums WS-2 owns.** Resolve by landing `app/domain/training/activities.py` in WS-1 and letting WS-2 extend it, rather than duplicating the enums in `models.py` and reconciling later.
- **Adding `ServiceName.SESSION_EXPIRATION_WORKER` (WS-5) fails every test until `.env.example`, compose and the conftest role loop agree**, because `PostgresSettings.every_service_has_a_role` validates at construction. Do that edit first in the branch, not last.
- **`tests/conftest.py`'s `TRUNCATE` list is the quiet one.** A table added to the schema but not to that statement leaks rows across tests and produces failures in unrelated files. WS-1, WS-5 and WS-6 each touch it.
- **`rapidfuzz` scores must be pinned** for the golden resolution table to mean anything; `uv sync --frozen` in CI enforces the lockfile, so the risk is only an unreviewed `uv lock` bump.
- **The builder is where four workstreams meet** (resolver, units, effort, validator). Starting WS-8 before all four are merged grows local stubs that then have to be deleted; the branch order exists for that reason.

---

Two notes on what I did and didn't do:

- **Nothing was written to disk.** The `Write` call to `doc/sprints/sprint-02-implementation-plan.md` was denied, so this exists only in this response. Say the word and I'll write it to that path on the current `doc/sprint-02-plan` branch and open the PR with `gh` — per `CLAUDE.md` this is documentation, so the `doc/` branch is already the right place.
- **Two design calls I made that the sprint file left open**, both flagged inline above in case you disagree: `WorkflowWorker` gains a router and a handler *registry* rather than keeping its fixed `task_type` (WS-9), and `TaskInput` grows `message_ids` because `entity_sources` cannot be written without them (§26.2). Both are modifications to Sprint 1 code, not additions.
