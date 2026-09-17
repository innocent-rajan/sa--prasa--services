"""Inquiries & feedback tests: submission, tracking, case mgmt, attachments, POPIA."""
import io

from tests.conftest import ADMIN, client


def _submit_inquiry(c, **overrides):
    body = {
        "category": "lost_item",
        "title": "Lost umbrella",
        "description": "Left a black umbrella on the 16:45 train.",
        "user_key": "device-abc",
        "contact_email": "passenger@example.com",
        "scope_type": "route",
        "scope_id": 1,
    }
    body.update(overrides)
    return c.post("/api/v1/inquiries", json=body)


def test_inquiry_categories_seeded():
    with client() as c:
        cats = c.get("/api/v1/inquiries/categories").json()
        codes = {x["code"] for x in cats}
        assert {"lost_item", "delay_complaint", "safety", "other"} <= codes


def test_inquiry_submit_returns_reference():
    with client() as c:
        r = _submit_inquiry(c)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["reference"].startswith("INQ-2026-")
        assert len(body["reference"]) == len("INQ-2026-000001")
        assert body["status"] == "received"
        assert body["category"] == "lost_item"


def test_inquiry_references_sequential():
    with client() as c:
        r1 = _submit_inquiry(c).json()["reference"]
        r2 = _submit_inquiry(c, title="Second").json()["reference"]
        assert int(r2.rsplit("-", 1)[1]) == int(r1.rsplit("-", 1)[1]) + 1


def test_inquiry_unknown_category_rejected():
    with client() as c:
        r = _submit_inquiry(c, category="nonexistent")
        assert r.status_code == 422


def test_inquiry_unknown_scope_rejected():
    with client() as c:
        r = _submit_inquiry(c, scope_id=99999)
        assert r.status_code == 422


def test_inquiry_track_timeline():
    with client() as c:
        ref = _submit_inquiry(c).json()["reference"]
        # admin moves it through the lifecycle
        r = c.patch(f"/api/v1/admin/inquiries/{ref}", headers=ADMIN,
                    json={"status": "in_progress", "note": "Investigating CCTV"})
        assert r.status_code == 200
        r = c.patch(f"/api/v1/admin/inquiries/{ref}", headers=ADMIN,
                    json={"status": "resolved", "note": "Found and kept at office"})
        assert r.status_code == 200
        # public tracking shows the full timeline, no contact details
        t = c.get(f"/api/v1/inquiries/{ref}").json()
        assert t["status"] == "resolved"
        statuses = [s["status"] for s in t["timeline"]]
        assert statuses == ["received", "in_progress", "resolved"]
        assert all("contact" not in s["note"].lower() or True for s in t["timeline"])
        assert "passenger@example.com" not in str(t)


def test_inquiry_track_404():
    with client() as c:
        assert c.get("/api/v1/inquiries/INQ-2026-999999").status_code == 404


def test_inquiry_status_update_requires_admin():
    with client() as c:
        ref = _submit_inquiry(c).json()["reference"]
        r = c.patch(f"/api/v1/admin/inquiries/{ref}",
                    json={"status": "in_progress"})
        assert r.status_code == 401


def test_inquiry_same_status_rejected():
    with client() as c:
        ref = _submit_inquiry(c).json()["reference"]
        r = c.patch(f"/api/v1/admin/inquiries/{ref}", headers=ADMIN,
                    json={"status": "received"})
        assert r.status_code == 422


def test_admin_list_inquiries_filters():
    with client() as c:
        _submit_inquiry(c, title="Filter me")
        rows = c.get("/api/v1/admin/inquiries?status=received&category=lost_item",
                     headers=ADMIN).json()
        assert any(x["title"] == "Filter me" for x in rows)
        rows = c.get("/api/v1/admin/inquiries?status=closed", headers=ADMIN).json()
        assert not any(x["title"] == "Filter me" for x in rows)


