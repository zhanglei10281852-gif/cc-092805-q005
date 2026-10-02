from __future__ import annotations

import json
import sqlite3
from typing import Any


SCHEMA = r''' 
CREATE TABLE IF NOT EXISTS mortuary_cases (
 id INTEGER PRIMARY KEY AUTOINCREMENT, external_ref TEXT NOT NULL UNIQUE,
 decedent_name TEXT NOT NULL, identity_number TEXT, death_time TEXT NOT NULL,
 received_from TEXT NOT NULL, family_contact TEXT NOT NULL, family_phone TEXT NOT NULL,
 special_notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'registered',
 current_location TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custody_transfers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 from_location TEXT NOT NULL, to_location TEXT NOT NULL, seal_code TEXT NOT NULL,
 requested_by TEXT NOT NULL, accepted_by TEXT NOT NULL DEFAULT '', observed_seal_code TEXT NOT NULL DEFAULT '',
 condition_note TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
 idempotency_key TEXT NOT NULL, requested_at TEXT NOT NULL, accepted_at TEXT,
 UNIQUE(case_id,idempotency_key)
);
CREATE TABLE IF NOT EXISTS facility_resources (
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 kind TEXT NOT NULL, site_code TEXT NOT NULL, capacity INTEGER NOT NULL,
 attributes_json TEXT NOT NULL DEFAULT '{}', active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS facility_reservations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, resource_id INTEGER NOT NULL REFERENCES facility_resources(id),
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id), start_at TEXT NOT NULL, end_at TEXT NOT NULL,
 purpose TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'confirmed', created_by TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(resource_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_reservation_window ON facility_reservations(resource_id,start_at,end_at,status);
CREATE TABLE IF NOT EXISTS funeral_service_orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 service_code TEXT NOT NULL, quantity INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL,
 amount_cents INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'draft', requested_by TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS burial_rights (
 id INTEGER PRIMARY KEY AUTOINCREMENT, plot_code TEXT NOT NULL UNIQUE, holder_name TEXT NOT NULL,
 holder_identity TEXT NOT NULL, starts_on TEXT NOT NULL, expires_on TEXT NOT NULL,
 case_id INTEGER REFERENCES mortuary_cases(id), status TEXT NOT NULL DEFAULT 'active',
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS burial_right_renewals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, right_id INTEGER NOT NULL REFERENCES burial_rights(id),
 previous_expires_on TEXT NOT NULL, new_expires_on TEXT NOT NULL, years INTEGER NOT NULL,
 payment_reference TEXT NOT NULL UNIQUE, handled_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 amount_cents INTEGER NOT NULL, paid_cents INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'issued', created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoice_items (
 invoice_id INTEGER NOT NULL REFERENCES invoices(id), order_id INTEGER NOT NULL UNIQUE REFERENCES funeral_service_orders(id),
 amount_cents INTEGER NOT NULL, PRIMARY KEY(invoice_id,order_id)
);
CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
 amount_cents INTEGER NOT NULL, channel TEXT NOT NULL, external_reference TEXT NOT NULL UNIQUE,
 received_by TEXT NOT NULL, received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mortuary_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_type TEXT NOT NULL, aggregate_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mortuary_event ON mortuary_events(aggregate_type,aggregate_id,id);
CREATE TABLE IF NOT EXISTS ceremony_orchestrations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'held' CHECK(status IN ('held','confirmed','released')),
 requested_by TEXT NOT NULL, confirmed_by TEXT NOT NULL DEFAULT '', released_by TEXT NOT NULL DEFAULT '',
 release_reason TEXT NOT NULL DEFAULT '', hold_expires_at TEXT NOT NULL,
 confirmed_at TEXT, released_at TEXT, idempotency_key TEXT NOT NULL, request_digest TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(case_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_orch_status_expiry ON ceremony_orchestrations(status,hold_expires_at);
CREATE INDEX IF NOT EXISTS idx_orch_case ON ceremony_orchestrations(case_id);
CREATE TABLE IF NOT EXISTS ceremony_orchestration_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 orchestration_id INTEGER NOT NULL REFERENCES ceremony_orchestrations(id) ON DELETE CASCADE,
 resource_id INTEGER NOT NULL REFERENCES facility_resources(id),
 reservation_id INTEGER REFERENCES facility_reservations(id),
 start_at TEXT NOT NULL, end_at TEXT NOT NULL, purpose TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'held' CHECK(status IN ('held','confirmed','released')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(orchestration_id,resource_id,start_at)
);
CREATE INDEX IF NOT EXISTS idx_orch_item_resource ON ceremony_orchestration_items(resource_id,start_at,end_at,status);
CREATE TABLE IF NOT EXISTS ceremony_waitlist_entries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 requested_by TEXT NOT NULL, requested_at TEXT NOT NULL, body_preserve_until TEXT NOT NULL,
 urgency_level INTEGER NOT NULL DEFAULT 0 CHECK(urgency_level BETWEEN 0 AND 3),
 urgency_reviewed_by TEXT NOT NULL DEFAULT '', urgency_reviewed_at TEXT,
 requirements_json TEXT NOT NULL, hold_minutes INTEGER NOT NULL DEFAULT 30,
 status TEXT NOT NULL DEFAULT 'waiting' CHECK(status IN ('waiting','promoted','cancelled','expired')),
 promoted_orchestration_id INTEGER REFERENCES ceremony_orchestrations(id), promoted_at TEXT,
 cancelled_by TEXT NOT NULL DEFAULT '', cancelled_reason TEXT NOT NULL DEFAULT '',
 manual_seq INTEGER NOT NULL DEFAULT 0, was_overridden INTEGER NOT NULL DEFAULT 0 CHECK(was_overridden IN (0,1)),
 promotion_token TEXT NOT NULL DEFAULT '',
 idempotency_key TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(case_id,idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_waitlist_status ON ceremony_waitlist_entries(status,manual_seq);
CREATE TABLE IF NOT EXISTS ceremony_waitlist_overrides (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 waitlist_entry_id INTEGER NOT NULL REFERENCES ceremony_waitlist_entries(id),
 ordered_position INTEGER NOT NULL CHECK(ordered_position >= 1),
 actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waitlist_override_entry ON ceremony_waitlist_overrides(waitlist_entry_id,id);
CREATE TABLE IF NOT EXISTS ceremony_adjustments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 orchestration_id INTEGER REFERENCES ceremony_orchestrations(id) ON DELETE CASCADE,
 waitlist_entry_id INTEGER REFERENCES ceremony_waitlist_entries(id) ON DELETE CASCADE,
 adjustment_type TEXT NOT NULL, actor TEXT NOT NULL,
 detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ceremony_adj_orch ON ceremony_adjustments(orchestration_id,id);
CREATE INDEX IF NOT EXISTS idx_ceremony_adj_wait ON ceremony_adjustments(waitlist_entry_id,id);
'''

