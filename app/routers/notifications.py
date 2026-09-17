"""Notifications: topics, opt-in preferences (POPIA), outbox status."""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from app.config import NOTIFICATION_CHANNELS
from app.db import get_db
from app.schemas.models import PreferenceOut, PreferencesPut, TopicOut

router = APIRouter(tags=["notifications"])


@router.get("/notifications/topics", response_model=list[TopicOut],
            summary="List opt-in topics",
            description="One topic per route/stop plus `network`. Seeded automatically "
                        "from the GTFS reference data. Codes: `network`, `route:{id}`, `stop:{id}`.",
            responses={200: {"content": {"application/json": {"example": [
                {"code": "network", "name": "PRASA Network",
                 "description": "All PRASA-wide announcements"},
                {"code": "route:1", "name": "CAPE TOWN - CHRIS HANI",
                 "description": "Alerts for route CAPE TOWN - CHRIS HANI"}]}}}})
def list_topics(conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        "SELECT code, name, description FROM notification_topics ORDER BY code"
    ).fetchall()
    return [TopicOut(code=r["code"], name=r["name"], description=r["description"]) for r in rows]


@router.get("/notifications/channels", summary="List approved channels",
            description="Channels PRASA has approved (config-driven). "
                        "Currently: push, whatsapp.",
            responses={200: {"content": {"application/json": {"example": {
                "channels": ["push", "whatsapp"]}}}}})
def list_channels():
    """Channels PRASA has approved (config-driven)."""
    return {"channels": NOTIFICATION_CHANNELS}


@router.get("/notifications/preferences/{user_key}", response_model=list[PreferenceOut],
            summary="Get a user's notification preferences",
            description="""`user_key` is the app-provided identifier: the anonymous
device key (e.g. hash of IMEI) for guests, or the device key linked to a
registered profile. No personal identity is stored (POPIA minimisation).

Empty list = user has never opted in = receives nothing.""",
            responses={200: {"content": {"application/json": {"example": [
                {"topic": "route:1", "channel": "push", "enabled": True,
                 "updated_at": "2026-09-15 12:30:05"},
                {"topic": "network", "channel": "whatsapp", "enabled": False,
                 "updated_at": "2026-09-15 12:31:00"}]}}}})
def get_preferences(user_key: str, conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute(
        """SELECT t.code, p.channel, p.enabled, p.updated_at
           FROM notification_preferences p
           JOIN notification_topics t ON t.id = p.topic_id
           WHERE p.user_key = ? ORDER BY t.code, p.channel""",
        (user_key,),
    ).fetchall()
    return [PreferenceOut(topic=r["code"], channel=r["channel"],
                          enabled=bool(r["enabled"]), updated_at=r["updated_at"])
            for r in rows]


@router.put("/notifications/preferences/{user_key}", response_model=list[PreferenceOut],
            summary="Set a user's notification preferences (opt-in / opt-out)",
            description="""Upsert preferences. **Default is OFF** - a user only receives
notifications for topics+channels they explicitly enabled (POPIA consent).

**Opt-in / opt-out semantics**
- Opt-in: `enabled: true`; Opt-out: `enabled: false` (row kept for consent audit trail)
- Consent is per topic AND per channel - opting in on push does not consent to WhatsApp
- Takes effect immediately: the next fan-out query simply won't match opted-out users
- Rejects unknown topics and non-approved channels with 422

**Transactional vs operational**
- *Operational* messages (delays, advisories, network notices) require opt-in -
  they are only sent to topics+channels the user enabled
- *Transactional* messages (e.g. future "your booking is confirmed", inquiry
  status updates for a reference the user lodged) are necessary to provide the
  service and may be delivered on the channel tied to the transaction,
  independent of topic opt-ins - per POPIA's service-necessity provision

**Request example**
```json
{"preferences": [
  {"topic": "route:1",  "channel": "push",     "enabled": true},
  {"topic": "network",  "channel": "whatsapp", "enabled": true},
  {"topic": "route:5",  "channel": "whatsapp", "enabled": false}
]}
```""",
            responses={200: {"content": {"application/json": {"example": [
                {"topic": "route:1", "channel": "push", "enabled": True,
                 "updated_at": "2026-09-16 10:00:00"}]}},
                "description": "Updated; returns the full preference list"},
                       422: {"description": "Unknown topic or non-approved channel"}})
def put_preferences(user_key: str, body: PreferencesPut,
                    conn: sqlite3.Connection = Depends(get_db)):
    """Upsert preferences. Opt-in model: rows only exist because the user set them.

    user_key is stored hashed (same scheme as inquiries/feedback) so guest
    upgrade can claim past preferences. Keeping rows with enabled=0 preserves
    the consent audit trail (POPIA).
    """
    from app.services.profiles import hash_user_key
    stored_key = hash_user_key(user_key)
    for item in body.preferences:
        if item.channel not in NOTIFICATION_CHANNELS:
            raise HTTPException(status_code=422, detail=f"Channel '{item.channel}' not approved")
        topic = conn.execute(
            "SELECT id FROM notification_topics WHERE code = ?", (item.topic,)
        ).fetchone()
        if not topic:
            raise HTTPException(status_code=422, detail=f"Unknown topic '{item.topic}'")
        conn.execute(
            """INSERT INTO notification_preferences (user_key, topic_id, channel, enabled, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(user_key, topic_id, channel)
               DO UPDATE SET enabled=excluded.enabled, updated_at=datetime('now')""",
            (stored_key, topic["id"], item.channel, 1 if item.enabled else 0),
        )
    return get_preferences(stored_key, conn)