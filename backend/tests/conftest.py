"""Shared test fixtures."""
import pytest

from qsde.config.settings import settings


@pytest.fixture(autouse=True)
def _auth_off_unless_a_test_turns_it_on(monkeypatch):
    """The developer's .env may set AUTH_ENFORCED=true; tests must not depend on it. Tests that exercise enforcement set it themselves."""
    monkeypatch.setattr(settings, "auth_enforced", False)
