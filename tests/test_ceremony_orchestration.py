from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock
from app.core.security import Principal
from app.database import close_connection, get_connection
from app.mortuary.orchestration import CeremonyOrchestrationService

PASSWORD = "Pass!23456789"

SCHEDULER_PERMS = [
    "mortuary.ceremony.read",
    "mortuary.ceremony.schedule",
    "mortuary.ceremony.waitlist",
]
CONFIRMER_PERMS = ["mortuary.ceremony.read", "mortuary.ceremony.confirm"]
REVIEWER_PERMS = [
    "mortuary.ceremony.read",
    "mortuary.ceremony.schedule",
    "mortuary.ceremony.waitlist",
    "mortuary.ceremony.review",
]
OVERRIDER_PERMS = REVIEWER_PERMS + ["mortuary.ceremony.override"]
READONLY_PERMS = ["mortuary.ceremony.read"]


def make_user(client, admin_headers, username: str, permission_codes: list[str]) -> dict:
    role_code = f"role_{username}"
    role = client.post("/api/roles", headers=admin_headers, json={"code": role_code, "name": role_code, "permission_codes": permission_codes})
    assert role.status_code == 201, role.text
    created = client.post(
        "/api/users",
        headers=admin_headers,
        json={"username": username, "password": PASSWORD, "display_name": f"{username}-显示名", "role_codes": [role_code]},
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={"username": username, "password": PASSWORD, "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['token']}"}, "display": f"{username}-显示名"}


