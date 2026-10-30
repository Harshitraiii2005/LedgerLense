# syntax=docker/dockerfile:1
# One image recipe for every Python service in the uv workspace:
#   docker build --build-arg SERVICE=ingest -t ledgerlens/ingest .
FROM ghcr.io/astral-sh/uv:0.12.2 AS uv

# --- build stage: resolve the locked deps for one workspace package into /app/.venv ---
FROM python:3.12.16-slim AS build
COPY --from=uv /uv /bin/uv
ARG SERVICE
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
# --no-editable installs our own packages as regular wheels, so the runtime stage needs no source tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${SERVICE}"

# --- runtime stage: just the interpreter and the venv, no build tools ---
FROM python:3.12.16-slim
ARG SERVICE
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 SERVICE=${SERVICE}
COPY --from=build /app/.venv /app/.venv
# Semantic catalog for the agent; the FAISS index is built into a writable dir at first start.
COPY semantic/catalog.yaml /app/semantic/catalog.yaml
ENV SEMANTIC_CATALOG_PATH=/app/semantic/catalog.yaml SEMANTIC_INDEX_DIR=/tmp/semantic-index HF_HOME=/tmp/hf
# Non-root. GID 0 matters for OpenShift, which runs pods as a random UID that belongs to group 0.
RUN useradd --uid 10001 --gid 0 --no-create-home --shell /usr/sbin/nologin app
USER 10001
# Each workspace package installs a console script with the same name as the service.
CMD ["sh", "-c", "exec \"$SERVICE\""]
