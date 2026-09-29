# One command interface for the platform. Every target is a thin wrapper around
# `docker compose` / scripts, so each command is also runnable by hand (see README).
# Requires: Docker with Compose v2, GNU make, python3 (standard library only, for `setup`).

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
COMPOSE ?= docker compose
DEV := $(COMPOSE) run --rm dev
PYTHON ?= $(shell command -v python3 2>/dev/null || command -v python 2>/dev/null)
# The repo is bind-mounted into the dev container: on Linux it must run as your uid/gid.
export DEV_UID ?= $(shell id -u)
export DEV_GID ?= $(shell id -g)

.DEFAULT_GOAL := help
.PHONY: help setup up pipeline pipeline-full report down reset test test-ci unit-test \
        integration-test e2e-test real-data-test lint format dbt-test backup restore \
        benchmark logs ps

help: ## list the available commands
	@grep -hE '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  make %-18s %s\n", $$1, $$2}'

# ---------------------------------------------------------------- lifecycle
setup: ## create .env with random secrets (if missing) and build all images
	@test -f .env || $(PYTHON) scripts/make_env.py
	$(COMPOSE) --profile dev build

up: ## start database, migrations, Prefect, metrics, Prometheus, Grafana; wait until healthy
	$(COMPOSE) up -d --wait
	@echo "Prefect http://localhost:4200 | Grafana http://localhost:3000 | Prometheus http://localhost:9090"

pipeline: ## fetch the dataset if needed and run the orchestrated pipeline once
	$(DEV) olist source fetch
	$(DEV) olist run

pipeline-full: ## same as pipeline, but rebuild even if inputs are unchanged
	$(DEV) olist run --full-refresh

report: ## business metrics from the published marts (read-only reporting role)
	$(DEV) python scripts/business_report.py

down: ## stop all services (data is kept)
	$(COMPOSE) --profile dev down

reset: ## stop all services AND delete all data volumes (warehouse, metrics, dashboards)
	$(COMPOSE) --profile dev down -v

logs: ## follow logs of all services
	$(COMPOSE) logs -f --tail 100

ps: ## service status
	$(COMPOSE) ps

# ---------------------------------------------------------------- quality
lint: ## ruff lint + format check
	$(COMPOSE) run --rm --no-deps dev sh -c "ruff check . && ruff format --check ."

format: ## apply ruff formatting
	$(COMPOSE) run --rm --no-deps dev ruff format .

test: ## every test (real-data tests need `make pipeline` or `olist source fetch` first)
	$(DEV) pytest

test-ci: ## what CI runs: all tests except real-data ones, with coverage + JUnit report
	$(DEV) pytest -m "not source_data" --cov=olist_platform --cov=orchestration \
	  --cov-report=term --cov-report=xml --junitxml=reports/junit.xml

unit-test: ## unit tests (no database)
	$(COMPOSE) run --rm --no-deps dev pytest tests/unit

integration-test: ## integration tests against the isolated test database
	$(DEV) pytest tests/integration -m "not source_data"

e2e-test: ## end-to-end flow tests on synthetic data
	$(DEV) pytest tests/e2e -m "not source_data"

real-data-test: ## end-to-end + idempotency tests on the real dataset (slow)
	$(DEV) pytest -m source_data

dbt-test: ## build dbt models into the build schemas and run all dbt tests (does not publish)
	$(DEV) dbt build --target-path /tmp/dbt-make

# ---------------------------------------------------------------- operations
backup: ## pg_dump the warehouse database to backups/ (DB=olist_dw)
	@scripts/db_backup.sh $(or $(DB),olist_dw)

restore: ## restore a backup in one transaction: make restore FILE=backups/<file>.dump [DB=olist_dw]
	@test -n "$(FILE)" || { echo "usage: make restore FILE=backups/<file>.dump" >&2; exit 1; }
	@scripts/db_restore.sh "$(FILE)" $(or $(DB),olist_dw)

verify-backup: ## prove backup+restore round-trip: identical content afterwards (DB=olist_dw)
	@scripts/verify_backup_restore.sh $(or $(DB),olist_dw)

# own host port, so it can run next to the main stack
benchmark: export POSTGRES_HOST_PORT := 55442
benchmark: ## reproducible benchmark in an isolated compose project (fresh database)
	$(COMPOSE) -p olistbench up -d --wait postgres
	$(COMPOSE) -p olistbench up --exit-code-from migrate migrate
	$(COMPOSE) -p olistbench run --rm -e BENCHMARK_GIT_COMMIT=$$(git rev-parse --short HEAD) \
	  dev python scripts/benchmark.py --reset --update-readme
	$(COMPOSE) -p olistbench down -v
