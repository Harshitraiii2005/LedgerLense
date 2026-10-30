PROFILES ?= core
COMPOSE := docker compose $(foreach p,$(PROFILES),--profile $(p))

.PHONY: up down logs ps test test-unit lint fmt bench deploy-local undeploy-local smoke helm-lint

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

deploy-local: .env ## kind cluster + images + Secret from .env + umbrella chart + smoke test
	scripts/deploy_local.sh

undeploy-local:
	kind delete cluster --name ledgerlens

smoke: .env ## re-run the post-deploy smoke test against the current kube context
	set -a && . ./.env && set +a && scripts/smoke_test.sh

helm-lint: ## helm lint + kubeconform for every chart and overlay (no cluster needed)
	scripts/helm_check.sh
