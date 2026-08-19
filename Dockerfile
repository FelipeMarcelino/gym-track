# One image, several roles (DEC-015). The entrypoint decides which process runs,
# so every role ships the same tested artifact.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first: they change far less often than the source, so an edit to
# a worker does not reinstall the world.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# Nothing runs as root: a compromised worker should not own the filesystem it
# was given.
RUN useradd --create-home --uid 1000 gym-track && chown -R gym-track /app
USER gym-track

CMD ["python", "-m", "app.entrypoints.api"]
