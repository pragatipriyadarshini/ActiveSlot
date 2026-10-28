"""Seat inventory: all rules about who may take, keep or lose a seat live in this one file.

BR1  A class can never be over-allocated: seats held + confirmed <= capacity.
     -> one atomic conditional UPDATE (`available_seats > 0`) + CHECK constraint in the table.
BR2  Unpaid holds are released automatically after they expire; a CONFIRMED (paid) seat is never
     released by expiry, and running the expiry job twice (even at the same time) is harmless.
     -> expire_holds(): a single UPDATE ... WHERE state='HELD' AND expires_at <= now.
BR6  A class that has already started cannot be booked.
     -> folded into the same atomic UPDATE as BR1 (`starts_at > now`).
"""
import time
from collections import Counter
from datetime import timedelta

from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .common import ApiError, log, utcnow
from .models import CONFIRMED, EXPIRED, HELD, RELEASED, ClassSession, SeatHold


def _hold_for_booking(db: Session, booking_id: str):
    return db.scalar(select(SeatHold).where(SeatHold.booking_id == booking_id))


def hold_seat(db: Session, session_id: str, booking_id: str, hold_seconds: int):
    """Take one seat for `booking_id`. Returns (hold, created). Idempotent per booking_id."""
    existing = _hold_for_booking(db, booking_id)
    if existing:
        db.rollback()
        return existing, False

    now = utcnow()
    # The whole check-and-decrement is ONE statement, so the database serialises concurrent
    # callers on the row: exactly `capacity` of them see rowcount == 1.
    result = db.execute(
        update(ClassSession)
        .where(ClassSession.id == session_id, ClassSession.available_seats > 0, ClassSession.starts_at > now)
        .values(available_seats=ClassSession.available_seats - 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        session = db.get(ClassSession, session_id)
        db.rollback()
        if session is None:
            raise ApiError(404, "SESSION_NOT_FOUND", "Class session does not exist")
        if session.starts_at <= now:
            raise ApiError(409, "SESSION_STARTED", "This class has already started")
        raise ApiError(409, "SESSION_FULL", "No seats left in this class")

    hold = SeatHold(session_id=session_id, booking_id=booking_id, state=HELD,
                    expires_at=now + timedelta(seconds=hold_seconds), created_at=now, updated_at=now)
    db.add(hold)
    try:
        db.commit()
    except IntegrityError:  # two identical hold requests raced: keep the winner, undo our decrement
        db.rollback()
        return _hold_for_booking(db, booking_id), False
    log.info("seat_held", extra={"session_id": session_id, "booking_id": booking_id, "hold_id": hold.id})
    return hold, True


def confirm_hold(db: Session, hold_id: str) -> SeatHold:
    """HELD -> CONFIRMED (booking was paid). Safe to call twice."""
    now = utcnow()
    changed = db.execute(
        update(SeatHold).where(SeatHold.id == hold_id, SeatHold.state == HELD)
        .values(state=CONFIRMED, updated_at=now).execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    hold = db.get(SeatHold, hold_id, populate_existing=True)
    db.rollback()
    if hold is None:
        raise ApiError(404, "HOLD_NOT_FOUND", "Hold does not exist")
    if hold.state != CONFIRMED:
        raise ApiError(409, "HOLD_NOT_ACTIVE", f"Hold is {hold.state}; the seat was already given up")
    if changed:
        log.info("seat_confirmed", extra={"hold_id": hold_id})
    return hold


def release_hold(db: Session, hold_id: str) -> SeatHold:
    """HELD -> RELEASED (booking cancelled). A paid (CONFIRMED) seat is never released here."""
    now = utcnow()
    row = db.execute(
        update(SeatHold).where(SeatHold.id == hold_id, SeatHold.state == HELD)
        .values(state=RELEASED, updated_at=now).returning(SeatHold.session_id)
        .execution_options(synchronize_session=False)
    ).first()
    if row:
        db.execute(update(ClassSession).where(ClassSession.id == row[0])
                   .values(available_seats=ClassSession.available_seats + 1)
                   .execution_options(synchronize_session=False))
        log.info("seat_released", extra={"hold_id": hold_id, "reason": "cancel"})
    db.commit()
    hold = db.get(SeatHold, hold_id, populate_existing=True)
    db.rollback()
    if hold is None:
        raise ApiError(404, "HOLD_NOT_FOUND", "Hold does not exist")
    if hold.state == CONFIRMED:
        raise ApiError(409, "HOLD_ALREADY_CONFIRMED", "A paid seat cannot be released")
    return hold  # RELEASED or EXPIRED: already gone, so repeating the call is harmless


def expire_holds(db: Session) -> int:
    """Release every HELD seat whose time is up. Returns how many were released.

    Idempotent and concurrency-safe: the UPDATE only touches rows still in state HELD, so a second
    run (or a second worker running at the same moment) finds nothing left to do. CONFIRMED rows
    can never match, so a paid seat is never released."""
    now = utcnow()
    rows = db.execute(
        update(SeatHold).where(SeatHold.state == HELD, SeatHold.expires_at <= now)
        .values(state=EXPIRED, updated_at=now).returning(SeatHold.session_id)
        .execution_options(synchronize_session=False)
    ).all()
    per_session = Counter(r[0] for r in rows)
    for session_id in sorted(per_session):  # fixed order avoids deadlocks between two sweepers
        seats_to_return = per_session[session_id]
        updated_seats = ClassSession.available_seats + seats_to_return
        # The naive concurrency demo can leave the counter inconsistent; never exceed capacity.
        db.execute(update(ClassSession).where(ClassSession.id == session_id)
                   .values(
                       available_seats=case(
                           (updated_seats > ClassSession.capacity, ClassSession.capacity),
                           else_=updated_seats,
                       )
                   )
                   .execution_options(synchronize_session=False))
    db.commit()
    if rows:
        log.info("holds_expired", extra={"released": len(rows)})
    return len(rows)


def hold_seat_naive(db: Session, session_id: str, booking_id: str, hold_seconds: int):
    """DEMO ONLY (Challenge 1 evidence): the classic check-then-update bug, kept to *show* why
    it fails. Enabled only when ENABLE_NAIVE_DEMO=true. Never used by the real booking flow."""
    session = db.get(ClassSession, session_id)
    if session is None:
        raise ApiError(404, "SESSION_NOT_FOUND", "Class session does not exist")
    if session.available_seats <= 0:  # CHECK ...
        db.rollback()
        raise ApiError(409, "SESSION_FULL", "No seats left in this class")
    time.sleep(0.05)  # widen the race window so the failure is reproducible on any machine
    session.available_seats = session.available_seats - 1  # ... then UPDATE from a stale value
    now = utcnow()
    hold = SeatHold(session_id=session_id, booking_id=booking_id, state=HELD,
                    expires_at=now + timedelta(seconds=hold_seconds))
    db.add(hold)
    db.commit()
    return hold
