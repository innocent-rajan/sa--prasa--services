"""Environment-driven configuration for the PRASA passenger services API."""
import os
from pathlib import Path

# Project root = prasa-services/ (this file is app/config.py)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# SQLite database file (WAL mode enabled on every connection)
DB_PATH = os.getenv("APIS_DB_PATH", str(PROJECT_ROOT / "data" / "apis.db"))

# Admin authentication: static key from environment (required in production)
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "prasa-admin-local-dev")

# Merged GTFS directory for the reference loader (routes.txt, stops.txt)
MERGED_GTFS_DIR = os.getenv(
    "MERGED_GTFS_DIR",
    str(PROJECT_ROOT.parent / "data" / "merged_gtfs"),
)

# Notification channels PRASA has approved (config, not hardcoded)
NOTIFICATION_CHANNELS = [c.strip() for c in os.getenv(
    "NOTIFICATION_CHANNELS", "push,whatsapp").split(",") if c.strip()]

# Notification provider: stub (default) | fcm | whatsapp (future)
NOTIFICATION_PROVIDER = os.getenv("NOTIFICATION_PROVIDER", "stub")

# Inquiry attachments: files saved under this directory
ATTACHMENTS_DIR = os.getenv(
    "ATTACHMENTS_DIR", str(PROJECT_ROOT / "data" / "attachments"))
MAX_ATTACHMENT_BYTES = int(os.getenv("MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024)))

# Fixed dev OTP (6 digits): when set, every OTP request issues this code.
# Empty = random OTPs. Demo/UAT convenience only - never set in production.
DEV_OTP = os.getenv("PRASA_DEV_OTP", "")

API_V1_PREFIX = "/api/v1"