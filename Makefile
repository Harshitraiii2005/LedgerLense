PROFILES ?= core
COMPOSE := docker compose $(foreach p,$(PROFILES),--profile $(p))

.PHONY: up down logs ps test test-unit test-spark lint fmt bench spark semantic-index

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

test-spark: ## Spark transformation tests, inside the Spark image (host has no Java)
	docker build -q --target test -t ledgerlens/spark-aggregates:test jobs/spark_aggregates
	docker run --rm ledgerlens/spark-aggregates:test

spark: .env ## run the nightly aggregates job once against the compose Mongo
	docker compose --profile core --profile spark run --rm --build spark-aggregates

semantic-index: ## embed semantic/catalog.yaml into semantic/index/ (FAISS)
	uv run ledgerlens-build-index

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy libs/common/src services/*/src semantic/src

fmt:
	uv run ruff check --fix .
	uv run ruff format .

bench: ## needs `make up` (uses the compose Mongo)
	uv run python benchmarks/mongo_explain.py
