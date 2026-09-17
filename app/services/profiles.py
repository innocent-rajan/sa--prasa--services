"""Profile service: registration (OTP + password), tokens, guest upgrade.

Identity model:
- profile_id is the canonical identity once registered.
- user_key (device hash, e.g. IMEI hash) is the GUEST identifier the app provides.
- On registration the device user_key is linked to the profile and past
  submissions (inquiries/feedback/preferences) with the same user_key hash
  are claimed (linked to profile_id).
"""
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta

from app.db import connect, to_db_timestamp

OTP_TTL_MINUTES = 5
TOKEN_TTL_DAYS = 30
BCRYPT_ROUNDS = 12

try:
    import bcrypt
    _HAS_BCRYPT = True
except ImportError:  # pragma: no cover - bcrypt expected in requirements
    _HAS_BCRYPT = False


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_user_key(user_key: str) -> str:
    """Canonical user_key hash used across inquiries/feedback/preferences.

    Matches routers/inquiries.py _hash_key: sha256 hex, truncated to 32 chars.
    """
    return _sha256(user_key)[:32]


def hash_password(password: str) -> str:
    if not _HAS_BCRYPT:
        raise RuntimeError("bcrypt not installed")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    if not _HAS_BCRYPT:
        return False
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def _hash_token(token: str) -> str:
    return _sha256(token)


def issue_token(conn: sqlite3.Connection, profile_id: int) -> str:
    """Create an opaque bearer token; only its hash is stored."""
    token = secrets.token_urlsafe(32)
    expires = (datetime.utcnow() + timedelta(days=TOKEN_TTL_DAYS)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO auth_tokens (token_hash, profile_id, expires_at) VALUES (?, ?, ?)",
        (_hash_token(token), profile_id,
         expires))
    return token


def profile_for_token(conn: sqlite3.Connection, authorization: str | None) -> int | None:
    """Resolve an Authorization: Bearer header to a profile_id (or None)."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    row = conn.execute(
        """SELECT profile_id FROM auth_tokens
           WHERE token_hash = ? AND expires_at > datetime('now')""",
        (_hash_token(token),)).fetchone()
    return row["profile_id"] if row else None


def create_otp(conn: sqlite3.Connection, phone: str) -> str:
    """Generate a 6-digit OTP. Returns the plain code (dev: returned to client;
    production: delivered via SmsProvider).

    Fixed dev OTP: when PRASA_DEV_OTP is set, that exact code is issued every
    time (deterministic for demos/UAT); otherwise a random code is generated.
    """
    import os
    from app.config import DEV_OTP
    fixed = (DEV_OTP or os.getenv("PRASA_DEV_OTP", "")).strip()
    code = fixed if len(fixed) == 6 and fixed.isdigit() else f"{secrets.randbelow(1000000):06d}"
    expires = (datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES)).strftime(
        "%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO otp_codes (phone, code_hash, expires_at) VALUES (?, ?, ?)",
        (phone, _sha256(code), expires))
    return code


def verify_otp(conn: sqlite3.Connection, phone: str, code: str) -> bool:
    rows = conn.execute(
        """SELECT id, code_hash FROM otp_codes
           WHERE phone = ? AND used = 0 AND expires_at > datetime('now')
           ORDER BY id DESC LIMIT 1""", (phone,)).fetchall()
    for row in rows:
        if secrets.compare_digest(row["code_hash"], _sha256(code)):
            conn.execute("UPDATE otp_codes SET used = 1 WHERE id = ?", (row["id"],))
            return True
    return False


def get_or_create_profile_by_phone(conn: sqlite3.Connection, phone: str) -> int:
    row = conn.execute("SELECT id FROM profiles WHERE phone = ?", (phone,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO profiles (phone) VALUES (?)", (phone,))
    return cur.lastrowid


def link_device_and_claim(conn: sqlite3.Connection, profile_id: int,
                          user_key: str) -> int:
    """Link a device user_key to the profile and claim past guest submissions.

    Claims: inquiries, feedback and notification preferences rows whose
    user_key matches. Returns the number of claimed rows.
    """
    conn.execute(
        """INSERT INTO profile_devices (profile_id, user_key)
           VALUES (?, ?)
           ON CONFLICT(user_key) DO UPDATE SET profile_id = excluded.profile_id""",
        (profile_id, user_key))
    hashed = hash_user_key(user_key)
    claimed = 0
    for table in ("inquiries", "feedback_entries"):
        cur = conn.execute(
            f"UPDATE {table} SET profile_id = ? WHERE user_key = ? AND profile_id IS NULL",
            (profile_id, hashed))
        claimed += cur.rowcount
    # preferences: keep rows but tag them with the profile
    cur = conn.execute(
        """UPDATE notification_preferences SET profile_id = ?
           WHERE user_key = ? AND profile_id IS NULL""",
        (profile_id, hashed))
    claimed += cur.rowcount
    return claimed