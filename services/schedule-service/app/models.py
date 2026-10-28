"""Database setup and tables owned by the schedule service."""
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .common import make_engine, utcnow

# Each service owns its database engine and request-scoped sessions.
engine = make_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for this service's tables."""


def get_db():
    """Provide one database session per request and always close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Hold states describe whether a seat is temporary, paid, or released.
HELD, CONFIRMED, RELEASED, EXPIRED = "HELD", "CONFIRMED", "RELEASED", "EXPIRED"


def new_id() -> str:
    return str(uuid.uuid4())


class ClassSession(Base):
    """A scheduled class; available_seats is the resource protected from overbooking."""

    __tablename__ = "class_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(100))
    trainer: Mapped[str] = mapped_column(String(100))
    starts_at: Mapped[DateTime] = mapped_column(DateTime)
    capacity: Mapped[int] = mapped_column(Integer)
    available_seats: Mapped[int] = mapped_column(Integer)
    price_paise: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        CheckConstraint("capacity > 0", name="ck_capacity_positive"),
        CheckConstraint("available_seats >= 0 AND available_seats <= capacity", name="ck_seats_in_range"),
    )


class SeatHold(Base):
    """A seat held for one booking until payment confirms it or the hold expires."""

    __tablename__ = "seat_holds"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("class_sessions.id"))
    booking_id: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(12), default=HELD)
    expires_at: Mapped[DateTime] = mapped_column(DateTime)
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (Index("ix_holds_state_expiry", "state", "expires_at"),)
