# Thin wrappers around docker compose. Every target also works as the raw command shown.
.PHONY: up down reset doctor test test-unit test-integration lint format

up:               ## start Postgres and apply migrations
	docker compose up -d --build --wait postgres
	docker compose up --build migrate

down:
	docker compose down

reset:            ## destroy all local data (volume) and start again
	docker compose down -v

doctor:
	docker compose run --rm dev olist doctor

test:
	docker compose run --rm dev pytest

test-unit:
	docker compose run --rm dev pytest tests/unit

test-integration:
	docker compose run --rm dev pytest tests/integration

lint:
	docker compose run --rm --no-deps dev sh -c "ruff check . && ruff format --check ."

format:
	docker compose run --rm --no-deps dev ruff format .
