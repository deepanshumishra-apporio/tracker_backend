"""
SQLite storage. Keeps the latest status per (carrier, tracking_number) plus a
full event history. Idempotent: re-running an update just refreshes the row.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import config
from models import Carrier, Status, TrackingResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shipments (
    tracking_number   TEXT NOT NULL,
    carrier           TEXT NOT NULL,
    status            TEXT NOT NULL,
    estimated_delivery TEXT,
    delivered_at      TEXT,
    origin            TEXT,
    destination       TEXT,
    service           TEXT,
    weight            TEXT,
    pieces            INTEGER,
    signed_by         TEXT,
    details_json      TEXT NOT NULL DEFAULT '{}',
    events_json       TEXT NOT NULL,
    scraped_at        TEXT,
    ok                INTEGER NOT NULL,
    error             TEXT,
    PRIMARY KEY (carrier, tracking_number)
);
"""

# Columns added after the initial release — brought in via ALTER TABLE so
# existing databases upgrade in place without losing data.
_MIGRATION_COLUMNS = {
    "service": "TEXT",
    "weight": "TEXT",
    "pieces": "INTEGER",
    "signed_by": "TEXT",
    "details_json": "TEXT NOT NULL DEFAULT '{}'",
}


@contextmanager
def _conn():
    con = sqlite3.connect(config.DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.execute(_SCHEMA)
        # Upgrade older databases: add any missing columns.
        existing = {row["name"] for row in con.execute("PRAGMA table_info(shipments)")}
        for col, decl in _MIGRATION_COLUMNS.items():
            if col not in existing:
                con.execute(f"ALTER TABLE shipments ADD COLUMN {col} {decl}")


def save(result: TrackingResult) -> None:
    with _conn() as con:
        con.execute(
            """INSERT INTO shipments
               (tracking_number, carrier, status, estimated_delivery, delivered_at,
                origin, destination, service, weight, pieces, signed_by,
                details_json, events_json, scraped_at, ok, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(carrier, tracking_number) DO UPDATE SET
                 status=excluded.status,
                 estimated_delivery=excluded.estimated_delivery,
                 delivered_at=excluded.delivered_at,
                 origin=excluded.origin,
                 destination=excluded.destination,
                 service=excluded.service,
                 weight=excluded.weight,
                 pieces=excluded.pieces,
                 signed_by=excluded.signed_by,
                 details_json=excluded.details_json,
                 events_json=excluded.events_json,
                 scraped_at=excluded.scraped_at,
                 ok=excluded.ok,
                 error=excluded.error
            """,
            (
                result.tracking_number,
                result.carrier.value,
                result.status.value,
                result.estimated_delivery.isoformat() if result.estimated_delivery else None,
                result.delivered_at.isoformat() if result.delivered_at else None,
                result.origin,
                result.destination,
                result.service,
                result.weight,
                result.pieces,
                result.signed_by,
                json.dumps(result.details or {}),
                json.dumps([e.model_dump(mode="json") for e in result.events]),
                result.scraped_at.isoformat() if result.scraped_at else None,
                1 if result.ok else 0,
                result.error,
            ),
        )


# ---------------------------------------------------------------------------
# READ / QUERY LAYER  — everything the web API needs.
# ---------------------------------------------------------------------------
def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Turn a DB row into the JSON-serializable shape the API/UI consume."""
    return {
        "tracking_number": row["tracking_number"],
        "carrier": row["carrier"],
        "status": row["status"],
        "estimated_delivery": row["estimated_delivery"],
        "delivered_at": row["delivered_at"],
        "origin": row["origin"],
        "destination": row["destination"],
        "service": row["service"],
        "weight": row["weight"],
        "pieces": row["pieces"],
        "signed_by": row["signed_by"],
        "details": json.loads(row["details_json"] or "{}"),
        "events": json.loads(row["events_json"] or "[]"),
        "scraped_at": row["scraped_at"],
        "ok": bool(row["ok"]),
        "error": row["error"],
    }


def list_shipments(
    *,
    status: Optional[str] = None,
    carrier: Optional[str] = None,
    search: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Latest status of every shipment, newest-scraped first, with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if carrier:
        clauses.append("carrier = ?")
        params.append(carrier)
    if search:
        # Escape LIKE wildcards so a literal '%' or '_' in the search term does
        # not act as a wildcard (otherwise search='%' matches every row).
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("tracking_number LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT * FROM shipments "
        f"{where} "
        "ORDER BY (scraped_at IS NULL), scraped_at DESC, tracking_number ASC"
    )
    with _conn() as con:
        return [_row_to_dict(r) for r in con.execute(sql, params).fetchall()]


def get_shipment(carrier: str, tracking_number: str) -> Optional[dict[str, Any]]:
    """A single shipment (with full event history), or None if not found."""
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM shipments WHERE carrier = ? AND tracking_number = ?",
            (carrier, tracking_number),
        ).fetchone()
    return _row_to_dict(row) if row else None


def add_pending(carrier: str, tracking_number: str) -> dict[str, Any]:
    """
    Register a tracking number for tracking. Creates a PENDING row if it does
    not exist yet; existing rows are left untouched (idempotent). The scraper
    picks it up on the next run and fills in real status/events.
    """
    result = TrackingResult(
        tracking_number=tracking_number,
        carrier=Carrier(carrier),
        status=Status.PENDING,
        scraped_at=None,
        ok=True,
    )
    with _conn() as con:
        con.execute(
            """INSERT INTO shipments
               (tracking_number, carrier, status, estimated_delivery, delivered_at,
                origin, destination, events_json, scraped_at, ok, error)
               VALUES (?,?,?,NULL,NULL,NULL,NULL,'[]',NULL,1,NULL)
               ON CONFLICT(carrier, tracking_number) DO NOTHING
            """,
            (result.tracking_number, result.carrier.value, result.status.value),
        )
    # Return whatever is now stored (existing row wins over our template).
    return get_shipment(carrier, tracking_number) or _row_to_dict_from_result(result)


def _row_to_dict_from_result(result: TrackingResult) -> dict[str, Any]:
    return {
        "tracking_number": result.tracking_number,
        "carrier": result.carrier.value,
        "status": result.status.value,
        "estimated_delivery": None,
        "delivered_at": None,
        "origin": None,
        "destination": None,
        "service": None,
        "weight": None,
        "pieces": None,
        "signed_by": None,
        "details": {},
        "events": [],
        "scraped_at": None,
        "ok": True,
        "error": None,
    }


def delete_shipment(carrier: str, tracking_number: str) -> bool:
    """Remove a shipment. Returns True if a row was actually deleted."""
    with _conn() as con:
        cur = con.execute(
            "DELETE FROM shipments WHERE carrier = ? AND tracking_number = ?",
            (carrier, tracking_number),
        )
        return cur.rowcount > 0


def stats() -> dict[str, Any]:
    """Aggregate counts for the dashboard: total, per-status, per-carrier, failures."""
    with _conn() as con:
        total = con.execute("SELECT COUNT(*) AS n FROM shipments").fetchone()["n"]
        by_status = {
            r["status"]: r["n"]
            for r in con.execute(
                "SELECT status, COUNT(*) AS n FROM shipments GROUP BY status"
            ).fetchall()
        }
        by_carrier = {
            r["carrier"]: r["n"]
            for r in con.execute(
                "SELECT carrier, COUNT(*) AS n FROM shipments GROUP BY carrier"
            ).fetchall()
        }
        failed = con.execute(
            "SELECT COUNT(*) AS n FROM shipments WHERE ok = 0"
        ).fetchone()["n"]

    # Guarantee every known status/carrier key exists (0-filled) so the UI is stable.
    status_counts = {s.value: 0 for s in Status}
    status_counts.update(by_status)
    carrier_counts = {c.value: 0 for c in Carrier}
    carrier_counts.update(by_carrier)

    active = total - status_counts.get(Status.DELIVERED.value, 0)
    return {
        "total": total,
        "active": active,
        "delivered": status_counts.get(Status.DELIVERED.value, 0),
        "failed": failed,
        "by_status": status_counts,
        "by_carrier": carrier_counts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
