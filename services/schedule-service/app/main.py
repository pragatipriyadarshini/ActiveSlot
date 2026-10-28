"""Schedule service: request validation, HTTP routes, and application startup."""
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import APIRouter, Depends, FastAPI, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import common, logic, models
from .common import ApiError, ERROR_RESPONSES, UtcDateTime, log, to_naive_utc, utcnow, wait_for_db
from .models import CONFIRMED, HELD, ClassSession, SeatHold


# API input and output shapes are defined beside the endpoints that use them.
class SessionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100, examples=["HIIT 6 AM"])
    trainer: str = Field(min_length=1, max_length=100, examples=["Coach Arjun"])
    starts_at: datetime = Field(examples=["2030-01-01T06:00:00Z"])
    capacity: int = Field(ge=1, le=500, examples=[20])
    price_paise: int = Field(ge=0, examples=[49900], description="Price in paise (1 INR = 100 paise)")

    @field_validator("starts_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return to_naive_utc(value)


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    trainer: str
    starts_at: UtcDateTime
    capacity: int
    available_seats: int
    price_paise: int


class SessionDetail(SessionOut):
    seats_held: int = Field(description="Holds in state HELD (unpaid, will expire)")
    seats_confirmed: int = Field(description="Holds in state CONFIRMED (paid)")
    consistent: bool = Field(description="capacity - available_seats == held + confirmed")


class HoldCreate(BaseModel):
    booking_id: str = Field(min_length=1, max_length=64)
    hold_seconds: int = Field(default=300, ge=1, le=3600)


class HoldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    booking_id: str
    state: str
    expires_at: UtcDateTime
    price_paise: int | None = Field(default=None, description="Price of the held class")


class ExpireResult(BaseModel):
    released: int


router = APIRouter()
ERRORS = {code: ERROR_RESPONSES[code] for code in (404, 409, 422)}


# The health route checks the database as well as the web process.
@router.get("/health", tags=["ops"])
def health(db: Session = Depends(models.get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "schedule-service"}


# Class-session routes create and browse available classes.
@router.post("/api/v1/sessions", status_code=201, response_model=SessionOut, tags=["sessions"],
             responses=ERRORS)
def create_session(body: SessionCreate, db: Session = Depends(models.get_db)):
    session = ClassSession(
        title=body.title,
        trainer=body.trainer,
        starts_at=body.starts_at,
        capacity=body.capacity,
        available_seats=body.capacity,
        price_paise=body.price_paise,
    )
    db.add(session)
    db.commit()
    return session


@router.get("/api/v1/sessions", response_model=list[SessionOut], tags=["sessions"])
def list_sessions(upcoming_only: bool = True, db: Session = Depends(models.get_db)):
    query = select(ClassSession).order_by(ClassSession.starts_at)
    if upcoming_only:
        query = query.where(ClassSession.starts_at > utcnow())
    return db.scalars(query).all()


def _detail(db: Session, session_id: str) -> SessionDetail:
    """Combine class data with hold counts for the class detail response."""
    session = db.get(ClassSession, session_id)
    if session is None:
        raise ApiError(404, "SESSION_NOT_FOUND", "Class session does not exist")
    counts = dict(
        db.execute(
            select(SeatHold.state, func.count())
            .where(SeatHold.session_id == session_id)
            .group_by(SeatHold.state)
        ).all()
    )
    held, confirmed = counts.get(HELD, 0), counts.get(CONFIRMED, 0)
    base = SessionOut.model_validate(session).model_dump()
    return SessionDetail(
        **base,
        seats_held=held,
        seats_confirmed=confirmed,
        consistent=(session.capacity - session.available_seats) == held + confirmed,
    )


@router.get("/api/v1/sessions/{session_id}", response_model=SessionDetail, tags=["sessions"],
            responses=ERRORS)
def get_session(session_id: str, db: Session = Depends(models.get_db)):
    return _detail(db, session_id)


@router.get("/api/v1/sessions/{session_id}/holds", response_model=list[HoldOut], tags=["holds"],
            responses=ERRORS)
def list_holds(session_id: str, db: Session = Depends(models.get_db)):
    _detail(db, session_id)
    return db.scalars(
        select(SeatHold).where(SeatHold.session_id == session_id).order_by(SeatHold.created_at)
    ).all()


# Hold routes delegate seat-state changes to logic.py.
def _hold_out(hold: SeatHold, db: Session) -> HoldOut:
    out = HoldOut.model_validate(hold, from_attributes=True)
    session = db.get(ClassSession, hold.session_id)
    out.price_paise = session.price_paise if session else None
    return out


@router.post("/api/v1/sessions/{session_id}/holds", status_code=201, response_model=HoldOut, tags=["holds"],
             responses={**ERRORS, 200: {"description": "Hold already existed for this booking_id (idempotent)"}})
def create_hold(session_id: str, body: HoldCreate, response: Response, db: Session = Depends(models.get_db)):
    hold, created = logic.hold_seat(db, session_id, body.booking_id, body.hold_seconds)
    response.status_code = 201 if created else 200
    return _hold_out(hold, db)


@router.get("/api/v1/holds/{hold_id}", response_model=HoldOut, tags=["holds"], responses=ERRORS)
def get_hold(hold_id: str, db: Session = Depends(models.get_db)):
    hold = db.get(SeatHold, hold_id)
    if hold is None:
        raise ApiError(404, "HOLD_NOT_FOUND", "Hold does not exist")
    return _hold_out(hold, db)


@router.post("/api/v1/holds/{hold_id}/confirm", response_model=HoldOut, tags=["holds"],
             responses=ERRORS)
def confirm_hold(hold_id: str, db: Session = Depends(models.get_db)):
    return _hold_out(logic.confirm_hold(db, hold_id), db)


@router.post("/api/v1/holds/{hold_id}/release", response_model=HoldOut, tags=["holds"],
             responses=ERRORS)
def release_hold(hold_id: str, db: Session = Depends(models.get_db)):
    return _hold_out(logic.release_hold(db, hold_id), db)


@router.post("/api/v1/internal/holds/expire", response_model=ExpireResult, tags=["internal"])
def expire_now(db: Session = Depends(models.get_db)):
    """Run the same expiry operation used by the background worker."""
    return ExpireResult(released=logic.expire_holds(db))


# Optional unsafe endpoint used only to demonstrate the overbooking race.
if os.environ.get("ENABLE_NAIVE_DEMO", "false").lower() == "true":
    @router.post("/api/v1/sessions/{session_id}/holds-naive", status_code=201,
                 response_model=HoldOut, tags=["demo-only"], responses=ERRORS)
    def create_hold_naive(session_id: str, body: HoldCreate, db: Session = Depends(models.get_db)):
        return _hold_out(
            logic.hold_seat_naive(db, session_id, body.booking_id, body.hold_seconds),
            db,
        )


# Periodically expire unpaid holds in the background.
SWEEP_INTERVAL = float(os.environ.get("EXPIRY_SWEEP_INTERVAL_SECONDS", "5"))


def _sweeper(stop: threading.Event) -> None:
    while not stop.wait(SWEEP_INTERVAL):
        try:
            with models.SessionLocal() as db:
                logic.expire_holds(db)
        except Exception:  # noqa: BLE001 - keep the background worker alive and log failures
            log.exception("sweeper_failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    wait_for_db(models.engine)
    models.Base.metadata.create_all(models.engine)
    stop = threading.Event()
    if SWEEP_INTERVAL > 0:
        threading.Thread(target=_sweeper, args=(stop,), daemon=True, name="hold-sweeper").start()
    log.info("started", extra={"sweep_interval_s": SWEEP_INTERVAL})
    yield
    stop.set()


app = FastAPI(
    title="Schedule Service",
    version="1.0.0",
    lifespan=lifespan,
    description="Owns class sessions and seat inventory. The only service that can allocate seats.",
)
common.install(app)
app.include_router(router)
