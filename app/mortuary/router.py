from fastapi import APIRouter, Query

from app.mortuary.orchestration import OrchestrationService
from app.mortuary.schemas import ActorGrant, CeremonyGroupConfirm, CeremonyGroupCreate, CeremonyGroupRelease, BurialRightCreate, BurialRightRenew, CaseCreate, CustodyAccept, CustodyTransferCreate, InvoiceCreate, PaymentCreate, PromotionRun, ReservationCreate, ResourceCreate, ServiceOrderCreate, UrgencyReview, WaitlistCancel, WaitlistOverride
from app.mortuary.service import MortuaryService

router = APIRouter(prefix="/api/mortuary", tags=["mortuary"])


def orchestration() -> OrchestrationService:
    return OrchestrationService()

@router.post("/cases", status_code=201)
def create_case(payload: CaseCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return MortuaryService().create_case(payload.model_dump(), actor)

@router.get("/cases")
def list_cases(status: str | None = None, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    return MortuaryService().list_cases(status, limit)

@router.get("/cases/{case_id}")
def get_case(case_id: int) -> dict:
    return MortuaryService().get_case(case_id)

@router.post("/cases/{case_id}/custody-transfers", status_code=201)
def request_transfer(case_id: int, payload: CustodyTransferCreate) -> dict:
    return MortuaryService().request_transfer(case_id, payload.model_dump())

@router.post("/custody-transfers/{transfer_id}/accept")
def accept_transfer(transfer_id: int, payload: CustodyAccept) -> dict:
    return MortuaryService().accept_transfer(transfer_id, payload.model_dump())

@router.post("/resources", status_code=201)
def create_resource(payload: ResourceCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return MortuaryService().create_resource(payload.model_dump(mode="json"), actor)

@router.get("/resources")
def resources(kind: str | None = None) -> list[dict]:
    return MortuaryService().list_resources(kind)

@router.post("/reservations", status_code=201)
def reserve(payload: ReservationCreate) -> dict:
    return MortuaryService().reserve(payload.model_dump())

@router.post("/reservations/{reservation_id}/cancel")
def cancel(reservation_id: int, actor: str = Query(min_length=2), reason: str = Query(min_length=2, max_length=500)) -> dict:
    return MortuaryService().cancel_reservation(reservation_id, actor, reason)

@router.post("/service-orders", status_code=201)
def order(payload: ServiceOrderCreate) -> dict:
    return MortuaryService().add_order(payload.model_dump())

@router.post("/service-orders/{order_id}/confirm")
def confirm_order(order_id: int, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return MortuaryService().confirm_order(order_id, actor)

@router.post("/burial-rights", status_code=201)
def create_right(payload: BurialRightCreate, actor: str = Query(min_length=2, max_length=80)) -> dict:
    return MortuaryService().create_right(payload.model_dump(), actor)

@router.post("/burial-rights/{right_id}/renew")
def renew_right(right_id: int, payload: BurialRightRenew) -> dict:
    return MortuaryService().renew_right(right_id, payload.model_dump())

@router.post("/invoices", status_code=201)
def create_invoice(payload: InvoiceCreate) -> dict:
    return MortuaryService().create_invoice(payload.model_dump())

@router.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int) -> dict:
    return MortuaryService().get_invoice(invoice_id)

@router.post("/invoices/{invoice_id}/payments")
def pay(invoice_id: int, payload: PaymentCreate) -> dict:
    return MortuaryService().pay(invoice_id, payload.model_dump())


# ---- 跨资源编排：整组占用与候补 ----
@router.post("/ceremony-groups", status_code=201)
def create_ceremony_group(payload: CeremonyGroupCreate) -> dict:
    return OrchestrationService().create_group(payload.model_dump(), as_waitlist=False)


@router.post("/ceremony-groups/waitlist", status_code=201)
def join_ceremony_waitlist(payload: CeremonyGroupCreate) -> dict:
    return OrchestrationService().create_group(payload.model_dump(), as_waitlist=True)


@router.get("/ceremony-groups")
def list_ceremony_groups(status: str | None = None, case_id: int | None = None, limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    return OrchestrationService().list_groups(status=status, case_id=case_id, limit=limit)


@router.get("/ceremony-groups/{group_id}")
def get_ceremony_group(group_id: int) -> dict:
    return OrchestrationService().get_group(group_id)


@router.post("/ceremony-groups/{group_id}/confirm")
def confirm_ceremony_group(group_id: int, payload: CeremonyGroupConfirm) -> dict:
    return OrchestrationService().confirm_group(group_id, payload.model_dump())


@router.post("/ceremony-groups/{group_id}/release")
def release_ceremony_group(group_id: int, payload: CeremonyGroupRelease) -> dict:
    return OrchestrationService().release_group(group_id, payload.model_dump())


@router.get("/waitlist")
def list_waitlist() -> dict:
    return OrchestrationService().list_waitlist()


@router.post("/ceremony-groups/{group_id}/waitlist/override")
def override_waitlist(group_id: int, payload: WaitlistOverride) -> dict:
    return OrchestrationService().override_waitlist(group_id, payload.model_dump())


@router.post("/ceremony-groups/{group_id}/waitlist/cancel")
def cancel_waitlist(group_id: int, payload: WaitlistCancel) -> dict:
    return OrchestrationService().cancel_waitlist(group_id, payload.model_dump())


@router.post("/ceremony-groups/{group_id}/urgency-review")
def review_urgency(group_id: int, payload: UrgencyReview) -> dict:
    return OrchestrationService().review_urgency(group_id, payload.model_dump())


@router.post("/waitlist/promote")
def promote_waitlist(payload: PromotionRun) -> dict:
    return OrchestrationService().promote_waitlist(payload.model_dump())


@router.post("/ceremony-groups/expire-holds")
def expire_holds(actor: str = Query(default="system-scheduler", min_length=2, max_length=80)) -> dict:
    return OrchestrationService().expire_holds(actor)


@router.post("/orchestration/actors/grant")
def grant_orchestration_actor(payload: ActorGrant) -> dict:
    return OrchestrationService().grant_actor(payload.model_dump())
