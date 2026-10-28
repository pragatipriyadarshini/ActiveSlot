"""Database setup and tables owned by the booking service."""
import uuid

from sqlalchemy import DateTime, Index, Integer, String, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .common import make_engine, utcnow

# Each service creates its own engine and SQLAlchemy session factory.
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


# Booking states used by the booking workflow.
PENDING_PAYMENT, CONFIRMED, CANCELLED, EXPIRED = (
    "PENDING_PAYMENT",
    "CONFIRMED",
    "CANCELLED",
    "EXPIRED",
)
ACTIVE_STATES = (PENDING_PAYMENT, CONFIRMED)


class Booking(Base):
    """A booking record; IDs owned by other services remain plain strings."""

    __tablename__ = "bookings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    member_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(16), default=PENDING_PAYMENT)
    hold_id: Mapped[str] = mapped_column(String(36))
    hold_expires_at: Mapped[DateTime] = mapped_column(DateTime)
    amount_paise: Mapped[int] = mapped_column(Integer)
    payment_id: Mapped[str] = mapped_column(String(36), nullable=True)
    payment_idempotency_key: Mapped[str] = mapped_column(String(128), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)

    # Enforce one active booking per member and class, including concurrent requests.
    __table_args__ = (
        Index(
            "uq_one_active_booking_per_member_session",
            "member_id",
            "session_id",
            unique=True,
            postgresql_where=text("state IN ('PENDING_PAYMENT','CONFIRMED')"),
            sqlite_where=text("state IN ('PENDING_PAYMENT','CONFIRMED')"),
        ),
    )
