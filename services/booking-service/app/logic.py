"""Booking workflows, state transitions, and HTTP calls to the other services."""
import os
import uuid
from datetime import datetime

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .common import ApiError, log, outbound_headers, to_naive_utc, utcnow
from .models import ACTIVE_STATES, CANCELLED, CONFIRMED, EXPIRED, PENDING_PAYMENT, Booking


# HTTP client settings come from environment variables, so services stay independently configurable.
SCHEDULE_URL = os.environ.get("SCHEDULE_SERVICE_URL", "http://localhost:8001")
PAYMENT_URL = os.environ.get("PAYMENT_SERVICE_URL", "http://localhost:8003")
NOTIFICATION_URL = os.environ.get("NOTIFICATION_SERVICE_URL", "http://localhost:8004")
SCHEDULE_TIMEOUT = float(os.environ.get("SCHEDULE_TIMEOUT_SECONDS", "5"))
PAYMENT_TIMEOUT = float(os.environ.get("PAYMENT_TIMEOUT_SECONDS", "15"))
NOTIFICATION_TIMEOUT = float(os.environ.get("NOTIFICATION_TIMEOUT_SECONDS", "2"))


class UpstreamError(Exception):
    """An upstream service returned an error response."""

    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(f"{status} {code}")


class UpstreamUnavailable(Exception):
    """An upstream request timed out or could not connect."""


