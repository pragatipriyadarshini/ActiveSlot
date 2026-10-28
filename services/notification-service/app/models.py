"""Database setup and the notification table owned by this service."""
import uuid

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .common import make_engine, utcnow

# Each service uses its own database and session factory.
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


SENT, FAILED = "SENT", "FAILED"


class Notification(Base):
    """A message attempt and its delivery result."""

    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    booking_id: Mapped[str] = mapped_column(String(64))
    member_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)

    # A booking can have one notification of each type.
    __table_args__ = (
        UniqueConstraint("booking_id", "type", name="uq_one_notification_per_type"),
    )
