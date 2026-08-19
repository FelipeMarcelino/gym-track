.DEFAULT_GOAL := help
.PHONY: help sync fmt lint typecheck test check up down logs demo migrate provision seed

COMPOSE := docker compose -f docker/compose.yaml

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

sync: ## Install/refresh dependencies into .venv
	uv sync --frozen

fmt: ## Format the codebase
	uv run ruff format src tests migrations scripts
	uv run ruff check --fix src tests migrations scripts

lint: ## Check formatting and lint rules
	uv run ruff format --check src tests migrations scripts
	uv run ruff check src tests migrations scripts

typecheck: ## Run mypy in strict mode
	uv run mypy

test: ## Run the test suite
	uv run pytest

check: lint typecheck test ## Everything CI runs

# `up` builds the application image as well, so a change to a worker is picked
# up without a separate build step. Migrations run as a one-shot compose service
# before any process starts; `migrate` here is for a stack you are already
# running against from the host.
up: ## Start the whole stack: infrastructure and every application process
	$(COMPOSE) up -d --wait --build

down: ## Stop the stack and drop its volumes
	$(COMPOSE) down -v

logs: ## Follow the logs of every application process
	$(COMPOSE) logs -f api message-aggregator workflow-worker outbox-publisher whatsapp-dispatcher

demo: ## Send a fragmented message to the running stack and report the reply
	uv run python scripts/demo.py

migrate: ## Apply database migrations to the local stack
	uv run alembic upgrade head

provision: ## Reconcile database roles, passwords and grants with the policy
	uv run python -m app.infrastructure.postgres.provisioning

seed: ## Reconcile the exercise catalog with the curated data
	uv run python -m app.infrastructure.postgres.seeding
