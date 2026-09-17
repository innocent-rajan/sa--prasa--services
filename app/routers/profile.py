"""Profile endpoints: OTP + password auth, profile management, guest upgrade.

Auth: opaque bearer token in Authorization header (stored hashed).
Guests: identified by device user_key (e.g. IMEI hash) provided by the app;
on upgrade the device key is linked and past submissions are claimed.
"""
import re
import sqlite3

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.db import get_db
from app.services import profiles as ps
from app.services.audit import write_audit

router = APIRouter(tags=["profile"])

PHONE_RE = re.compile(r"^\+[0-9]{7,15}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def require_profile(authorization: str | None = Header(default=None),
                    conn: sqlite3.Connection = Depends(get_db)) -> int:
    profile_id = ps.profile_for_token(conn, authorization)
    if not profile_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return profile_id


# ---------- schemas ----------
class OtpRequest(BaseModel):
    phone: str = Field(pattern=r"^\+[0-9]{7,15}$")


class OtpVerify(BaseModel):
    phone: str = Field(pattern=r"^\+[0-9]{7,15}$")
    code: str = Field(min_length=6, max_length=6)
    user_key: str | None = Field(default=None, max_length=100,
                                 description="Device key to link (guest upgrade)")


class RegisterRequest(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(min_length=8, max_length=100)
    display_name: str = Field(default="", max_length=100)
    user_key: str | None = Field(default=None, max_length=100)


class LoginRequest(BaseModel):
    email: str
    password: str


class ProfileOut(BaseModel):
    id: int
    phone: str | None
    email: str | None
    display_name: str
    language: str
    home_station: int | None
    devices: list[str]
    favourite_routes: list[int]


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, min_length=2, max_length=5)
    home_station: int | None = None


class TokenOut(BaseModel):
    token: str
    profile: ProfileOut


class UpgradeResult(BaseModel):
    profile_id: int
    linked_device: str
    claimed_submissions: int


# ---------- helpers ----------
def _profile_out(conn: sqlite3.Connection, profile_id: int) -> ProfileOut:
    row = conn.execute(
        "SELECT id, phone, email, display_name, language, home_station "
        "FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    devices = [r["user_key"] for r in conn.execute(
        "SELECT user_key FROM profile_devices WHERE profile_id = ?",
        (profile_id,)).fetchall()]
    favs = [r["route_id"] for r in conn.execute(
        "SELECT route_id FROM favourite_routes WHERE profile_id = ? ORDER BY route_id",
        (profile_id,)).fetchall()]
    return ProfileOut(id=row["id"], phone=row["phone"], email=row["email"],
                      display_name=row["display_name"], language=row["language"],
                      home_station=row["home_station"], devices=devices,
                      favourite_routes=favs)


def _validate_home_station(conn: sqlite3.Connection, station_id: int | None) -> None:
    if station_id is None:
        return
    if not conn.execute("SELECT 1 FROM stops WHERE stop_id = ?",
                        (station_id,)).fetchone():
        raise HTTPException(status_code=422, detail=f"Unknown stop_id {station_id}")


# ---------- OTP flow ----------
@router.post("/profile/otp/request", summary="Request an OTP for phone login",
             description="""Sends a 6-digit OTP to the phone number (valid 5 minutes,
single use). In this development build the OTP is returned in the response and
logged; production wires an SMS provider adapter. Rate limited by phone.""",
             responses={200: {"content": {"application/json": {"example": {
                 "message": "OTP sent", "dev_otp": "123456"}}}},
                 429: {"description": "Too many OTP requests"}})
def request_otp(body: dict, conn: sqlite3.Connection = Depends(get_db)):
    phone = body.get("phone", "")
    if not PHONE_RE.match(phone):
        raise HTTPException(status_code=422, detail="Invalid phone format (+XXXXXXXX)")
    recent = conn.execute(
        """SELECT COUNT(*) FROM otp_codes
           WHERE phone = ? AND created_at > datetime('now', '-1 minute')""",
        (phone,)).fetchone()[0]
    if recent >= 3:
        raise HTTPException(status_code=429, detail="Too many OTP requests, wait a minute")
    code = ps.create_otp(conn, phone)
    # Dev convenience: return the code. Production: send via SmsProvider, return nothing.
    return {"message": "OTP sent", "dev_otp": code}


@router.post("/profile/otp/verify", summary="Verify OTP and login/register",
             description="""Verifies the OTP. Creates the profile on first login.
If `user_key` is provided, the device is linked (guest upgrade) and past
anonymous submissions with that device key are claimed.""",
             responses={200: {"description": "Authenticated; bearer token returned"},
                        401: {"description": "Invalid or expired OTP"}})
def verify_otp(body: dict, conn: sqlite3.Connection = Depends(get_db)):
    phone = body.get("phone", "")
    code = str(body.get("code", ""))
    if not PHONE_RE.match(phone):
        raise HTTPException(status_code=422, detail="Invalid phone format")
    if not ps.verify_otp(conn, phone, code):
        raise HTTPException(status_code=401, detail="Invalid or expired OTP")
    profile_id = ps.get_or_create_profile_by_phone(conn, phone)
    claimed = 0
    user_key = body.get("user_key")
    if user_key:
        claimed = ps.link_device_and_claim(conn, profile_id, user_key)
    token = ps.issue_token(conn, profile_id)
    return {"token": token, "profile": _profile_out(conn, profile_id),
            "claimed_submissions": claimed}


