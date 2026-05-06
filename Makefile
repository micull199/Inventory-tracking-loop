# Makefile — single source of truth for "how do I run the project."
#
# The loop runner (loop.sh) calls `make check` between iterations as its
# independent verification signal. If you change target names here, update
# loop.sh and MISSION.md to match.

.PHONY: help install dev test e2e e2e-install lint typecheck format \
        migrate migration seed clean check ci

# Default target: show available commands.
help:
	@echo "UC Inventory — common commands"
	@echo ""
	@echo "  make install        install Python deps via uv"
	@echo "  make dev            run the app with auto-reload"
	@echo "  make test           pytest (unit + integration)"
	@echo "  make e2e            Playwright end-to-end tests"
	@echo "  make e2e-install    install Playwright browser binaries (one-time)"
	@echo "  make lint           ruff lint"
	@echo "  make typecheck      mypy on app/"
	@echo "  make format         ruff format"
	@echo "  make migrate        alembic upgrade head"
	@echo "  make migration m=\"...\"  generate a new migration"
	@echo "  make seed           load dev fixtures"
	@echo "  make check          lint + typecheck + test + e2e (loop runner uses this)"
	@echo "  make ci             same as check, no extra output (CI-friendly)"
	@echo "  make clean          remove caches and build artifacts"

install:
	uv sync

dev:
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:
	uv run pytest -x --tb=short tests/unit tests/integration

e2e-install:
	uv run playwright install chromium

e2e:
	uv run pytest -x --tb=short tests/e2e

lint:
	uv run ruff check app tests

typecheck:
	uv run mypy app

format:
	uv run ruff format app tests
	uv run ruff check --fix app tests

migrate:
	uv run alembic upgrade head

migration:
	@if [ -z "$(m)" ]; then \
		echo "Usage: make migration m=\"description of migration\""; \
		exit 2; \
	fi
	uv run alembic revision --autogenerate -m "$(m)"

seed:
	uv run python -m scripts.seed

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build
	find . -type d -name __pycache__ -exec rm -rf {} +

# `check` is the loop runner's verification target. Order matters:
# fast checks first so a failure surfaces quickly.
check: lint typecheck test e2e
	@echo ""
	@echo "✓ all checks passed"

# `ci` is identical but quieter — for use in GitHub Actions etc. once that exists.
ci: lint typecheck test e2e
