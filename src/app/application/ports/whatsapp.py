"""The provider port (§25, Q119, D6).

No real Meta call happens this sprint: there is no test number (D6), and the
value of a walking skeleton is in the durability path, not in the HTTP call at
the end of it. The port exists so that path is exercised end to end, and so
Sprint 2 swaps one implementation instead of rewriting the dispatcher.

Failures are split in two, because they need opposite handling:

* :class:`TransientSendError` -- the provider might succeed later, so the
  message keeps its place and the retry tiers redeliver it;
* :class:`PermanentSendError` -- it will never succeed (an invalid recipient,
  a rejected template), so retrying only burns quota and delays the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SendError(RuntimeError):
    """Base class for a failed provider send."""


class TransientSendError(SendError):
    """Retry is worth attempting."""


class PermanentSendError(SendError):
    """Retry will not help."""


@dataclass(frozen=True, slots=True)
class SentMessage:
    provider_message_id: str


class WhatsAppClient(Protocol):
    async def send_text(self, *, recipient: str, text: str) -> SentMessage:
        """Deliver one message, or raise a Send error."""
        ...
