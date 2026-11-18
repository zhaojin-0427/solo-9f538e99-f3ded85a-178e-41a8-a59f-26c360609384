"""Pytest fixtures: each test gets an isolated SQLite file and TestClients."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.database import Database
from app.main import app

# Ensure any lifespan-created default database lands in a writable temp
# directory rather than the container-only /data path.
_TMP_ROOT = tempfile.mkdtemp(prefix="flood-tests-")
os.environ.setdefault("FLOOD_DB_PATH", os.path.join(_TMP_ROOT, "default.db"))


@pytest.fixture()
def db_path(tmp_path) -> str:
    return str(tmp_path / "test_flood.db")


@pytest.fixture()
def db(db_path) -> Database:
    database = Database(db_path)
    app.state.db = database
    return database


@pytest.fixture()
def client(db) -> TestClient:
    # The app lifespan would create its own Database; swap in the test
    # database before entering the client context.
    with TestClient(app) as c:
        app.state.db = db
        yield c


@pytest.fixture()
def relation_id(request) -> int:
    # Unique, positive relation per test node so data never leaks between tests.
    digest = sum(request.node.nodeid.encode("utf-8"))
    return 900_000 + digest % 90_000
