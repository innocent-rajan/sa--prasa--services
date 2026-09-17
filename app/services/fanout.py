"""Fan-out: publishing a status/advisory queues notifications for opted-in users.

Runs synchronously after the publish commits. Idempotent via outbox dedup_key
UNIQUE constraint - re-publishing the same entity never double-sends.
Failure here never blocks the publish (caller catches and logs).
"""
import logging
import sqlite3

from app.db import connect

log = logging.getLogger("prasa.fanout")


def topic_for_scope(scope_type: str, scope_id: int | None) -> str:
    """Map a scope to its notification topic code."""
    if scope_type == "network":
        return "network"
    return f"{scope_type}:{scope_id}"


def fan_out(conn: sqlite3.Connection, scope_type: str, scope_id: int | None,
            entity_type: str, entity_id: int, title: str, body: str) -> int:
    """Queue one outbox row per opted-in (user, channel) for the scope topic.

    Returns the number of rows queued. Uses the caller's transaction.
    """
    topic_code = topic_for_scope(scope_type, scope_id)
    rows = conn.execute(
        """SELECT p.user_key, p.channel
           FROM notification_preferences p
           JOIN notification_topics t ON t.id = p.topic_id
           WHERE t.code = ? AND p.enabled = 1""",
        (topic_code,),
    ).fetchall()

    queued = 0
    for row in rows:
        dedup = f"{entity_type}:{entity_id}:{row['user_key']}:{row['channel']}"
        try:
            conn.execute(
                """INSERT INTO notification_outbox
                   (dedup_key, topic_code, channel, recipient, title, body)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (dedup, topic_code, row["channel"], row["user_key"], title, body),
            )
            queued += 1
        except sqlite3.IntegrityError:
            # Already queued for this entity+user+channel: idempotent re-run
            continue
    log.info("fan_out entity=%s/%s topic=%s queued=%d", entity_type, entity_id, topic_code, queued)
    return queued


def dispatch_pending(limit: int = 100) -> tuple[int, int]:
    """Dispatch pending outbox rows via the configured provider.

    Returns (sent, failed). Runs in its own transaction per row so one
    failure does not roll back the rest.
    """
    from app.services.dispatcher import get_provider

    provider = get_provider()
    conn = connect()
    sent = failed = 0
    try:
        pending = conn.execute(
            "SELECT id, channel, recipient, title, body FROM notification_outbox "
            "WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        for row in pending:
            try:
                provider.send(row["channel"], row["recipient"], row["title"], row["body"])
                conn.execute(
                    "UPDATE notification_outbox SET status='sent', sent_at=datetime('now') WHERE id=?",
                    (row["id"],),
                )
                sent += 1
            except Exception as exc:  # noqa: BLE001 - record and continue
                conn.execute(
                    "UPDATE notification_outbox SET status='failed', error=? WHERE id=?",
                    (str(exc), row["id"]),
                )
                failed += 1
        conn.commit()
    finally:
        conn.close()
    return sent, failed