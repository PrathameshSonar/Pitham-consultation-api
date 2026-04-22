"""Shared pytest fixtures.

Uses a dedicated temp-file SQLite DB so every connection sees the same data
(in-memory SQLite creates a separate empty DB per connection, which breaks
apps that use a connection pool).

Tables are recreated before every test for isolation.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Force test config BEFORE importing the app
_TEST_DB = Path(tempfile.gettempdir()) / "pitham_test.db"
if _TEST_DB.exists():
    try:
        _TEST_DB.unlink()
    except OSError:
        pass

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-do-not-use-in-prod"
os.environ["ENV"] = "test"
# Disable startup noise that isn't relevant to tests
os.environ.setdefault("SENTRY_DSN", "")

# Make `import main` work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from database import Base, engine  # noqa: E402
from main import app  # noqa: E402
import models  # noqa: F401, E402


@pytest.fixture(autouse=True)
def _fresh_db():
    """Drop and recreate every table before each test for isolation."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def client():
    return TestClient(app)


def pytest_sessionfinish(session, exitstatus):
    """Clean up the test database file after the whole session."""
    try:
        if _TEST_DB.exists():
            _TEST_DB.unlink()
    except OSError:
        pass
