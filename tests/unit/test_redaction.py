"""WS-4: the redactor protects §33, so it is table-driven and cheap to extend."""

from __future__ import annotations

from typing import Any

import pytest

from app.observability import REDACTED, ContentPolicy, TelemetryRedactor


@pytest.fixture
def redactor() -> TelemetryRedactor:
    return TelemetryRedactor()


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "PASSWORD",
        "db_password",
        "api_key",
        "apiKey",
        "authorization",
        "access_token",
        "client_secret",
        "bsuid",
        "user_bsuid",
        "external_id",
        "external_id_ciphertext",
        "external_id_lookup_hmac",
        "phone",
        "phone_number",
        "user_phone",
        "msisdn",
        "wa_id",
        "whatsapp_id",
    ],
)
def test_denied_field_names_are_replaced_whole(redactor: TelemetryRedactor, key: str) -> None:
    assert redactor.redact({key: "sensitive"}) == {key: REDACTED}


@pytest.mark.parametrize(
    "key",
    ["user_id", "conversation_id", "trace_id", "sequence", "exercise", "reps"],
)
def test_operational_fields_survive(redactor: TelemetryRedactor, key: str) -> None:
    """Redaction that eats the useful fields gets switched off by whoever is
    on call, so the deny-list must stay narrow."""
    assert redactor.redact({key: "value"}) == {key: "value"}


@pytest.mark.parametrize(
    "text",
    [
        "+5511912345678",
        "+55 11 91234-5678",
        "55 (11) 91234 5678",
        "me chama no 11912345678",
        "wa 5511987654321 agora",
    ],
)
def test_phone_numbers_are_stripped_from_free_text(redactor: TelemetryRedactor, text: str) -> None:
    redacted = redactor.redact_text(text)

    assert REDACTED in redacted
    assert not any(chunk.isdigit() and len(chunk) >= 11 for chunk in redacted.split())


@pytest.mark.parametrize(
    "text",
    ["fiz 3 series de 12", "supino 80 kg", "corri 5 km em 25 min"],
)
def test_ordinary_numbers_are_left_alone(redactor: TelemetryRedactor, text: str) -> None:
    assert redactor.redact_text(text) == text


def test_nested_structures_keep_their_shape(redactor: TelemetryRedactor) -> None:
    payload: dict[str, Any] = {
        "user_id": "u-1",
        "identity": {"bsuid": "abc", "locale": "pt-BR"},
        "messages": [{"text": "liga pra +55 11 91234-5678", "sequence": 1}],
        "raw": b"\x00binary",
    }

    redacted = redactor.redact(payload)

    assert redacted["user_id"] == "u-1"
    assert redacted["identity"] == {"bsuid": REDACTED, "locale": "pt-BR"}
    assert redacted["messages"][0]["sequence"] == 1
    assert REDACTED in redacted["messages"][0]["text"]
    assert redacted["raw"] == REDACTED


def test_extra_denied_keys_extend_the_list() -> None:
    redactor = TelemetryRedactor(extra_denied_keys=frozenset({"diagnosis"}))

    assert redactor.redact({"diagnosis": "x"}) == {"diagnosis": REDACTED}


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (ContentPolicy.FULL, "liga pra +5511912345678"),
        (ContentPolicy.REDACTED, f"liga pra {REDACTED}"),
        (ContentPolicy.METADATA_ONLY, REDACTED),
        (ContentPolicy.DISABLED, REDACTED),
    ],
)
def test_content_policy_decides_how_much_text_survives(
    policy: ContentPolicy, expected: str
) -> None:
    """§30.3 makes raw-content policy environment-specific rather than global."""
    redactor = TelemetryRedactor(policy=policy)

    assert redactor.redact_text("liga pra +5511912345678") == expected
