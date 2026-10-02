from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.database import get_connection, transaction
from app.mortuary.repository import MortuaryRepository

WAITING = "waiting"


def content_digest(payload: dict[str, Any]) -> str:
    canonical = {
        "case_id": payload["case_id"],
        "purpose": payload["purpose"],
        "urgency_level": payload["urgency_level"],
        "body_preservation_deadline": payload["body_preservation_deadline"],
        "resources": sorted(
            ({"resource_code": x["resource_code"], "start_at": x["start_at"], "end_at": x["end_at"]} for x in payload["resources"]),
            key=lambda x: x["resource_code"],
        ),
    }
    text = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class OrchestrationService:
    """跨资源整组占用、授权确认/释放与候补推进。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = MortuaryRepository(self.connection)
        self.repository.ensure_schema()

    def now(self) -> str:
        return to_storage(self.clock.now())

    # ---- 授权 ----
    def grant_actor(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            if not self._is_administrator(connection, payload["granted_by"]) and self._role(repo, payload["granted_by"]) != "approver":
                raise PermissionDeniedError("只有审批员或系统管理员可以授予编排角色")
            repo.grant_actor(payload["actor"], payload["role"], payload["granted_by"], now)
            return {"actor": payload["actor"], "role": payload["role"], "granted_by": payload["granted_by"]}

    @staticmethod
    def _role(repo: MortuaryRepository, actor: str) -> str | None:
        row = repo.actor_role(actor)
        return None if row is None else row["role"]

    @staticmethod
    def _is_administrator(connection: sqlite3.Connection, username: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM users u JOIN user_roles ur ON ur.user_id=u.id JOIN roles r ON r.id=ur.role_id "
            "WHERE u.username=? AND u.status='active' AND r.code='administrator' LIMIT 1",
            (username,),
        ).fetchone()
        return row is not None

    def _authorize(self, connection: sqlite3.Connection, actor: str, allowed: tuple[str, ...]) -> str:
        repo = MortuaryRepository(connection)
        role = self._role(repo, actor)
        if role in allowed:
            return role  # type: ignore[return-value]
        if "approver" in allowed and self._is_administrator(connection, actor):
            return "approver"
        raise PermissionDeniedError("当前人员没有该编排操作权限")

    # ---- 整组占用 ----
    def create_group(self, payload: dict[str, Any], *, as_waitlist: bool) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        normalized = self._normalize(payload)
        digest = content_digest(normalized)
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            existing = repo.group_by_idempotency(normalized["idempotency_key"])
            if existing is not None:
                if existing["content_digest"] != digest:
                    raise ConflictError("同一幂等键对应了不同的仪式资源内容")
                return self._detail(connection, repo, existing, now)
            if repo.case(normalized["case_id"]) is None:
                raise NotFoundError("逝者业务档案不存在")
            resolved = self._resolve_lines(repo, normalized["resources"])
            if as_waitlist:
                return self._insert_waitlisted(connection, repo, normalized, digest, now)
            blockers = self._blockers(repo, resolved)
            if blockers:
                raise ConflictError("整组资源存在冲突，未保留任何占用", context={"blocking_resources": blockers})
            return self._insert_held(connection, repo, normalized, digest, resolved, now_value, now, origin="request")

    def confirm_group(self, group_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            self._authorize(connection, payload["actor"], ("approver",))
            group = repo.group(group_id)
            if group is None:
                raise NotFoundError("整场仪式占用不存在")
            before = dict(group)
            if group["status"] == "confirmed":
                return self._detail(connection, repo, group, now)
            if group["status"] != "held":
                raise ConflictError("只有持有中的整组占用可以确认")
            if group["hold_expires_at"] and group["hold_expires_at"] <= now:
                raise ConflictError("整组占用已超过确认期限，请释放后重新安排")
            connection.execute("UPDATE facility_reservations SET status='confirmed',updated_at=? WHERE group_id=?", (now, group_id))
            connection.execute(
                "UPDATE ceremony_groups SET status='confirmed',confirmed_by=?,confirmed_at=?,version=version+1,updated_at=? WHERE id=?",
                (payload["actor"], now, now, group_id),
            )
            after = repo.group(group_id) or {}
            repo.intervention(group_id, payload["actor"], "ceremony.confirmed", "授权确认整组占用", before, after, now)
            repo.event("ceremony", group_id, "ceremony.confirmed", payload["actor"], {"resources": self._line_codes(after)}, now)
            repo.event("case", group["case_id"], "ceremony.confirmed", payload["actor"], {"group_id": group_id}, now)
            return self._detail(connection, repo, after, now)

    def release_group(self, group_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            self._authorize(connection, payload["actor"], ("approver",))
            group = repo.group(group_id)
            if group is None:
                raise NotFoundError("整场仪式占用不存在")
            if group["status"] in {"released", "expired"}:
                return self._detail(connection, repo, group, now)
            if group["status"] == "waitlisted":
                raise ConflictError("候补中的申请请使用候补取消接口")
            return self._release_and_promote(connection, repo, group, payload["actor"], payload["reason"], "ceremony.released", now_value, now)

    def get_group(self, group_id: int) -> dict[str, Any]:
        with transaction() as connection:
            repo = MortuaryRepository(connection)
            group = repo.group(group_id)
            if group is None:
                raise NotFoundError("整场仪式占用不存在")
            return self._detail(connection, repo, group, self.now())

    def list_groups(self, status: str | None = None, case_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if case_id is not None:
            clauses.append("case_id=?")
            params.append(case_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 500)))
        rows = self.connection.execute(f"SELECT * FROM ceremony_groups{where} ORDER BY id DESC LIMIT ?", params).fetchall()
        return [dict(row) for row in rows]

    # ---- 候补 ----
    def list_waitlist(self) -> dict[str, Any]:
        with transaction() as connection:
            repo = MortuaryRepository(connection)
            now = self.now()
            rows = repo.waitlist_view()
            waiting = [row for row in rows if row["status"] == WAITING]
            for index, row in enumerate(waiting, start=1):
                row["rank"] = index
            return {"items": rows, "waiting_total": len(waiting), "checked_at": now}

    def override_waitlist(self, group_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            self._authorize(connection, payload["actor"], ("approver",))
            group = repo.group(group_id)
            entry = repo.waitlist_entry(group_id)
            if group is None or entry is None:
                raise NotFoundError("候补记录不存在")
            if entry["status"] != WAITING:
                raise ConflictError("只有等待中的候补可以越序调整")
            target_id = payload.get("ahead_of_group_id")
            if target_id is not None:
                target_entry = repo.waitlist_entry(target_id)
                if target_entry is None:
                    raise NotFoundError("指定的参照候补不存在")
                if target_entry["status"] != WAITING:
                    raise ConflictError("参照候补已不在等待队列中")
                if target_id == group_id:
                    raise ValidationError("越序参照不能是候补自身")
            before_seq = self._lane(repo)
            ordered = [row["group_id"] for row in repo.active_waitlist()]
            if target_id is None:
                insert_at = 0
            else:
                insert_at = ordered.index(target_id)
            ordered.remove(group_id)
            ordered.insert(insert_at, group_id)
            after_seq = self._apply_lane(repo, ordered, group_id, now)
            after = repo.group(group_id) or {}
            repo.intervention(
                group_id, payload["actor"], "waitlist.override", payload["reason"],
                {"lane_order": before_seq}, {"lane_order": after_seq, "ahead_of_group_id": target_id}, now,
            )
            repo.event("ceremony", group_id, "waitlist.overridden", payload["actor"], {"reason": payload["reason"], "ahead_of_group_id": target_id}, now)
            return self._detail(connection, repo, after, now)

    def cancel_waitlist(self, group_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            self._authorize(connection, payload["actor"], ("family_service", "planner", "approver"))
            group = repo.group(group_id)
            entry = repo.waitlist_entry(group_id)
            if group is None or entry is None:
                raise NotFoundError("候补记录不存在")
            if entry["status"] != WAITING:
                return self._detail(connection, repo, group, now)
            connection.execute(
                "UPDATE ceremony_waitlist SET status='cancelled',cancelled_by=?,cancel_reason=?,updated_at=? WHERE group_id=?",
                (payload["actor"], payload["reason"], now, group_id),
            )
            connection.execute(
                "UPDATE ceremony_groups SET status='released',released_by=?,released_at=?,release_reason=?,version=version+1,updated_at=? WHERE id=?",
                (payload["actor"], now, payload["reason"], now, group_id),
            )
            after = repo.group(group_id) or {}
            repo.intervention(group_id, payload["actor"], "waitlist.cancel", payload["reason"], {"status": "waitlisted"}, after, now)
            repo.event("ceremony", group_id, "waitlist.cancelled", payload["actor"], {"reason": payload["reason"]}, now)
            return self._detail(connection, repo, after, now)

    def review_urgency(self, group_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            self._authorize(connection, payload["actor"], ("approver",))
            group = repo.group(group_id)
            if group is None:
                raise NotFoundError("整场仪式占用不存在")
            if group["status"] not in {"waitlisted", "held"}:
                raise ConflictError("当前状态不能调整紧急等级")
            before = {"urgency_level": group["urgency_level"], "urgency_reviewed_by": group["urgency_reviewed_by"]}
            connection.execute(
                "UPDATE ceremony_groups SET urgency_level=?,urgency_reviewed_by=?,urgency_reviewed_at=?,version=version+1,updated_at=? WHERE id=?",
                (payload["urgency_level"], payload["actor"], now, now, group_id),
            )
            after = repo.group(group_id) or {}
            repo.intervention(group_id, payload["actor"], "urgency.review", "审核紧急等级", before, {"urgency_level": payload["urgency_level"]}, now)
            repo.event("ceremony", group_id, "urgency.reviewed", payload["actor"], {"urgency_level": payload["urgency_level"]}, now)
            return self._detail(connection, repo, after, now)

    # ---- 候补推进与过期回收 ----
    def promote_waitlist(self, payload: dict[str, Any]) -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            self._authorize(connection, payload["actor"], ("planner", "approver"))
            existing = repo.promotion_run(payload["idempotency_key"])
            if existing is not None:
                result = json.loads(existing["result_json"])
                result["idempotent_replay"] = True
                return result
            promoted, blocked = self._promote(connection, repo, now_value, now, trigger="manual")
            result = {"promoted": promoted, "still_blocked": blocked, "promoted_at": now}
            repo.save_promotion_run(payload["idempotency_key"], "manual", payload["actor"], result, promoted, now)
            return result

    def expire_holds(self, actor: str = "system-scheduler") -> dict[str, Any]:
        now_value = self.clock.now()
        now = to_storage(now_value)
        with transaction(immediate=True) as connection:
            repo = MortuaryRepository(connection)
            expired_rows = connection.execute(
                "SELECT * FROM ceremony_groups WHERE status='held' AND hold_expires_at IS NOT NULL AND hold_expires_at<=? ORDER BY id",
                (now,),
            ).fetchall()
            expired: list[int] = []
            for row in expired_rows:
                group = dict(row)
                connection.execute("UPDATE facility_reservations SET status='cancelled',updated_at=? WHERE group_id=?", (now, group["id"]))
                connection.execute(
                    "UPDATE ceremony_groups SET status='expired',updated_at=?,version=version+1 WHERE id=?",
                    (now, group["id"]),
                )
                after = repo.group(group["id"]) or {}
                repo.intervention(group["id"], actor, "hold.expired", "确认期限届满自动释放", group, after, now)
                repo.event("ceremony", group["id"], "ceremony.expired", actor, {"hold_expires_at": group["hold_expires_at"]}, now)
                repo.event("case", group["case_id"], "ceremony.expired", actor, {"group_id": group["id"]}, now)
                expired.append(group["id"])
            promoted: list[int] = []
            blocked: list[dict[str, Any]] = []
            if expired:
                promoted, blocked = self._promote(connection, repo, now_value, now, trigger="hold_expired")
            result = {"expired": expired, "promoted": promoted, "still_blocked": blocked, "swept_at": now}
            if expired:
                repo.save_promotion_run(f"expiry-sweep:{now}", "hold_expired", actor, result, promoted, now)
            return result

    # ---- 内部实现 ----
    @staticmethod
    def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "case_id": payload["case_id"],
            "purpose": payload["purpose"],
            "created_by": payload["created_by"],
            "idempotency_key": payload["idempotency_key"],
            "hold_ttl_minutes": payload["hold_ttl_minutes"],
            "body_preservation_deadline": to_storage(payload["body_preservation_deadline"]),
            "urgency_level": payload["urgency_level"],
            "urgency_reviewed_by": payload.get("urgency_reviewed_by", ""),
            "resources": [
                {"resource_code": line["resource_code"], "start_at": to_storage(line["start_at"]), "end_at": to_storage(line["end_at"])}
                for line in payload["resources"]
            ],
        }

    @staticmethod
    def _resolve_lines(repo: MortuaryRepository, lines: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, str]]:
        resolved: list[tuple[dict[str, Any], str, str]] = []
        for line in lines:
            resource = repo.resource_code(line["resource_code"])
            if resource is None or not resource["active"]:
                raise NotFoundError(f"设施资源不存在或已停用：{line['resource_code']}")
            resolved.append((resource, line["start_at"], line["end_at"]))
        return resolved

    @staticmethod
    def _blockers(repo: MortuaryRepository, resolved: list[tuple[dict[str, Any], str, str]]) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for resource, start_at, end_at in resolved:
            holds = repo.overlapping_holds(resource["id"], start_at, end_at)
            if len(holds) >= int(resource["capacity"]):
                blockers.append({
                    "resource_code": resource["code"], "kind": resource["kind"],
                    "start_at": start_at, "end_at": end_at,
                    "held_by_groups": sorted({hold["group_id"] for hold in holds if hold["group_id"] is not None}),
                    "reservation_ids": [hold["id"] for hold in holds],
                })
        return blockers

    def _insert_held(self, connection: sqlite3.Connection, repo: MortuaryRepository, payload: dict[str, Any], digest: str,
                     resolved: list[tuple[dict[str, Any], str, str]], now_value, now: str, *, origin: str) -> dict[str, Any]:
        hold_expires_at = to_storage(now_value + timedelta(minutes=payload["hold_ttl_minutes"]))
        cursor = connection.execute(
            "INSERT INTO ceremony_groups(case_id,purpose,status,idempotency_key,content_digest,hold_ttl_minutes,hold_expires_at,"
            "body_preservation_deadline,urgency_level,urgency_reviewed_by,urgency_reviewed_at,requested_lines_json,created_by,created_at,updated_at) "
            "VALUES(?,?, 'held',?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload["case_id"], payload["purpose"], payload["idempotency_key"], digest, payload["hold_ttl_minutes"], hold_expires_at,
             payload["body_preservation_deadline"], payload["urgency_level"], payload["urgency_reviewed_by"],
             now if payload["urgency_level"] > 0 else None,
             json.dumps(payload["resources"], ensure_ascii=False, sort_keys=True), payload["created_by"], now, now),
        )
        group_id = int(cursor.lastrowid)
        self._insert_reservations(connection, payload, resolved, group_id, now)
        connection.execute(
            "UPDATE mortuary_cases SET status='services_planned',version=version+1,updated_at=? WHERE id=? AND status IN ('registered','in_custody')",
            (now, payload["case_id"]),
        )
        group = repo.group(group_id) or {}
        repo.event("ceremony", group_id, "ceremony.held", payload["created_by"],
                   {"resources": self._line_codes(group), "hold_expires_at": hold_expires_at, "origin": origin}, now)
        repo.event("case", payload["case_id"], "ceremony.held", payload["created_by"], {"group_id": group_id, "origin": origin}, now)
        return self._detail(connection, repo, group, now)

    def _insert_waitlisted(self, connection: sqlite3.Connection, repo: MortuaryRepository, payload: dict[str, Any], digest: str, now: str) -> dict[str, Any]:
        cursor = connection.execute(
            "INSERT INTO ceremony_groups(case_id,purpose,status,idempotency_key,content_digest,hold_ttl_minutes,hold_expires_at,"
            "body_preservation_deadline,urgency_level,urgency_reviewed_by,urgency_reviewed_at,requested_lines_json,created_by,created_at,updated_at) "
            "VALUES(?,?,'waitlisted',?,?,?,NULL,?,?,?,?,?,?,?,?)",
            (payload["case_id"], payload["purpose"], payload["idempotency_key"], digest, payload["hold_ttl_minutes"],
             payload["body_preservation_deadline"], payload["urgency_level"], payload["urgency_reviewed_by"],
             now if payload["urgency_level"] > 0 else None,
             json.dumps(payload["resources"], ensure_ascii=False, sort_keys=True), payload["created_by"], now, now),
        )
        group_id = int(cursor.lastrowid)
        connection.execute("INSERT INTO ceremony_waitlist(group_id,created_at,updated_at) VALUES(?,?,?)", (group_id, now, now))
        group = repo.group(group_id) or {}
        repo.event("ceremony", group_id, "waitlist.joined", payload["created_by"], {"resources": self._line_codes(group)}, now)
        return self._detail(connection, repo, group, now)

    @staticmethod
    def _insert_reservations(connection: sqlite3.Connection, payload: dict[str, Any],
                             resolved: list[tuple[dict[str, Any], str, str]], group_id: int, now: str) -> None:
        for (resource, start_at, end_at), line in zip(resolved, payload["resources"]):
            connection.execute(
                "INSERT INTO facility_reservations(resource_id,case_id,group_id,start_at,end_at,purpose,status,created_by,idempotency_key,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'held',?,?,?,?)",
                (resource["id"], payload["case_id"], group_id, start_at, end_at, payload["purpose"],
                 payload["created_by"], f"{payload['idempotency_key']}#{line['resource_code']}", now, now),
            )

    def _release_and_promote(self, connection: sqlite3.Connection, repo: MortuaryRepository, group: dict[str, Any],
                             actor: str, reason: str, action: str, now_value, now: str) -> dict[str, Any]:
        before = dict(group)
        connection.execute("UPDATE facility_reservations SET status='cancelled',updated_at=? WHERE group_id=?", (now, group["id"]))
        connection.execute(
            "UPDATE ceremony_groups SET status='released',released_by=?,released_at=?,release_reason=?,version=version+1,updated_at=? WHERE id=?",
            (actor, now, reason, now, group["id"]),
        )
        after = repo.group(group["id"]) or {}
        repo.intervention(group["id"], actor, action, reason, before, after, now)
        repo.event("ceremony", group["id"], "ceremony.released", actor, {"reason": reason}, now)
        repo.event("case", group["case_id"], "ceremony.released", actor, {"group_id": group["id"], "reason": reason}, now)
        promoted, blocked = self._promote(connection, repo, now_value, now, trigger="release")
        run = {"promoted": promoted, "still_blocked": blocked, "released_group": group["id"], "released_at": now}
        repo.save_promotion_run(f"release:{group['id']}", "release", actor, run, promoted, now)
        return self._detail(connection, repo, after, now, promotion=run)

    def _promote(self, connection: sqlite3.Connection, repo: MortuaryRepository, now_value, now: str, *, trigger: str) -> tuple[list[int], list[dict[str, Any]]]:
        promoted: list[int] = []
        blocked: list[dict[str, Any]] = []
        for row in repo.active_waitlist():
            group_id = int(row["gid"])
            lines = json.loads(row["requested_lines_json"])
            try:
                resolved = self._resolve_lines(repo, lines)
            except NotFoundError as exc:
                blocked.append({"group_id": group_id, "reason": exc.message})
                continue
            blockers = self._blockers(repo, resolved)
            if blockers:
                blocked.append({"group_id": group_id, "blocking_resources": blockers})
                continue
            payload = {
                "case_id": row["case_id"], "purpose": row["purpose"], "created_by": row["created_by"],
                "idempotency_key": row["idempotency_key"], "hold_ttl_minutes": int(row["hold_ttl_minutes"]),
                "body_preservation_deadline": row["body_preservation_deadline"],
                "urgency_level": int(row["urgency_level"]), "urgency_reviewed_by": row["urgency_reviewed_by"],
                "resources": lines,
            }
            hold_expires_at = to_storage(now_value + timedelta(minutes=payload["hold_ttl_minutes"]))
            connection.execute(
                "UPDATE ceremony_groups SET status='held',hold_expires_at=?,version=version+1,updated_at=? WHERE id=? AND status='waitlisted'",
                (hold_expires_at, now, group_id),
            )
            self._insert_reservations(connection, payload, resolved, group_id, now)
            connection.execute(
                "UPDATE ceremony_waitlist SET status='promoted',promoted_at=?,updated_at=? WHERE group_id=? AND status='waiting'",
                (now, now, group_id),
            )
            connection.execute(
                "UPDATE mortuary_cases SET status='services_planned',version=version+1,updated_at=? WHERE id=? AND status IN ('registered','in_custody')",
                (now, payload["case_id"]),
            )
            group = repo.group(group_id) or {}
            repo.event("ceremony", group_id, "ceremony.promoted", "waitlist-promoter",
                       {"trigger": trigger, "hold_expires_at": hold_expires_at, "resources": self._line_codes(group)}, now)
            repo.event("case", payload["case_id"], "ceremony.promoted", "waitlist-promoter", {"group_id": group_id, "trigger": trigger}, now)
            promoted.append(group_id)
        return promoted, blocked

    @staticmethod
    def _lane(repo: MortuaryRepository) -> list[int]:
        return [int(row["group_id"]) for row in repo.active_waitlist() if row["override_seq"] is not None]

    @staticmethod
    def _apply_lane(repo: MortuaryRepository, ordered: list[int], moved_group: int, now: str) -> list[int]:
        lane_identities = {int(row["group_id"]) for row in repo.active_waitlist() if row["override_seq"] is not None}
        lane_identities.add(moved_group)
        lane = [group_id for group_id in ordered if group_id in lane_identities]
        for index, group_id in enumerate(lane, start=1):
            connection = repo.connection
            connection.execute("UPDATE ceremony_waitlist SET override_seq=?,updated_at=? WHERE group_id=?", (index * 10, now, group_id))
        return lane

    @staticmethod
    def _line_codes(group: dict[str, Any]) -> list[str]:
        try:
            return [line["resource_code"] for line in json.loads(group.get("requested_lines_json") or "[]")]
        except (TypeError, ValueError):
            return []

    def _detail(self, connection: sqlite3.Connection, repo: MortuaryRepository, group: dict[str, Any], now: str, *, promotion: dict[str, Any] | None = None) -> dict[str, Any]:
        group_id = int(group["id"])
        result = dict(group)
        result["requested_resources"] = json.loads(group.get("requested_lines_json") or "[]")
        result["lines"] = repo.group_lines(group_id)
        entry = repo.waitlist_entry(group_id)
        result["waitlist"] = entry
        if entry is not None and entry["status"] == WAITING:
            result["waitlist_rank"] = self._rank(repo, group_id)
            resolved = self._safe_resolve(repo, result["requested_resources"])
            result["blocking_resources"] = self._blockers(repo, resolved) if resolved else []
        result["interventions"] = repo.interventions(group_id)
        result["timeline"] = repo.timeline("ceremony", group_id)
        result["expired"] = bool(group["status"] == "held" and group["hold_expires_at"] and group["hold_expires_at"] <= now)
        if promotion is not None:
            result["promotion"] = promotion
        return result

    @staticmethod
    def _rank(repo: MortuaryRepository, group_id: int) -> int | None:
        for index, row in enumerate(repo.active_waitlist(), start=1):
            if int(row["gid"]) == group_id:
                return index
        return None

    @staticmethod
    def _safe_resolve(repo: MortuaryRepository, lines: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, str]]:
        try:
            return OrchestrationService._resolve_lines(repo, lines)
        except NotFoundError:
            return []
