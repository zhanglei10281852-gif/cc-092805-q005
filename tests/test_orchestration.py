from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock
from app.database import close_connection, get_connection
from app.mortuary.orchestration import OrchestrationService


def make_case(client, ref: str) -> dict:
    response = client.post("/api/mortuary/cases?actor=intake-clerk", json={
        "external_ref": ref, "decedent_name": "告别家属", "identity_number": f"ID-{ref}",
        "death_time": "2026-09-27T08:30:00Z", "received_from": "合作医院",
        "family_contact": "家属", "family_phone": "13800000000", "special_notes": "",
    })
    assert response.status_code == 201, response.text
    return response.json()


def make_resource(client, code: str, kind: str, capacity: int = 1) -> None:
    response = client.post("/api/mortuary/resources?actor=scheduler", json={
        "code": code, "name": f"资源{code}", "kind": kind, "site_code": "SITE-1", "capacity": capacity, "attributes": {},
    })
    assert response.status_code == 201, response.text


def setup_roles(client, admin) -> None:
    for actor, role in (("boss-wang", "approver"), ("planner-li", "planner"), ("family-zhao", "family_service")):
        response = client.post("/api/mortuary/orchestration/actors/grant", json={
            "actor": actor, "role": role, "granted_by": "admin",
        })
        assert response.status_code == 200, response.text


def group_payload(case_id: int, key: str, *, codes=("HALL-A", "VAN-1", "TEAM-A", "FURNACE-1"),
                  hour: int = 9, urgency: int = 0, reviewer: str = "", deadline: str = "2026-10-05T08:00:00Z",
                  ttl: int = 30, created_by: str = "planner-li") -> dict:
    return {
        "case_id": case_id, "purpose": "大型告别仪式", "created_by": created_by, "idempotency_key": key,
        "hold_ttl_minutes": ttl, "body_preservation_deadline": deadline,
        "urgency_level": urgency, "urgency_reviewed_by": reviewer,
        "resources": [
            {"resource_code": code, "start_at": f"2026-10-03T{hour:02d}:00:00Z", "end_at": f"2026-10-03T{hour + 1:02d}:00:00Z"}
            for code in codes
        ],
    }


def test_group_hold_confirm_requires_permission_and_confirms_all_lines(client, admin):
    setup_roles(client, admin)
    for code, kind in (("HALL-A", "farewell_hall"), ("VAN-1", "vehicle"), ("TEAM-A", "burial_team"), ("FURNACE-1", "cremator")):
        make_resource(client, code, kind)
    case = make_case(client, "CASE-G-001")
    payload = group_payload(case["id"], "group-hold-001")

    held = client.post("/api/mortuary/ceremony-groups", json=payload)
    assert held.status_code == 201, held.text
    group = held.json()
    assert group["status"] == "held"
    assert group["hold_expires_at"]
    assert {line["resource_code"] for line in group["lines"]} == {"HALL-A", "VAN-1", "TEAM-A", "FURNACE-1"}
    assert all(line["status"] == "held" for line in group["lines"])

    # 未经授权的人员不能确认
    denied = client.post(f"/api/mortuary/ceremony-groups/{group['id']}/confirm", json={"actor": "planner-li"})
    assert denied.status_code == 403

    confirmed = client.post(f"/api/mortuary/ceremony-groups/{group['id']}/confirm", json={"actor": "boss-wang"})
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["status"] == "confirmed"
    assert result["confirmed_by"] == "boss-wang"
    assert all(line["status"] == "confirmed" for line in result["lines"])
    assert [event["event_type"] for event in result["timeline"]] == ["ceremony.held", "ceremony.confirmed"]
    assert result["interventions"][0]["action"] == "ceremony.confirmed"


