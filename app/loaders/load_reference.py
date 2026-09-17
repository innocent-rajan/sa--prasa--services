"""Idempotent reference loader: routes + stops from merged_gtfs/, topic seeding."""
import csv
import sqlite3
import sys
from pathlib import Path

from app.config import MERGED_GTFS_DIR
from app.db import connect, init_schema


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        sys.exit(f"load_reference: missing input {path}")
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_reference(conn: sqlite3.Connection, gtfs_dir: Path | None = None) -> dict:
    """Import routes and stops; seed topics. Idempotent: INSERT OR REPLACE."""
    gtfs_dir = gtfs_dir or Path(MERGED_GTFS_DIR)
    routes = _read_csv(gtfs_dir / "routes.txt")
    stops = _read_csv(gtfs_dir / "stops.txt")

    conn.executemany(
        """INSERT INTO routes (route_id, route_short_name, route_long_name, route_type)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(route_id) DO UPDATE SET
             route_short_name=excluded.route_short_name,
             route_long_name=excluded.route_long_name,
             route_type=excluded.route_type""",
        [(int(r["route_id"]), r.get("route_short_name") or "",
          r["route_long_name"], int(r["route_type"])) for r in routes],
    )
    conn.executemany(
        """INSERT INTO stops (stop_id, stop_name, stop_lat, stop_lon)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(stop_id) DO UPDATE SET
             stop_name=excluded.stop_name, stop_lat=excluded.stop_lat,
             stop_lon=excluded.stop_lon""",
        [(int(r["stop_id"]), r["stop_name"],
          float(r["stop_lat"]) if r.get("stop_lat") else None,
          float(r["stop_lon"]) if r.get("stop_lon") else None) for r in stops],
    )

    # Seed topics: network + one per route/stop
    conn.execute(
        """INSERT INTO notification_topics (code, name, description)
           VALUES ('network', 'PRASA Network', 'All PRASA-wide announcements')
           ON CONFLICT(code) DO NOTHING"""
    )
    for r in routes:
        conn.execute(
            """INSERT INTO notification_topics (code, name, description)
               VALUES (?, ?, ?)
               ON CONFLICT(code) DO NOTHING""",
            (f"route:{r['route_id']}", r["route_long_name"],
             f"Alerts for route {r['route_long_name']}"),
        )
    for s in stops:
        conn.execute(
            """INSERT INTO notification_topics (code, name, description)
               VALUES (?, ?, ?)
               ON CONFLICT(code) DO NOTHING""",
            (f"stop:{s['stop_id']}", s["stop_name"],
             f"Alerts affecting {s['stop_name']}"),
        )
    conn.commit()

    # Seed inquiry categories (idempotent)
    for code, name, desc in [
        ("lost_item", "Lost item", "Report an item lost on a train or in a station"),
        ("delay_complaint", "Delay complaint", "Complaint about a delayed service"),
        ("cleanliness", "Cleanliness", "Cleanliness of trains or stations"),
        ("staff_conduct", "Staff conduct", "Report about staff behaviour"),
        ("safety", "Safety concern", "Safety or security concern"),
        ("ticketing", "Ticketing", "Ticket purchase, fare or gate issues"),
        ("accessibility", "Accessibility", "Accessibility of services or facilities"),
        ("other", "Other", "Anything not covered by the categories above"),
    ]:
        conn.execute(
            """INSERT INTO inquiry_categories (code, name, description)
               VALUES (?, ?, ?)
               ON CONFLICT(code) DO NOTHING""",
            (code, name, desc),
        )
    conn.commit()

    counts = {
        "routes": conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0],
        "stops": conn.execute("SELECT COUNT(*) FROM stops").fetchone()[0],
        "topics": conn.execute("SELECT COUNT(*) FROM notification_topics").fetchone()[0],
        "inquiry_categories": conn.execute(
            "SELECT COUNT(*) FROM inquiry_categories").fetchone()[0],
    }
    expected_routes, expected_stops = len(routes), len(stops)
    if counts["routes"] != expected_routes or counts["stops"] != expected_stops:
        raise RuntimeError(
            f"Loader validation failed: expected {expected_routes} routes / "
            f"{expected_stops} stops, got {counts}")
    return counts


if __name__ == "__main__":
    from app.db import init_schema
    conn = sqlite3.connect()
    init_schema(conn)
    print(load_reference(conn))
    conn.close()