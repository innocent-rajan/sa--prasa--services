"""SQLite connection management and schema for the services API.

Every connection uses WAL journal mode and a busy timeout so concurrent
reads during writes never fail with 'database is locked'.
"""
import sqlite3
from pathlib import Path

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    route_id         INTEGER PRIMARY KEY,
    route_short_name TEXT NOT NULL DEFAULT '',
    route_long_name  TEXT NOT NULL,
    route_type       INTEGER NOT NULL CHECK (route_type IN (2, 3))
);

CREATE TABLE IF NOT EXISTS stops (
    stop_id   INTEGER PRIMARY KEY,
    stop_name TEXT NOT NULL,
    stop_lat  REAL,
    stop_lon  REAL
);

CREATE TABLE IF NOT EXISTS service_status (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type     TEXT NOT NULL CHECK (scope_type IN ('route', 'stop', 'network')),
    scope_id       INTEGER,
    status_type    TEXT NOT NULL CHECK (status_type IN ('delay', 'disruption', 'cancellation')),
    severity       TEXT NOT NULL DEFAULT 'minor' CHECK (severity IN ('minor', 'major', 'severe')),
    title          TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    info_status    TEXT NOT NULL DEFAULT 'manual' CHECK (info_status IN ('live', 'manual')),
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (scope_type = 'network' OR scope_id IS NOT NULL),
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);
CREATE INDEX IF NOT EXISTS ix_service_status_scope ON service_status (scope_type, scope_id);
CREATE INDEX IF NOT EXISTS ix_service_status_window ON service_status (effective_from, effective_to);

CREATE TABLE IF NOT EXISTS advisories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    category     TEXT NOT NULL,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    scope_type   TEXT NOT NULL DEFAULT 'network' CHECK (scope_type IN ('route', 'stop', 'network')),
    scope_id     INTEGER,
    published_at TEXT NOT NULL,
    expires_at   TEXT,
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (scope_type = 'network' OR scope_id IS NOT NULL),
    CHECK (expires_at IS NULL OR expires_at > published_at)
);
CREATE INDEX IF NOT EXISTS ix_advisories_window ON advisories (published_at, expires_at, is_active);
CREATE INDEX IF NOT EXISTS ix_advisories_scope ON advisories (scope_type, scope_id);

