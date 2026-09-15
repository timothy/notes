.DEFAULT_GOAL := help
.PHONY: help build bootstrap up down logs token check test test-postgres smoke e2e verify
SUB ?= ada
NAME ?= Ada Okafor
export SUB NAME

help:
	@echo 'Startup: Docker Engine + Compose >= 2.24, make, Bash. Tests also need uv, Python 3.12 and curl.'
	@echo 'build          Build the shared application image'
	@echo 'bootstrap      Build and safely create missing development settings'
	@echo 'up / down      Bootstrap and start with readiness / stop and preserve data'
	@echo 'logs / token   Follow API logs / mint a development token'
	@echo 'check          Locked setup, format, lint, typing, model drift, contract'
	@echo 'test           SQLite suite, ignoring inherited PostgreSQL test URLs'
	@echo 'test-postgres  Suite against a new disposable PostgreSQL database'
	@echo 'smoke / e2e    Isolated container checks / curl workflows'
	@echo 'verify         Run all verification targets sequentially'
	@echo 'make token SUB=ada NAME="Ada Okafor"; make down preserves development data.'

build:
	docker compose build

bootstrap: build
	server/scripts/bootstrap.sh

up: bootstrap
	docker compose up --wait --no-build

down:
	docker compose down

logs:
	docker compose logs -f api

token:
	@docker compose run --rm --no-deps -T api python -m notes_api.dev_issuer token --sub "$$SUB" --name "$$NAME"

check:
	cd server && uv sync --locked
	cd server && uv run ruff check . && uv run ruff format --check . && uv run mypy
	cd server && uv run scripts/gen_models.py --check
	uv run --python 3.12 --with-requirements requirements-dev.txt python scripts/validate_contract.py

test:
	cd server && NOTES_API_TEST_DATABASE_URL= uv run pytest

test-postgres:
	cd server && uv run python scripts/test_postgres.py

smoke: build
	NOTES_API_IMAGE=$${NOTES_API_IMAGE:-notes-api:dev} server/scripts/smoke_image.sh

e2e: build
	cd server && uv run python scripts/e2e.py

verify:
	$(MAKE) check
	$(MAKE) test
	$(MAKE) test-postgres
	$(MAKE) smoke
	$(MAKE) e2e
