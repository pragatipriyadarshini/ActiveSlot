"""Booking service: request validation, HTTP routes, and application startup."""
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import common, logic, models
from .common import ApiError, ERROR_RESPONSES, UtcDateTime, log, wait_for_db
from .models import Booking


# Request and response shapes are kept beside the routes that use them.
class BookingCreate(BaseModel):
    member_id: str = Field(min_length=1, max_length=64, examples=["member-101"])
    session_id: str = Field(min_length=1, max_length=36)


class BookingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    member_id: str
    session_id: str
    state: str
    amount_paise: int
    hold_id: str
    hold_expires_at: UtcDateTime
    payment_id: str | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime


router = APIRouter()
ERRORS = {code: ERROR_RESPONSES[code] for code in (400, 404, 409, 422, 503)}


# Health endpoint used by the launcher and monitoring checks.
@router.get("/health", tags=["ops"])
def health(db: Session = Depends(models.get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "booking-service"}


# Booking endpoints delegate business rules to logic.py.
@router.post("/api/v1/bookings", status_code=201, response_model=BookingOut, tags=["bookings"],
             responses=ERRORS)
def create_booking(body: BookingCreate, db: Session = Depends(models.get_db)):
    """Reserve a seat and create a booking waiting for payment."""
    return logic.create_booking(db, body.member_id, body.session_id)


@router.get("/api/v1/bookings", response_model=list[BookingOut], tags=["bookings"])
def list_bookings(member_id: str | None = None, session_id: str | None = None,
                  db: Session = Depends(models.get_db)):
    query = select(Booking).order_by(Booking.created_at)
    if member_id:
        query = query.where(Booking.member_id == member_id)
    if session_id:
        query = query.where(Booking.session_id == session_id)
    return db.scalars(query).all()


@router.get("/api/v1/bookings/{booking_id}", response_model=BookingOut, tags=["bookings"],
            responses=ERRORS)
def get_booking(booking_id: str, db: Session = Depends(models.get_db)):
    return logic.refresh_expiry(db, logic.get_booking(db, booking_id))


@router.post("/api/v1/bookings/{booking_id}/pay", response_model=BookingOut, tags=["bookings"],
             responses={**ERRORS, 402: {"description": "Card declined"}})
def pay_booking(
    booking_id: str,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Required. Repeating the request with the same key never charges twice.",
    ),
    db: Session = Depends(models.get_db),
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise ApiError(400, "MISSING_IDEMPOTENCY_KEY", "Header 'Idempotency-Key' is required (max 128 chars)")
    return logic.pay_booking(db, booking_id, idempotency_key)


@router.post("/api/v1/bookings/{booking_id}/cancel", response_model=BookingOut, tags=["bookings"],
             responses=ERRORS)
def cancel_booking(booking_id: str, db: Session = Depends(models.get_db)):
    return logic.cancel_booking(db, booking_id)


# Initialize the database before serving requests.
@asynccontextmanager
async def lifespan(_: FastAPI):
    wait_for_db(models.engine)
    models.Base.metadata.create_all(models.engine)
    log.info("started")
    yield


app = FastAPI(
    title="Booking Service",
    version="1.0.0",
    lifespan=lifespan,
    description="Orchestrates class booking: hold seat -> pay -> confirm -> notify.",
)
common.install(app)
app.include_router(router)
