"""Service status: public reads + admin CRUD.

Active = effective window contains now. 'Normal service' is an empty list.
Every response carries info_status so clients can label live vs manual.
"""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_admin
from app.db import get_db, to_db_timestamp
from app.schemas.models import ServiceStatusCreate, ServiceStatusOut, ServiceStatusUpdate
from app.services.audit import write_audit
from app.services.fanout import fan_out, dispatch_pending

router = APIRouter(tags=["service-status"])

ACTIVE_FILTER = """
    (effective_from <= datetime('now'))
    AND (effective_to IS NULL OR effective_to > datetime('now'))
"""


def _row_to_out(row: sqlite3.Row) -> ServiceStatusOut:
    return ServiceStatusOut(
        id=row["id"], scope_type=row["scope_type"], scope_id=row["scope_id"],
        status_type=row["status_type"], severity=row["severity"],
        title=row["title"], description=row["description"],
        info_status=row["info_status"], effective_from=row["effective_from"],
        effective_to=row["effective_to"], created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _validate_scope(conn: sqlite3.Connection, scope_type: str, scope_id: int | None) -> None:
    if scope_type == "network":
        return
    table, col = ("routes", "route_id") if scope_type == "route" else ("stops", "stop_id")
    hit = conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ?", (scope_id,)).fetchone()
    if not hit:
        raise HTTPException(status_code=422, detail=f"Unknown {scope_type}_id {scope_id}")


@router.get("/service-status", response_model=list[ServiceStatusOut],
            summary="List active service statuses",
            response_description="Active statuses sorted by severity. Empty list = normal service.",
            responses={
                200: {"content": {"application/json": {"example": [{
                    "id": 1, "scope_type": "route", "scope_id": 1,
                    "status_type": "delay", "severity": "minor",
                    "title": "Route 1 delayed 15 min",
                    "description": "Signal fault at Woodstock",
                    "info_status": "manual",
                    "effective_from": "2026-09-15 10:00:00",
                    "effective_to": "2026-09-15 18:00:00",
                    "created_at": "2026-09-15 09:58:00",
                    "updated_at": "2026-09-15 09:58:00"}]}}}})
def list_service_status(scope_type: str | None = Query(default=None,
                        description="Filter: route | stop | network"),
                        scope_id: int | None = Query(default=None,
                        description="Route/stop id (ignored for network)"),
                        active_only: bool = Query(default=True,
                        description="false = include expired/future items (admin/debug use)"),
                        conn: sqlite3.Connection = Depends(get_db)):
    if scope_type and scope_type not in ("route", "stop", "network"):
        raise HTTPException(status_code=422, detail="scope_type must be route|stop|network")
    where = [ACTIVE_FILTER] if active_only else ["1=1"]
    params: list = []
    if scope_type:
        where.append("scope_type = ?")
        params.append(scope_type)
        if scope_type != "network" and scope_id is not None:
            where.append("scope_id = ?")
            params.append(scope_id)
    rows = conn.execute(
        f"SELECT * FROM service_status WHERE {' AND '.join(where)} ORDER BY severity DESC, id DESC",
        params,
    ).fetchall()
    return [_row_to_out(r) for r in rows]


@router.get("/service-status/{status_id}", response_model=ServiceStatusOut,
            summary="Get one service status by id",
            responses={404: {"description": "Not found"}})
def get_service_status(status_id: int, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM service_status WHERE id = ?", (status_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Service status not found")
    return _row_to_out(row)


@router.post("/service-status", response_model=ServiceStatusOut, status_code=201,
             summary="Publish a service status (admin)",
             description="""Publish a delay/disruption/cancellation. Requires `X-Admin-Key`.

Automatically queues notifications for every user opted into the scope's topic
(`route:{id}`, `stop:{id}` or `network`) and dispatches them via the configured
provider. Re-publishing the same entity never duplicates messages.""",
             responses={
                 201: {"description": "Created and fanned out"},
                 401: {"description": "Missing/wrong admin key"},
                 422: {"description": "Validation error (bad scope, unknown route/stop id)"}})
def create_service_status(body: ServiceStatusCreate, actor: str = Depends(require_admin),
                          conn: sqlite3.Connection = Depends(get_db)):
    if body.scope_type != "network" and body.scope_id is None:
        raise HTTPException(status_code=422, detail="scope_id required for route/stop scope")
    _validate_scope(conn, body.scope_type, body.scope_id)
    cur = conn.execute(
        """INSERT INTO service_status
           (scope_type, scope_id, status_type, severity, title, description,
            info_status, effective_from, effective_to)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (body.scope_type, body.scope_id, body.status_type, body.severity,
         body.title, body.description, body.info_status,
         to_db_timestamp(body.effective_from.isoformat()),
         to_db_timestamp(body.effective_to.isoformat()) if body.effective_to else None),
    )
    new_id = cur.lastrowid
    write_audit(conn, actor, "create", "service_status", new_id, body.model_dump(mode="json"))
    # Fan-out after insert (same transaction); commit before dispatching so the
    # dispatcher's separate connection never deadlocks on our uncommitted writes.
    try:
        fan_out(conn, body.scope_type, body.scope_id, "service_status", new_id,
                body.title, body.description or body.title)
    except Exception:  # noqa: BLE001 - fan-out must not block publish
        pass
    conn.commit()
    row = conn.execute("SELECT * FROM service_status WHERE id = ?", (new_id,)).fetchone()
    dispatch_pending()
    return _row_to_out(row)


@router.patch("/service-status/{status_id}", response_model=ServiceStatusOut,
              summary="Update a service status (admin)",
              responses={401: {"description": "Missing/wrong admin key"},
                         404: {"description": "Not found"},
                         422: {"description": "Validation error or no fields to update"}})
def update_service_status(status_id: int, body: ServiceStatusUpdate,
                          actor: str = Depends(require_admin),
                          conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM service_status WHERE id = ?", (status_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Service status not found")
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
        if isinstance(value, __import__("datetime").datetime):
            params.append(to_db_timestamp(value.isoformat()))
        else:
            params.append(value)
    fields.append("updated_at = datetime('now')")
    params.append(status_id)
    conn.execute(f"UPDATE service_status SET {', '.join(fields)} WHERE id = ?", params)
    write_audit(conn, actor, "update", "service_status", status_id, changes)
    row = conn.execute("SELECT * FROM service_status WHERE id = ?", (status_id,)).fetchone()
    return _row_to_out(row)


@router.delete("/service-status/{status_id}", status_code=204,
               summary="Delete a service status (admin)",
               responses={204: {"description": "Deleted"},
                          401: {"description": "Missing/wrong admin key"},
                          404: {"description": "Not found"}})
def delete_service_status(status_id: int, actor: str = Depends(require_admin),
                          conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT id FROM service_status WHERE id = ?", (status_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Service status not found")
    conn.execute("DELETE FROM service_status WHERE id = ?", (status_id,))
    write_audit(conn, actor, "delete", "service_status", status_id)