ACTIVE_RESERVATION_STATUSES = ("held", "confirmed")
NATURAL_WAITLIST_ORDER = (
    "CASE WHEN urgency_reviewed_at IS NOT NULL THEN urgency_level ELSE 0 END DESC, "
    "body_preserve_until, requested_at, id"
)


class MortuaryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)

    @staticmethod
    def one(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return None if row is None else dict(row)

    def event(self, kind: str, aggregate_id: int | str, event_type: str, actor: str, payload: dict[str, Any], now: str) -> None:
        self.connection.execute("INSERT INTO mortuary_events(aggregate_type,aggregate_id,event_type,actor,payload_json,created_at) VALUES(?,?,?,?,?,?)", (kind, str(aggregate_id), event_type, actor, json.dumps(payload, ensure_ascii=False, sort_keys=True), now))

    def case(self, case_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE id=?", (case_id,)).fetchone())

    def case_ref(self, ref: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM mortuary_cases WHERE external_ref=?", (ref,)).fetchone())

    def transfer(self, transfer_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM custody_transfers WHERE id=?", (transfer_id,)).fetchone())

    def transfer_key(self, case_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM custody_transfers WHERE case_id=? AND idempotency_key=?", (case_id, key)).fetchone())

    def resource_code(self, code: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM facility_resources WHERE code=?", (code,)).fetchone())

    def resource(self, resource_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM facility_resources WHERE id=?", (resource_id,)).fetchone())

    def reservation_key(self, resource_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM facility_reservations WHERE resource_id=? AND idempotency_key=?", (resource_id, key)).fetchone())

    def reservation(self, reservation_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT r.*,f.code resource_code,f.kind resource_kind FROM facility_reservations r JOIN facility_resources f ON f.id=r.resource_id WHERE r.id=?", (reservation_id,)).fetchone())

    def conflicts(self, resource_id: int, start_at: str, end_at: str) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in ACTIVE_RESERVATION_STATUSES)
        rows = self.connection.execute(
            f"SELECT r.*,o.id orchestration_id,o.status orchestration_status FROM facility_reservations r "
            f"LEFT JOIN ceremony_orchestration_items oi ON oi.reservation_id=r.id "
            f"LEFT JOIN ceremony_orchestrations o ON o.id=oi.orchestration_id "
            f"WHERE r.resource_id=? AND r.status IN ({placeholders}) "
            f"AND r.start_at<? AND r.end_at>? ORDER BY r.start_at",
            (resource_id, *ACTIVE_RESERVATION_STATUSES, end_at, start_at),
        ).fetchall()
        return [dict(row) for row in rows]

    def order(self, order_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM funeral_service_orders WHERE id=?", (order_id,)).fetchone())

    def right(self, right_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM burial_rights WHERE id=?", (right_id,)).fetchone())

    def right_plot(self, plot_code: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM burial_rights WHERE plot_code=?", (plot_code,)).fetchone())

    def invoice(self, invoice_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone())

    def timeline(self, kind: str, aggregate_id: int | str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM mortuary_events WHERE aggregate_type=? AND aggregate_id=? ORDER BY id", (kind, str(aggregate_id))).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def orchestration(self, orchestration_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM ceremony_orchestrations WHERE id=?", (orchestration_id,)).fetchone())

    def orchestration_key(self, case_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM ceremony_orchestrations WHERE case_id=? AND idempotency_key=?", (case_id, key)).fetchone())

    def orchestration_items(self, orchestration_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT oi.*,f.code resource_code,f.name resource_name,f.kind resource_kind,f.capacity resource_capacity "
            "FROM ceremony_orchestration_items oi JOIN facility_resources f ON f.id=oi.resource_id "
            "WHERE oi.orchestration_id=? ORDER BY oi.start_at,f.code",
            (orchestration_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def expired_holds(self, now: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM ceremony_orchestrations WHERE status='held' AND hold_expires_at<=? ORDER BY id",
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def waitlist_entry(self, entry_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM ceremony_waitlist_entries WHERE id=?", (entry_id,)).fetchone())

    def waitlist_key(self, case_id: int, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM ceremony_waitlist_entries WHERE case_id=? AND idempotency_key=?", (case_id, key)).fetchone())

    def waiting_entries(self) -> list[dict[str, Any]]:
        pinned = self.connection.execute(
            "SELECT * FROM ceremony_waitlist_entries WHERE status='waiting' AND manual_seq>0 ORDER BY manual_seq,id"
        ).fetchall()
        natural = self.connection.execute(
            f"SELECT * FROM ceremony_waitlist_entries WHERE status='waiting' AND manual_seq=0 ORDER BY {NATURAL_WAITLIST_ORDER}"
        ).fetchall()
        return [dict(row) for row in (*pinned, *natural)]

    def case_waitlist(self, case_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM ceremony_waitlist_entries WHERE case_id=? ORDER BY id", (case_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def adjustments(self, *, orchestration_id: int | None = None, waitlist_entry_id: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM ceremony_adjustments"
        conditions: list[str] = []
        params: list[Any] = []
        if orchestration_id is not None:
            conditions.append("orchestration_id=?")
            params.append(orchestration_id)
        if waitlist_entry_id is not None:
            conditions.append("waitlist_entry_id=?")
            params.append(waitlist_entry_id)
        if conditions:
            sql += " WHERE " + " OR ".join(conditions)
        sql += " ORDER BY id"
        rows = self.connection.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
