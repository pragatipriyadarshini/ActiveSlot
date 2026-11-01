"""Payment service: request validation, HTTP routes, and application startup."""
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import common, logic, models
from .common import ApiError, ERROR_RESPONSES, UtcDateTime, log, wait_for_db
from .models import Payment


# Validate incoming charges and describe payment responses.
class ChargeIn(BaseModel):
    booking_id: str = Field(min_length=1, max_length=64)
    member_id: str = Field(min_length=1, max_length=64)
    amount_paise: int = Field(ge=1, le=10_000_000)


class PaymentOut(BaseModel):
    id: str
    booking_id: str
    member_id: str
    amount_paise: int
    state: str
    gateway_ref: str | None = None
    created_at: UtcDateTime


router = APIRouter()
IDEMPOTENCY_WAIT = float(os.environ.get("IDEMPOTENCY_WAIT_SECONDS", "10"))
ERRORS = {code: ERROR_RESPONSES[code] for code in (400, 404, 409, 422, 503)}


# This route checks that the database is reachable.
@router.get("/health", tags=["ops"])
def health(db: Session = Depends(models.get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "payment-service"}


# The idempotency wrapper ensures a repeated request returns its saved response.
@router.post("/api/v1/payments", status_code=201, response_model=PaymentOut, tags=["payments"],
             responses={**ERRORS, 402: {"description": "Card declined"}})
def create_payment(
    body: ChargeIn,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Required. Repeating a request with the same key never charges twice.",
    ),
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise ApiError(400, "MISSING_IDEMPOTENCY_KEY", "Header 'Idempotency-Key' is required (max 128 chars)")
    status, payload, replayed = logic.run_once(
        idempotency_key,
        body.model_dump(),
        work=lambda: logic.charge_payment(
            idempotency_key,
            body.booking_id,
            body.member_id,
            body.amount_paise,
        ),
        wait_seconds=IDEMPOTENCY_WAIT,
    )
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(status_code=status, content=payload, headers=headers)


# Read-only routes return payments in creation order.
@router.get("/api/v1/payments", response_model=list[PaymentOut], tags=["payments"])
def list_payments(booking_id: str | None = None, db: Session = Depends(models.get_db)):
    query = select(Payment).order_by(Payment.created_at)
    if booking_id:
        query = query.where(Payment.booking_id == booking_id)
    return [logic.payment_dict(payment) for payment in db.scalars(query).all()]


@router.get("/api/v1/payments/{payment_id}", response_model=PaymentOut, tags=["payments"],
            responses=ERRORS)
def get_payment(payment_id: str, db: Session = Depends(models.get_db)):
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise ApiError(404, "PAYMENT_NOT_FOUND", "Payment does not exist")
    return logic.payment_dict(payment)


@router.post("/api/v1/payments/{payment_id}/refund", response_model=PaymentOut, tags=["payments"],
             responses=ERRORS)
def refund(payment_id: str, db: Session = Depends(models.get_db)):
    return logic.payment_dict(logic.refund_payment(db, payment_id))


# Create database tables before accepting service requests.
@asynccontextmanager
async def lifespan(_: FastAPI):
    wait_for_db(models.engine)
    models.Base.metadata.create_all(models.engine)
    log.info("started")
    yield


app = FastAPI(
    title="Payment Service",
    version="1.0.0",
    lifespan=lifespan,
    description="Charges payments through the gateway exactly once per Idempotency-Key.",
)
common.install(app)
app.include_router(router)
