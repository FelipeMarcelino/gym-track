"""An in-memory WhatsApp client (D6).

It records what it was asked to send, in order, which is what the dispatch
tests assert against. Failures are scripted per sequence so a test can express
"the second message fails" without patching anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from app.application.ports.whatsapp import (
    PermanentSendError,
    SendError,
    SentMessage,
    TransientSendError,
)


@dataclass(frozen=True, slots=True)
class RecordedSend:
    recipient: str
    text: str


@dataclass
class FakeWhatsAppClient:
    #: Texts that must fail, and how. Consumed on first use unless `sticky`.
    failures: dict[str, SendError] = field(default_factory=dict)
    sticky: bool = False
    sent: list[RecordedSend] = field(default_factory=list)

    async def send_text(self, *, recipient: str, text: str) -> SentMessage:
        failure = self.failures.get(text)
        if failure is not None:
            if not self.sticky:
                del self.failures[text]
            raise failure

        self.sent.append(RecordedSend(recipient=recipient, text=text))
        return SentMessage(provider_message_id=f"wamid.fake.{uuid4()}")

    def fail_once(self, text: str, *, permanent: bool = False) -> None:
        self.failures[text] = (
            PermanentSendError("rejected by the provider")
            if permanent
            else TransientSendError("provider unavailable")
        )

    @property
    def texts(self) -> list[str]:
        return [record.text for record in self.sent]
