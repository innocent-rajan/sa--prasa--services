"""Admin router: notification sending, dispatch retry, audit log, case management."""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_admin
from app.db import get_db
from app.schemas.models import (AuditOut, DispatchResult, FeedbackOut,
                                FeedbackStatsOut, InquiryOut, InquiryStatusUpdate,
                                SendRequest)
from app.services.audit import write_audit
from app.services.fanout import dispatch_pending, fan_out

router = APIRouter(tags=["admin"])


@router.post("/admin/notifications/send", response_model=DispatchResult,
             summary="Send a notification to a topic's opted-in users (admin)",
             description="""Queues one message per opted-in (user, channel) on the topic,
then dispatches via the configured provider. Requires `X-Admin-Key`.

Idempotent per (topic, admin, user, channel): re-sending the same topic from the
same admin session does not duplicate messages.""",
             responses={200: {"description": "Dispatch result (sent/failed counts)"},
                        401: {"description": "Missing/wrong admin key"},
                        422: {"description": "Unknown topic"}})
def send_notification(body: SendRequest, actor: str = Depends(require_admin),
                      conn: sqlite3.Connection = Depends(get_db)):
    """Manually send a notification to all users opted into a topic."""
    topic = conn.execute(
        "SELECT id, code FROM notification_topics WHERE code = ?", (body.topic,)
    ).fetchone()
    if not topic:
        raise HTTPException(status_code=422, detail=f"Unknown topic '{body.topic}'")
    rows = conn.execute(
        """SELECT p.user_key, p.channel FROM notification_preferences p
           WHERE p.topic_id = ? AND p.enabled = 1""",
        (topic["id"],),
    ).fetchall()
    for row in rows:
        dedup = f"manual:{body.topic}:{actor}:{row['user_key']}:{row['channel']}"
        try:
            conn.execute(
                """INSERT INTO notification_outbox
                   (dedup_key, topic_code, channel, recipient, title, body)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (dedup, topic["code"], row["channel"], row["user_key"],
                 body.title, body.body),
            )
        except sqlite3.IntegrityError:
            continue
    write_audit(conn, actor, "send", "notification", None,
                {"topic": body.topic, "title": body.title})
    # Commit before dispatching: the dispatcher opens its own connection and
    # would deadlock against our uncommitted outbox writes otherwise.
    conn.commit()
    sent, failed = dispatch_pending()
    return DispatchResult(sent=sent, failed=failed)


@router.post("/admin/notifications/dispatch", response_model=DispatchResult,
             summary="Retry pending outbox rows (admin)",
             description="Re-dispatches pending notifications, e.g. after a provider "
                         "outage. Failed rows keep their error message in the outbox.",
             responses={200: {"description": "Dispatch result"},
                        401: {"description": "Missing/wrong admin key"}})
def dispatch_outbox(actor: str = Depends(require_admin)):
    """Retry pending outbox rows (e.g. after a provider outage)."""
    sent, failed = dispatch_pending()
    return DispatchResult(sent=sent, failed=failed)


@router.get("/admin/audit-log", response_model=list[AuditOut],
            summary="List audit log entries (admin)",
            description="Every admin write is recorded automatically: actor, action "
                        "(create/update/delete/send), entity, entity id and payload.",
            responses={200: {"description": "Audit entries, newest first"},
                       401: {"description": "Missing/wrong admin key"}})
def get_audit_log(entity: str | None = Query(default=None,
                  description="Filter by entity: service_status | advisory | kb_article | notification"),
                  limit: int = Query(default=100, le=500),
                  actor: str = Depends(require_admin),
                  conn: sqlite3.Connection = Depends(get_db)):
    where, params = "1=1", []
    if entity:
        where, params = "entity = ?", [entity]
    rows = conn.execute(
        f"SELECT * FROM audit_log WHERE {where} ORDER BY id DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [AuditOut(id=r["id"], actor=r["actor"], action=r["action"], entity=r["entity"],
                     entity_id=r["entity_id"], payload=r["payload"], created_at=r["created_at"])
            for r in rows]


# ---------- Inquiry case management (admin) ----------
@router.get("/admin/inquiries", response_model=list[InquiryOut],
            summary="List inquiries (admin)",
            description="Filter by status, category code or journey scope. "
                        "Newest first.",
            responses={401: {"description": "Missing/wrong admin key"}})
def list_inquiries(status: str | None = Query(default=None,
                   description="received | in_progress | resolved | closed | rejected"),
                   category: str | None = Query(default=None),
                   scope_type: str | None = Query(default=None),
                   scope_id: int | None = Query(default=None),
                   limit: int = Query(default=100, le=500),
                   actor: str = Depends(require_admin),
                   conn: sqlite3.Connection = Depends(get_db)):
    where, params = ["1=1"], []
    if status:
        where.append("i.status = ?")
        params.append(status)
    if category:
        where.append("c.code = ?")
        params.append(category)
    if scope_type:
        where.append("i.scope_type = ?")
        params.append(scope_type)
        if scope_type != "network" and scope_id is not None:
            where.append("i.scope_id = ?")
            params.append(scope_id)
    rows = conn.execute(
        f"""SELECT i.reference, c.code AS category, i.status, i.title, i.description,
                   i.scope_type, i.scope_id, i.created_at, i.updated_at
            FROM inquiries i JOIN inquiry_categories c ON c.id = i.category_id
            WHERE {' AND '.join(where)} ORDER BY i.id DESC LIMIT ?""",
        params + [limit]).fetchall()
    return [InquiryOut(reference=r["reference"], category=r["category"],
                       status=r["status"], title=r["title"],
                       description=r["description"], scope_type=r["scope_type"],
                       scope_id=r["scope_id"], created_at=r["created_at"],
                       updated_at=r["updated_at"]) for r in rows]


@router.patch("/admin/inquiries/{reference}", response_model=InquiryOut,
              summary="Update inquiry status (admin)",
              description="""Move an inquiry through its lifecycle
(received → in_progress → resolved/closed/rejected). Every change is recorded
in the inquiry's public timeline and the audit log.""",
              responses={200: {"description": "Updated"},
                         401: {"description": "Missing/wrong admin key"},
                         404: {"description": "Unknown reference"},
                         422: {"description": "Invalid status"}})