# ---------- Email + password flow ----------
@router.post("/profile/register", status_code=201,
             summary="Register with email and password",
             description="""Creates a profile with email+password (bcrypt).
If `user_key` is provided, the device is linked and past guest submissions
are claimed.""",
             responses={201: {"description": "Profile created; token returned"},
                        409: {"description": "Email already registered"},
                        422: {"description": "Invalid email or weak password"}})
def register(body: RegisterRequest, conn: sqlite3.Connection = Depends(get_db)):
    if not EMAIL_RE.match(body.email):
        raise HTTPException(status_code=422, detail="Invalid email")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    if conn.execute("SELECT 1 FROM profiles WHERE email = ?",
                    (body.email,)).fetchone():
        raise HTTPException(status_code=409, detail="Email already registered")
    cur = conn.execute(
        "INSERT INTO profiles (email, password_hash, display_name) VALUES (?, ?, ?)",
        (body.email, ps.hash_password(body.password), body.display_name))
    profile_id = cur.lastrowid
    claimed = 0
    if body.user_key:
        claimed = ps.link_device_and_claim(conn, profile_id, body.user_key)
    token = ps.issue_token(conn, profile_id)
    return {"token": token, "profile": _profile_out(conn, profile_id),
            "claimed_submissions": claimed}


@router.post("/profile/login", summary="Login with email and password",
             responses={200: {"description": "Authenticated; token returned"},
                        401: {"description": "Invalid credentials"}})
def login(body: dict, conn: sqlite3.Connection = Depends(get_db)):
    email = body.get("email", "")
    password = str(body.get("password", ""))
    row = conn.execute("SELECT id, password_hash FROM profiles WHERE email = ?",
                       (email,)).fetchone()
    if not row or not row["password_hash"] or not ps.verify_password(
            password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = ps.issue_token(conn, row["id"])
    return {"token": token, "profile": _profile_out(conn, row["id"])}


# ---------- Profile management ----------
@router.get("/profile/me", response_model=ProfileOut,
            summary="Get the authenticated profile",
            responses={401: {"description": "Missing/invalid bearer token"}})
def get_me(authorization: str | None = Header(default=None),
           conn: sqlite3.Connection = Depends(get_db)):
    profile_id = ps.profile_for_token(conn, authorization)
    if not profile_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return _profile_out(conn, profile_id)


@router.patch("/profile/me", response_model=ProfileOut,
              summary="Update profile fields",
              responses={401: {"description": "Missing/invalid bearer token"},
                         422: {"description": "Unknown home station"}})
def update_me(body: ProfileUpdate, authorization: str | None = Header(default=None),
              conn: sqlite3.Connection = Depends(get_db)):
    profile_id = ps.profile_for_token(conn, authorization)
    if not profile_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    changes = body.model_dump(exclude_unset=True)
    if "home_station" in changes:
        _validate_home_station(conn, changes["home_station"])
    fields, params = [], []
    for key, value in changes.items():
        fields.append(f"{key} = ?")
        params.append(value)
    if fields:
        fields.append("updated_at = datetime('now')")
        params.append(profile_id)
        conn.execute(f"UPDATE profiles SET {', '.join(fields)} WHERE id = ?", params)
    return _profile_out(conn, profile_id)


@router.post("/profile/me/favourites/{route_id}", status_code=201,
             summary="Add a favourite route",
             responses={201: {"description": "Added"},
                        401: {"description": "Authentication required"},
                        404: {"description": "Unknown route"},
                        409: {"description": "Already a favourite"}})
def add_favourite(route_id: int, authorization: str | None = Header(default=None),
                  conn: sqlite3.Connection = Depends(get_db)):
    profile_id = ps.profile_for_token(conn, authorization)
    if not profile_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not conn.execute("SELECT 1 FROM routes WHERE route_id = ?",
                        (route_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Unknown route")
    try:
        conn.execute("INSERT INTO favourite_routes (profile_id, route_id) VALUES (?, ?)",
                     (profile_id, route_id))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Already a favourite")
    return {"added": route_id}


@router.delete("/profile/me/favourites/{route_id}", status_code=204,
               summary="Remove a favourite route",
               responses={204: {"description": "Removed"},
                          401: {"description": "Authentication required"}})
def remove_favourite(route_id: int, authorization: str | None = Header(default=None),
                     conn: sqlite3.Connection = Depends(get_db)):
    profile_id = ps.profile_for_token(conn, authorization)
    if not profile_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    conn.execute("DELETE FROM favourite_routes WHERE profile_id = ? AND route_id = ?",
                 (profile_id, route_id))


# ---------- Guest upgrade ----------
@router.post("/profile/upgrade", summary="Link a guest device key to the profile",
             description="""Guest upgrade: the app provides the device `user_key`
(e.g. hash of IMEI). The key is linked to the authenticated profile and all
past guest submissions (inquiries, feedback, notification preferences) made
with that key are claimed for the profile.""",
             responses={200: {"description": "Device linked; claimed count returned"},
                        401: {"description": "Authentication required"}})
def upgrade_guest(body: dict, authorization: str | None = Header(default=None),
                  conn: sqlite3.Connection = Depends(get_db)):
    profile_id = ps.profile_for_token(conn, authorization)
    if not profile_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    user_key = str(body.get("user_key", "")).strip()
    if not user_key:
        raise HTTPException(status_code=422, detail="user_key required")
    claimed = ps.link_device_and_claim(conn, profile_id, user_key)
    return {"profile_id": profile_id, "linked_device": user_key,
            "claimed_submissions": claimed}