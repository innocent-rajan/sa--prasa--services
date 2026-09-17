"""Shared test fixtures: temp DB, seeded reference data, test client."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make app importable and point config at a temp DB before importing app modules
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_tmpdir = tempfile.mkdtemp()
os.environ["APIS_DB_PATH"] = os.path.join(_tmpdir, "test_apis.db")
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["MERGED_GTFS_DIR"] = str(
    PROJECT_ROOT.parent / "data" / "merged_gtfs")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

ADMIN = {"X-Admin-Key": "test-admin-key"}

_client = TestClient(app)


def client() -> TestClient:
    """Reuse one TestClient instance (its context manager is not re-enterable)."""
    return _client


def fresh_db():
    """Delete the temp DB so a test can start clean (loader idempotency tests)."""
    for suffix in ("", "-wal", "-shm"):
        path = os.environ["APIS_DB_PATH"] + suffix
        if os.path.exists(path):
            os.remove(path)