"""Webhook signature verification (§35.1, §33.2).

Meta signs the raw request body with the app secret and sends the digest in
`X-Hub-Signature-256`. Verification happens on the **raw bytes**, before
parsing: a body that is re-serialized before checking is a different body, and
a signature check that runs after persistence protects nothing.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

from pydantic import SecretStr

SIGNATURE_HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="


def sign(body: bytes, app_secret: SecretStr) -> str:
    """Produce the header value for a body. Used by tests and by fake clients."""
    digest = hmac.new(app_secret.get_secret_value().encode(), body, sha256).hexdigest()
    return f"{_PREFIX}{digest}"


def is_valid_signature(body: bytes, header_value: str | None, app_secret: SecretStr) -> bool:
    """Constant-time comparison of the provided signature against the expected one."""
    if not header_value or not header_value.startswith(_PREFIX):
        return False
    return hmac.compare_digest(sign(body, app_secret), header_value)
