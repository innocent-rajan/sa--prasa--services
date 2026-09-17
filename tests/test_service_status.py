"""Service status tests: public reads, admin CRUD, windows, auth, fan-out."""
from datetime import datetime, timedelta

from tests.conftest import ADMIN, client


def _future(hours=2):
    return (datetime.utcnow() + timedelta(hours=hours)).isoformat() + "Z"


def _past(hours=2):
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat() + "Z"


def test_health():
    with client() as c:
        assert c.get("/health").status_code == 200


def test_create_and_list_active():
    with client() as c:
        r = c.post("/api/v1/service-status", headers=ADMIN, json={
            "scope_type": "route", "scope_id": 1, "status_type": "delay",
            "title": "Route 1 delayed", "info_status": "manual",
            "effective_from": _past(1), "effective_to": _future(1),
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["info_status"] == "manual"
        assert body["scope_type"] == "route"

        listed = c.get("/api/v1/service-status?scope_type=route&scope_id=1").json()
        assert any(s["id"] == body["id"] for s in listed)


def test_expired_status_excluded_by_default():
    with client() as c:
        r = c.post("/api/v1/service-status", headers=ADMIN, json={
            "scope_type": "network", "status_type": "disruption",
            "title": "Old disruption",
            "effective_from": _past(5), "effective_to": _past(1),
        })
        assert r.status_code == 201
        listed = c.get("/api/v1/service-status").json()
        assert not any(s["title"] == "Old disruption" for s in listed)
        # but visible with active_only=false
        all_rows = c.get("/api/v1/service-status?active_only=false").json()
        assert any(s["title"] == "Old disruption" for s in all_rows)


def test_create_requires_admin_key():
    with client() as c:
        r = c.post("/api/v1/service-status", json={
            "scope_type": "network", "status_type": "delay", "title": "x",
            "effective_from": _past(0),
        })
        assert r.status_code == 401
        r = c.post("/api/v1/service-status", headers={"X-Admin-Key": "wrong"}, json={
            "scope_type": "network", "status_type": "delay", "title": "x",
            "effective_from": _past(0),
        })
        assert r.status_code == 401


def test_create_validates_scope():
    with client() as c:
        r = c.post("/api/v1/service-status", headers=ADMIN, json={
            "scope_type": "route", "scope_id": 99999, "status_type": "delay",
            "title": "bad scope", "effective_from": _past(0),
        })
        assert r.status_code == 422
        r = c.post("/api/v1/service-status", headers=ADMIN, json={
            "scope_type": "route", "status_type": "delay",
            "title": "missing scope_id", "effective_from": _past(0),
        })
        assert r.status_code == 422


def test_update_and_delete():
    with client() as c:
        r = c.post("/api/v1/service-status", headers=ADMIN, json={
            "scope_type": "network", "status_type": "delay", "title": "Original",
            "effective_from": _past(0),
        })
        sid = r.json()["id"]
        r = c.patch(f"/api/v1/service-status/{sid}", headers=ADMIN,
                    json={"title": "Updated", "severity": "major"})
        assert r.status_code == 200
        assert r.json()["title"] == "Updated"
        assert r.json()["severity"] == "major"
        r = c.delete(f"/api/v1/service-status/{sid}", headers=ADMIN)
        assert r.status_code == 204
        assert c.get(f"/api/v1/service-status/{sid}").status_code == 404


def test_get_404():
    with client() as c:
        assert c.get("/api/v1/service-status/424242").status_code == 404


def test_publish_fans_out_to_opted_in_users():
    with client() as c:
        # opt in a user to route:1 on push
        r = c.put("/api/v1/notifications/preferences/user-1", json={
            "preferences": [{"topic": "route:1", "channel": "push", "enabled": True}]
        })
        assert r.status_code == 200
        # publish a delay for route 1
        r = c.post("/api/v1/service-status", headers=ADMIN, json={
            "scope_type": "route", "scope_id": 1, "status_type": "delay",
            "title": "Route 1 delayed 10 min", "effective_from": _past(0),
        })
        assert r.status_code == 201
        # outbox should contain a row for the user (recipient = hashed user_key)
        from app.db import connect
        from app.services.profiles import hash_user_key
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT recipient, channel, status FROM notification_outbox "
                "WHERE title = 'Route 1 delayed 10 min'").fetchall()
            assert len(rows) == 1
            assert rows[0]["recipient"] == hash_user_key("user-1")
            assert rows[0]["channel"] == "push"
            assert rows[0]["status"] == "sent"  # stub provider dispatched
        finally:
            conn.close()


def test_opted_out_user_not_notified():
    with client() as c:
        # user-2 opts OUT explicitly
        c.put("/api/v1/notifications/preferences/user-2", json={
            "preferences": [{"topic": "route:2", "channel": "push", "enabled": False}]
        })
        c.post("/api/v1/service-status", headers=ADMIN, json={
            "scope_type": "route", "scope_id": 2, "status_type": "delay",
            "title": "Route 2 delay", "effective_from": _past(0),
        })
        from app.db import connect
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT recipient FROM notification_outbox WHERE title='Route 2 delay'"
            ).fetchall()
            assert rows == []
        finally:
            conn.close()


def test_fanout_idempotent_on_republish():
    with client() as c:
        c.put("/api/v1/notifications/preferences/user-3", json={
            "preferences": [{"topic": "network", "channel": "push", "enabled": True}]
        })
        r = c.post("/api/v1/service-status", headers=ADMIN, json={
            "scope_type": "network", "status_type": "delay",
            "title": "Network delay", "effective_from": _past(0),
        })
        sid = r.json()["id"]
        # update the same entity - dedup key is per entity+user+channel,
        # so re-fanning-out the same entity never duplicates the row
        c.patch(f"/api/v1/service-status/{sid}", headers=ADMIN, json={"title": "Route delay v2"})
        c.patch(f"/api/v1/service-status/{sid}", headers=ADMIN, json={"title": "Network delay"})
        from app.db import connect
        from app.services.profiles import hash_user_key
        conn = connect()
        try:
            # dedup key is per entity+user+channel: exactly one row for user-3
            # no matter how many times the same entity is re-fanned-out
            rows = conn.execute(
                "SELECT COUNT(*) FROM notification_outbox "
                "WHERE title='Network delay' AND recipient=?",
                (hash_user_key("user-3"),)).fetchone()[0]
            assert rows == 1
        finally:
            conn.close()


def test_audit_log_written():
    with client() as c:
        r = c.post("/api/v1/service-status", headers=ADMIN, json={
            "scope_type": "network", "status_type": "delay",
            "title": "audit check", "effective_from": _past(0),
        })
        sid = r.json()["id"]
        logs = c.get("/api/v1/admin/audit-log?entity=service_status", headers=ADMIN).json()
        assert any(a["entity_id"] == str(sid) and a["action"] == "create" for a in logs)