def test_any_resource_conflict_rolls_back_entire_group(client, admin):
    setup_roles(client, admin)
    make_resource(client, "HALL-A", "farewell_hall")
    make_resource(client, "VAN-1", "vehicle")
    make_resource(client, "TEAM-A", "burial_team")
    case_a = make_case(client, "CASE-G-002")
    case_b = make_case(client, "CASE-G-003")

    first = client.post("/api/mortuary/ceremony-groups", json=group_payload(case_a["id"], "group-atomic-1", codes=("HALL-A", "VAN-1", "TEAM-A")))
    assert first.status_code == 201
    # 第二个申请只在 HALL-A 的 09:00 时段冲突，车辆/人员改在 10:00 时段（首尾相接不算重叠）
    second_payload = group_payload(case_b["id"], "group-atomic-2", codes=("HALL-A", "VAN-1", "TEAM-A"))
    second_payload["resources"] = [
        {"resource_code": "HALL-A", "start_at": "2026-10-03T09:00:00Z", "end_at": "2026-10-03T10:00:00Z"},
        {"resource_code": "VAN-1", "start_at": "2026-10-03T10:00:00Z", "end_at": "2026-10-03T11:00:00Z"},
        {"resource_code": "TEAM-A", "start_at": "2026-10-03T10:00:00Z", "end_at": "2026-10-03T11:00:00Z"},
    ]
    second = client.post("/api/mortuary/ceremony-groups", json=second_payload)
    assert second.status_code == 409
    blockers = second.json()["error"]["context"]["blocking_resources"]
    assert [item["resource_code"] for item in blockers] == ["HALL-A"]

    # 数据库中不能留下第二个申请的任何预约或整组记录
    db = get_connection()
    leftovers = db.execute("SELECT COUNT(*) FROM facility_reservations WHERE idempotency_key LIKE ?", ("group-atomic-2#%",)).fetchone()[0]
    assert leftovers == 0
    assert client.get("/api/mortuary/ceremony-groups?case_id={}".format(case_b["id"])).json() == []

    # 持有的占用同样阻塞旧的单项预约接口
    legacy = client.post("/api/mortuary/reservations", json={
        "resource_code": "HALL-A", "case_id": case_b["id"], "start_at": "2026-10-03T09:30:00Z",
        "end_at": "2026-10-03T10:00:00Z", "purpose": "临时告别", "created_by": "planner-li", "idempotency_key": "legacy-block-001",
    })
    assert legacy.status_code == 409

    # 整体释放后，同一申请可以成功，且不产生部分成功残留
    released = client.post(f"/api/mortuary/ceremony-groups/{first.json()['id']}/release", json={"actor": "boss-wang", "reason": "家属改期"})
    assert released.status_code == 200
    retry = client.post("/api/mortuary/ceremony-groups", json=group_payload(case_b["id"], "group-atomic-2", codes=("HALL-A", "VAN-1", "TEAM-A")))
    assert retry.status_code == 201


def test_create_idempotency_rejects_changed_content(client, admin):
    setup_roles(client, admin)
    make_resource(client, "HALL-A", "farewell_hall")
    case = make_case(client, "CASE-G-004")
    payload = group_payload(case["id"], "group-idem-1", codes=("HALL-A",))
    first = client.post("/api/mortuary/ceremony-groups", json=payload)
    assert first.status_code == 201
    repeated = client.post("/api/mortuary/ceremony-groups", json=payload)
    assert repeated.status_code == 201 and repeated.json()["id"] == first.json()["id"]

    changed = group_payload(case["id"], "group-idem-1", codes=("HALL-A",), hour=11)
    conflict = client.post("/api/mortuary/ceremony-groups", json=changed)
    assert conflict.status_code == 409


