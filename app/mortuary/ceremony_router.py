from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.mortuary.orchestration import CeremonyOrchestrationService
from app.mortuary.schemas import (
    CeremonyHoldCreate,
    CeremonyOrderOverride,
    CeremonyPreflight,
    CeremonyRelease,
    CeremonyUrgencyReview,
    CeremonyWaitlistCancel,
    CeremonyWaitlistJoin,
)

router = APIRouter(prefix="/api/mortuary", tags=["mortuary-ceremony"])


def service() -> CeremonyOrchestrationService:
    return CeremonyOrchestrationService()


@router.post("/ceremonies/preflight")
def preflight(payload: CeremonyPreflight, principal: Principal = Depends(current_principal)) -> dict:
    return service().preflight(principal, payload.model_dump())


@router.post("/ceremonies/holds", status_code=201)
def create_hold(payload: CeremonyHoldCreate, principal: Principal = Depends(current_principal)) -> dict:
    return service().create_hold(principal, payload.model_dump())


@router.get("/ceremonies/{orchestration_id}")
def get_orchestration(orchestration_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().get_orchestration(principal, orchestration_id)


@router.post("/ceremonies/{orchestration_id}/confirm")
def confirm_orchestration(orchestration_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().confirm(principal, orchestration_id)


@router.post("/ceremonies/{orchestration_id}/release")
def release_orchestration(orchestration_id: int, payload: CeremonyRelease, principal: Principal = Depends(current_principal)) -> dict:
    return service().release(principal, orchestration_id, payload.reason)


@router.get("/cases/{case_id}/ceremony-overview")
def case_ceremony_overview(case_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().case_ceremony_overview(principal, case_id)


@router.get("/waitlist")
def list_waitlist(principal: Principal = Depends(current_principal)) -> list[dict]:
    return service().list_waiting_endpoint(principal)


@router.post("/waitlist", status_code=201)
def join_waitlist(payload: CeremonyWaitlistJoin, principal: Principal = Depends(current_principal)) -> dict:
    return service().join_waitlist(principal, payload.model_dump())


@router.post("/waitlist/advance")
def advance_waitlist(principal: Principal = Depends(current_principal)) -> dict:
    return service().advance_waitlist(principal)


@router.get("/waitlist/{entry_id}")
def get_waitlist_entry(entry_id: int, principal: Principal = Depends(current_principal)) -> dict:
    return service().get_waitlist_entry(principal, entry_id)


@router.post("/waitlist/{entry_id}/urgency-review")
def review_urgency(entry_id: int, payload: CeremonyUrgencyReview, principal: Principal = Depends(current_principal)) -> dict:
    return service().review_urgency(principal, entry_id, payload.urgency_level, payload.note)


@router.post("/waitlist/{entry_id}/override")
def override_order(entry_id: int, payload: CeremonyOrderOverride, principal: Principal = Depends(current_principal)) -> dict:
    return service().override_order(principal, entry_id, payload.position, payload.reason)


@router.post("/waitlist/{entry_id}/cancel")
def cancel_waitlist(entry_id: int, payload: CeremonyWaitlistCancel, principal: Principal = Depends(current_principal)) -> dict:
    return service().cancel_waitlist(principal, entry_id, payload.reason)
