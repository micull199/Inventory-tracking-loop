"""Placeholder so pytest doesn't error on an empty integration test directory.

Real integration tests land here from slice F2 onward (auth, role enforcement,
audit log hooks). Until then this keeps `pytest tests/unit tests/integration`
from exiting with code 5 (no tests collected).
"""


def test_integration_suite_is_wired() -> None:
    assert True