def passthrough(exc: UpstreamError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


def _call(method: str, url: str, timeout: float, headers: dict | None = None, **kwargs) -> httpx.Response:
    try:
        response = httpx.request(
            method, url, timeout=timeout, headers=outbound_headers(headers), **kwargs
        )
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable(type(exc).__name__) from exc
    if response.status_code >= 400:
        try:
            error = response.json().get("error", {})
        except ValueError:
            error = {}
        raise UpstreamError(
            response.status_code,
            error.get("code", "UPSTREAM_ERROR"),
            error.get("message", "upstream error"),
        )
    return response


def parse_ts(value: str) -> datetime:
    return to_naive_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


# Schedule service calls: the schedule service is the source of truth for seat state.
def create_hold(session_id: str, booking_id: str, hold_seconds: int) -> dict:
    return _call(
        "POST",
        f"{SCHEDULE_URL}/api/v1/sessions/{session_id}/holds",
        SCHEDULE_TIMEOUT,
        json={"booking_id": booking_id, "hold_seconds": hold_seconds},
    ).json()


def get_hold(hold_id: str) -> dict:
    return _call("GET", f"{SCHEDULE_URL}/api/v1/holds/{hold_id}", SCHEDULE_TIMEOUT).json()


def confirm_hold(hold_id: str) -> dict:
    return _call("POST", f"{SCHEDULE_URL}/api/v1/holds/{hold_id}/confirm", SCHEDULE_TIMEOUT).json()


def release_hold(hold_id: str) -> dict:
    return _call("POST", f"{SCHEDULE_URL}/api/v1/holds/{hold_id}/release", SCHEDULE_TIMEOUT).json()


# Payment and notification calls include the idempotency key and correlation ID.
def charge(key: str, booking_id: str, member_id: str, amount_paise: int) -> dict:
    return _call(
        "POST",
        f"{PAYMENT_URL}/api/v1/payments",
        PAYMENT_TIMEOUT,
        headers={"Idempotency-Key": key},
        json={"booking_id": booking_id, "member_id": member_id, "amount_paise": amount_paise},
    ).json()


def refund(payment_id: str) -> dict:
    return _call("POST", f"{PAYMENT_URL}/api/v1/payments/{payment_id}/refund", PAYMENT_TIMEOUT).json()


def notify(booking_id: str, member_id: str, type_: str, message: str) -> dict:
    return _call(
        "POST",
        f"{NOTIFICATION_URL}/api/v1/notifications",
        NOTIFICATION_TIMEOUT,
        json={"booking_id": booking_id, "member_id": member_id, "type": type_, "message": message},
    ).json()


# Only these booking state changes are allowed.
ALLOWED = {
    PENDING_PAYMENT: {CONFIRMED, CANCELLED, EXPIRED},
    CONFIRMED: set(),
    CANCELLED: set(),
    EXPIRED: set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, set())


def move(db: Session, booking_id: str, current: str, target: str, **fields) -> bool:
    """Update a booking only if it still has the expected state."""
    if not can_transition(current, target):
        raise ApiError(409, "INVALID_STATE", f"A booking cannot go from {current} to {target}")
    changed = db.execute(
        update(Booking)
        .where(Booking.id == booking_id, Booking.state == current)
        .values(state=target, updated_at=utcnow(), **fields)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    return changed == 1

HOLD_SECONDS = int(os.environ.get("HOLD_SECONDS", "300"))


def _unavailable(name: str) -> ApiError:
    return ApiError(503, f"{name}_UNAVAILABLE", f"{name.lower()} service did not answer in time; please retry")


def get_booking(db: Session, booking_id: str) -> Booking:
    booking = db.get(Booking, booking_id, populate_existing=True)
    db.rollback()
    if booking is None:
        raise ApiError(404, "BOOKING_NOT_FOUND", "Booking does not exist")
    return booking


def refresh_expiry(db: Session, booking: Booking) -> Booking:
    """If the hold time is up, ask the seat owner (schedule-service) what happened to the hold and
    mirror it. The schedule service stays the source of truth: we never mark EXPIRED on our own clock."""
    if booking.state != PENDING_PAYMENT or utcnow() < booking.hold_expires_at:
        return booking
    try:
        hold = get_hold(booking.hold_id)
    except (UpstreamError, UpstreamUnavailable):
        return booking
    if hold["state"] in ("EXPIRED", "RELEASED"):
        move(db, booking.id, PENDING_PAYMENT, EXPIRED)
        log.info("booking_expired", extra={"booking_id": booking.id})
        return get_booking(db, booking.id)
    return booking


def create_booking(db: Session, member_id: str, session_id: str) -> Booking:
    # BR3, friendly check first (the DB index below is the real guard against a race)
    existing = db.scalar(select(Booking).where(Booking.member_id == member_id, Booking.session_id == session_id,
                                               Booking.state.in_(ACTIVE_STATES)))
    db.rollback()
    if existing is not None:
        existing = refresh_expiry(db, existing)
        if existing.state in ACTIVE_STATES:
            raise ApiError(409, "ALREADY_BOOKED", "This member already has an active booking for this class")

    booking_id = str(uuid.uuid4())
    try:
        hold = create_hold(session_id, booking_id, HOLD_SECONDS)   # BR1 is enforced over there
    except UpstreamError as exc:
        raise passthrough(exc)      # SESSION_FULL, SESSION_NOT_FOUND, SESSION_STARTED ...
    except UpstreamUnavailable:
        raise _unavailable("SCHEDULE")

    booking = Booking(id=booking_id, member_id=member_id, session_id=session_id, state=PENDING_PAYMENT,
                      hold_id=hold["id"], hold_expires_at=parse_ts(hold["expires_at"]),
                      amount_paise=hold["price_paise"])
    db.add(booking)
    try:
        db.commit()
    except IntegrityError:          # lost a race against a parallel booking by the same member (BR3)
        db.rollback()
        _release_quietly(hold["id"])
        raise ApiError(409, "ALREADY_BOOKED", "This member already has an active booking for this class")
    log.info("booking_created", extra={"booking_id": booking.id, "session_id": session_id, "member_id": member_id})
    return booking


def pay_booking(db: Session, booking_id: str, key: str) -> Booking:
    booking = get_booking(db, booking_id)
    if booking.state == CONFIRMED:
        if booking.payment_idempotency_key == key:
            return booking                                   # same request repeated: same answer
        raise ApiError(409, "ALREADY_PAID", "This booking is already paid")
    booking = refresh_expiry(db, booking)
    if booking.state == EXPIRED or (booking.state == PENDING_PAYMENT and utcnow() >= booking.hold_expires_at):
        raise ApiError(409, "HOLD_EXPIRED", "The seat hold has expired; please book again")
    if booking.state != PENDING_PAYMENT:
        raise ApiError(409, "INVALID_STATE", f"A {booking.state} booking cannot be paid")

    try:                                                     # step 2: charge (idempotent by key)
        payment = charge(key, booking.id, booking.member_id, booking.amount_paise)
    except UpstreamError as exc:
        raise passthrough(exc)          # PAYMENT_DECLINED, GATEWAY_UNAVAILABLE, IDEMPOTENCY_KEY_REUSED ...
    except UpstreamUnavailable:
        raise _unavailable("PAYMENT")

    try:                                                     # step 3: make the seat permanent
        confirm_hold(booking.hold_id)
    except UpstreamError as exc:
        if exc.status == 409:       # seat was lost (expired/cancelled) AFTER we took the money: compensate
            _refund_quietly(payment["id"])
            move(db, booking.id, PENDING_PAYMENT, EXPIRED)
            raise ApiError(409, "HOLD_EXPIRED_REFUNDED", "The seat expired while paying; the payment was refunded")
        raise passthrough(exc)
    except UpstreamUnavailable:
        # Money is taken, seat not yet confirmed. Retrying the SAME request is safe: the payment is
        # replayed (not repeated) and only the confirmation is attempted again.
        raise ApiError(503, "SCHEDULE_UNAVAILABLE", "Payment taken; retry the same request to finish confirming")

    changed = move(db, booking.id, PENDING_PAYMENT, CONFIRMED, payment_id=payment["id"], payment_idempotency_key=key)
    booking = get_booking(db, booking.id)
    if booking.state != CONFIRMED:
        raise ApiError(409, "INVALID_STATE", f"Booking ended up {booking.state}")
    if changed:                      # only the request that did the transition notifies (no double SMS)
        log.info("booking_confirmed", extra={"booking_id": booking.id, "payment_id": payment["id"]})
        notify_best_effort(booking)
    return booking


def notify_best_effort(booking: Booking) -> None:
    """BR7: the SMS is a courtesy. Whatever happens here, the booking stays CONFIRMED."""
    try:
        notify(booking.id, booking.member_id, "BOOKING_CONFIRMED",
               f"Booking {booking.id[:8]} confirmed. See you in class!")
    except Exception as exc:  # noqa: BLE001 - deliberately broad
        log.warning("notification_failed", extra={"booking_id": booking.id, "reason": type(exc).__name__})


def cancel_booking(db: Session, booking_id: str) -> Booking:
    booking = get_booking(db, booking_id)
    if booking.state == CANCELLED:
        return booking
    if booking.state != PENDING_PAYMENT:
        raise ApiError(409, "INVALID_STATE", f"A {booking.state} booking cannot be cancelled")
    try:                              # the seat owner decides: a paid seat can never be released
        release_hold(booking.hold_id)
    except UpstreamError as exc:
        raise passthrough(exc)
    except UpstreamUnavailable:
        raise _unavailable("SCHEDULE")
    move(db, booking.id, PENDING_PAYMENT, CANCELLED)
    log.info("booking_cancelled", extra={"booking_id": booking.id})
    return get_booking(db, booking.id)


def _release_quietly(hold_id: str) -> None:
    try:
        release_hold(hold_id)
    except Exception:  # noqa: BLE001 - the hold will expire by itself (BR2) if this fails
        log.warning("release_failed_hold_will_expire", extra={"hold_id": hold_id})


def _refund_quietly(payment_id: str) -> None:
    try:
        refund(payment_id)
    except Exception:  # noqa: BLE001
        log.error("refund_failed_needs_manual_action", extra={"payment_id": payment_id})
