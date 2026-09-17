"""Profile module tests: OTP, password auth, favourites, guest upgrade/claim."""
from tests.conftest import ADMIN, client


def _otp_login(c, phone="+27821234567", user_key=None):
    r = c.post("/api/v1/profile/otp/request", json={"phone": phone})
    assert r.status_code == 200, r.text
    code = r.json()["dev_otp"]
    body = {"phone": phone, "code": code}
    if user_key:
        body["user_key"] = user_key
    return c.post("/api/v1/profile/otp/verify", json=body)


def test_otp_request_returns_dev_code():
    with client() as c:
        r = c.post("/api/v1/profile/otp/request", json={"phone": "+27821111111"})
        assert r.status_code == 200
        assert len(r.json()["dev_otp"]) == 6


def test_otp_invalid_phone_rejected():
    with client() as c:
        r = c.post("/api/v1/profile/otp/request", json={"phone": "0821111"})
        assert r.status_code == 422


def test_otp_verify_wrong_code():
    with client() as c:
        c.post("/api/v1/profile/otp/request", json={"phone": "+27822222222"})
        r = c.post("/api/v1/profile/otp/verify", json={"phone": "+27822222222", "code": "000000"})
        assert r.status_code == 401


def test_otp_login_creates_profile_and_token():
    with client() as c:
        r = _otp_login(c, phone="+27823333333")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"]
        assert body["profile"]["phone"] == "+27823333333"
        # me works with the token
        me = c.get("/api/v1/profile/me", headers={"Authorization": f"Bearer {body['token']}"})
        assert me.status_code == 200
        assert me.json()["phone"] == "+27823333333"


def test_otp_reuse_rejected():
    with client() as c:
        r = c.post("/api/v1/profile/otp/request", json={"phone": "+27824444444"})
        code = r.json()["dev_otp"]
        assert c.post("/api/v1/profile/otp/verify",
                      json={"phone": "+27824444444", "code": code}).status_code == 200
        # second use of same code fails (single-use)
        assert c.post("/api/v1/profile/otp/verify",
                      json={"phone": "+27824444444", "code": code}).status_code == 401


def test_register_email_password():
    with client() as c:
        r = c.post("/api/v1/profile/register", json={
            "email": "rider@example.com", "password": "s3curePass!",
            "display_name": "Thabo", "user_key": "device-reg"})
        assert r.status_code == 201, r.text
        assert r.json()["profile"]["email"] == "rider@example.com" or \
               r.json()["profile"]["email"] == "rider@example.com"
        assert r.json()["profile"]["display_name"] == "Thabo"


def test_register_duplicate_email_conflict():
    with client() as c:
        body = {"email": "dup@example.com", "password": "longenough1"}
        assert c.post("/api/v1/profile/register", json=body).status_code == 201
        assert c.post("/api/v1/profile/register", json=body).status_code == 409


def test_register_weak_password_rejected():
    with client() as c:
        r = c.post("/api/v1/profile/register", json={"email": "weak@example.com",
                                                     "password": "short"})
        assert r.status_code == 422


def test_login_success_and_failure():
    with client() as c:
        c.post("/api/v1/profile/register", json={"email": "log@example.com",
                                                 "password": "goodpassword1"})
        ok = c.post("/api/v1/profile/login", json={"email": "log@example.com",
                                                   "password": "goodpassword1"})
        assert ok.status_code == 200
        bad = c.post("/api/v1/profile/login", json={"email": "log@example.com",
                                                    "password": "wrongpassword"})
        assert bad.status_code == 401


