"""Audit logging: every admin write records who did what, when."""
import json
import sqlite3

from app.db import connect


def write_audit(conn: sqlite3.Connection, actor: str, action: str,
                entity: str, entity_id: str | int | None,
                payload: dict | None = None) -> None:
    """Insert an audit row inside the caller's transaction."""
    conn.execute(
        "INSERT INTO audit_log (actor, action, entity, entity_id, payload) VALUES (?, ?, ?, ?, ?)",
        (actor, action, entity,
         str(entity_id) if entity_id is not None else None,
         json.dumps(payload, default=str) if payload else None),
    )


def write_audit_standalone(actor: str, action: str, entity: str,
                           entity_id: str | int | None,
                           payload: dict | None = None) -> None:
    """Standalone audit write when no caller transaction exists."""
    conn = connect()
    try:
        write_audit(conn, actor, action, entity, entity_id, payload)
        conn.commit()
    finally:
        conn.close()