def create_case(client, ref: str) -> dict:
    response = client.post(
        "/api/mortuary/cases?actor=intake",
        json={"external_ref": ref, "decedent_name": f"逝者{ref}", "identity_number": None,
              "death_time": "2026-09-27T08:30:00Z", "received_from": "市第二医院", "family_contact": "家属",
              "family_phone": "13800000000", "special_notes": ""},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_resource(client, code: str, kind: str = "farewell_hall", capacity: int = 1) -> dict:
    response = client.post(
        "/api/mortuary/resources?actor=resource-admin",
        json={"code": code, "name": f"资源{code}", "kind": kind, "site_code": "SITE-1", "capacity": capacity, "attributes": {}},
    )
    assert response.status_code == 201, response.text
    return response.json()


def item(code: str, start: str, end: str, purpose: str = "大型告别仪式") -> dict:
    return {"resource_code": code, "start_at": start, "end_at": end, "purpose": purpose}


def service_principal(name: str = "排班主任") -> Principal:
    return Principal(None, "scheduler", name, None, frozenset({"*"}), 1)


def svc(clock: FrozenClock | None = None) -> CeremonyOrchestrationService:
    return CeremonyOrchestrationService(get_connection(), clock or FrozenClock(datetime.now(UTC)))


# ------------------------------------------------------------- 原子性与生命周期

def test_group_hold_is_all_or_nothing_on_conflict(client, admin):
    scheduler = make_user(client, admin["headers"], "sched1", SCHEDULER_PERMS)
    blocker_case = create_case(client, "CASE-BLOCKER")
    case = create_case(client, "CASE-GROUP-1")
    create_resource(client, "GRP-HALL", capacity=1)
    create_resource(client, "GRP-VAN", kind="vehicle", capacity=1)
    # 先占用接运车辆这一个资源
    occupied = client.post(
        "/api/mortuary/reservations",
        json={"resource_code": "GRP-VAN", "case_id": blocker_case["id"], "start_at": "2026-10-05T09:00:00Z",
              "end_at": "2026-10-05T10:00:00Z", "purpose": "既有接运", "created_by": "old-scheduler",
              "idempotency_key": "van-blocker-0001"},
    )
    assert occupied.status_code == 201
    payload = {
        "case_id": case["id"], "title": "整场告别", "hold_minutes": 30,
        "items": [item("GRP-HALL", "2026-10-05T09:00:00Z", "2026-10-05T10:30:00Z"),
                  item("GRP-VAN", "2026-10-05T09:00:00Z", "2026-10-05T10:00:00Z")],
        "idempotency_key": "group-hold-fail-001",
    }
    failed = client.post("/api/mortuary/ceremonies/holds", headers=scheduler["headers"], json=payload)
    assert failed.status_code == 409
    blocked = failed.json()["error"]["context"]["blocked_resources"]
    assert [b["resource_code"] for b in blocked] == ["GRP-VAN"]
    assert blocked[0]["blocking"][0]["reservation_id"] == occupied.json()["id"]
    # 没有任何部分成功：礼厅没有留下 held 预约，也没有编排记录
    case_detail = client.get(f"/api/mortuary/cases/{case['id']}").json()
    assert case_detail["reservations"] == []
    # 礼厅本身仍可独立预约
    only_hall = dict(payload, items=[item("GRP-HALL", "2026-10-05T09:00:00Z", "2026-10-05T10:30:00Z")], idempotency_key="group-hall-only-01")
    assert client.post("/api/mortuary/ceremonies/holds", headers=scheduler["headers"], json=only_hall).status_code == 201


def test_hold_confirm_and_release_lifecycle(client, admin):
    scheduler = make_user(client, admin["headers"], "sched2", SCHEDULER_PERMS)
    confirmer = make_user(client, admin["headers"], "confirmer1", CONFIRMER_PERMS)
    case = create_case(client, "CASE-LIFE-1")
    create_resource(client, "LIFE-HALL")
    create_resource(client, "LIFE-CREM", kind="cremator")
    payload = {
        "case_id": case["id"], "title": "生命周期", "hold_minutes": 45,
        "items": [item("LIFE-HALL", "2026-10-06T09:00:00Z", "2026-10-06T10:00:00Z"),
                  item("LIFE-CREM", "2026-10-06T10:30:00Z", "2026-10-06T11:00:00Z")],
        "idempotency_key": "life-hold-0000001",
    }
    held = client.post("/api/mortuary/ceremonies/holds", headers=scheduler["headers"], json=payload)
    assert held.status_code == 201
    body = held.json()
    assert body["status"] == "held"
    assert {i["status"] for i in body["items"]} == {"held"}
    # 持有期间其他整组占用会被挡住
    clash = client.post("/api/mortuary/ceremonies/holds", headers=scheduler["headers"], json=dict(payload, idempotency_key="life-hold-clash-1"))
    assert clash.status_code == 409
    # 无确认权限的排班员不能确认
    assert client.post(f"/api/mortuary/ceremonies/{body['id']}/confirm", headers=scheduler["headers"]).status_code == 403
    confirmed = client.post(f"/api/mortuary/ceremonies/{body['id']}/confirm", headers=confirmer["headers"])
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert {i["status"] for i in confirmed.json()["items"]} == {"confirmed"}
    assert confirmed.json()["confirmed_by"] == "confirmer1-显示名"
    # 再开一场持有并整体释放，释放后容量回来
    case2 = create_case(client, "CASE-LIFE-2")
    second = client.post(
        "/api/mortuary/ceremonies/holds", headers=scheduler["headers"],
        json={"case_id": case2["id"], "title": "第二场", "hold_minutes": 30,
              "items": [item("LIFE-HALL", "2026-10-07T09:00:00Z", "2026-10-07T10:00:00Z")],
              "idempotency_key": "life-hold-second-01"},
    )
    assert second.status_code == 201
    released = client.post(
        f"/api/mortuary/ceremonies/{second.json()['id']}/release", headers=scheduler["headers"],
        json={"reason": "家属改期，整体释放"},
    )
    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert {i["status"] for i in released.json()["items"]} == {"released"}
    assert [a["adjustment_type"] for a in released.json()["adjustments"]] == ["hold.created", "hold.released"]


def test_create_hold_is_idempotent(client, admin):
    scheduler = make_user(client, admin["headers"], "sched3", SCHEDULER_PERMS)
    case = create_case(client, "CASE-IDEM-1")
    create_resource(client, "IDEM-HALL")
    payload = {"case_id": case["id"], "title": "幂等", "hold_minutes": 20,
               "items": [item("IDEM-HALL", "2026-10-08T09:00:00Z", "2026-10-08T10:00:00Z")],
               "idempotency_key": "idem-hold-0000001"}
    first = client.post("/api/mortuary/ceremonies/holds", headers=scheduler["headers"], json=payload)
    second = client.post("/api/mortuary/ceremonies/holds", headers=scheduler["headers"], json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    changed = dict(payload, hold_minutes=60)
    assert client.post("/api/mortuary/ceremonies/holds", headers=scheduler["headers"], json=changed).status_code == 409


# ------------------------------------------------------------- 候补排序与推进

def _seed_ranking_case(clock: FrozenClock) -> CeremonyOrchestrationService:
    service = svc(clock)
    actor = service_principal()
    # 阻塞资源 H 的既有持有
    blocker_case = service.connection.execute(
        "INSERT INTO mortuary_cases(external_ref,decedent_name,death_time,received_from,family_contact,family_phone,created_at,updated_at) "
        "VALUES('RANK-BLOCKER','阻塞档案',?,'医院','家属','13800000000',?,?)",
        (clock.now().isoformat(), clock.now().isoformat(), clock.now().isoformat()),
    )
    blocker_id = int(blocker_case.lastrowid)
    resource = service.connection.execute(
        "INSERT INTO facility_resources(code,name,kind,site_code,capacity,created_at,updated_at) VALUES('RANK-HALL','排序厅','farewell_hall','S',1,?,?)",
        (clock.now().isoformat(), clock.now().isoformat()),
    )
    resource_id = int(resource.lastrowid)
    start = clock.now() + timedelta(hours=2)
    hold_payload = {
        "case_id": blocker_id, "title": "阻塞场", "hold_minutes": 60,
        "items": [{"resource_code": "RANK-HALL", "start_at": start, "end_at": start + timedelta(minutes=30), "purpose": "阻塞"}],
        "idempotency_key": "rank-blocker-hold-1",
    }
    service.create_hold(actor, hold_payload)
    return service


def _join_entry(service: CeremonyOrchestrationService, clock: FrozenClock, ref: str, preserve_hours: int, key: str, urgency: int = 0) -> int:
    actor = service_principal()
    case_id = int(service.connection.execute(
        "INSERT INTO mortuary_cases(external_ref,decedent_name,death_time,received_from,family_contact,family_phone,created_at,updated_at) "
        "VALUES(?,? ,?,'医院','家属','13800000000',?,?)",
        (ref, ref, clock.now().isoformat(), clock.now().isoformat(), clock.now().isoformat()),
    ).lastrowid)
    start = clock.now() + timedelta(hours=2)
    result = service.join_waitlist(actor, {
        "case_id": case_id,
        "items": [{"resource_code": "RANK-HALL", "start_at": start, "end_at": start + timedelta(minutes=30), "purpose": "候补"}],
        "body_preserve_until": clock.now() + timedelta(hours=preserve_hours),
        "urgency_level": urgency,
        "hold_minutes": 30,
        "idempotency_key": key,
    })
    return result["id"]


def test_waitlist_ranking_reviewed_urgency_then_preserve_deadline(client):
    clock = FrozenClock(datetime(2026, 10, 2, 0, 0, tzinfo=UTC))
    service = _seed_ranking_case(clock)
    actor = service_principal()
    id_a = _join_entry(service, clock, "RANK-A", preserve_hours=72, key="rank-a-00000001")
    clock.advance(minutes=1)
    id_b = _join_entry(service, clock, "RANK-B", preserve_hours=24, key="rank-b-00000001")
    clock.advance(minutes=1)
    id_c = _join_entry(service, clock, "RANK-C", preserve_hours=48, key="rank-c-00000001")
    # 未经审核的高紧急等级不参与排序：此时按保存期限 B(24h)、C(48h)、A(72h)
    waiting = service.list_waiting()
    assert [row["id"] for row in waiting] == [id_b, id_c, id_a]
    # 审核 C 为最高紧急等级后，C 升到队首，B 仍在 A 前
    service.review_urgency(actor, id_c, urgency_level=3, note="公安部门协查，需尽快办理")
    assert [row["id"] for row in service.list_waiting()] == [id_c, id_b, id_a]
    # 释放阻塞容量后，只有队首 C 递补成功（容量为 1）
    blocker = service.repository.orchestration_key(int(service.connection.execute("SELECT id FROM mortuary_cases WHERE external_ref='RANK-BLOCKER'").fetchone()[0]), "rank-blocker-hold-1")
    service.release(actor, blocker["id"], "仪式结束，释放容量")
    c_entry = service.get_waitlist_entry(actor, id_c)
    assert c_entry["status"] == "promoted"
    assert service.get_waitlist_entry(actor, id_b)["status"] == "waiting"
    # 推进幂等：再次推进不产生新的递补
    again = service.advance_waitlist(actor)
    assert again["promotions"] == []
    # 释放 C 递补得到的持有后，B 先于 A 递补
    service.release(actor, c_entry["promoted_orchestration_id"], "C 家属取消")
    assert service.get_waitlist_entry(actor, id_b)["status"] == "promoted"
    assert service.get_waitlist_entry(actor, id_a)["status"] == "waiting"


def test_hold_expiry_releases_capacity_and_promotes_waitlist(client):
    clock = FrozenClock(datetime(2026, 10, 3, 6, 0, tzinfo=UTC))
    service = _seed_ranking_case(clock)
    actor = service_principal()
    entry_id = _join_entry(service, clock, "EXPIRE-W1", preserve_hours=48, key="expire-w-000001")
    assert service.get_waitlist_entry(actor, entry_id)["status"] == "waiting"
    clock.advance(minutes=61)
    result = service.advance_waitlist(actor)
    assert result["expired_holds"], "超时持有应被自动释放"
    promoted = service.get_waitlist_entry(actor, entry_id)
    assert promoted["status"] == "promoted"
    assert promoted["promoted_orchestration_id"] == result["promotions"][0]["orchestration_id"]
    # 幂等：再次推进不会重复递补
    replay = service.advance_waitlist(actor)
    assert replay["expired_holds"] == [] and replay["promotions"] == []
    detail = service.get_orchestration(actor, promoted["promoted_orchestration_id"])
    assert detail["status"] == "held"


# ------------------------------------------------------------- 越序与审计

def test_capacity_two_promotes_multiple_entries_in_one_sweep(client):
    clock = FrozenClock(datetime(2026, 10, 5, 2, 0, tzinfo=UTC))
    service = svc(clock)
    actor = service_principal()
    conn = get_connection()
    now = clock.now().isoformat()
    conn.execute("INSERT INTO facility_resources(code,name,kind,site_code,capacity,created_at,updated_at) VALUES('CAP2-HALL','双容量厅','farewell_hall','S',2,?,?)", (now, now))
    window_start, window_end = clock.now() + timedelta(hours=3), clock.now() + timedelta(hours=4)
    # 两个独立的短时持有各占 1 个容量，合起来占满容量 2
    for index in (1, 2):
        blocker = conn.execute("INSERT INTO mortuary_cases(external_ref,decedent_name,death_time,received_from,family_contact,family_phone,created_at,updated_at) VALUES(?,?,?,'医院','家属','138',?,?)", (f"CAP2-BLK{index}", "阻塞", now, now, now)).lastrowid
        hold = CeremonyOrchestrationService(conn, clock, ensure=False).create_hold(actor, {
            "case_id": blocker, "title": f"占{index}", "hold_minutes": 30,
            "items": [{"resource_code": "CAP2-HALL", "start_at": window_start, "end_at": window_end, "purpose": f"占{index}"}],
            "idempotency_key": f"cap2-blocker-hold-{index}"})
        assert hold["status"] == "held"
    entry_ids = []
    for ref in ("CAP2-A", "CAP2-B", "CAP2-C"):
        case_id = conn.execute("INSERT INTO mortuary_cases(external_ref,decedent_name,death_time,received_from,family_contact,family_phone,created_at,updated_at) VALUES(?,? ,?,'医院','家属','138',?,?)", (ref, ref, now, now, now)).lastrowid
        entry = CeremonyOrchestrationService(conn, clock, ensure=False).join_waitlist(actor, {
            "case_id": case_id,
            "items": [{"resource_code": "CAP2-HALL", "start_at": window_start, "end_at": window_end, "purpose": "候补"}],
            "body_preserve_until": clock.now() + timedelta(hours=48),
            "urgency_level": 0, "hold_minutes": 30,
            "idempotency_key": f"cap2-wait-{ref}"})
        entry_ids.append(entry["id"])
    assert all(service.get_waitlist_entry(actor, eid)["status"] == "waiting" for eid in entry_ids)
    # 超时整组释放一次腾出 2 个名额：前两名在同一轮推进内递补，第三名继续等待
    clock.advance(minutes=31)
    result = service.advance_waitlist(actor)
    assert len(result["promotions"]) == 2
    assert service.get_waitlist_entry(actor, entry_ids[0])["status"] == "promoted"
    assert service.get_waitlist_entry(actor, entry_ids[1])["status"] == "promoted"
    assert service.get_waitlist_entry(actor, entry_ids[2])["status"] == "waiting"
    assert service.advance_waitlist(actor)["promotions"] == []


def test_manual_override_requires_reason_and_is_audited(client, admin):
    scheduler = make_user(client, admin["headers"], "sched4", SCHEDULER_PERMS)
    reviewer = make_user(client, admin["headers"], "review1", REVIEWER_PERMS)
    overrider = make_user(client, admin["headers"], "override1", OVERRIDER_PERMS)
    create_resource(client, "OVR-HALL")
    blocker = create_case(client, "OVR-BLOCK")
    client.post("/api/mortuary/reservations", json={
        "resource_code": "OVR-HALL", "case_id": blocker["id"], "start_at": "2026-10-09T09:00:00Z",
        "end_at": "2026-10-09T10:00:00Z", "purpose": "阻塞", "created_by": "scheduler", "idempotency_key": "ovr-block-0001"})
    entries = []
    for index in range(2):
        case = create_case(client, f"OVR-W{index}")
        joined = client.post("/api/mortuary/waitlist", headers=scheduler["headers"], json={
            "case_id": case["id"],
            "items": [item("OVR-HALL", "2026-10-09T09:00:00Z", "2026-10-09T09:30:00Z")],
            "body_preserve_until": "2026-10-10T00:00:00Z", "urgency_level": 0, "hold_minutes": 30,
            "idempotency_key": f"ovr-wait-000{index}"})
        assert joined.status_code == 201, joined.text
        entries.append(joined.json()["id"])
    # 审核员没有越序权限
    forbidden = client.post(f"/api/mortuary/waitlist/{entries[1]}/override", headers=reviewer["headers"],
                            json={"position": 1, "reason": "特事特办，需要置顶处理"})
    assert forbidden.status_code == 403
    # 理由过短会被拒绝
    bad = client.post(f"/api/mortuary/waitlist/{entries[1]}/override", headers=overrider["headers"],
                      json={"position": 1, "reason": "无"})
    assert bad.status_code == 422
    moved = client.post(f"/api/mortuary/waitlist/{entries[1]}/override", headers=overrider["headers"],
                       json={"position": 1, "reason": "公安协查通报，需优先安排告别时段"})
    assert moved.status_code == 200
    queue = client.get("/api/mortuary/waitlist", headers=overrider["headers"]).json()
    assert queue[0]["id"] == entries[1]
    assert queue[0]["was_overridden"] == 1
    # 审计中可查见越序动作与理由
    audit = client.get("/api/audit?resource_type=ceremony_orchestration&action=ceremony.waitlist_overridden&size=10",
                       headers=admin["headers"]).json()
    assert audit["total"] == 1
    import json as json_mod
    assert json_mod.loads(audit["data"][0]["metadata_json"])["reason"] == "公安协查通报，需优先安排告别时段"


# ------------------------------------------------------------- 查询、权限与持久化

def test_family_service_overview_tracks_blocking_and_adjustments(client, admin):
    scheduler = make_user(client, admin["headers"], "sched5", SCHEDULER_PERMS)
    reader = make_user(client, admin["headers"], "reader1", READONLY_PERMS)
    create_resource(client, "FAM-HALL")
    blocker_case = create_case(client, "FAM-BLOCK")
    client.post("/api/mortuary/reservations", json={
        "resource_code": "FAM-HALL", "case_id": blocker_case["id"], "start_at": "2026-10-11T09:00:00Z",
        "end_at": "2026-10-11T10:00:00Z", "purpose": "阻塞", "created_by": "scheduler", "idempotency_key": "fam-block-0001"})
    case = create_case(client, "FAM-CASE-1")
    joined = client.post("/api/mortuary/waitlist", headers=scheduler["headers"], json={
        "case_id": case["id"],
        "items": [item("FAM-HALL", "2026-10-11T09:00:00Z", "2026-10-11T09:30:00Z")],
        "body_preserve_until": "2026-10-12T00:00:00Z", "hold_minutes": 30, "idempotency_key": "fam-wait-00001"})
    assert joined.json()["status"] == "waiting"
    overview = client.get(f"/api/mortuary/cases/{case['id']}/ceremony-overview", headers=reader["headers"]).json()
    entry = overview["waitlist_entries"][0]
    assert entry["blocked_resources"][0]["resource_code"] == "FAM-HALL"
    assert entry["blocked_resources"][0]["blocking"][0]["case_id"] == blocker_case["id"]
    # 取消旧的单资源预约后，无需手动推进即自动递补为整组持有
    reservation_id = client.get(f"/api/mortuary/cases/{blocker_case['id']}").json()["reservations"][0]["id"]
    client.post(f"/api/mortuary/reservations/{reservation_id}/cancel?actor=scheduler&reason=腾出容量")
    overview = client.get(f"/api/mortuary/cases/{case['id']}/ceremony-overview", headers=reader["headers"]).json()
    orchestration = overview["orchestrations"][0]
    assert orchestration["status"] == "held"
    types_ = {a["adjustment_type"] for a in orchestration["adjustments"]}
    assert "waitlist.promoted" in types_
    assert [e["event_type"] for e in overview["case_timeline"] if e["event_type"] == "ceremony.hold_promoted"]


def test_waitlist_entry_expires_after_body_preserve_deadline(client):
    clock = FrozenClock(datetime(2026, 10, 4, 6, 0, tzinfo=UTC))
    service = _seed_ranking_case(clock)
    actor = service_principal()
    entry_id = _join_entry(service, clock, "PRESERVE-1", preserve_hours=12, key="preserve-0000001")
    assert service.get_waitlist_entry(actor, entry_id)["status"] == "waiting"
    clock.advance(hours=13)
    result = service.advance_waitlist(actor)
    assert entry_id in result["expired_waitlist"]
    entry = service.get_waitlist_entry(actor, entry_id)
    assert entry["status"] == "expired"
    assert [a["adjustment_type"] for a in entry["adjustments"]][-1] == "waitlist.expired"
    # 再次推进幂等
    assert service.advance_waitlist(actor)["expired_waitlist"] == []


def test_permissions_are_enforced(client, admin):
    case = create_case(client, "CASE-PERM-1")
    create_resource(client, "PERM-HALL")
    payload = {"case_id": case["id"], "title": "权限", "hold_minutes": 20,
               "items": [item("PERM-HALL", "2026-10-12T09:00:00Z", "2026-10-12T10:00:00Z")],
               "idempotency_key": "perm-hold-000001"}
    assert client.post("/api/mortuary/ceremonies/holds", json=payload).status_code == 401
    reader = make_user(client, admin["headers"], "reader2", READONLY_PERMS)
    assert client.post("/api/mortuary/ceremonies/holds", headers=reader["headers"], json=payload).status_code == 403
    assert client.get("/api/mortuary/waitlist", headers=reader["headers"]).status_code == 200


def test_unexpired_holds_and_waitlist_survive_restart(client, admin):
    scheduler = make_user(client, admin["headers"], "sched6", SCHEDULER_PERMS)
    create_resource(client, "PERSIST-HALL")
    blocker_case = create_case(client, "PERSIST-BLOCK")
    client.post("/api/mortuary/reservations", json={
        "resource_code": "PERSIST-HALL", "case_id": blocker_case["id"], "start_at": "2026-10-13T09:00:00Z",
        "end_at": "2026-10-13T10:00:00Z", "purpose": "阻塞", "created_by": "scheduler", "idempotency_key": "persist-blk-01"})
    hold_case = create_case(client, "PERSIST-HOLD")
    held = client.post("/api/mortuary/ceremonies/holds", headers=scheduler["headers"], json={
        "case_id": hold_case["id"], "title": "持久持有", "hold_minutes": 120,
        "items": [item("PERSIST-HALL", "2026-10-14T09:00:00Z", "2026-10-14T10:00:00Z")],
        "idempotency_key": "persist-hold-0001"})
    assert held.status_code == 201
    wait_case = create_case(client, "PERSIST-WAIT")
    joined = client.post("/api/mortuary/waitlist", headers=scheduler["headers"], json={
        "case_id": wait_case["id"],
        "items": [item("PERSIST-HALL", "2026-10-13T09:00:00Z", "2026-10-13T09:30:00Z")],
        "body_preserve_until": "2026-10-15T00:00:00Z", "hold_minutes": 30, "idempotency_key": "persist-wait-001"})
    assert joined.status_code == 201
    # 模拟进程重启：关闭线程连接后，新请求重新打开同一个 SQLite 文件
    close_connection()
    overview = client.get(f"/api/mortuary/cases/{hold_case['id']}/ceremony-overview", headers=scheduler["headers"]).json()
    assert overview["orchestrations"][0]["status"] == "held"
    wait_overview = client.get(f"/api/mortuary/cases/{wait_case['id']}/ceremony-overview", headers=scheduler["headers"]).json()
    assert wait_overview["waitlist_entries"][0]["status"] == "waiting"
    assert wait_overview["waitlist_entries"][0]["rank_position"] == 1
