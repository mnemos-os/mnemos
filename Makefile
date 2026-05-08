.PHONY: install dev test test-uv lint docker-build docker-up docker-down clean help install-agent install-wizard install-check install-upgrade bootstrap import-docling import-json import-chatgpt import-obsidian import-stats test-archive test-all lint-fix setup-db docker-logs

PYTHON := python3
VENV := venv
PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest
RUFF := $(VENV)/bin/ruff

help:          ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:       ## Install production dependencies
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

dev:           ## Install development dependencies
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:          ## Run unit tests (auto-installs dev deps on first run)
	@test -x $(PYTEST) || $(MAKE) dev
	$(PYTEST) tests/ -v --tb=short --ignore=tests/test_live_e2e.py

test-uv:       ## Run tests in a fresh uv-managed .venv (reproducible, no pre-existing venv needed)
	uv venv .venv
	uv pip install --python .venv/bin/python -e '.[dev]'
	.venv/bin/pytest tests/ -v --tb=short --ignore=tests/test_live_e2e.py

test-archive:  ## Run archive salvage tests
	@test -x $(PYTEST) || $(MAKE) dev
	$(PYTEST) archive/tests/ -v --tb=short

test-all:      ## Run all tests including archive
	@test -x $(PYTEST) || $(MAKE) dev
	$(PYTEST) tests/ archive/tests/ -v --tb=short --ignore=tests/test_live_e2e.py

lint:          ## Run ruff linter
	$(RUFF) check . --exclude venv,archive

lint-fix:      ## Run ruff with auto-fix
	$(RUFF) check . --fix --exclude venv,archive

setup-db:      ## Run database migrations (requires PostgreSQL running)
	$(PYTHON) -m mnemos.installer --upgrade

docker-build:  ## Build Docker image
	docker build -t mnemos:dev .

docker-up:     ## Start MNEMOS + PostgreSQL via docker-compose
	docker compose up -d

docker-down:   ## Stop docker-compose services
	docker compose down

docker-logs:   ## Follow MNEMOS container logs
	docker compose logs -f mnemos

clean:         ## Remove build artifacts and caches
	find . -type d -name __pycache__ -not -path "./venv/*" -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -not -path "./venv/*" -delete 2>/dev/null || true
	rm -rf .pytest_cache dist build *.egg-info

# ── Installer targets ─────────────────────────────────────────────────────────

install-agent:  ## Run agentic LLM-guided installer (default)
	$(PYTHON) -m mnemos.installer --agent

install-wizard: ## Run traditional interactive wizard installer
	$(PYTHON) -m mnemos.installer --wizard

install-check:  ## Check environment prerequisites only (no changes)
	$(PYTHON) -m mnemos.installer --check

install-upgrade: ## Re-run migrations only (upgrade existing install)
	$(PYTHON) -m mnemos.installer --upgrade

bootstrap:      ## Run install.sh bootstrap (installs system packages first)
	bash install.sh

# ── Import utilities ──────────────────────────────────────────────────────────-

import-docling: ## Import documents via IBM Docling (ARGS='--source DIR')
	$(VENV)/bin/python -m mnemos.tools.docling_import $(ARGS)

import-json:    ## Import memories from JSON file (ARGS='--file memories.json')
	$(VENV)/bin/python -m mnemos.tools.memory_import json $(ARGS)

import-chatgpt: ## Import ChatGPT conversation export (ARGS='--file conversations.json')
	$(VENV)/bin/python -m mnemos.tools.memory_import chatgpt $(ARGS)

import-obsidian: ## Import Obsidian vault (ARGS='--vault /path/to/vault')
	$(VENV)/bin/python -m mnemos.tools.memory_import obsidian $(ARGS)

import-stats:   ## Show MNEMOS memory statistics
	$(VENV)/bin/python -m mnemos.tools.memory_import stats --endpoint http://localhost:5002
