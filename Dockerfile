# Two-stage build: builder installs deps via uv; runtime copies only the venv
# and app code so the final image has no build tooling.

FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (better layer caching when only code changes).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code and migration scripts.
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./

# Install the project itself into the same venv.
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------------------

FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH"
ENV APP_ENV=prod

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
