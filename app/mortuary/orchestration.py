from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal, request_fingerprint
from app.database import get_connection, transaction
from app.mortuary.repository import MortuaryRepository
from app.services.audit import AuditContext, AuditService

SYSTEM_ACTOR = "system:hold-expiry"
MAX_ITEMS = 20
MIN_HOLD_MINUTES = 1
MAX_HOLD_MINUTES = 4320


class CeremonyOrchestrationService:
    """跨资源（礼厅、接运车辆、礼仪人员、火化时段等）整组占用与候补编排。

    - create_hold：在单个 IMMEDIATE 事务内为整组资源落“held”占用，任一资源冲突整笔回滚。
    - confirm / release：由有权限人员整体确认或整体释放。
    - 候补排序：已审核紧急等级（降序）→ 遗体保存期限（升序）→ 申请时间（升序）；
      人工越序的条目固定在自然队列之前，且每次越序必须填写理由并写入审计。
    - 所有状态落 SQLite（WAL），进程重启后未过期的整组占用与候补位置不丢失。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None, *, ensure: bool = True) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = MortuaryRepository(self.connection)
        if ensure:
            self.repository.ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    # ------------------------------------------------------------------ 工具

    @staticmethod
    def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
        return start_a < end_b and end_a > start_b

    @staticmethod
    def _case(conn: sqlite3.Connection, case_id: int) -> dict[str, Any]:
        case = MortuaryRepository(conn).case(case_id)
        if case is None:
            raise NotFoundError("逝者业务档案不存在")
        return case

    def _normalize_items(self, conn: sqlite3.Connection, raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not raw_items:
            raise ValidationError("整组占用至少需要一个资源")
        if len(raw_items) > MAX_ITEMS:
            raise ValidationError(f"整组占用不能超过 {MAX_ITEMS} 个资源")
        normalized: list[dict[str, Any]] = []
        repo = MortuaryRepository(conn)
        for raw in raw_items:
            start_at = to_storage(raw["start_at"])
            end_at = to_storage(raw["end_at"])
            if end_at <= start_at:
                raise ValidationError("结束时间必须晚于开始时间")
            resource = repo.resource_code(raw["resource_code"])
            if resource is None or not resource["active"]:
                raise NotFoundError(f"设施资源不存在或已停用：{raw['resource_code']}")
            purpose = (raw.get("purpose") or "").strip()
            normalized.append(
                {
                    "resource_id": int(resource["id"]),
                    "resource_code": resource["code"],
                    "resource_kind": resource["kind"],
                    "start_at": start_at,
                    "end_at": end_at,
                    "purpose": purpose,
                }
            )
        if len({(item["resource_id"], item["start_at"]) for item in normalized}) != len(normalized):
            raise ValidationError("整组占用中存在重复的资源时段")
        return normalized

    @staticmethod
    def _dump_items(items: list[dict[str, Any]]) -> str:
        return json.dumps(items, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _requirements_fingerprint(items: list[dict[str, Any]], hold_minutes: int, anchor: str) -> str:
        return request_fingerprint({"items": items, "hold_minutes": hold_minutes, "anchor": anchor})

    def _evaluate_fit(
        self,
        conn: sqlite3.Connection,
        items: list[dict[str, Any]],
        *,
        tentative: list[dict[str, Any]] | None = None,
        exclude_reservation_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """返回无法满足的资源清单；空清单表示全部资源可用。"""
        repo = MortuaryRepository(conn)
        exclude = exclude_reservation_ids or set()
        placed = list(tentative or [])
        blocked: list[dict[str, Any]] = []
        for item in items:
            resource = repo.resource(item["resource_id"])
            assert resource is not None
            active = [row for row in repo.conflicts(item["resource_id"], item["start_at"], item["end_at"]) if row["id"] not in exclude]
            own_extra = [
                other
                for other in placed
                if other["resource_id"] == item["resource_id"]
                and self._overlaps(item["start_at"], item["end_at"], other["start_at"], other["end_at"])
            ]
            occupied = len(active) + len(own_extra)
            if occupied >= int(resource["capacity"]):
                blockers = [
                    {
                        "reservation_id": row["id"],
                        "case_id": row["case_id"],
                        "orchestration_id": row["orchestration_id"],
                        "reservation_status": row["status"],
                        "orchestration_status": row["orchestration_status"],
                        "start_at": row["start_at"],
                        "end_at": row["end_at"],
                    }
                    for row in active
                ]
                blocked.append(
                    {
                        "resource_id": item["resource_id"],
                        "resource_code": item["resource_code"],
                        "resource_kind": item["resource_kind"],
                        "start_at": item["start_at"],
                        "end_at": item["end_at"],
                        "capacity": int(resource["capacity"]),
                        "occupied": occupied,
                        "blocking": blockers,
                    }
                )
            placed.append(item)
        return blocked

    def _adjustment(
        self,
        conn: sqlite3.Connection,
        now: str,
        *,
        adjustment_type: str,
        actor: str,
        orchestration_id: int | None = None,
        waitlist_entry_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO ceremony_adjustments(orchestration_id,waitlist_entry_id,adjustment_type,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (orchestration_id, waitlist_entry_id, adjustment_type, actor, json.dumps(detail or {}, ensure_ascii=False, sort_keys=True), now),
        )

    def _events(
        self,
        conn: sqlite3.Connection,
        now: str,
        *,
        case_id: int,
        orchestration_id: int | None,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        repo = MortuaryRepository(conn)
        repo.event("case", case_id, event_type, actor, payload, now)
        if orchestration_id is not None:
            repo.event("ceremony", orchestration_id, event_type, actor, payload, now)

    def _audit(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        principal: Principal | None,
        actor: str,
        *,
        action: str,
        resource_id: int | None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        outcome: str = "success",
    ) -> None:
        AuditService(conn, clock).record(
            AuditContext(principal.user_id if principal else None, actor),
            action=action,
            resource_type="ceremony_orchestration",
            resource_id=resource_id,
            outcome=outcome,
            before=before,
            after=after,
            metadata=metadata,
        )

    def _materialize_hold(
        self,
        conn: sqlite3.Connection,
        now: str,
        *,
        case_id: int,
        title: str,
        items: list[dict[str, Any]],
        hold_minutes: int,
        requested_by: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> int:
        hold_expires_at = to_storage(self.clock.now() + timedelta(minutes=hold_minutes))
        cursor = conn.execute(
            "INSERT INTO ceremony_orchestrations(case_id,title,status,requested_by,hold_expires_at,idempotency_key,request_digest,created_at,updated_at) "
            "VALUES(?,?,'held',?,?,?,?,?,?)",
            (case_id, title, requested_by, hold_expires_at, idempotency_key, fingerprint, now, now),
        )
        orchestration_id = int(cursor.lastrowid)
        for index, item in enumerate(items):
            reservation_key = f"orch-{orchestration_id}-{item['resource_id']}-{index}"
            reservation_cursor = conn.execute(
                "INSERT INTO facility_reservations(resource_id,case_id,start_at,end_at,purpose,status,created_by,idempotency_key,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'held',?,?,?,?)",
                (item["resource_id"], case_id, item["start_at"], item["end_at"], item["purpose"], requested_by, reservation_key, now, now),
            )
            conn.execute(
                "INSERT INTO ceremony_orchestration_items(orchestration_id,resource_id,reservation_id,start_at,end_at,purpose,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'held',?,?)",
                (orchestration_id, item["resource_id"], int(reservation_cursor.lastrowid), item["start_at"], item["end_at"], item["purpose"], now, now),
            )
        conn.execute(
            "UPDATE mortuary_cases SET status='services_planned',version=version+1,updated_at=? WHERE id=? AND status IN ('registered','in_custody')",
            (now, case_id),
        )
        return orchestration_id

    # -------------------------------------------------- 过期释放与候补推进

    def _expire_holds(self, conn: sqlite3.Connection, now: str) -> list[int]:
        repo = MortuaryRepository(conn)
        expired_ids: list[int] = []
        for hold in repo.expired_holds(now):
            conn.execute("UPDATE ceremony_orchestrations SET status='released',released_by=?,release_reason=?,released_at=?,version=version+1,updated_at=? WHERE id=?", (SYSTEM_ACTOR, "持有超时自动释放", now, now, hold["id"]))
            conn.execute("UPDATE ceremony_orchestration_items SET status='released',updated_at=? WHERE orchestration_id=?", (now, hold["id"]))
            conn.execute("UPDATE facility_reservations SET status='cancelled',updated_at=? WHERE id IN (SELECT reservation_id FROM ceremony_orchestration_items WHERE orchestration_id=?)", (now, hold["id"]))
            self._adjustment(conn, now, adjustment_type="hold.expired", actor=SYSTEM_ACTOR, orchestration_id=hold["id"], detail={"hold_expires_at": hold["hold_expires_at"]})
            self._events(conn, now, case_id=hold["case_id"], orchestration_id=hold["id"], event_type="ceremony.hold_expired", actor=SYSTEM_ACTOR, payload={"orchestration_id": hold["id"], "hold_expires_at": hold["hold_expires_at"]})
            self._audit(conn, self.clock, None, SYSTEM_ACTOR, action="ceremony.hold_expired", resource_id=hold["id"], before={"status": "held"}, after={"status": "released"}, metadata={"hold_expires_at": hold["hold_expires_at"]})
            expired_ids.append(int(hold["id"]))
        return expired_ids

    def _expire_waitlist_entries(self, conn: sqlite3.Connection, now: str) -> list[int]:
        repo = MortuaryRepository(conn)
        expired_ids: list[int] = []
        rows = conn.execute(
            "SELECT * FROM ceremony_waitlist_entries WHERE status='waiting' AND body_preserve_until<=? ORDER BY id",
            (now,),
        ).fetchall()
        for row in rows:
            entry = dict(row)
            conn.execute("UPDATE ceremony_waitlist_entries SET status='expired',updated_at=? WHERE id=?", (now, entry["id"]))
            self._adjustment(conn, now, adjustment_type="waitlist.expired", actor=SYSTEM_ACTOR, waitlist_entry_id=entry["id"], detail={"body_preserve_until": entry["body_preserve_until"]})
            self._events(conn, now, case_id=entry["case_id"], orchestration_id=None, event_type="ceremony.waitlist_expired", actor=SYSTEM_ACTOR, payload={"waitlist_entry_id": entry["id"], "body_preserve_until": entry["body_preserve_until"]})
            self._audit(conn, self.clock, None, SYSTEM_ACTOR, action="ceremony.waitlist_expired", resource_id=entry["id"], before={"status": "waiting"}, after={"status": "expired"}, metadata={"body_preserve_until": entry["body_preserve_until"]})
            expired_ids.append(int(entry["id"]))
        return expired_ids

    def _housekeeping(self, conn: sqlite3.Connection, now: str) -> dict[str, Any]:
        """释放超时持有并失效超过遗体保存期限的候补，返回受影响编号。"""
        expired_holds = self._expire_holds(conn, now)
        expired_waitlist = self._expire_waitlist_entries(conn, now)
        return {"expired_holds": expired_holds, "expired_waitlist": expired_waitlist}

    def _promote_waitlist(self, conn: sqlite3.Connection, now: str, *, trigger: str, actor: str) -> list[dict[str, Any]]:
        """按综合排序幂等推进候补；排在最前且容量仍不足者会拦住后续递补。"""
        repo = MortuaryRepository(conn)
        promotions: list[dict[str, Any]] = []
        for entry in repo.waiting_entries():
            requirements = json.loads(entry["requirements_json"])
            # 同事务内此前递补落库的 held 预约已对 conflicts() 可见，无需另行累计
            blocked = self._evaluate_fit(conn, requirements)
            if blocked:
                break
            orchestration_id = self._materialize_hold(
                conn,
                now,
                case_id=entry["case_id"],
                title=f"候补递补 #{entry['id']}",
                items=requirements,
                hold_minutes=int(entry["hold_minutes"]),
                requested_by=entry["requested_by"],
                idempotency_key=f"waitlist-{entry['id']}-hold",
                fingerprint=f"waitlist-promotion-{entry['id']}",
            )
            conn.execute(
                "UPDATE ceremony_waitlist_entries SET status='promoted',promoted_orchestration_id=?,promoted_at=?,promotion_token=?,updated_at=? WHERE id=?",
                (orchestration_id, now, f"promotion-{entry['id']}", now, entry["id"]),
            )
            self._adjustment(
                conn,
                now,
                adjustment_type="waitlist.promoted",
                actor=actor,
                orchestration_id=orchestration_id,
                waitlist_entry_id=entry["id"],
                detail={"trigger": trigger, "hold_expires_in_minutes": int(entry["hold_minutes"])},
            )
            self._events(
                conn,
                now,
                case_id=entry["case_id"],
                orchestration_id=orchestration_id,
                event_type="ceremony.hold_promoted",
                actor=actor,
                payload={"waitlist_entry_id": entry["id"], "orchestration_id": orchestration_id, "trigger": trigger},
            )
            self._audit(
                conn,
                self.clock,
                None,
                actor,
                action="ceremony.waitlist_promoted",
                resource_id=orchestration_id,
                after={"waitlist_entry_id": entry["id"], "status": "held"},
                metadata={"trigger": trigger, "waitlist_entry_id": entry["id"]},
            )
            promotions.append({"waitlist_entry_id": int(entry["id"]), "orchestration_id": orchestration_id})
        return promotions

    def maintenance_in_transaction(self, conn: sqlite3.Connection, actor: str, trigger: str) -> dict[str, Any]:
        now = self.now()
        swept = self._housekeeping(conn, now)
        promotions = self._promote_waitlist(conn, now, trigger=trigger, actor=actor)
        return {**swept, "promotions": promotions}

    # ------------------------------------------------------------- 整组占用

    def create_hold(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("mortuary.ceremony.schedule")
        with transaction(immediate=True) as conn:
            return self._create_hold(conn, principal, payload)

    def _create_hold(self, conn: sqlite3.Connection, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        actor = principal.display_name
        self._housekeeping(conn, now)
        case_id = int(payload["case_id"])
        self._case(conn, case_id)
        items = self._normalize_items(conn, payload["items"])
        hold_minutes = int(payload["hold_minutes"])
        if not MIN_HOLD_MINUTES <= hold_minutes <= MAX_HOLD_MINUTES:
            raise ValidationError(f"持有期限必须在 {MIN_HOLD_MINUTES} 到 {MAX_HOLD_MINUTES} 分钟之间")
        title = (payload.get("title") or "").strip()
        key = payload["idempotency_key"]
        fingerprint = self._requirements_fingerprint(items, hold_minutes, title)
        repo = MortuaryRepository(conn)
        existing = repo.orchestration_key(case_id, key)
        if existing:
            if existing["request_digest"] != fingerprint:
                raise ConflictError("同一幂等键对应了不同的整组占用内容")
            return self._orchestration_detail(conn, existing["id"])
        blocked = self._evaluate_fit(conn, items)
        if blocked:
            self._audit(conn, self.clock, principal, actor, action="ceremony.hold_blocked", resource_id=None, outcome="failure", metadata={"case_id": case_id, "blocked_resources": blocked, "idempotency_key": key})
            raise ConflictError("存在资源冲突，整组占用未创建", context={"blocked_resources": blocked})
        orchestration_id = self._materialize_hold(
            conn,
            now,
            case_id=case_id,
            title=title,
            items=items,
            hold_minutes=hold_minutes,
            requested_by=actor,
            idempotency_key=key,
            fingerprint=fingerprint,
        )
        self._adjustment(conn, now, adjustment_type="hold.created", actor=actor, orchestration_id=orchestration_id, detail={"hold_minutes": hold_minutes, "items": items})
        self._events(conn, now, case_id=case_id, orchestration_id=orchestration_id, event_type="ceremony.hold_created", actor=actor, payload={"orchestration_id": orchestration_id, "hold_expires_in_minutes": hold_minutes, "resources": [item["resource_code"] for item in items]})
        self._audit(conn, self.clock, principal, actor, action="ceremony.hold_created", resource_id=orchestration_id, after={"status": "held", "hold_minutes": hold_minutes}, metadata={"resources": [item["resource_code"] for item in items], "idempotency_key": key})
        return self._orchestration_detail(conn, orchestration_id)

    def confirm(self, principal: Principal, orchestration_id: int) -> dict[str, Any]:
        principal.require("mortuary.ceremony.confirm")
        with transaction(immediate=True) as conn:
            now = self.now()
            self._housekeeping(conn, now)
            repo = MortuaryRepository(conn)
            hold = repo.orchestration(orchestration_id)
            if hold is None:
                raise NotFoundError("整组占用不存在")
            if hold["status"] == "confirmed":
                return self._orchestration_detail(conn, orchestration_id)
            if hold["status"] == "released":
                raise ConflictError("整组占用已释放，不能确认")
            conn.execute("UPDATE ceremony_orchestrations SET status='confirmed',confirmed_by=?,confirmed_at=?,version=version+1,updated_at=? WHERE id=?", (principal.display_name, now, now, orchestration_id))
            conn.execute("UPDATE ceremony_orchestration_items SET status='confirmed',updated_at=? WHERE orchestration_id=?", (now, orchestration_id))
            conn.execute("UPDATE facility_reservations SET status='confirmed',updated_at=? WHERE id IN (SELECT reservation_id FROM ceremony_orchestration_items WHERE orchestration_id=?)", (now, orchestration_id))
            self._adjustment(conn, now, adjustment_type="hold.confirmed", actor=principal.display_name, orchestration_id=orchestration_id)
            self._events(conn, now, case_id=hold["case_id"], orchestration_id=orchestration_id, event_type="ceremony.confirmed", actor=principal.display_name, payload={"orchestration_id": orchestration_id})
            self._audit(conn, self.clock, principal, principal.display_name, action="ceremony.confirmed", resource_id=orchestration_id, before={"status": "held"}, after={"status": "confirmed"})
            return self._orchestration_detail(conn, orchestration_id)

    def release(self, principal: Principal, orchestration_id: int, reason: str) -> dict[str, Any]:
        principal.require("mortuary.ceremony.schedule")
        with transaction(immediate=True) as conn:
            return self._release(conn, principal, orchestration_id, reason)

    def _release(self, conn: sqlite3.Connection, principal: Principal | None, orchestration_id: int, reason: str) -> dict[str, Any]:
        now = self.now()
        actor = principal.display_name if principal else SYSTEM_ACTOR
        self._housekeeping(conn, now)
        repo = MortuaryRepository(conn)
        hold = repo.orchestration(orchestration_id)
        if hold is None:
            raise NotFoundError("整组占用不存在")
        if hold["status"] == "released":
            return self._orchestration_detail(conn, orchestration_id)
        before_status = hold["status"]
        conn.execute("UPDATE ceremony_orchestrations SET status='released',released_by=?,release_reason=?,released_at=?,version=version+1,updated_at=? WHERE id=?", (actor, reason, now, now, orchestration_id))
        conn.execute("UPDATE ceremony_orchestration_items SET status='released',updated_at=? WHERE orchestration_id=?", (now, orchestration_id))
        conn.execute("UPDATE facility_reservations SET status='cancelled',updated_at=? WHERE id IN (SELECT reservation_id FROM ceremony_orchestration_items WHERE orchestration_id=?)", (now, orchestration_id))
        self._adjustment(conn, now, adjustment_type="hold.released", actor=actor, orchestration_id=orchestration_id, detail={"reason": reason, "previous_status": before_status})
        self._events(conn, now, case_id=hold["case_id"], orchestration_id=orchestration_id, event_type="ceremony.released", actor=actor, payload={"orchestration_id": orchestration_id, "reason": reason})
        self._audit(conn, self.clock, principal, actor, action="ceremony.released", resource_id=orchestration_id, before={"status": before_status}, after={"status": "released"}, metadata={"reason": reason})
        self._promote_waitlist(conn, now, trigger="orchestration.released", actor=actor)
        return self._orchestration_detail(conn, orchestration_id)

    def preflight(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("mortuary.ceremony.read")
        with transaction(immediate=True) as conn:
            now = self.now()
            self._housekeeping(conn, now)
            if payload.get("case_id") is not None:
                self._case(conn, int(payload["case_id"]))
            items = self._normalize_items(conn, payload["items"])
            blocked = self._evaluate_fit(conn, items)
            return {"fits": not blocked, "checked_at": now, "blocked_resources": blocked}

    # ----------------------------------------------------------------- 候补

    def join_waitlist(self, principal: Principal, payload: dict[str, Any]) -> dict[str, Any]:
        principal.require("mortuary.ceremony.waitlist")
        with transaction(immediate=True) as conn:
            now = self.now()
            self._housekeeping(conn, now)
            case_id = int(payload["case_id"])
            self._case(conn, case_id)
            items = self._normalize_items(conn, payload["items"])
            body_preserve_until = to_storage(payload["body_preserve_until"])
            if body_preserve_until <= now:
                raise ValidationError("遗体保存期限不能早于当前时间")
            hold_minutes = int(payload["hold_minutes"])
            if not MIN_HOLD_MINUTES <= hold_minutes <= MAX_HOLD_MINUTES:
                raise ValidationError(f"递补后持有期限必须在 {MIN_HOLD_MINUTES} 到 {MAX_HOLD_MINUTES} 分钟之间")
            key = payload["idempotency_key"]
            repo = MortuaryRepository(conn)
            existing = repo.waitlist_key(case_id, key)
            if existing:
                if existing["requirements_json"] != self._dump_items(items):
                    raise ConflictError("同一幂等键对应了不同的候补内容")
                return self._waitlist_detail(conn, existing["id"])
            declared_urgency = int(payload.get("urgency_level", 0))
            cursor = conn.execute(
                "INSERT INTO ceremony_waitlist_entries(case_id,requested_by,requested_at,body_preserve_until,urgency_level,requirements_json,hold_minutes,idempotency_key,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (case_id, principal.display_name, now, body_preserve_until, declared_urgency, self._dump_items(items), hold_minutes, key, now, now),
            )
            entry_id = int(cursor.lastrowid)
            self._adjustment(conn, now, adjustment_type="waitlist.joined", actor=principal.display_name, waitlist_entry_id=entry_id, detail={"body_preserve_until": body_preserve_until, "declared_urgency_level": declared_urgency})
            self._events(conn, now, case_id=case_id, orchestration_id=None, event_type="ceremony.waitlist_joined", actor=principal.display_name, payload={"waitlist_entry_id": entry_id, "body_preserve_until": body_preserve_until})
            self._audit(conn, self.clock, principal, principal.display_name, action="ceremony.waitlist_joined", resource_id=entry_id, after={"body_preserve_until": body_preserve_until, "declared_urgency_level": declared_urgency}, metadata={"resources": [item["resource_code"] for item in items]})
            self._promote_waitlist(conn, now, trigger="waitlist.joined", actor=principal.display_name)
            return self._waitlist_detail(conn, entry_id)

    def review_urgency(self, principal: Principal, entry_id: int, urgency_level: int, note: str) -> dict[str, Any]:
        principal.require("mortuary.ceremony.review")
        with transaction(immediate=True) as conn:
            now = self.now()
            self._housekeeping(conn, now)
            repo = MortuaryRepository(conn)
            entry = repo.waitlist_entry(entry_id)
            if entry is None:
                raise NotFoundError("候补记录不存在")
            if entry["status"] != "waiting":
                raise ConflictError("只有处于候补状态的记录可以审核紧急等级")
            before = int(entry["urgency_level"])
            effective_before = before if entry["urgency_reviewed_at"] else 0
            conn.execute(
                "UPDATE ceremony_waitlist_entries SET urgency_level=?,urgency_reviewed_by=?,urgency_reviewed_at=?,updated_at=? WHERE id=?",
                (urgency_level, principal.display_name, now, now, entry_id),
            )
            self._adjustment(conn, now, adjustment_type="waitlist.urgency_reviewed", actor=principal.display_name, waitlist_entry_id=entry_id, detail={"before": before, "after": urgency_level, "effective_before": effective_before, "note": note})
            self._audit(conn, self.clock, principal, principal.display_name, action="ceremony.waitlist_urgency_reviewed", resource_id=entry_id, before={"urgency_level": before, "reviewed": bool(entry["urgency_reviewed_at"])}, after={"urgency_level": urgency_level, "reviewed": True}, metadata={"note": note})
            self._promote_waitlist(conn, now, trigger="waitlist.urgency_reviewed", actor=principal.display_name)
            return self._waitlist_detail(conn, entry_id)

    def override_order(self, principal: Principal, entry_id: int, position: int, reason: str) -> dict[str, Any]:
        principal.require("mortuary.ceremony.override")
        with transaction(immediate=True) as conn:
            now = self.now()
            self._housekeeping(conn, now)
            repo = MortuaryRepository(conn)
            entry = repo.waitlist_entry(entry_id)
            if entry is None:
                raise NotFoundError("候补记录不存在")
            if entry["status"] != "waiting":
                raise ConflictError("只有处于候补状态的记录可以人工越序")
            if position < 1:
                raise ValidationError("越序位次必须从 1 开始")
            pinned = [row["id"] for row in conn.execute("SELECT id FROM ceremony_waitlist_entries WHERE status='waiting' AND manual_seq>0 ORDER BY manual_seq,id").fetchall()]
            if entry_id in pinned:
                pinned.remove(entry_id)
            target_index = min(position, len(pinned) + 1) - 1
            pinned.insert(target_index, entry_id)
            for index, pinned_id in enumerate(pinned, start=1):
                conn.execute("UPDATE ceremony_waitlist_entries SET manual_seq=?,was_overridden=1,updated_at=? WHERE id=?", (index * 10, now, pinned_id))
            conn.execute(
                "INSERT INTO ceremony_waitlist_overrides(waitlist_entry_id,ordered_position,actor,reason,created_at) VALUES(?,?,?,?,?)",
                (entry_id, position, principal.display_name, reason, now),
            )
            self._adjustment(conn, now, adjustment_type="waitlist.overridden", actor=principal.display_name, waitlist_entry_id=entry_id, detail={"ordered_position": position, "reason": reason, "effective_pinned_position": target_index + 1})
            self._events(conn, now, case_id=entry["case_id"], orchestration_id=None, event_type="ceremony.waitlist_overridden", actor=principal.display_name, payload={"waitlist_entry_id": entry_id, "ordered_position": position, "reason": reason})
            self._audit(conn, self.clock, principal, principal.display_name, action="ceremony.waitlist_overridden", resource_id=entry_id, before={"manual_seq": entry["manual_seq"]}, after={"ordered_position": position, "pinned": True}, metadata={"reason": reason})
            self._promote_waitlist(conn, now, trigger="waitlist.overridden", actor=principal.display_name)
            return self._waitlist_detail(conn, entry_id)

    def cancel_waitlist(self, principal: Principal, entry_id: int, reason: str) -> dict[str, Any]:
        principal.require("mortuary.ceremony.waitlist")
        with transaction(immediate=True) as conn:
            now = self.now()
            self._housekeeping(conn, now)
            repo = MortuaryRepository(conn)
            entry = repo.waitlist_entry(entry_id)
            if entry is None:
                raise NotFoundError("候补记录不存在")
            if entry["status"] == "cancelled":
                return self._waitlist_detail(conn, entry_id)
            if entry["status"] != "waiting":
                raise ConflictError("只有处于候补状态的记录可以撤销")
            conn.execute(
                "UPDATE ceremony_waitlist_entries SET status='cancelled',cancelled_by=?,cancelled_reason=?,updated_at=? WHERE id=?",
                (principal.display_name, reason, now, entry_id),
            )
            self._adjustment(conn, now, adjustment_type="waitlist.cancelled", actor=principal.display_name, waitlist_entry_id=entry_id, detail={"reason": reason})
            self._audit(conn, self.clock, principal, principal.display_name, action="ceremony.waitlist_cancelled", resource_id=entry_id, before={"status": "waiting"}, after={"status": "cancelled"}, metadata={"reason": reason})
            self._promote_waitlist(conn, now, trigger="waitlist.cancelled", actor=principal.display_name)
            return self._waitlist_detail(conn, entry_id)

    def advance_waitlist(self, principal: Principal) -> dict[str, Any]:
        principal.require("mortuary.ceremony.review")
        with transaction(immediate=True) as conn:
            result = self.maintenance_in_transaction(conn, principal.display_name, "manual.advance")
            result["waiting"] = self.list_waiting(conn)
            return result

    # ----------------------------------------------------------------- 查询

    def _orchestration_detail(self, conn: sqlite3.Connection, orchestration_id: int) -> dict[str, Any]:
        repo = MortuaryRepository(conn)
        hold = repo.orchestration(orchestration_id)
        if hold is None:
            raise NotFoundError("整组占用不存在")
        items = repo.orchestration_items(orchestration_id)
        own_reservation_ids = {int(item["reservation_id"]) for item in items if item["reservation_id"]}
        blocked_resources: list[dict[str, Any]] = []
        for item in items:
            blockers = [
                row
                for row in repo.conflicts(int(item["resource_id"]), item["start_at"], item["end_at"])
                if row["id"] not in own_reservation_ids
            ]
            blocked_resources.append(
                {
                    "item_id": item["id"],
                    "resource_id": item["resource_id"],
                    "resource_code": item["resource_code"],
                    "resource_kind": item["resource_kind"],
                    "item_status": item["status"],
                    "start_at": item["start_at"],
                    "end_at": item["end_at"],
                    "contended": bool(blockers),
                    "blocking": [
                        {
                            "reservation_id": row["id"],
                            "case_id": row["case_id"],
                            "orchestration_id": row["orchestration_id"],
                            "reservation_status": row["status"],
                            "start_at": row["start_at"],
                            "end_at": row["end_at"],
                        }
                        for row in blockers
                    ],
                }
            )
        result = dict(hold)
        result["items"] = items
        result["blocked_resources"] = blocked_resources
        result["adjustments"] = repo.adjustments(orchestration_id=orchestration_id)
        result["events"] = repo.timeline("ceremony", orchestration_id)
        return result

    def get_orchestration(self, principal: Principal, orchestration_id: int) -> dict[str, Any]:
        principal.require("mortuary.ceremony.read")
        with transaction(immediate=True) as conn:
            self._housekeeping(conn, self.now())
            return self._orchestration_detail(conn, orchestration_id)

    def _waitlist_detail(self, conn: sqlite3.Connection, entry_id: int) -> dict[str, Any]:
        repo = MortuaryRepository(conn)
        entry = repo.waitlist_entry(entry_id)
        if entry is None:
            raise NotFoundError("候补记录不存在")
        result = dict(entry)
        requirements = json.loads(entry["requirements_json"])
        result["requirements"] = requirements
        result["urgency_effective"] = bool(entry["urgency_reviewed_at"])
        result["adjustments"] = repo.adjustments(waitlist_entry_id=entry_id)
        waiting = repo.waiting_entries()
        result["rank_position"] = next((index + 1 for index, row in enumerate(waiting) if row["id"] == entry_id), None)
        result["blocked_resources"] = self._evaluate_fit(conn, requirements) if entry["status"] == "waiting" else []
        return result

    def get_waitlist_entry(self, principal: Principal, entry_id: int) -> dict[str, Any]:
        principal.require("mortuary.ceremony.read")
        with transaction(immediate=True) as conn:
            self._housekeeping(conn, self.now())
            return self._waitlist_detail(conn, entry_id)

    def list_waiting(self, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if conn is None:
            with transaction(immediate=True) as connection:
                self._housekeeping(connection, self.now())
                rows = MortuaryRepository(connection).waiting_entries()
        else:
            rows = MortuaryRepository(conn).waiting_entries()
        result = []
        for position, row in enumerate(rows, start=1):
            item = dict(row)
            item["requirements"] = json.loads(item.pop("requirements_json"))
            item["rank_position"] = position
            item["urgency_effective"] = bool(item["urgency_reviewed_at"])
            result.append(item)
        return result

    def list_waiting_endpoint(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("mortuary.ceremony.read")
        return self.list_waiting()

    def case_ceremony_overview(self, principal: Principal, case_id: int) -> dict[str, Any]:
        principal.require("mortuary.ceremony.read")
        with transaction(immediate=True) as conn:
            self._housekeeping(conn, self.now())
            case = self._case(conn, case_id)
            repo = MortuaryRepository(conn)
            orchestration_ids = [row["id"] for row in conn.execute("SELECT id FROM ceremony_orchestrations WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]
            waitlist_ids = [row["id"] for row in conn.execute("SELECT id FROM ceremony_waitlist_entries WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]
            return {
                "case_id": case_id,
                "external_ref": case["external_ref"],
                "decedent_name": case["decedent_name"],
                "orchestrations": [self._orchestration_detail(conn, oid) for oid in orchestration_ids],
                "waitlist_entries": [self._waitlist_detail(conn, wid) for wid in waitlist_ids],
                "case_timeline": repo.timeline("case", case_id),
            }
