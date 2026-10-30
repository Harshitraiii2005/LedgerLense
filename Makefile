PROFILES ?= core
COMPOSE := docker compose $(foreach p,$(PROFILES),--profile $(p))

PLANNER ?= catalog

.PHONY: eval up down logs ps test test-unit test-spark lint fmt bench spark semantic-index

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

bench: .env ## needs `make up` (uses the compose Mongo); raw output saved under benchmarks/results/
	set -a && . ./.env && set +a && \
	MONGO_URI="mongodb://$$MONGO_ROOT_USER:$$MONGO_ROOT_PASSWORD@localhost:27017/?authSource=admin" \
	uv run python -u benchmarks/mongo_explain.py --docs 200000 | tee benchmarks/results/mongo_explain.stdout.txt

eval: .env ## agent eval (PLANNER=catalog|adk; adk needs GOOGLE_API_KEY or ANTHROPIC_API_KEY + AGENT_MODEL)
	set -a && . ./.env && set +a && \
	MONGO_URI="mongodb://$$MONGO_ROOT_USER:$$MONGO_ROOT_PASSWORD@localhost:27017/?authSource=admin" \
	uv run python -u benchmarks/agent_eval.py --planner $(PLANNER) | tee benchmarks/results/agent_eval_$(PLANNER).stdout.txt
