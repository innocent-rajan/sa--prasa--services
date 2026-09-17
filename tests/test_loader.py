"""Loader idempotency test: run twice, same counts."""
from tests.conftest import fresh_db


def test_loader_idempotent():
    from app.db import connect, init_schema
    from app.loaders.load_reference import load_reference

    fresh_db()
    conn = connect()
    try:
        init_schema(conn)
        first = load_reference(conn)
        second = load_reference(conn)
        assert first == second
        assert first["routes"] == 74
        assert first["stops"] == 344
        # topics = network + routes + stops
        assert first["topics"] == 1 + first["routes"] + first["stops"]
    finally:
        conn.close()