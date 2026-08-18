"""The public ingress (§35, Q112, Q153).

FastAPI is ingress only. The webhook verifies, resolves identity, deduplicates,
persists and enqueues -- then returns. It never waits for speech-to-text,
debounce, a graph or a model, because a provider that does not get a fast 200
retries, and retries are how one message becomes five.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, status

from app.api.dependencies import ApiContext
from app.application.services.ingestion import (
    persist_inbound_message,
    resolve_conversation,
    resolve_user,
)
from app.domain.events import DomainEventEnvelope
from app.infrastructure.postgres.engine import unit_of_work
from app.infrastructure.postgres.outbox import record_domain_event
from app.infrastructure.rabbitmq.topology import Exchanges
from app.infrastructure.whatsapp.payloads import InboundMessage, parse_webhook
from app.observability import CorrelationMiddleware, current_context
from app.security.signatures import SIGNATURE_HEADER, is_valid_signature

logger = logging.getLogger(__name__)

MESSAGE_RECEIVED = "message.received"


def create_app(context: ApiContext) -> FastAPI:
    app = FastAPI(title="gym-track ingress", version="0.1.0")
    app.state.context = context
    app.include_router(_router(context))
    # Added last so it wraps everything: every request, including a rejected
    # one, is logged under its own trace.
    app.add_middleware(CorrelationMiddleware)
    return app


def _router(context: ApiContext) -> APIRouter:
    router = APIRouter()

    @router.get("/health", status_code=status.HTTP_200_OK)
    async def health() -> dict[str, str]:
        """Liveness: is this process running? Deliberately touches nothing."""
        return {"status": "ok"}

    @router.get("/ready")
    async def ready(response: Response) -> dict[str, Any]:
        """Readiness: can this process actually serve? Checks its dependencies.

        Kept separate from /health so a database outage does not get the
        container killed and restarted into the same outage.
        """
        checks: dict[str, bool] = {}
        try:
            async with context.engine.connect() as connection:
                await connection.execute(sa.text("SELECT 1"))
            checks["postgres"] = True
        except Exception:
            logger.warning("readiness check failed for postgres", exc_info=True)
            checks["postgres"] = False

        ready_now = all(checks.values())
        response.status_code = (
            status.HTTP_200_OK if ready_now else status.HTTP_503_SERVICE_UNAVAILABLE
        )
        return {"status": "ready" if ready_now else "not ready", "checks": checks}

    @router.post("/webhooks/whatsapp", status_code=status.HTTP_202_ACCEPTED)
    async def whatsapp_webhook(request: Request) -> dict[str, Any]:
        body = await request.body()

        # Verified on the raw bytes, before parsing and before anything is
        # written: a signature check that runs after persistence protects
        # nothing.
        if not is_valid_signature(
            body,
            request.headers.get(SIGNATURE_HEADER),
            context.settings.security.whatsapp_app_secret,
        ):
            logger.warning("rejected a webhook with an invalid signature")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature"
            )

        messages = parse_webhook(await request.json())
        if not messages:
            # Meta sends status callbacks to the same endpoint; acknowledging
            # them is correct, and treating them as an error would make the
            # provider retry forever.
            return {"accepted": 0, "duplicates": 0}

        accepted = 0
        duplicates = 0
        for inbound in messages:
            ingested = await _ingest(context, inbound)
            if ingested:
                accepted += 1
            else:
                duplicates += 1

        return {"accepted": accepted, "duplicates": duplicates}

    return router


def _rejected(request: Request) -> dict[str, Any]:
    from fastapi import HTTPException

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")


async def _ingest(context: ApiContext, inbound: InboundMessage) -> bool:
    """One transaction per message: rows and the outbox event commit together."""
    correlation = current_context()
    trace_id = correlation.trace_id if correlation else None

    async with unit_of_work(context.session_factory) as session:
        user = await resolve_user(
            session,
            provider=inbound.provider,
            external_id=inbound.external_id,
            settings=context.settings,
        )
        conversation = await resolve_conversation(
            session, user_id=user.id, settings=context.settings
        )
        ingested = await persist_inbound_message(
            session,
            inbound,
            user_id=user.id,
            conversation_id=conversation.id,
            trace_id=trace_id,
        )

        if not ingested.is_new:
            logger.info(
                "duplicate webhook delivery ignored",
                extra={"external_message_id": inbound.external_message_id},
            )
            return False

        await record_domain_event(
            session,
            _message_received(ingested.message_id, ingested.user_id, ingested.conversation_id),
            exchange=Exchanges.WHATSAPP_INBOUND,
            routing_key=MESSAGE_RECEIVED,
        )

    return True


def _message_received(
    message_id: UUID, user_id: UUID, conversation_id: UUID
) -> DomainEventEnvelope:
    return DomainEventEnvelope(
        event_type=MESSAGE_RECEIVED,
        aggregate_type="message",
        aggregate_id=message_id,
        user_id=user_id,
        payload={
            "message_id": str(message_id),
            "user_id": str(user_id),
            "conversation_id": str(conversation_id),
        },
    )
