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
 case_id INTEGER NOT NULL REFERENCES mortuary_cases(id), group_id INTEGER REFERENCES ceremony_groups(id),
 start_at TEXT NOT NULL, end_at TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS ceremony_groups (
 id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES mortuary_cases(id),
 purpose TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('held','confirmed','released','expired','waitlisted')),
 idempotency_key TEXT NOT NULL UNIQUE, content_digest TEXT NOT NULL,
 hold_ttl_minutes INTEGER NOT NULL, hold_expires_at TEXT,
 body_preservation_deadline TEXT NOT NULL,
 urgency_level INTEGER NOT NULL DEFAULT 0,
 urgency_reviewed_by TEXT NOT NULL DEFAULT '', urgency_reviewed_at TEXT,
 requested_lines_json TEXT NOT NULL DEFAULT '[]',
 created_by TEXT NOT NULL,
 confirmed_by TEXT NOT NULL DEFAULT '', confirmed_at TEXT,
 released_by TEXT NOT NULL DEFAULT '', released_at TEXT, release_reason TEXT NOT NULL DEFAULT '',
 promoted_run_key TEXT NOT NULL DEFAULT '',
 version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ceremony_case ON ceremony_groups(case_id,id);
CREATE TABLE IF NOT EXISTS ceremony_waitlist (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL UNIQUE REFERENCES ceremony_groups(id),
 override_seq INTEGER, status TEXT NOT NULL DEFAULT 'waiting' CHECK(status IN ('waiting','promoted','cancelled','expired')),
 promoted_at TEXT, cancelled_by TEXT NOT NULL DEFAULT '', cancel_reason TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waitlist_rank ON ceremony_waitlist(status,override_seq);
CREATE TABLE IF NOT EXISTS ceremony_interventions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, group_id INTEGER NOT NULL REFERENCES ceremony_groups(id),
 actor TEXT NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
 before_json TEXT NOT NULL DEFAULT '{}', after_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intervention_group ON ceremony_interventions(group_id,id);
CREATE TABLE IF NOT EXISTS orchestration_actors (
 actor TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('family_service','planner','approver')),
 granted_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orchestration_promotion_runs (
 idempotency_key TEXT PRIMARY KEY, trigger_type TEXT NOT NULL, trigger_ref TEXT NOT NULL DEFAULT '',
 result_json TEXT NOT NULL, promoted_group_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservation_group ON facility_reservations(group_id);
'''


class MortuaryRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self) -> None:
        self.connection.executescript(SCHEMA)
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(facility_reservations)").fetchall()}
        if "group_id" not in columns:
            self.connection.execute("ALTER TABLE facility_reservations ADD COLUMN group_id INTEGER REFERENCES ceremony_groups(id)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_reservation_group ON facility_reservations(group_id)")

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
        rows = self.connection.execute("SELECT * FROM facility_reservations WHERE resource_id=? AND status IN ('confirmed','held') AND start_at<? AND end_at>? ORDER BY start_at", (resource_id, end_at, start_at)).fetchall()
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

    # ---- 跨资源编排 ----
    def group_by_idempotency(self, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM ceremony_groups WHERE idempotency_key=?", (key,)).fetchone())

    def group(self, group_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM ceremony_groups WHERE id=?", (group_id,)).fetchone())

    def group_lines(self, group_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT r.*,f.code resource_code,f.name resource_name,f.kind resource_kind,f.capacity resource_capacity FROM facility_reservations r JOIN facility_resources f ON f.id=r.resource_id WHERE r.group_id=? ORDER BY r.id", (group_id,)).fetchall()
        return [dict(row) for row in rows]

    def waitlist_entry(self, group_id: int) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM ceremony_waitlist WHERE group_id=?", (group_id,)).fetchone())

    def waitlist_view(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT w.id,w.group_id,w.status,w.override_seq,w.created_at,g.case_id,g.urgency_level,g.body_preservation_deadline "
            "FROM ceremony_waitlist w JOIN ceremony_groups g ON g.id=w.group_id "
            "ORDER BY CASE WHEN w.override_seq IS NULL THEN 1 ELSE 0 END, w.override_seq, "
            "g.urgency_level DESC, g.body_preservation_deadline ASC, w.created_at ASC, w.id ASC",
        ).fetchall()
        return [dict(row) for row in rows]

    def active_waitlist(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT w.*,g.id gid,g.idempotency_key,g.case_id,g.purpose,g.created_by,g.hold_ttl_minutes,"
            "g.body_preservation_deadline,g.urgency_level,g.urgency_reviewed_by,g.requested_lines_json "
            "FROM ceremony_waitlist w JOIN ceremony_groups g ON g.id=w.group_id WHERE w.status='waiting' "
            "ORDER BY CASE WHEN w.override_seq IS NULL THEN 1 ELSE 0 END, w.override_seq, "
            "g.urgency_level DESC, g.body_preservation_deadline ASC, w.created_at ASC, w.id ASC",
        ).fetchall()

    def overlapping_holds(self, resource_id: int, start_at: str, end_at: str, exclude_group: int | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT r.*,f.code resource_code,f.kind resource_kind FROM facility_reservations r "
               "JOIN facility_resources f ON f.id=r.resource_id "
               "WHERE r.resource_id=? AND r.status IN ('confirmed','held') AND r.start_at<? AND r.end_at>?")
        params: list[Any] = [resource_id, end_at, start_at]
        if exclude_group is not None:
            sql += " AND COALESCE(r.group_id,-1)<>?"
            params.append(exclude_group)
        sql += " ORDER BY r.start_at"
        rows = self.connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def intervention(self, group_id: int, actor: str, action: str, reason: str, before: dict[str, Any], after: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO ceremony_interventions(group_id,actor,action,reason,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (group_id, actor, action, reason, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), now),
        )

    def interventions(self, group_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM ceremony_interventions WHERE group_id=? ORDER BY id", (group_id,)).fetchall()]

    def actor_role(self, actor: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM orchestration_actors WHERE actor=?", (actor,)).fetchone())

    def grant_actor(self, actor: str, role: str, granted_by: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO orchestration_actors(actor,role,granted_by,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(actor) DO UPDATE SET role=excluded.role,granted_by=excluded.granted_by,created_at=excluded.created_at",
            (actor, role, granted_by, now),
        )

    def promotion_run(self, key: str) -> dict[str, Any] | None:
        return self.one(self.connection.execute("SELECT * FROM orchestration_promotion_runs WHERE idempotency_key=?", (key,)).fetchone())

    def save_promotion_run(self, key: str, trigger_type: str, trigger_ref: str, result: dict[str, Any], promoted: list[int], now: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO orchestration_promotion_runs(idempotency_key,trigger_type,trigger_ref,result_json,promoted_group_ids,created_at) VALUES(?,?,?,?,?,?)",
            (key, trigger_type, trigger_ref, json.dumps(result, ensure_ascii=False, sort_keys=True), json.dumps(promoted), now),
        )