def test_me_requires_token():
    with client() as c:
        assert c.get("/api/v1/profile/me").status_code == 401
        assert c.get("/api/v1/profile/me",
                     headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_update_profile_fields():
    with client() as c:
        r = _otp_login(c, phone="+27825555555")
        token = r.json()["token"]
        r = c.patch("/api/v1/profile/me", headers={"Authorization": f"Bearer {token}"},
                    json={"display_name": "Aisha", "language": "af",
                          "home_station": 1})
        assert r.status_code == 200
        assert r.json()["display_name"] == "Aisha"
        assert r.json()["home_station"] == 1


def test_update_profile_unknown_station_rejected():
    with client() as c:
        r = _otp_login(c, phone="+27826666666")
        token = r.json()["token"]
        r = c.patch("/api/v1/profile/me", headers={"Authorization": f"Bearer {token}"},
                    json={"home_station": 99999})
        assert r.status_code == 422


def test_favourites_add_remove():
    with client() as c:
        r = _otp_login(c, phone="+27827777777")
        token = {"Authorization": f"Bearer {r.json()['token']}"}
        assert c.post("/api/v1/profile/me/favourites/1", headers=token).status_code == 201
        assert c.post("/api/v1/profile/me/favourites/1", headers=token).status_code == 409
        me = c.get("/api/v1/profile/me", headers=token).json()
        assert 1 in me["favourite_routes"]
        assert c.delete("/api/v1/profile/me/favourites/1", headers=token).status_code == 204
        me = c.get("/api/v1/profile/me", headers=token).json()
        assert 1 not in me["favourite_routes"]


def test_favourite_unknown_route_404():
    with client() as c:
        r = _otp_login(c, phone="+27828888888")
        token = {"Authorization": f"Bearer {r.json()['token']}"}
        assert c.post("/api/v1/profile/me/favourites/99999", headers=token).status_code == 404


def test_guest_upgrade_claims_past_submissions():
    with client() as c:
        # guest submits an inquiry and a preference with device key
        c.post("/api/v1/inquiries", json={
            "category": "lost_item", "title": "Guest inquiry",
            "description": "As a guest", "user_key": "device-guest-1"})
        c.put("/api/v1/notifications/preferences/device-guest-1", json={
            "preferences": [{"topic": "network", "channel": "push", "enabled": True}]})
        # register with the same device key
        r = c.post("/api/v1/profile/register", json={
            "email": "upgrade@example.com", "password": "longpassword1",
            "user_key": "device-guest-1"})
        assert r.status_code == 201, r.text
        assert r.json()["claimed_submissions"] >= 2
        # device linked
        assert "device-guest-1" in r.json()["profile"]["devices"]


def test_upgrade_endpoint_links_and_claims():
    with client() as c:
        # guest activity first
        c.post("/api/v1/feedback", json={"rating": 3, "user_key": "device-guest-2",
                                         "comment": "guest feedback"})
        # register WITHOUT device key
        r = c.post("/api/v1/profile/register", json={
            "email": "later@example.com", "password": "longpassword2"})
        token = {"Authorization": f"Bearer {r.json()['token']}"}
        # upgrade later
        r = c.post("/api/v1/profile/upgrade", headers=token,
                   json={"user_key": "device-guest-2"})
        assert r.status_code == 200, r.text
        assert r.json()["claimed_submissions"] >= 1
        assert r.json()["linked_device"] == "device-guest-2"


def test_upgrade_requires_auth():
    with client() as c:
        r = c.post("/api/v1/profile/upgrade", json={"user_key": "device-x"})
        assert r.status_code == 401


def test_inquiry_status_notification_queued():
    with client() as c:
        # opt into the network topic (inquiry updates fan out on network topic)
        c.put("/api/v1/notifications/preferences/notif-user", json={
            "preferences": [{"topic": "network", "channel": "push", "enabled": True}]})
        ref = c.post("/api/v1/inquiries", json={
            "category": "other", "title": "Notify me",
            "description": "test", "user_key": "notif-user"}).json()["reference"]
        r = c.patch(f"/api/v1/admin/inquiries/{ref}", headers=ADMIN,
                    json={"status": "resolved", "note": "All done"})
        assert r.status_code == 200
        from app.db import connect
        from app.services.profiles import hash_user_key
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT recipient, status FROM notification_outbox "
                "WHERE title = ? AND recipient = ?",
                (f"Inquiry {ref} update", hash_user_key("notif-user"))).fetchall()
            assert len(rows) == 1
            assert rows[0]["status"] == "sent"
        finally:
            conn.close()