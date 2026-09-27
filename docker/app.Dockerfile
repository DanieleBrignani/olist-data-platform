# syntax=docker/dockerfile:1.7
# One image for CLI, ingestion, dbt, Prefect worker and metrics exporter.
# `runtime` has production deps only; `dev` adds test/lint tooling.

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:${PATH}"
RUN pip install --no-cache-dir uv==0.12.19 \
 && useradd --create-home --uid 10001 olist
WORKDIR /app

FROM base AS runtime
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY orchestration ./orchestration
COPY migrations ./migrations
COPY data_contracts ./data_contracts
COPY dbt ./dbt
COPY alembic.ini ./
RUN uv sync --frozen --no-dev \
 && mkdir -p /app/logs /app/data && chown olist /app/logs /app/data
# /app is read-only for the app user: dbt and Prefect write their state under /tmp
ENV DBT_PROFILES_DIR=/app/dbt \
    DBT_PROJECT_DIR=/app/dbt \
    DBT_TARGET_PATH=/tmp/dbt/target \
    DBT_LOG_PATH=/tmp/dbt/logs \
    PREFECT_HOME=/tmp/prefect \
    OLIST_DATA_ROOT=/app/data
USER olist
ENTRYPOINT ["olist"]
CMD ["--help"]

FROM base AS dev
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen && chown -R olist /app
USER olist
CMD ["pytest"]
