"""BSUID protection: ciphertext plus a keyed lookup hash (§7.1, §33.2, Q143).

An external identifier is a phone-derived value: storing it in plaintext makes
the whole users table a directory, and encrypting it alone makes exact lookup
impossible. So both are stored.

* The **ciphertext** is AES-256-GCM, which is authenticated -- a tampered value
  fails to decrypt rather than decrypting into something else.
* The **lookup hash** is HMAC-SHA256 under a separate key. It is deterministic,
  so `UNIQUE(provider, external_id_lookup_hmac)` can enforce identity and a
  lookup is an index seek. A plain hash would be brute-forceable across the
  small space of phone numbers; the key is what prevents that.

The two keys are separate on purpose: the lookup key has to be usable by any
process that resolves identity, while the encryption key is only needed where a
plaintext identifier is actually required.
"""

from __future__ import annotations

import hmac
from hashlib import sha256, sha512

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

#: AES-GCM nonce length in bytes, per NIST SP 800-38D.
NONCE_BYTES = 12


def _derive_key(secret: SecretStr) -> bytes:
    """A 32-byte key from a configured secret of any length.

    Operators supply passphrases, not 32 raw bytes. Deriving rather than
    requiring an exact length keeps configuration honest instead of pushing
    people to pad a short value until it fits.
    """
    return sha256(secret.get_secret_value().encode()).digest()


def lookup_hash(external_id: str, hmac_key: SecretStr) -> bytes:
    """The deterministic value stored in `external_id_lookup_hmac`."""
    return hmac.new(_derive_key(hmac_key), external_id.encode(), sha512).digest()[:32]


def encrypt_external_id(
    external_id: str,
    encryption_key: SecretStr,
    *,
    nonce: bytes | None = None,
) -> bytes:
    """Encrypt, returning nonce || ciphertext so the value is self-contained."""
    key = _derive_key(encryption_key)
    if nonce is None:
        import os

        nonce = os.urandom(NONCE_BYTES)
    if len(nonce) != NONCE_BYTES:
        raise ValueError(f"nonce must be {NONCE_BYTES} bytes")
    return nonce + AESGCM(key).encrypt(nonce, external_id.encode(), None)


def decrypt_external_id(ciphertext: bytes, encryption_key: SecretStr) -> str:
    """Recover the plaintext. Only used where an identifier must leave the system."""
    if len(ciphertext) <= NONCE_BYTES:
        raise ValueError("ciphertext is too short to contain a nonce")
    key = _derive_key(encryption_key)
    nonce, body = ciphertext[:NONCE_BYTES], ciphertext[NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, body, None).decode()
