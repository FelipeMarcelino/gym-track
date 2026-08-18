"""WS-7: BSUID protection (§7.1, §33.2, Q143)."""

from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import SecretStr

from app.security.identifiers import (
    NONCE_BYTES,
    decrypt_external_id,
    encrypt_external_id,
    lookup_hash,
)
from app.security.signatures import SIGNATURE_HEADER, is_valid_signature, sign

ENCRYPTION_KEY = SecretStr("encryption-key-for-tests")
HMAC_KEY = SecretStr("lookup-key-for-tests")
BSUID = "5511987654321"


def test_the_lookup_hash_is_deterministic() -> None:
    """It backs a UNIQUE constraint and an index seek, so the same identifier
    must always produce the same bytes."""
    assert lookup_hash(BSUID, HMAC_KEY) == lookup_hash(BSUID, HMAC_KEY)


def test_different_identifiers_hash_differently() -> None:
    assert lookup_hash(BSUID, HMAC_KEY) != lookup_hash("5511900000000", HMAC_KEY)


def test_the_lookup_hash_depends_on_the_key() -> None:
    """A plain digest would be brute-forceable across the space of phone
    numbers; the key is the only thing standing between a leaked column and a
    directory of users."""
    assert lookup_hash(BSUID, HMAC_KEY) != lookup_hash(BSUID, SecretStr("another-key"))


def test_the_lookup_hash_does_not_contain_the_identifier() -> None:
    assert BSUID.encode() not in lookup_hash(BSUID, HMAC_KEY)


def test_encryption_round_trips() -> None:
    ciphertext = encrypt_external_id(BSUID, ENCRYPTION_KEY)

    assert decrypt_external_id(ciphertext, ENCRYPTION_KEY) == BSUID
    assert BSUID.encode() not in ciphertext


def test_encryption_is_randomised() -> None:
    """A deterministic ciphertext would leak equality, which is exactly the
    property the HMAC column is supposed to own."""
    first = encrypt_external_id(BSUID, ENCRYPTION_KEY)
    second = encrypt_external_id(BSUID, ENCRYPTION_KEY)

    assert first != second
    assert decrypt_external_id(first, ENCRYPTION_KEY) == decrypt_external_id(second, ENCRYPTION_KEY)


def test_a_tampered_ciphertext_is_rejected() -> None:
    """AES-GCM is authenticated: a modified value fails rather than decrypting
    into something else."""
    ciphertext = bytearray(encrypt_external_id(BSUID, ENCRYPTION_KEY))
    ciphertext[-1] ^= 0x01

    with pytest.raises(InvalidTag):
        decrypt_external_id(bytes(ciphertext), ENCRYPTION_KEY)


def test_the_wrong_key_cannot_decrypt() -> None:
    ciphertext = encrypt_external_id(BSUID, ENCRYPTION_KEY)

    with pytest.raises(InvalidTag):
        decrypt_external_id(ciphertext, SecretStr("not-the-key"))


def test_a_truncated_value_is_reported_clearly() -> None:
    with pytest.raises(ValueError, match="too short"):
        decrypt_external_id(b"\x00" * (NONCE_BYTES - 1), ENCRYPTION_KEY)


def test_encryption_and_lookup_keys_are_independent() -> None:
    """They are separate secrets for a reason: identity resolution needs the
    lookup key everywhere, while the encryption key is only needed where a
    plaintext identifier is actually required."""
    ciphertext = encrypt_external_id(BSUID, ENCRYPTION_KEY)

    with pytest.raises(InvalidTag):
        decrypt_external_id(ciphertext, HMAC_KEY)


# --------------------------------------------------------------------------
# Webhook signatures
# --------------------------------------------------------------------------

APP_SECRET = SecretStr("app-secret")
BODY = b'{"entry":[]}'


def test_a_signature_produced_here_validates() -> None:
    assert is_valid_signature(BODY, sign(BODY, APP_SECRET), APP_SECRET)


def test_the_header_name_is_the_one_meta_sends() -> None:
    assert SIGNATURE_HEADER == "X-Hub-Signature-256"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=",
        "sha256=deadbeef",
        "deadbeef",
        "sha1=" + sign(BODY, APP_SECRET).removeprefix("sha256="),
    ],
)
def test_malformed_or_wrong_signatures_are_rejected(header: str | None) -> None:
    assert not is_valid_signature(BODY, header, APP_SECRET)


def test_a_changed_body_invalidates_the_signature() -> None:
    signature = sign(BODY, APP_SECRET)

    assert not is_valid_signature(b'{"entry":[{"tampered":true}]}', signature, APP_SECRET)


def test_a_signature_from_another_secret_is_rejected() -> None:
    assert not is_valid_signature(BODY, sign(BODY, SecretStr("other")), APP_SECRET)
