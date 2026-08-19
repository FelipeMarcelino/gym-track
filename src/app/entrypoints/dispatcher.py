"""The `whatsapp-dispatcher` process (§25).

The provider client is chosen by environment: without a real Meta integration
(D6) the fake client is the only honest option, and a local stack that pretends
otherwise would fail at the last step of the walking skeleton.
"""

from __future__ import annotations

import logging

from app.application.ports.whatsapp import WhatsAppClient
from app.config import ApplicationSettings, Environment, ServiceName
from app.entrypoints.runtime import consume_forever, run, worker_runtime
from app.infrastructure.whatsapp.fake_client import FakeWhatsAppClient
from app.workers.dispatcher import WhatsAppDispatcher

logger = logging.getLogger(__name__)

OUTBOUND_QUEUE = "outbound.dispatch"


def build_client(settings: ApplicationSettings) -> WhatsAppClient:
    if settings.environment in (Environment.LOCAL, Environment.TEST):
        logger.warning(
            "using the fake WhatsApp client; no message leaves this machine",
            extra={"environment": settings.environment.value},
        )
        return FakeWhatsAppClient()
    raise NotImplementedError(
        "the Meta WhatsApp client arrives with the real integration (D6); "
        "this process refuses to start in a deployed environment without it "
        "rather than silently dropping replies"
    )


async def main() -> None:
    async with worker_runtime(ServiceName.DISPATCHER) as runtime:
        dispatcher = WhatsAppDispatcher(
            session_factory=runtime.session_factory,
            client=build_client(runtime.settings),
            settings=runtime.settings,
        )
        await consume_forever(runtime, OUTBOUND_QUEUE, dispatcher.dispatch)


if __name__ == "__main__":  # pragma: no cover
    run(main)
