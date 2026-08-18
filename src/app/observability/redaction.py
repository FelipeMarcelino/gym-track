"""TelemetryRedactor: the last thing that runs before telemetry leaves (Q146).

Vendor-side scrubbing is a second net, not the first one. §30.3 requires the
application itself to redact, because a value that reaches Datadog or Langfuse
has already left the boundary §33.1 draws.

Two mechanisms, on purpose:

* **Field names** -- anything whose key looks like a credential or an identity
  is replaced wholesale. Cheap and total; the deny-list is the load-bearing
  part and is meant to be extended.
* **Value patterns** -- phone numbers and long digit runs leak through free
  text no matter how the surrounding field is named.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final

REDACTED: Final = "[redacted]"

#: Substrings that make a field name mean "never in telemetry". Matched
#: case-insensitively against the whole key, so `user_phone_number` is caught
#: by `phone`.
DENY_LISTED_KEY_PARTS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "bsuid",
        "ciphertext",
        "credential",
        "external_id",
        "hmac",
        "msisdn",
        "passwd",
        "password",
        "phone",
        "secret",
        "token",
        "wa_id",
        "whatsapp_id",
    }
)

#: E.164 and the loosely formatted shapes people actually paste, including the
#: Brazilian `+55 11 91234-5678` spacing.
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")

#: A run of digits long enough to be an identifier rather than a set count.
_LONG_DIGITS = re.compile(r"\b\d{11,}\b")


class ContentPolicy(StrEnum):
    """How much raw content a component may emit (§30.3).

    Environment- and component-specific: a local debugging session may want
    FULL, while anything shipping telemetry off the box should not.
    """

    FULL = "full"
    REDACTED = "redacted"
    METADATA_ONLY = "metadata_only"
    DISABLED = "disabled"


class TelemetryRedactor:
    def __init__(
        self,
        *,
        policy: ContentPolicy = ContentPolicy.REDACTED,
        extra_denied_keys: frozenset[str] = frozenset(),
    ) -> None:
        self._policy = policy
        self._denied = DENY_LISTED_KEY_PARTS | extra_denied_keys

    @property
    def policy(self) -> ContentPolicy:
        return self._policy

    def is_denied_key(self, key: str) -> bool:
        lowered = key.lower()
        return any(part in lowered for part in self._denied)

    def redact_text(self, text: str) -> str:
        if self._policy is ContentPolicy.FULL:
            return text
        if self._policy in (ContentPolicy.METADATA_ONLY, ContentPolicy.DISABLED):
            return REDACTED
        redacted = _PHONE.sub(REDACTED, text)
        return _LONG_DIGITS.sub(REDACTED, redacted)

    def redact(self, value: Any) -> Any:
        """Redact a payload of arbitrary shape, preserving its structure.

        Structure survives because the shape of a payload is usually what makes
        a log line readable, while the values are what makes it dangerous.
        """
        if isinstance(value, Mapping):
            return {
                key: REDACTED if self.is_denied_key(str(key)) else self.redact(item)
                for key, item in value.items()
            }
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, bytes):
            return REDACTED
        if isinstance(value, Sequence):
            return [self.redact(item) for item in value]
        return value
