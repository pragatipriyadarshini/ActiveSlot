"""Notification service: request validation, HTTP routes, and application startup."""
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import common, logic, models
from .common import ERROR_RESPONSES, UtcDateTime, log, wait_for_db
from .models import FAILED, SENT, Notification


# Validate notification requests and define the fields returned to callers.
class NotificationIn(BaseModel):
    booking_id: str = Field(min_length=1, max_length=64)
    member_id: str = Field(min_length=1, max_length=64)
    type: Literal["BOOKING_CONFIRMED", "BOOKING_EXPIRED", "BOOKING_CANCELLED"]
    message: str = Field(min_length=1, max_length=500)


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    booking_id: str
    member_id: str
    type: str
    message: str
    state: str
    created_at: UtcDateTime


router = APIRouter()


# Health endpoint used by the launcher.
@router.get("/health", tags=["ops"])
def health(db: Session = Depends(models.get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "notification-service"}


# Repeated requests for a booking and notification type return the stored result.
@router.post(
    "/api/v1/notifications",
    status_code=201,
    response_model=NotificationOut,
    tags=["notifications"],
    responses={
        422: ERROR_RESPONSES[422],
        200: {"description": "Already sent earlier (no duplicate SMS)"},
    },
)
def send_notification(body: NotificationIn, response: Response, db: Session = Depends(models.get_db)):
    existing_query = select(Notification).where(
        Notification.booking_id == body.booking_id,
        Notification.type == body.type,
    )
    existing = db.scalar(existing_query)
    if existing:
        response.status_code = 200
        return existing

    sent = logic.send_sms(to=body.member_id, text=body.message)
    notification = Notification(
        booking_id=body.booking_id,
        member_id=body.member_id,
        type=body.type,
        message=body.message,
        state=SENT if sent else FAILED,
    )
    db.add(notification)
    try:
        db.commit()
    except IntegrityError:  # another identical request committed first
        db.rollback()
        response.status_code = 200
        return db.scalar(existing_query)

    log.info(
        "notification_recorded",
        extra={"booking_id": body.booking_id, "state": notification.state},
    )
    return notification


@router.get("/api/v1/notifications", response_model=list[NotificationOut], tags=["notifications"])
def list_notifications(booking_id: str | None = None, db: Session = Depends(models.get_db)):
    query = select(Notification).order_by(Notification.created_at)
    if booking_id:
        query = query.where(Notification.booking_id == booking_id)
    return db.scalars(query).all()


# Create the service database tables before accepting requests.
@asynccontextmanager
async def lifespan(_: FastAPI):
    wait_for_db(models.engine)
    models.Base.metadata.create_all(models.engine)
    log.info("started")
    yield


app = FastAPI(
    title="Notification Service",
    version="1.0.0",
    lifespan=lifespan,
    description="Sends member notifications (SMS via a mock provider) and keeps a delivery log.",
)
common.install(app)
app.include_router(router)
