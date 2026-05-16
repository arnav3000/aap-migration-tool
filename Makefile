.PHONY: help install install-dev clean format lint typecheck test test-unit test-integration \
       test-performance test-cov test-watch check pre-commit docs docs-serve run-example \
       init-env setup version venv install-editable all \
       build build-api build-ui prepare-pgdata up up-dev down shell shell-engine logs \
       c-test c-lint c-format c-typecheck c-check \
       web-install web-dev web-build serve

.DEFAULT_GOAL := help

# ===========================================================================
#  Local development (runs on host, no containers needed)
# ===========================================================================

PYTHON := python3
PIP := uv pip
PYTEST := $(PYTHON) -m pytest
BLACK := $(PYTHON) -m black
ISORT := $(PYTHON) -m isort
RUFF := $(PYTHON) -m ruff
MYPY := $(PYTHON) -m mypy

SRC_DIR := src
TESTS_DIR := tests
DOCS_DIR := docs

help: ## Show this help message
	@echo "AAP Migration Tool - Development Commands"
	@echo ""
	@echo "  Local development (no containers):"
	@echo "    make setup                         # Complete dev setup"
	@echo "    make test                          # Run all tests"
	@echo "    make check                         # Format + lint + typecheck + test"
	@echo "    make docs-serve                    # Serve docs locally"
	@echo ""
	@echo "  Container deployment:"
	@echo "    make build && make up              # Build and start all containers"
	@echo "    make up                            # Start db + engine + ui"
	@echo "    make up-dev                        # Start db + bridge (CLI dev)"
	@echo "    make c-check                       # Run checks inside container"
	@echo ""
	@echo "  Web UI:"
	@echo "    make web-install                   # Install frontend dependencies"
	@echo "    make web-dev                       # Start Vite dev server"
	@echo "    make web-build                     # Build frontend for production"
	@echo "    make serve                         # Start FastAPI API server"
	@echo ""
	@echo "  All targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "    \033[36m%-28s\033[0m %s\n", $$1, $$2}'

venv: ## Create virtual environment with uv
	uv venv --seed --python 3.12

install: ## Install production dependencies
	$(PIP) install -r requirements.txt

install-dev: ## Install development dependencies
	$(PIP) install -r requirements-dev.txt
	$(PYTHON) -m pre_commit install

install-editable: ## Install package in editable mode
	$(PIP) install -e .

clean: ## Clean up generated files
	find . -type f -name '*.py[co]' -delete
	find . -type d -name '__pycache__' -delete
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null || true
	rm -rf build dist .eggs htmlcov .coverage coverage.xml .pytest_cache .mypy_cache .ruff_cache
	rm -f migration_state.db*

format: ## Format code with black and isort
	$(BLACK) $(SRC_DIR) $(TESTS_DIR)
	$(ISORT) $(SRC_DIR) $(TESTS_DIR)

lint: ## Run linters (ruff)
	$(RUFF) check $(SRC_DIR) $(TESTS_DIR)

typecheck: ## Run type checking with mypy
	$(MYPY) $(SRC_DIR)

test: ## Run all tests
	$(PYTEST) $(TESTS_DIR)

test-unit: ## Run only unit tests
	$(PYTEST) $(TESTS_DIR)/unit -v

test-integration: ## Run only integration tests
	$(PYTEST) $(TESTS_DIR)/integration -v -m integration

test-performance: ## Run only performance tests
	$(PYTEST) $(TESTS_DIR)/performance -v -m performance

test-cov: ## Run tests with coverage report
	$(PYTEST) $(TESTS_DIR) --cov=$(SRC_DIR) --cov-report=html --cov-report=term

test-watch: ## Run tests in watch mode
	$(PYTEST) $(TESTS_DIR) -f

check: format lint typecheck test ## Run all checks (format, lint, typecheck, test)

pre-commit: ## Run pre-commit hooks on all files
	$(PYTHON) -m pre_commit run --all-files

docs: ## Build documentation
	uv pip install -e ".[docs]"
	uv run mkdocs build

docs-serve: ## Serve documentation locally
	uv pip install -e ".[docs]"
	uv run mkdocs serve -a localhost:8001

run-example: ## Run example migration (requires config)
	$(PYTHON) -m aap_migration.cli migrate full --config config/config.yaml --dry-run

init-env: ## Initialize .env file from .env.example
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo ".env file created from .env.example"; \
		echo "Please edit .env with your actual configuration"; \
	else \
		echo ".env file already exists"; \
	fi

setup: install-dev install-editable init-env ## Complete development setup

version: ## Show current version
	@$(PYTHON) -c "from importlib.metadata import version; print(f'AAP Migration Tool v{version(\"aap-migration-tool\")}')" 2>/dev/null || echo "Package not installed"

all: check docs ## Run all checks and build docs

# ===========================================================================
#  Container deployment (requires podman)
# ===========================================================================

COMPOSE          := podman compose -f container/docker-compose.yml
BRIDGE_SVC       := bridge
BRIDGE_IMAGE     := localhost/aap-bridge:latest
BRIDGE_API_IMAGE := localhost/aap-bridge-api:latest
UI_IMAGE         := localhost/aap-bridge-ui:latest
PROJECT_NAME     := $(notdir $(CURDIR))
PGDATA_VOLUME    := $(PROJECT_NAME)_postgres-data

define run-bridge
	$(COMPOSE) exec $(BRIDGE_SVC)
endef

build: ## Build all container images (cli, api, ui)
	podman build -t $(BRIDGE_IMAGE) -f container/Containerfile .
	podman build -t $(BRIDGE_API_IMAGE) -f container/Containerfile.api .
	podman build -t $(UI_IMAGE) -f container/Containerfile.ui .

build-api: ## Build API container image only
	podman build -t $(BRIDGE_API_IMAGE) -f container/Containerfile.api .

build-ui: ## Build UI container image only
	podman build -t $(UI_IMAGE) -f container/Containerfile.ui .

prepare-pgdata: ## Prepare PostgreSQL volume ownership for rootless Podman
	@podman volume inspect $(PGDATA_VOLUME) >/dev/null 2>&1 || podman volume create $(PGDATA_VOLUME) >/dev/null
	@podman unshare chown -R 26:26 "$$(podman volume inspect $(PGDATA_VOLUME) --format '{{.Mountpoint}}')"

up: prepare-pgdata ## Start db + engine + ui (web interface)
	$(COMPOSE) up -d db engine ui

up-dev: prepare-pgdata ## Start db + bridge (CLI dev container)
	$(COMPOSE) up -d db bridge

down: ## Stop all containers
	$(COMPOSE) down

shell: ## Shell into bridge container
	$(COMPOSE) exec $(BRIDGE_SVC) /bin/bash

shell-engine: ## Shell into engine container
	$(COMPOSE) exec engine /bin/bash

logs: ## Tail all container logs
	$(COMPOSE) logs -f

c-test: ## Run unit tests inside bridge container
	$(run-bridge) python3.12 -m pytest tests/unit/ -v

c-lint: ## Run ruff linter inside bridge container
	$(run-bridge) python3.12 -m ruff check src/ tests/unit/

c-format: ## Run black + isort inside bridge container
	$(run-bridge) python3.12 -m black src/ tests/unit/
	$(run-bridge) python3.12 -m isort src/ tests/unit/

c-typecheck: ## Run mypy inside bridge container
	$(run-bridge) python3.12 -m mypy src/

c-check: c-lint c-typecheck c-test ## Run all checks inside bridge container

# ===========================================================================
#  Web UI
# ===========================================================================

web-install: ## Install frontend dependencies
	cd web && npm ci

web-dev: ## Start Vite dev server (proxies API to localhost:8000)
	cd web && npm run dev

web-build: ## Build frontend for production
	cd web && npm run build

serve: ## Start FastAPI API server (requires pip install '.[api]')
	aap-bridge serve --host 0.0.0.0 --port 8000
