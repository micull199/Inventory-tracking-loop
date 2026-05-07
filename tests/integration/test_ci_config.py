"""Forcing-function tests for .github/workflows/ci.yml.

Pins the key structural properties of the CI workflow so that any future drift
(removing the Postgres step, dropping TEST_DATABASE_URL, etc.) fails the test
suite immediately.  Pattern mirrors test_readme.py: read the file from disk,
assert substrings rather than parsing YAML (no pyyaml dep needed; substrings
are sufficient forcing functions for the properties we care about).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WORKFLOW_PATH = (
    Path(__file__).parent.parent.parent / ".github" / "workflows" / "ci.yml"
)


@pytest.fixture(scope="module")
def workflow_text() -> str:
    return _WORKFLOW_PATH.read_text()


class TestCIWorkflowFileExists:
    def test_workflow_file_exists(self) -> None:
        assert _WORKFLOW_PATH.exists(), (
            f"CI workflow file not found at {_WORKFLOW_PATH}. "
            "Create .github/workflows/ci.yml to enable CI."
        )


class TestCIWorkflowTriggers:
    def test_triggers_on_push(self, workflow_text: str) -> None:
        assert "push:" in workflow_text

    def test_triggers_on_pull_request(self, workflow_text: str) -> None:
        assert "pull_request:" in workflow_text

    def test_targets_main_branch(self, workflow_text: str) -> None:
        # Both push and pull_request should target the main branch.
        assert "main" in workflow_text


class TestCIWorkflowRunner:
    def test_uses_ubuntu_latest(self, workflow_text: str) -> None:
        assert "ubuntu-latest" in workflow_text


class TestCIWorkflowPostgresService:
    def test_has_postgres_service(self, workflow_text: str) -> None:
        assert "postgres:" in workflow_text

    def test_postgres_uses_postgres_16_image(self, workflow_text: str) -> None:
        assert "postgres:16" in workflow_text

    def test_postgres_service_has_health_check(self, workflow_text: str) -> None:
        # pg_isready is the standard Postgres health-check command.
        assert "pg_isready" in workflow_text


class TestCIWorkflowPostgresParity:
    def test_sets_test_database_url(self, workflow_text: str) -> None:
        assert "TEST_DATABASE_URL:" in workflow_text

    def test_uses_postgresql_psycopg_url(self, workflow_text: str) -> None:
        # Must use the psycopg v3 driver (psycopg), not psycopg2.
        assert "postgresql+psycopg://" in workflow_text


class TestCIWorkflowDependencies:
    def test_installs_uv(self, workflow_text: str) -> None:
        assert "setup-uv" in workflow_text

    def test_installs_playwright(self, workflow_text: str) -> None:
        assert "playwright install" in workflow_text


class TestCIWorkflowSteps:
    def test_runs_lint(self, workflow_text: str) -> None:
        assert "make lint" in workflow_text

    def test_runs_typecheck(self, workflow_text: str) -> None:
        assert "make typecheck" in workflow_text

    def test_runs_tests(self, workflow_text: str) -> None:
        assert "make test" in workflow_text

    def test_runs_e2e(self, workflow_text: str) -> None:
        assert "make e2e" in workflow_text
