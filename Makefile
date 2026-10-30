PROFILES ?= core
COMPOSE := docker compose $(foreach p,$(PROFILES),--profile $(p))

.PHONY: up down logs ps test test-unit lint fmt bench

.env:
	cp .env.example .env

up: .env ## start the local stack (PROFILES="core" by default)
	$(COMPOSE) up -d --build --wait

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

test: ## all tests, including testcontainers integration tests (needs Docker)
	uv run pytest --cov --cov-report=term-missing

test-unit:
	uv run pytest -m "not integration"

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy libs/common/src services/*/src

fmt:
	uv run ruff check --fix .
	uv run ruff format .

bench: .env ## needs `make up` (uses the compose Mongo); raw output saved under benchmarks/results/
	set -a && . ./.env && set +a && \
	MONGO_URI="mongodb://$$MONGO_ROOT_USER:$$MONGO_ROOT_PASSWORD@localhost:27017/?authSource=admin" \
	uv run python -u benchmarks/mongo_explain.py --docs 200000 | tee benchmarks/results/mongo_explain.stdout.txt