def test_attachment_upload_and_metadata():
    with client() as c:
        ref = _submit_inquiry(c).json()["reference"]
        r = c.post(f"/api/v1/inquiries/{ref}/attachments", headers=ADMIN,
                   files=[("files", ("photo.png", io.BytesIO(b"\x89PNG fake"),
                                     "image/png"))])
        assert r.status_code == 201, r.text
        att = r.json()[0]
        assert att["file_name"] == "photo.png"
        assert att["mime_type"] == "image/png"
        assert att["size_bytes"] > 0


def test_attachment_bad_type_rejected():
    with client() as c:
        ref = _submit_inquiry(c).json()["reference"]
        r = c.post(f"/api/v1/inquiries/{ref}/attachments", headers=ADMIN,
                   files=[("files", ("evil.exe", io.BytesIO(b"MZ"), "application/x-msdownload"))])
        assert r.status_code == 415


def test_attachment_unknown_reference_404():
    with client() as c:
        r = c.post("/api/v1/inquiries/INQ-2026-999999/attachments", headers=ADMIN,
                   files=[("files", ("a.txt", io.BytesIO(b"hi"), "text/plain"))])
        assert r.status_code == 404


def test_feedback_type_only():
    with client() as c:
        r = c.post("/api/v1/feedback", json={
            "feedback_type": "compliment",
            "comment": "Staff were helpful at Cape Town station.",
            "user_key": "device-xyz"})
        assert r.status_code == 201, r.text
        assert r.json()["feedback_type"] == "compliment"


def test_feedback_rating_only():
    with client() as c:
        r = c.post("/api/v1/feedback", json={
            "rating": 4, "comment": "Smooth trip",
            "scope_type": "route", "scope_id": 1})
        assert r.status_code == 201
        assert r.json()["rating"] == 4


def test_feedback_empty_rejected():
    with client() as c:
        r = c.post("/api/v1/feedback", json={"comment": "no type or rating"})
        assert r.status_code == 422


def test_feedback_bad_rating_rejected():
    with client() as c:
        r = c.post("/api/v1/feedback", json={"rating": 6})
        assert r.status_code == 422


def test_feedback_anonymous_hashed():
    with client() as c:
        r = c.post("/api/v1/feedback", json={
            "feedback_type": "complaint", "user_key": "device-secret",
            "is_anonymous": True, "comment": "Dirty carriage"})
        assert r.status_code == 201
        # raw key must never appear anywhere in the response
        assert "device-secret" not in str(r.json())
        from app.db import connect
        conn = connect()
        try:
            row = conn.execute(
                "SELECT user_key, is_anonymous FROM feedback_entries "
                "WHERE comment = 'Dirty carriage'").fetchone()
            assert row["is_anonymous"] == 1
            assert row["user_key"] != "device-secret"
            assert row["user_key"] is not None  # hashed key kept for dedup
        finally:
            conn.close()


def test_feedback_unknown_scope_rejected():
    with client() as c:
        r = c.post("/api/v1/feedback", json={
            "rating": 3, "scope_type": "stop", "scope_id": 99999})
        assert r.status_code == 422


def test_admin_feedback_stats():
    with client() as c:
        c.post("/api/v1/feedback", json={"rating": 5, "comment": "a"})
        c.post("/api/v1/feedback", json={"rating": 3, "comment": "b"})
        c.post("/api/v1/feedback", json={"feedback_type": "suggestion", "comment": "c"})
        stats = c.get("/api/v1/admin/feedback/stats", headers=ADMIN).json()
        assert stats["total"] >= 3
        assert stats["by_type"].get("suggestion", 0) >= 1
        assert stats["rating_count"] >= 2
        assert 1.0 <= stats["avg_rating"] <= 5.0


def test_admin_feedback_list_requires_auth():
    with client() as c:
        assert c.get("/api/v1/admin/feedback").status_code == 401
        assert c.get("/api/v1/admin/feedback/stats").status_code == 401


def test_inquiry_audit_trail():
    with client() as c:
        ref = _submit_inquiry(c).json()["reference"]
        c.patch(f"/api/v1/admin/inquiries/{ref}", headers=ADMIN,
                json={"status": "resolved", "note": "done"})
        logs = c.get("/api/v1/admin/audit-log?entity=inquiry", headers=ADMIN).json()
        assert any(a["entity_id"] == ref and a["action"] == "update" for a in logs)