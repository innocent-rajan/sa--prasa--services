"""Inquiries & feedback: public submission + tracking.

Inquiries get a human-friendly reference (INQ-YYYY-NNNNNN) and a status
timeline. Feedback supports compliments/complaints/suggestions and 1-5
journey ratings, with POPIA anonymity (hashed key flagged anonymous).
"""
import hashlib
import re
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File

from app.config import ATTACHMENTS_DIR, MAX_ATTACHMENT_BYTES
from app.db import get_db
from app.schemas.models import (AttachmentOut, FeedbackCreate, FeedbackOut,
                                InquiryCategoryOut, InquiryCreate, InquiryOut,
                                InquiryStatusOut)

router = APIRouter(tags=["inquiries-feedback"])

ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/webp", "image/heic",
    "application/pdf", "text/plain",
}


def _generate_reference(conn: sqlite3.Connection) -> str:
    """INQ-YYYY-NNNNNN, sequential per calendar year."""
    year = conn.execute("SELECT strftime('%Y', 'now')").fetchone()[0]
    prefix = f"INQ-{year}-"
    row = conn.execute(
        "SELECT reference FROM inquiries WHERE reference LIKE ? "
        "ORDER BY reference DESC LIMIT 1", (prefix + "%",),
    ).fetchone()
    if row:
        seq = int(row["reference"].rsplit("-", 1)[1]) + 1
    else:
        seq = 1
    return f"{prefix}{seq:06d}"


def _validate_scope(conn: sqlite3.Connection, scope_type: str | None,
                    scope_id: int | None) -> None:
    if scope_type is None:
        return
    table, col = ("routes", "route_id") if scope_type == "route" else ("stops", "stop_id")
    hit = conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", (scope_id,)).fetchone()
    if not hit:
        raise HTTPException(status_code=422, detail=f"Unknown {scope_type}_id {scope_id}")


def _hash_key(user_key: str | None) -> str | None:
    """One-way hash of the device key: dedup/abuse checks without storing raw IDs."""
    if not user_key:
        return None
    return hashlib.sha256(user_key.encode()).hexdigest()[:32]


@router.get("/inquiries/categories", response_model=list[InquiryCategoryOut],
            summary="List inquiry categories",
            responses={200: {"content": {"application/json": {"example": [
                {"code": "lost_item", "name": "Lost item",
                 "description": "Report an item lost on a train or in a station"}]}}}})
def list_categories(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT code, name, description FROM inquiry_categories "
        "WHERE is_active = 1 ORDER BY name").fetchall()
    return [InquiryCategoryOut(code=r["code"], name=r["name"],
                               description=r["description"]) for r in rows]


@router.post("/inquiries", response_model=InquiryOut, status_code=201,
             summary="Lodge an inquiry",
             description="""Submit a passenger inquiry. Returns a **reference number**
(INQ-YYYY-NNNNNN) for tracking. Category must be a valid code from
`GET /inquiries/categories`. Scope (route/stop) is optional context.
Attachments can be added afterwards via `POST /inquiries/{reference}/attachments`.""",
             responses={201: {"description": "Created; reference returned"},
                        422: {"description": "Unknown category or scope"}})