CREATE TABLE IF NOT EXISTS kb_articles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT NOT NULL UNIQUE,
    category   TEXT NOT NULL,
    language   TEXT NOT NULL DEFAULT 'en',
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    is_published INTEGER NOT NULL DEFAULT 0 CHECK (is_published IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_kb_articles_cat ON kb_articles (category, is_published);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_articles_fts USING fts5(
    title, body, content='kb_articles', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS kb_articles_ai AFTER INSERT ON kb_articles BEGIN
    INSERT INTO kb_articles_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS kb_articles_au AFTER UPDATE ON kb_articles BEGIN
    INSERT INTO kb_articles_fts(kb_articles_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO kb_articles_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS kb_articles_ad AFTER DELETE ON kb_articles BEGIN
    INSERT INTO kb_articles_fts(kb_articles_fts, rowid, title, body)
    VALUES ('delete', old.id, old.title, old.body);
END;

CREATE TABLE IF NOT EXISTS notification_topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notification_preferences (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key  TEXT NOT NULL,
    topic_id  INTEGER NOT NULL REFERENCES notification_topics(id),
    channel   TEXT NOT NULL,
    enabled   INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    profile_id INTEGER REFERENCES profiles(id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_key, topic_id, channel)
);
CREATE INDEX IF NOT EXISTS ix_notif_prefs_user ON notification_preferences (user_key);
CREATE INDEX IF NOT EXISTS ix_notif_prefs_topic ON notification_preferences (topic_id, enabled);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key  TEXT NOT NULL UNIQUE,
    topic_code TEXT NOT NULL,
    channel    TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    error      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON notification_outbox (status);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    entity_id  TEXT,
    payload    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log (entity, entity_id);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log (created_at);

CREATE TABLE IF NOT EXISTS inquiry_categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS inquiries (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reference      TEXT NOT NULL UNIQUE,
    category_id    INTEGER NOT NULL REFERENCES inquiry_categories(id),
    user_key       TEXT,
    profile_id     INTEGER REFERENCES profiles(id),
    contact_name   TEXT,
    contact_email  TEXT,
    contact_phone  TEXT,
    scope_type     TEXT CHECK (scope_type IN ('route', 'stop', 'network')),
    scope_id       INTEGER,
    title          TEXT NOT NULL,
    description    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'received'
                   CHECK (status IN ('received', 'in_progress', 'resolved', 'closed', 'rejected')),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_inquiries_status ON inquiries (status);
CREATE INDEX IF NOT EXISTS ix_inquiries_scope ON inquiries (scope_type, scope_id);
CREATE INDEX IF NOT EXISTS ix_inquiries_category ON inquiries (category_id);

CREATE TABLE IF NOT EXISTS inquiry_updates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id  INTEGER NOT NULL REFERENCES inquiries(id),
    status      TEXT NOT NULL
                CHECK (status IN ('received', 'in_progress', 'resolved', 'closed', 'rejected')),
    note        TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT 'system',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_inquiry_updates_inquiry ON inquiry_updates (inquiry_id);

CREATE TABLE IF NOT EXISTS inquiry_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inquiry_id   INTEGER NOT NULL REFERENCES inquiries(id),
    file_name    TEXT NOT NULL,
    mime_type    TEXT NOT NULL DEFAULT 'application/octet-stream',
    size_bytes   INTEGER NOT NULL,
    stored_path  TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_inquiry_attachments_inquiry ON inquiry_attachments (inquiry_id);

CREATE TABLE IF NOT EXISTS feedback_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_type TEXT CHECK (feedback_type IN ('compliment', 'complaint', 'suggestion')),
    rating       INTEGER CHECK (rating BETWEEN 1 AND 5),
    user_key     TEXT,
    profile_id   INTEGER REFERENCES profiles(id),
    is_anonymous INTEGER NOT NULL DEFAULT 0 CHECK (is_anonymous IN (0, 1)),
    scope_type   TEXT CHECK (scope_type IN ('route', 'stop', 'network')),
    scope_id     INTEGER,
    comment      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK ((feedback_type IS NOT NULL) OR (rating IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_feedback_type ON feedback_entries (feedback_type);
CREATE INDEX IF NOT EXISTS ix_feedback_scope ON feedback_entries (scope_type, scope_id);

CREATE TABLE IF NOT EXISTS profiles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    phone          TEXT UNIQUE,
    email          TEXT UNIQUE,
    password_hash  TEXT,
    display_name   TEXT NOT NULL DEFAULT '',
    language       TEXT NOT NULL DEFAULT 'en',
    home_station   INTEGER REFERENCES stops(stop_id),
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (phone IS NOT NULL OR email IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_profiles_phone ON profiles (phone);
CREATE INDEX IF NOT EXISTS ix_profiles_email ON profiles (email);

CREATE TABLE IF NOT EXISTS otp_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone      TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_otp_phone ON otp_codes (phone, used);

CREATE TABLE IF NOT EXISTS profile_devices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    user_key   TEXT NOT NULL UNIQUE,
    linked_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_profile_devices_profile ON profile_devices (profile_id);

CREATE TABLE IF NOT EXISTS favourite_routes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id),
    route_id   INTEGER NOT NULL REFERENCES routes(route_id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (profile_id, route_id)
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash  TEXT PRIMARY KEY,
    profile_id  INTEGER NOT NULL REFERENCES profiles(id),
    expires_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_auth_tokens_profile ON auth_tokens (profile_id);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection with WAL, busy timeout and Row factory.

    check_same_thread=False: FastAPI may run a sync dependency and an async
    endpoint on different worker threads within one request. Each request
    still gets its own connection, and SQLite WAL serializes writes.
    """
    from app.config import DB_PATH as default_path
    path = db_path or default_path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created before a column existed.

    CREATE TABLE IF NOT EXISTS does not alter existing tables, so new columns
    are added here when missing. Safe to run on every startup.
    """
    if not _column_exists(conn, "profiles", "id"):
        return  # fresh database: SCHEMA already created everything
    if not _column_exists(conn, "inquiries", "profile_id"):
        conn.execute("ALTER TABLE inquiries ADD COLUMN profile_id INTEGER REFERENCES profiles(id)")
    if not _column_exists(conn, "feedback_entries", "profile_id"):
        conn.execute("ALTER TABLE feedback_entries ADD COLUMN profile_id INTEGER REFERENCES profiles(id)")
    if not _column_exists(conn, "notification_preferences", "profile_id"):
        conn.execute("ALTER TABLE notification_preferences ADD COLUMN profile_id INTEGER REFERENCES profiles(id)")


def get_db():
    """FastAPI dependency: yields a connection, commits on success, rolls back on error.

    Plain generator (no @contextmanager) so FastAPI calls it correctly.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def to_db_timestamp(value: str | None) -> str | None:
    """Normalize an ISO datetime to SQLite's 'YYYY-MM-DD HH:MM:SS' format so
    string comparisons against datetime('now') behave correctly."""
    if value is None:
        return None
    return value.replace("T", " ").split(".")[0]