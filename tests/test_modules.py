"""Advisories, KB, notifications, admin tests."""
from datetime import datetime, timedelta

from tests.conftest import ADMIN, client


def _future(days=1):
    return (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"


def _past(days=0):
    return (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"


# ---------- Advisories ----------
def test_advisory_lifecycle():
    with client() as c:
        r = c.post("/api/v1/advisories", headers=ADMIN, json={
            "category": "maintenance", "title": "Sunday maintenance",
            "body": "Planned maintenance on Sunday.", "scope_type": "network",
            "published_at": _past(), "expires_at": _future(7),
        })
        assert r.status_code == 201, r.text
        aid = r.json()["id"]
        listed = c.get("/api/v1/advisories").json()
        assert any(a["id"] == aid for a in listed)
        r = c.patch(f"/api/v1/advisories/{aid}", headers=ADMIN, json={"is_active": False})
        assert r.status_code == 200
        assert c.get("/api/v1/advisories").json() == [] or not any(
            a["id"] == aid for a in c.get("/api/v1/advisories").json())
        assert c.delete(f"/api/v1/advisories/{aid}", headers=ADMIN).status_code == 204


def test_advisory_requires_admin():
    with client() as c:
        r = c.post("/api/v1/advisories", json={
            "category": "x", "title": "t", "body": "b", "published_at": _past()})
        assert r.status_code == 401


def test_advisory_validates_scope():
    with client() as c:
        r = c.post("/api/v1/advisories", headers=ADMIN, json={
            "category": "x", "title": "t", "body": "b", "scope_type": "stop",
            "scope_id": 99999, "published_at": _past()})
        assert r.status_code == 422


# ---------- Knowledge Base ----------
def test_kb_create_search_get():
    with client() as c:
        r = c.post("/api/v1/kb/articles", headers=ADMIN, json={
            "slug": "buy-ticket", "category": "tickets",
            "title": "How to buy a ticket",
            "body": "You can buy a long distance ticket at the station or in the app.",
            "is_published": True})
        assert r.status_code == 201, r.text
        # search finds it
        hits = c.get("/api/v1/kb/articles?q=ticket").json()
        assert any(a["slug"] == "buy-ticket" for a in hits)
        # get by slug
        got = c.get("/api/v1/kb/articles/buy-ticket")
        assert got.status_code == 200
        assert got.json()["category"] == "tickets"


def test_kb_unpublished_hidden():
    with client() as c:
        c.post("/api/v1/kb/articles", headers=ADMIN, json={
            "slug": "draft-article", "category": "general",
            "title": "Draft", "body": "Not ready", "is_published": False})
        assert c.get("/api/v1/kb/articles/draft-article").status_code == 404
        assert not any(a["slug"] == "draft-article"
                       for a in c.get("/api/v1/kb/articles").json())


def test_kb_duplicate_slug_conflict():
    with client() as c:
        body = {"slug": "dup-slug", "category": "c", "title": "t", "body": "b"}
        assert c.post("/api/v1/kb/articles", headers=ADMIN, json=body).status_code == 201
        assert c.post("/api/v1/kb/articles", headers=ADMIN, json=body).status_code == 409


def test_kb_requires_admin():
    with client() as c:
        r = c.post("/api/v1/kb/articles", json={
            "slug": "no-auth", "category": "c", "title": "t", "body": "b"})
        assert r.status_code == 401


# ---------- Notifications ----------
def test_topics_seeded_from_gtfs():
    with client() as c:
        topics = c.get("/api/v1/notifications/topics").json()
        codes = {t["code"] for t in topics}
        assert "network" in codes
        assert "route:1" in codes
        assert "stop:1" in codes


def test_channels_endpoint():
    with client() as c:
        r = c.get("/api/v1/notifications/channels")
        assert r.status_code == 200
        assert "push" in r.json()["channels"]


def test_preferences_roundtrip_and_opt_out():
    with client() as c:
        r = c.put("/api/v1/notifications/preferences/user-x", json={
            "preferences": [
                {"topic": "network", "channel": "push", "enabled": True},
                {"topic": "network", "channel": "whatsapp", "enabled": True},
            ]})
        assert r.status_code == 200
        prefs = {p["channel"]: p["enabled"] for p in r.json()}
        assert prefs == {"push": True, "whatsapp": True}
        # opt out of whatsapp
        r = c.put("/api/v1/notifications/preferences/user-x", json={
            "preferences": [{"topic": "network", "channel": "whatsapp", "enabled": False}]})
        prefs = {p["channel"]: p["enabled"] for p in r.json()}
        assert prefs == {"push": True, "whatsapp": False}


def test_preferences_reject_unknown_topic_and_channel():
    with client() as c:
        r = c.put("/api/v1/notifications/preferences/user-y", json={
            "preferences": [{"topic": "route:99999", "channel": "push", "enabled": True}]})
        assert r.status_code == 422
        r = c.put("/api/v1/notifications/preferences/user-y", json={
            "preferences": [{"topic": "network", "channel": "sms", "enabled": True}]})
        assert r.status_code == 422


def test_admin_send_and_dispatch():
    with client() as c:
        c.put("/api/v1/notifications/preferences/user-z", json={
            "preferences": [{"topic": "network", "channel": "push", "enabled": True}]})
        r = c.post("/api/v1/admin/notifications/send", headers=ADMIN, json={
            "topic": "network", "title": "Hello", "body": "Network message"})
        assert r.status_code == 200
        assert r.json()["sent"] >= 1
        # manual send dedup: same admin+topic+user+channel won't duplicate
        r2 = c.post("/api/v1/admin/notifications/send", headers=ADMIN, json={
            "topic": "network", "title": "Hello", "body": "Network message"})
        assert r2.json()["sent"] == 0


def test_send_requires_admin():
    with client() as c:
        r = c.post("/api/v1/admin/notifications/send", json={
            "topic": "network", "title": "t", "body": "b"})
        assert r.status_code == 401


# ---------- Admin / audit ----------
def test_audit_log_requires_admin():
    with client() as c:
        assert c.get("/api/v1/admin/audit-log").status_code == 401


def test_audit_log_lists_writes():
    with client() as c:
        c.post("/api/v1/advisories", headers=ADMIN, json={
            "category": "audit", "title": "audit advisory", "body": "b",
            "published_at": _past()})
        logs = c.get("/api/v1/admin/audit-log?entity=advisory", headers=ADMIN).json()
        assert any(a["action"] == "create" for a in logs)