def create_inquiry(body: InquiryCreate, conn: sqlite3.Connection = Depends(get_db)):
    cat = conn.execute(
        "SELECT id FROM inquiry_categories WHERE code = ? AND is_active = 1",
        (body.category,)).fetchone()
    if not cat:
        raise HTTPException(status_code=422, detail=f"Unknown category '{body.category}'")
    if body.scope_type and body.scope_id is None:
        raise HTTPException(status_code=422, detail="scope_id required with scope_type")
    _validate_scope(conn, body.scope_type, body.scope_id)

    reference = _generate_reference(conn)
    conn.execute(
        """INSERT INTO inquiries
           (reference, category_id, user_key, contact_name, contact_email,
            contact_phone, scope_type, scope_id, title, description)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (reference, cat["id"], _hash_key(body.user_key),
         body.contact_name, body.contact_email, body.contact_phone,
         body.scope_type, body.scope_id, body.title, body.description),
    )
    conn.execute(
        "INSERT INTO inquiry_updates (inquiry_id, status, note, actor) "
        "VALUES (?, 'received', 'Inquiry submitted', 'system')",
        (conn.execute("SELECT id FROM inquiries WHERE reference = ?",
                      (reference,)).fetchone()["id"],),
    )
    row = conn.execute(
        """SELECT i.reference, c.code AS category, i.status, i.title, i.description,
                  i.scope_type, i.scope_id, i.created_at, i.updated_at
           FROM inquiries i JOIN inquiry_categories c ON c.id = i.category_id
           WHERE i.reference = ?""", (reference,)).fetchone()
    return InquiryOut(
        reference=row["reference"], category=row["category"], status=row["status"],
        title=row["title"], description=row["description"],
        scope_type=row["scope_type"], scope_id=row["scope_id"],
        created_at=row["created_at"], updated_at=row["updated_at"])


@router.get("/inquiries/{reference}", response_model=InquiryStatusOut,
            summary="Track an inquiry by reference number",
            description="Public status tracking with the full timeline. "
                        "No personal contact details are exposed.",
            responses={404: {"description": "Unknown reference"}})
def track_inquiry(reference: str, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute(
        """SELECT i.reference, i.status, u.status AS tl_status, u.note, u.created_at
           FROM inquiries i
           LEFT JOIN inquiry_updates u ON u.inquiry_id = i.id
           WHERE i.reference = ?
           ORDER BY u.created_at, u.id""", (reference,)).fetchall()
    if not row:
        raise HTTPException(status_code=404, detail="Unknown reference")
    timeline = [{"status": r["tl_status"], "note": r["note"],
                 "at": r["created_at"]} for r in row if r["tl_status"]]
    return InquiryStatusOut(reference=row[0]["reference"],
                            status=row[0]["status"], timeline=timeline)


@router.post("/inquiries/{reference}/attachments",
             response_model=list[AttachmentOut], status_code=201,
             summary="Attach files to an inquiry",
             description="""Upload one or more files (images, PDF, text) for an inquiry.
Max 10 MB per file. Stored locally; metadata returned.""",
             responses={201: {"description": "Attachments stored"},
                        404: {"description": "Unknown reference"},
                        415: {"description": "Unsupported file type"},
                        413: {"description": "File too large"}})
async def upload_attachments(reference: str,
                             files: list[UploadFile] = File(...),
                             conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT id FROM inquiries WHERE reference = ?",
                       (reference,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Unknown reference")
    inquiry_id = row["id"]

    saved = []
    base_dir = Path(ATTACHMENTS_DIR) / reference
    base_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        if f.content_type not in ALLOWED_MIME:
            raise HTTPException(status_code=415,
                                detail=f"Unsupported file type {f.content_type}")
        data = await f.read()
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail=f"{f.filename} exceeds 10 MB")
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", f.filename or "attachment")
        stored = base_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        stored.write_bytes(data)
        cur = conn.execute(
            """INSERT INTO inquiry_attachments
               (inquiry_id, file_name, mime_type, size_bytes, stored_path)
               VALUES (?, ?, ?, ?, ?)""",
            (inquiry_id, safe_name, f.content_type, len(data), str(stored_path := stored)))
        saved.append(AttachmentOut(id=cur.lastrowid, file_name=safe_name,
                                   mime_type=f.content_type, size_bytes=len(data)))
    return saved


@router.post("/feedback", response_model=FeedbackOut, status_code=201,
             summary="Submit feedback or a journey rating",
             description="""Submit structured feedback: a type (compliment / complaint /
suggestion) and/or a 1-5 journey rating, with optional comment and journey scope.

POPIA: if `is_anonymous` is true, the user key is stored only as a one-way hash
flagged anonymous (dedup/abuse checks, never re-identifiable). At least one of
`feedback_type` or `rating` is required.""",
             responses={201: {"description": "Feedback recorded"},
                        422: {"description": "Validation error (unknown scope, empty submission)"}})
def create_feedback(body: FeedbackCreate, conn: sqlite3.Connection = Depends(get_db)):
    if body.feedback_type is None and body.rating is None:
        raise HTTPException(status_code=422,
                            detail="feedback_type or rating is required")
    if body.scope_type and body.scope_id is None:
        raise HTTPException(status_code=422, detail="scope_id required with scope_type")
    _validate_scope(conn, body.scope_type, body.scope_id)
    hashed = _hash_key(body.user_key) if body.user_key else None
    cur = conn.execute(
        """INSERT INTO feedback_entries
           (feedback_type, rating, user_key, is_anonymous, scope_type, scope_id, comment)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (body.feedback_type, body.rating, hashed,
         1 if body.is_anonymous else 0, body.scope_type, body.scope_id, body.comment),
    )
    row = conn.execute("SELECT * FROM feedback_entries WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    return FeedbackOut(
        id=row["id"], feedback_type=row["feedback_type"], rating=row["rating"],
        is_anonymous=bool(row["is_anonymous"]), scope_type=row["scope_type"],
        scope_id=row["scope_id"], comment=row["comment"],
        created_at=row["created_at"])