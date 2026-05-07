"""End-to-end conftest: spawn a real uvicorn against an isolated test DB.

A fresh sqlite file is created per session, ``alembic upgrade head`` runs
against it, then uvicorn is started in a subprocess with the test env vars.
We use ``APP_ENV=test`` so the dev-login backdoor route is mounted (Playwright
uses it to sign in without going through Google).

A second fixture ``oauth_stub_app_server`` boots a separate uvicorn with
``OAUTH_STUB_MODE=1`` and real (stub-valued) ``GOOGLE_CLIENT_ID/SECRET``, so
Playwright can exercise the genuine ``/auth/google/login`` + callback flow
against the local stub provider.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest


def _wait_for_port(host: str, port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"server at {host}:{port} did not start within {timeout}s")


@pytest.fixture(scope="session")
def app_server(unused_tcp_port_factory: object) -> Iterator[str]:
    """Boot a real uvicorn process for the test session and yield its base URL."""
    factory = unused_tcp_port_factory  # type: ignore[assignment]
    port: int = factory()  # type: ignore[operator]
    host = "127.0.0.1"

    project_root = Path(__file__).resolve().parents[2]
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "e2e.db"
    db_url = f"sqlite:///{db_path}"

    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "SECRET_KEY": "e2e-secret-key-for-tests-only",
            "DATABASE_URL": db_url,
            "APP_BASE_URL": f"http://{host}:{port}",
            # Explicitly leave Google client creds blank; e2e uses /auth/_dev-login.
            "GOOGLE_CLIENT_ID": "",
            "GOOGLE_CLIENT_SECRET": "",
            # Set so the first sign-in with this email becomes the seed admin.
            # The bootstrap rule is one-shot (won't fire once an admin exists),
            # so this is safe across the session — only the very first sign-in
            # as admin@uc.test gets the auto-promotion.
            "BOOTSTRAP_ADMIN_EMAIL": "admin@uc.test",
        }
    )

    # Apply migrations against the test DB before starting the server.
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if migrate.returncode != 0:
        tmp.cleanup()
        raise RuntimeError(
            f"alembic upgrade head failed (rc={migrate.returncode}):\n"
            f"stdout:\n{migrate.stdout}\nstderr:\n{migrate.stderr}"
        )

    proc = subprocess.Popen(  # noqa: S603 -- args list, no shell
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=project_root,
        env=env,
    )
    try:
        _wait_for_port(host, port)
        yield f"http://{host}:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        tmp.cleanup()


def _boot_uvicorn(
    port: int, host: str, project_root: Path, env: dict[str, str]
) -> subprocess.Popen[bytes]:
    """Start uvicorn in a subprocess and return the process handle."""
    return subprocess.Popen(  # noqa: S603 -- args list, no shell
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=project_root,
        env=env,
    )


@pytest.fixture(scope="session")
def oauth_stub_app_server(unused_tcp_port_factory: object) -> Iterator[str]:
    """Boot a separate uvicorn with OAUTH_STUB_MODE=1 for Google OAuth e2e tests.

    Uses a fresh SQLite file + migrations.  The server has real (stub-valued)
    GOOGLE_CLIENT_ID/SECRET so Authlib registers the 'google' client against the
    local stub endpoints.  Tests using this fixture exercise the genuine
    /auth/google/login and /auth/google/callback routes end-to-end in a browser.
    """
    factory = unused_tcp_port_factory  # type: ignore[assignment]
    port: int = factory()  # type: ignore[operator]
    host = "127.0.0.1"

    project_root = Path(__file__).resolve().parents[2]
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "e2e_oauth_stub.db"
    db_url = f"sqlite:///{db_path}"
    base_url = f"http://{host}:{port}"

    env = os.environ.copy()
    env.update(
        {
            "APP_ENV": "test",
            "SECRET_KEY": "e2e-oauth-stub-secret-key-for-tests-only",
            "DATABASE_URL": db_url,
            "APP_BASE_URL": base_url,
            # Non-empty so Authlib registers the 'google' client.
            "GOOGLE_CLIENT_ID": "stub-client-id",
            "GOOGLE_CLIENT_SECRET": "stub-client-secret",
            # Activates the stub router + stub-URL registration in auth.py.
            "OAUTH_STUB_MODE": "1",
            "BOOTSTRAP_ADMIN_EMAIL": "admin@uc.test",
        }
    )

    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if migrate.returncode != 0:
        tmp.cleanup()
        raise RuntimeError(
            f"alembic upgrade head failed (rc={migrate.returncode}):\n"
            f"stdout:\n{migrate.stdout}\nstderr:\n{migrate.stderr}"
        )

    proc = _boot_uvicorn(port, host, project_root, env)
    try:
        _wait_for_port(host, port)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        tmp.cleanup()
