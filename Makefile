.DEFAULT_GOAL := help
.PHONY: help sync fmt lint typecheck test check

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
