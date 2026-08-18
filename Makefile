.DEFAULT_GOAL := help
.PHONY: help sync fmt lint typecheck test check up down migrate

COMPOSE := docker compose -f docker/compose.yaml

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync: ## Install/refresh dependencies into .venv
	uv sync --frozen

fmt: ## Format the codebase
	uv run ruff format src tests
	uv run ruff check --fix src tests

lint: ## Check formatting and lint rules
	uv run ruff format --check src tests
	uv run ruff check src tests

typecheck: ## Run mypy in strict mode
	uv run mypy

test: ## Run the test suite
	uv run pytest

check: lint typecheck test ## Everything CI runs

# up/down/migrate drive files that land later in this sprint: docker/compose.yaml
# in WS-2 (local infra) and the Alembic environment in WS-3 (persistence base).
up: ## Start local infrastructure (postgres, rabbitmq, redis)
	$(COMPOSE) up -d --wait

down: ## Stop local infrastructure and drop its volumes
	$(COMPOSE) down -v

migrate: ## Apply database migrations to the local stack
	uv run alembic upgrade head
