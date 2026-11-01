"""Database setup and tables owned by the payment service."""
import uuid

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .common import make_engine, utcnow

# Each service creates its own database engine and request-scoped sessions.
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


PENDING, CAPTURED, DECLINED, FAILED, REFUNDED = (
    "PENDING",
    "CAPTURED",
    "DECLINED",
    "FAILED",
    "REFUNDED",
)


def new_id() -> str:
    return str(uuid.uuid4())


class Payment(Base):
    """A charge or refund record."""

    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    booking_id: Mapped[str] = mapped_column(String(64), index=True)
    member_id: Mapped[str] = mapped_column(String(64))
    amount_paise: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(10), default=PENDING)
    gateway_ref: Mapped[str] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)

    # A booking can have only one successfully captured payment.
    __table_args__ = (
        Index(
            "uq_one_captured_payment_per_booking",
            "booking_id",
            unique=True,
            postgresql_where=text("state = 'CAPTURED'"),
            sqlite_where=text("state = 'CAPTURED'"),
        ),
    )


class IdempotencyRecord(Base):
    """Stores a request key and response so retries return the original result."""

    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12))
    response_status: Mapped[int] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
