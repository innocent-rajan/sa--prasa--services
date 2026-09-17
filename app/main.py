"""PRASA passenger services API - app factory."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import API_V1_PREFIX
from app.db import connect, init_schema
from app.loaders.load_reference import load_reference
from app.routers import (admin, advisories, inquiries, knowledge, notifications,
                         profile, service_status)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure schema exists and reference data is loaded on startup
    conn = connect()
    try:
        init_schema(conn)
        load_reference(conn)
    finally:
        conn.close()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="PRASA Passenger Services API",
        version="1.0.0",
        description="""Non-ticketing shared services for PRASA: **service status**,
**advisories**, **knowledge base** and **notifications**.

Consumed identically by the mobile app and the WhatsApp channel (SOW requirement:
both channels use the same approved source).

## How to use

### Public endpoints (passenger-facing)
No authentication. Mobile app / WhatsApp call these directly:

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/service-status` | Active delays/disruptions/cancellations (filter by route/stop/network) |
| `GET /api/v1/advisories` | Active passenger advisories |
| `GET /api/v1/kb/articles` | FAQ articles (`?q=` full-text search) |
| `GET /api/v1/notifications/topics` | Opt-in topics (one per route/stop + network) |
| `GET/PUT /api/v1/notifications/preferences/{user_key}` | Per-topic, per-channel opt-in (POPIA) |

### Admin endpoints (PRASA staff)
Require the `X-Admin-Key` header (click **Authorize** below and paste the admin key).
Admins publish service status / advisories / KB articles and send notifications.
Publishing a route-scoped status or advisory **automatically queues notifications**
for every user opted into that topic (outbox + provider dispatch).

### Data conventions
- `info_status`: `live` (from an operational feed) or `manual` (staff-published).
  Clients MUST display this label - static data must never appear as real-time.
- Active = inside its effective window; expired items disappear automatically.
- Topics: `network`, `route:{route_id}` (metro 1-34, coach 1001-1040),
  `stop:{stop_id}` (metro 1-274, coach 10001+).
- Channels: `push`, `whatsapp` (config-driven; PRASA-approved list).
""",
        openapi_tags=[
            {"name": "service-status", "description": "Delays, disruptions, cancellations. Public reads; admin writes."},
            {"name": "advisories", "description": "Passenger advisories with publish/expiry windows."},
            {"name": "knowledge", "description": "FAQ / knowledge articles with full-text search."},
            {"name": "notifications", "description": "Topics, channels and per-user opt-in preferences."},
            {"name": "inquiries-feedback", "description": "Inquiry submission with reference tracking; feedback and journey ratings (POPIA anonymity)."},
            {"name": "profile", "description": "Passenger identity: phone-OTP or email+password auth, profile management, favourites, guest upgrade (device key linking + claiming past submissions)."},
            {"name": "admin", "description": "Staff operations: case management, send notifications, retry dispatch, audit log."},
        ],
        lifespan=lifespan,
    )
    for router in (service_status.router, advisories.router, knowledge.router,
                   notifications.router, inquiries.router, profile.router,
                   admin.router):
        app.include_router(router, prefix=API_V1_PREFIX)
    return app


app = create_app()


@app.get("/health")
def health():
    return {"status": "ok"}