def test_waitlist_ordering_override_and_idempotent_promotion(client, admin):
    setup_roles(client, admin)
    make_resource(client, "HALL-A", "farewell_hall")
    case_a = make_case(client, "CASE-W-001")
    case_b = make_case(client, "CASE-W-002")
    case_c = make_case(client, "CASE-W-003")
    case_d = make_case(client, "CASE-W-004")

    # A 先占住 11:00-12:00 的送别厅
    holder = client.post("/api/mortuary/ceremony-groups", json=group_payload(case_a["id"], "wl-holder", codes=("HALL-A",), hour=11))
    assert holder.status_code == 201

    b = client.post("/api/mortuary/ceremony-groups/waitlist", json=group_payload(case_b["id"], "wl-b-0001", codes=("HALL-A",), hour=11, deadline="2026-10-05T08:00:00Z"))
    c = client.post("/api/mortuary/ceremony-groups/waitlist", json=group_payload(case_c["id"], "wl-c-0001", codes=("HALL-A",), hour=11, urgency=80, reviewer="boss-wang", deadline="2026-10-08T08:00:00Z"))
    d = client.post("/api/mortuary/ceremony-groups/waitlist", json=group_payload(case_d["id"], "wl-d-0001", codes=("HALL-A",), hour=11, deadline="2026-10-03T08:00:00Z"))
    assert b.status_code == c.status_code == d.status_code == 201
    b_id, c_id, d_id = b.json()["id"], c.json()["id"], d.json()["id"]

    waiting = client.get("/api/mortuary/waitlist").json()["items"]
    ranks = {row["group_id"]: row["rank"] for row in waiting if row["status"] == "waiting"}
    # 紧急等级优先（C），同级遗体保存期限更早优先（D 早于 B）
    assert [ranks[c_id], ranks[d_id], ranks[b_id]] == [1, 2, 3]

    # 容量未释放时推进：全部仍阻塞，且推进幂等
    run_body = {"idempotency_key": "promote-run-1", "actor": "planner-li"}
    blocked_run = client.post("/api/mortuary/waitlist/promote", json=run_body)
    assert blocked_run.status_code == 200
    assert blocked_run.json()["promoted"] == []
    assert {item["group_id"] for item in blocked_run.json()["still_blocked"]} == {b_id, c_id, d_id}
    replay = client.post("/api/mortuary/waitlist/promote", json=run_body)
    assert replay.status_code == 200 and replay.json().get("idempotent_replay") is True

    # 家属服务人员可以查询阻塞资源与候补位置，但不能推进队列
    detail_b = client.get(f"/api/mortuary/ceremony-groups/{b_id}").json()
    assert detail_b["waitlist_rank"] == 3
    assert detail_b["blocking_resources"][0]["resource_code"] == "HALL-A"
    forbidden = client.post("/api/mortuary/waitlist/promote", json={"idempotency_key": "promote-run-x", "actor": "family-zhao"})
    assert forbidden.status_code == 403

    # 审批员人工越序：把 B 提到 C 前面，必须写理由并进入审计
    no_reason = client.post(f"/api/mortuary/ceremony-groups/{b_id}/waitlist/override", json={"actor": "boss-wang", "reason": "", "ahead_of_group_id": c_id})
    assert no_reason.status_code == 422
    override = client.post(f"/api/mortuary/ceremony-groups/{b_id}/waitlist/override", json={"actor": "boss-wang", "reason": "逝者为见义勇为人员，经治丧办核准优先", "ahead_of_group_id": c_id})
    assert override.status_code == 200, override.text
    ranks_after = {row["group_id"]: row["rank"] for row in client.get("/api/mortuary/waitlist").json()["items"] if row["status"] == "waiting"}
    assert [ranks_after[b_id], ranks_after[c_id], ranks_after[d_id]] == [1, 2, 3]
    intervention = client.get(f"/api/mortuary/ceremony-groups/{b_id}").json()["interventions"][-1]
    assert intervention["action"] == "waitlist.override" and intervention["reason"]
    # 非审批员不能越序
    assert client.post(f"/api/mortuary/ceremony-groups/{d_id}/waitlist/override", json={"actor": "planner-li", "reason": "试图插队的理由说明文字", "ahead_of_group_id": b_id}).status_code == 403

    # 释放 A：级联推进只放进队首 B；B 占用后 C、D 继续等待
    release = client.post(f"/api/mortuary/ceremony-groups/{holder.json()['id']}/release", json={"actor": "boss-wang", "reason": "家属取消"})
    assert release.status_code == 200
    assert release.json()["promotion"]["promoted"] == [b_id]
    assert client.get(f"/api/mortuary/ceremony-groups/{b_id}").json()["status"] == "held"
    ranks_tail = {row["group_id"]: row["rank"] for row in client.get("/api/mortuary/waitlist").json()["items"] if row["status"] == "waiting"}
    assert ranks_tail == {c_id: 1, d_id: 2}

    # 释放 B 后 C 自动顶上；再释放 C 后 D 顶上（释放即级联推进）
    client.post(f"/api/mortuary/ceremony-groups/{b_id}/release", json={"actor": "boss-wang", "reason": "再次调整"})
    assert client.get(f"/api/mortuary/ceremony-groups/{c_id}").json()["status"] == "held"
    client.post(f"/api/mortuary/ceremony-groups/{c_id}/release", json={"actor": "boss-wang", "reason": "流程结束"})
    assert client.get(f"/api/mortuary/ceremony-groups/{d_id}").json()["status"] == "held"


