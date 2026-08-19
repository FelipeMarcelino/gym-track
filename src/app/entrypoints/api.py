"""The `api` process: FastAPI ingress (§5, §35).

Run with `python -m app.entrypoints.api`, or point uvicorn at `app.entrypoints.api:app`.
"""

from __future__ import annotations

from app.api.app import create_app
from app.api.dependencies import ApiContext
from app.config import ServiceName, load_settings
from app.infrastructure.postgres.engine import create_engine_for, create_session_factory
from app.observability import configure_logging

settings = load_settings()
configure_logging(settings.observability.log_level)

_engine = create_engine_for(settings, ServiceName.API)
app = create_app(
    ApiContext(
        settings=settings,
        engine=_engine,
        session_factory=create_session_factory(_engine),
    )
)


def main() -> None:  # pragma: no cover - exercised by running the container
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_config=None,  # keep the structured formatter installed above
    )


if __name__ == "__main__":  # pragma: no cover
    main()
