.DEFAULT_GOAL := help

IMAGE ?= renzof93/obsidian-vexa-bridge
TAG ?= latest

.PHONY: install
install: ## Install runtime + dev deps into a local venv (uv)
	uv sync --extra dev

.PHONY: lock
lock: ## Update the uv lockfile
	uv lock

.PHONY: lint
lint: ## Run ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

.PHONY: fix
fix: ## Auto-fix lint + format
	uv run ruff check --fix .
	uv run ruff format .

.PHONY: typecheck
typecheck: ## Run mypy
	uv run mypy summarizer

.PHONY: security
security: ## Run bandit security scan
	uv run bandit -r summarizer -ll

.PHONY: test
test: ## Run the unit test suite
	uv run pytest -v

.PHONY: check-all
check-all: lint typecheck security test ## Lint, typecheck, security, and test

.PHONY: pre-commit
pre-commit: ## Run all pre-commit hooks
	uv run pre-commit run --all-files

.PHONY: build
build: ## Cross-build the amd64 image for the NAS and push it
	docker buildx build --platform linux/amd64 -t $(IMAGE):$(TAG) --push .

.PHONY: clean
clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache **/__pycache__

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'