def test_waitlist_cancel_and_urgency_review_are_authorized(client, admin):
    setup_roles(client, admin)
    make_resource(client, "HALL-A", "farewell_hall")
    case = make_case(client, "CASE-W-005")
    holder = client.post("/api/mortuary/ceremony-groups", json=group_payload(make_case(client, "CASE-W-006")["id"], "wl-hold-x", codes=("HALL-A",), hour=14))
    waiting = client.post("/api/mortuary/ceremony-groups/waitlist", json=group_payload(case["id"], "wl-x-0001", codes=("HALL-A",), hour=14))
    group_id = waiting.json()["id"]

    # 家属服务人员可以取消候补
    cancelled = client.post(f"/api/mortuary/ceremony-groups/{group_id}/waitlist/cancel", json={"actor": "family-zhao", "reason": "家属放弃治丧档期"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "released"

    # 紧急等级审核仅审批员可做，且候补/持有中才允许
    review = client.post(f"/api/mortuary/ceremony-groups/{group_id}/urgency-review", json={"actor": "boss-wang", "urgency_level": 90})
    assert review.status_code == 409
    assert holder.status_code == 201


def test_expired_hold_is_released_and_waitlist_promoted(client, admin):
    setup_roles(client, admin)
    make_resource(client, "HALL-A", "farewell_hall")
    case_a = make_case(client, "CASE-E-001")
    case_b = make_case(client, "CASE-E-002")
    holder = client.post("/api/mortuary/ceremony-groups", json=group_payload(case_a["id"], "exp-holder", codes=("HALL-A",), hour=15, ttl=30))
    waiting = client.post("/api/mortuary/ceremony-groups/waitlist", json=group_payload(case_b["id"], "exp-wait", codes=("HALL-A",), hour=15, ttl=30))
    holder_id, waiting_id = holder.json()["id"], waiting.json()["id"]

    # 用冻结时钟模拟 TTL 到期：超过期限后整组被系统清扫并推进候补
    frozen = FrozenClock(datetime.now(UTC) + timedelta(minutes=31))
    service = OrchestrationService(get_connection(), frozen)
    detail = service.get_group(holder_id)
    assert detail["expired"] is True

    sweep = service.expire_holds("system-scheduler")
    assert holder_id in sweep["expired"]
    assert waiting_id in sweep["promoted"]
    promoted = service.get_group(waiting_id)
    assert promoted["status"] == "held"
    assert service.get_group(holder_id)["status"] == "expired"


def test_state_survives_restart(client, admin):
    setup_roles(client, admin)
    make_resource(client, "HALL-A", "farewell_hall")
    case = make_case(client, "CASE-P-001")
    held = client.post("/api/mortuary/ceremony-groups", json=group_payload(case["id"], "persist-hold", codes=("HALL-A",), hour=8))
    wait_case = make_case(client, "CASE-P-002")
    waiting = client.post("/api/mortuary/ceremony-groups/waitlist", json=group_payload(wait_case["id"], "persist-wait", codes=("HALL-A",), hour=8))

    # 模拟进程重启：丢弃线程连接并用全新服务实例读取
    close_connection()
    restarted = OrchestrationService()
    held_group = restarted.get_group(held.json()["id"])
    wait_group = restarted.get_group(waiting.json()["id"])
    assert held_group["status"] == "held" and held_group["hold_expires_at"]
    assert all(line["status"] == "held" for line in held_group["lines"])
    assert wait_group["status"] == "waitlisted"
    assert wait_group["waitlist"]["status"] == "waiting"
    assert wait_group["waitlist_rank"] == 1
    # 越序审计与时间线同样完整保留
    assert [event["event_type"] for event in wait_group["timeline"]] == ["waitlist.joined"]