def update_inquiry_status(reference: str, body: InquiryStatusUpdate,
                          actor: str = Depends(require_admin),
                          conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT id, status FROM inquiries WHERE reference = ?",
                       (reference,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Unknown reference")
    if body.status == row["status"]:
        raise HTTPException(status_code=422, detail=f"Already {body.status}")
    conn.execute(
        "INSERT INTO inquiry_updates (inquiry_id, status, note, actor) "
        "VALUES (?, ?, ?, ?)", (row["id"], body.status, body.note, actor))
    conn.execute(
        "UPDATE inquiries SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (body.status, row["id"]))
    write_audit(conn, actor, "update", "inquiry", reference,
                {"status": body.status, "note": body.note})
    # Notify the inquirer: queue a notification on the inquiry's own topic.
    # Only reaches users who opted into topic inquiry:{reference} (or the app
    # polls the public tracking endpoint); dedup key prevents re-sends.
    try:
        fan_out(conn, "network", None, "inquiry", row["id"],
                f"Inquiry {reference} update",
                f"Status: {body.status}. {body.note}".strip())
    except Exception:  # noqa: BLE001 - notification must not block the update
        pass
    conn.commit()
    updated = conn.execute(
        """SELECT i.reference, c.code AS category, i.status, i.title, i.description,
                  i.scope_type, i.scope_id, i.created_at, i.updated_at
           FROM inquiries i JOIN inquiry_categories c ON c.id = i.category_id
           WHERE i.reference = ?""", (reference,)).fetchone()
    dispatch_pending()
    return InquiryOut(reference=updated["reference"], category=updated["category"],
                      status=updated["status"], title=updated["title"],
                      description=updated["description"],
                      scope_type=updated["scope_type"], scope_id=updated["scope_id"],
                      created_at=updated["created_at"], updated_at=updated["updated_at"])


# ---------- Feedback management (admin) ----------
@router.get("/admin/feedback", response_model=list[FeedbackOut],
            summary="List feedback entries (admin)",
            description="Filter by type or journey scope. Anonymous entries are "
                        "included but never expose identifiers.",
            responses={401: {"description": "Missing/wrong admin key"}})
def list_feedback(feedback_type: str | None = Query(default=None,
                  description="compliment | complaint | suggestion"),
                  scope_type: str | None = Query(default=None),
                  scope_id: int | None = Query(default=None),
                  limit: int = Query(default=100, le=500),
                  actor: str = Depends(require_admin),
                  conn: sqlite3.Connection = Depends(get_db)):
    where, params = ["1=1"], []
    if feedback_type:
        where.append("feedback_type = ?")
        params.append(feedback_type)
    if scope_type:
        where.append("scope_type = ?")
        params.append(scope_type)
        if scope_type != "network" and scope_id is not None:
            where.append("scope_id = ?")
            params.append(scope_id)
    rows = conn.execute(
        f"""SELECT id, feedback_type, rating, is_anonymous, scope_type, scope_id,
                   comment, created_at FROM feedback_entries
            WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?""",
        params + [limit]).fetchall()
    return [FeedbackOut(id=r["id"], feedback_type=r["feedback_type"],
                        rating=r["rating"], is_anonymous=bool(r["is_anonymous"]),
                        scope_type=r["scope_type"], scope_id=r["scope_id"],
                        comment=r["comment"], created_at=r["created_at"])
            for r in rows]


@router.get("/admin/feedback/stats", response_model=FeedbackStatsOut,
            summary="Feedback aggregate stats (admin)",
            description="Totals by type and average journey rating. "
                        "Feeds the SOW's operational reporting requirement.",
            responses={401: {"description": "Missing/wrong admin key"}})
def feedback_stats(scope_type: str | None = Query(default=None),
                   scope_id: int | None = Query(default=None),
                   actor: str = Depends(require_admin),
                   conn: sqlite3.Connection = Depends(get_db)):
    where, params = ["1=1"], []
    if scope_type:
        where.append("scope_type = ?")
        params.append(scope_type)
        if scope_type != "network" and scope_id is not None:
            where.append("scope_id = ?")
            params.append(scope_id)
    clause = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM feedback_entries WHERE {clause}", params).fetchone()[0]
    by_type = {r["feedback_type"]: r["n"] for r in conn.execute(
        f"""SELECT feedback_type, COUNT(*) AS n FROM feedback_entries
            WHERE {clause} AND feedback_type IS NOT NULL GROUP BY feedback_type""",
        params).fetchall()}
    rating_row = conn.execute(
        f"""SELECT AVG(rating) AS avg_r, COUNT(rating) AS n FROM feedback_entries
            WHERE {clause} AND rating IS NOT NULL""", params).fetchone()
    return FeedbackStatsOut(
        total=total, by_type=by_type,
        avg_rating=round(rating_row["avg_r"], 2) if rating_row["avg_r"] else None,
        rating_count=rating_row["n"])