SHELL := /bin/bash
.SHELLFLAGS := -o pipefail -c
PROFILES ?= core
COMPOSE := docker compose $(foreach p,$(PROFILES),--profile $(p))

PLANNER ?= catalog

.PHONY: eval up down logs ps test test-unit test-spark lint fmt bench bench-ingest bench-gateway bench-risk spark semantic-index deploy-local undeploy-local smoke helm-lint

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

# Phase 8 benchmarks: run against the live `make up` stack; raw stdout saved next to each summary.
BENCH_ENV = set -a && . ./.env && set +a && \
	MONGO_URI="mongodb://$$MONGO_ROOT_USER:$$MONGO_ROOT_PASSWORD@localhost:27017/?authSource=admin"

bench-ingest: .env
	$(BENCH_ENV) uv run python -u benchmarks/ingest_throughput.py --n 50000 \
		| tee benchmarks/results/ingest_throughput.stdout.txt

bench-gateway: .env
	$(BENCH_ENV) uv run python -u benchmarks/gateway_latency.py \
		| tee benchmarks/results/gateway_latency.stdout.txt

# Starts a throwaway Couchbase on fixed ports (its cluster map advertises them) and removes it after.
# Stop the compose couchbase first if it is running: they use the same ports.
bench-risk:
	docker run -d --name ll-bench-couchbase -p 8091-8097:8091-8097 -p 11210:11210 couchbase/server:community-8.0.0
	docker cp services/risk/couchbase-init.sh ll-bench-couchbase:/init.sh
	docker exec -e CB_USER=Administrator -e CB_PASSWORD=bench-only-password ll-bench-couchbase sh /init.sh
	COUCHBASE_URL=couchbase://localhost COUCHBASE_PASSWORD=bench-only-password \
		uv run python -u benchmarks/risk_throughput.py | tee benchmarks/results/risk_throughput.stdout.txt; \
		status=$$?; docker rm -f ll-bench-couchbase; exit $$status

eval: .env ## agent eval (PLANNER=catalog|adk; adk needs GOOGLE_API_KEY or ANTHROPIC_API_KEY + AGENT_MODEL)
	set -a && . ./.env && set +a && \
	MONGO_URI="mongodb://$$MONGO_ROOT_USER:$$MONGO_ROOT_PASSWORD@localhost:27017/?authSource=admin" \
	uv run python -u benchmarks/agent_eval.py --planner $(PLANNER) | tee benchmarks/results/agent_eval_$(PLANNER).stdout.txt

deploy-local: .env ## kind cluster + images + Secret from .env + umbrella chart + smoke test
	scripts/deploy_local.sh

undeploy-local:
	kind delete cluster --name ledgerlens

smoke: .env ## re-run the post-deploy smoke test against the current kube context
	set -a && . ./.env && set +a && scripts/smoke_test.sh

helm-lint: ## helm lint + kubeconform for every chart and overlay (no cluster needed)
	scripts/helm_check.sh
