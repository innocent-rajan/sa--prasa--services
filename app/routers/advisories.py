"""Advisories: public reads + admin CRUD. Active = published, not expired, is_active."""
import datetime
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_admin
from app.db import get_db, to_db_timestamp
from app.schemas.models import AdvisoryCreate, AdvisoryOut, AdvisoryUpdate
from app.services.audit import write_audit
from app.services.fanout import fan_out, dispatch_pending

router = APIRouter(tags=["advisories"])

ACTIVE_FILTER = "is_active = 1 AND published_at <= datetime('now') AND (expires_at IS NULL OR expires_at > datetime('now'))"


def _row_to_out(row: sqlite3.Row) -> AdvisoryOut:
    return AdvisoryOut(
        id=row["id"], category=row["category"], title=row["title"], body=row["body"],
        scope_type=row["scope_type"], scope_id=row["scope_id"],
        published_at=row["published_at"], expires_at=row["expires_at"],
        is_active=bool(row["is_active"]), created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_scope(conn: sqlite3.Connection, scope_type: str, scope_id: int | None) -> None:
    if scope_type == "network":
        return
    table, col = ("routes", "route_id") if scope_type == "route" else ("stops", "stop_id")
    hit = conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", (scope_id,)).fetchone()
    if not hit:
        raise HTTPException(status_code=422, detail=f"Unknown {scope_type}_id {scope_id}")


@router.get("/advisories", response_model=list[AdvisoryOut],
            summary="List active advisories",
            response_description="Active advisories, newest first. Empty list = none.",
            responses={200: {"content": {"application/json": {"example": [{
                "id": 1, "category": "maintenance", "title": "Sunday maintenance",
                "body": "Planned maintenance on Sunday morning.",
                "scope_type": "network", "scope_id": None,
                "published_at": "2026-09-15 09:00:00", "expires_at": None,
                "is_active": True,
                "created_at": "2026-09-15 09:00:00",
                "updated_at": "2026-09-15 09:00:00"}]}}}})
def list_advisories(category: str | None = Query(default=None,
                    description="Filter by category, e.g. maintenance | safety | general"),
                    scope_type: str | None = Query(default=None,
                    description="Filter: route | stop | network"),
                    scope_id: int | None = Query(default=None),
                    active_only: bool = Query(default=True,
                    description="false = include inactive/expired (admin/debug use)"),
                    conn: sqlite3.Connection = Depends(get_db)):
    where = [ACTIVE_FILTER] if active_only else ["1=1"]
    params: list = []
    if category:
        where.append("category = ?")
        params.append(category)
    if scope_type:
        where.append("scope_type = ?")
        params.append(scope_type)
        if scope_type != "network" and scope_id is not None:
            where.append("scope_id = ?")
            params.append(scope_id)
    rows = conn.execute(
        f"SELECT * FROM advisories WHERE {' AND '.join(where)} ORDER BY published_at DESC",
        params,
    ).fetchall()
    return [_row_to_out(r) for r in rows]


@router.get("/advisories/{advisory_id}", response_model=AdvisoryOut,
            summary="Get one advisory by id",
            responses={404: {"description": "Not found"}})
def get_advisory(advisory_id: int, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM advisories WHERE id = ?", (advisory_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Advisory not found")
    return _row_to_out(row)


@router.post("/advisories", response_model=AdvisoryOut, status_code=201,
             summary="Publish an advisory (admin)",
             description="""Publish a passenger advisory. Requires `X-Admin-Key`.

If `is_active` is true, automatically queues notifications for users opted into
the scope's topic. `published_at` may be in the future (scheduled publish) -
it only becomes publicly visible then.""",
             responses={201: {"description": "Created"},
                        401: {"description": "Missing/wrong admin key"},
                        422: {"description": "Validation error"}})
def create_advisory(body: AdvisoryCreate, actor: str = Depends(require_admin),
                    conn: sqlite3.Connection = Depends(get_db)):
    if body.scope_type != "network" and body.scope_id is None:
        raise HTTPException(status_code=422, detail="scope_id required for route/stop scope")
    _validate_scope(conn, body.scope_type, body.scope_id)
    cur = conn.execute(
        """INSERT INTO advisories
           (category, title, body, scope_type, scope_id, published_at, expires_at, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (body.category, body.title, body.body, body.scope_type, body.scope_id,
         to_db_timestamp(body.published_at.isoformat()),
         to_db_timestamp(body.expires_at.isoformat()) if body.expires_at else None,
         1 if body.is_active else 0),
    )
    new_id = cur.lastrowid
    write_audit(conn, actor, "create", "advisory", new_id, body.model_dump(mode="json"))
    if body.is_active:
        try:
            fan_out(conn, body.scope_type, body.scope_id, "advisory", new_id,
                    body.title, body.body)
        except Exception:  # noqa: BLE001 - fan-out must not block publish
            pass
    conn.commit()
    row = conn.execute("SELECT * FROM advisories WHERE id = ?", (new_id,)).fetchone()
    dispatch_pending()
    return _row_to_out(row)


@router.patch("/advisories/{advisory_id}", response_model=AdvisoryOut,
              summary="Update an advisory (admin)",
              responses={401: {"description": "Missing/wrong admin key"},
                         404: {"description": "Not found"},
                         422: {"description": "Validation error or no fields to update"}})
def update_advisory(advisory_id: int, body: AdvisoryUpdate,
                    actor: str = Depends(require_admin),
                    conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM advisories WHERE id = ?", (advisory_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Advisory not found")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update")
    if "scope_type" in changes or "scope_id" in changes:
        new_type = changes.get("scope_type", row["scope_type"])
        new_id = changes.get("scope_id", row["scope_id"])
        if new_type != "network" and new_id is None:
            raise HTTPException(status_code=422, detail="scope_id required for route/stop scope")
        _validate_scope(conn, new_type, new_id)
    fields, params = [], []
    for key, value in changes.items():
        fields.append(f"{key} = ?")
        if isinstance(value, datetime.datetime):
            params.append(to_db_timestamp(value.isoformat()))
        else:
            params.append(1 if value is True else 0 if value is False else value)
    fields.append("updated_at = datetime('now')")
    params.append(advisory_id)
    conn.execute(f"UPDATE advisories SET {', '.join(fields)} WHERE id = ?", params)
    write_audit(conn, actor, "update", "advisory", advisory_id, changes)
    row = conn.execute("SELECT * FROM advisories WHERE id = ?", (advisory_id,)).fetchone()
    return _row_to_out(row)


@router.delete("/advisories/{advisory_id}", status_code=204,
               summary="Delete an advisory (admin)",
               responses={204: {"description": "Deleted"},
                          401: {"description": "Missing/wrong admin key"},
                          404: {"description": "Not found"}})
def delete_advisory(advisory_id: int, actor: str = Depends(require_admin),
                    conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT id FROM advisories WHERE id = ?", (advisory_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Advisory not found")
    conn.execute("DELETE FROM advisories WHERE id = ?", (advisory_id,))
    write_audit(conn, actor, "delete", "advisory", advisory_id)