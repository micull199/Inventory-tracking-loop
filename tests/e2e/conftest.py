import socket
import subprocess
import sys
import time
from collections.abc import Iterator

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
    # cast: pytest-asyncio types this loosely; we know it's callable.
    factory = unused_tcp_port_factory  # type: ignore[assignment]
    port: int = factory()  # type: ignore[operator]
    host = "127.0.0.1"

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
