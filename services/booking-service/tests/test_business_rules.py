"""Booking service business rules: BR3 (one active booking), BR5 (state machine), BR7 (notification is
best effort), plus the orchestration paths (happy, declined, gateway down, seat lost while paying)."""
import pytest
from sqlalchemy import update

from app import logic as state_machine
from app.logic import UpstreamError
from app.common import ApiError, utcnow
from app.models import SessionLocal
from app.models import Booking
from datetime import timedelta


def book(client, member="m1", session="s1"):
    return client.post("/api/v1/bookings", json={"member_id": member, "session_id": session})


def pay(client, booking_id, key="key-1"):
    return client.post(f"/api/v1/bookings/{booking_id}/pay", headers={"Idempotency-Key": key})


def age_hold(booking_id, world, hold_state="EXPIRED"):
    with SessionLocal() as db:
        db.execute(update(Booking).where(Booking.id == booking_id).values(hold_expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
        hold_id = db.get(Booking, booking_id).hold_id
    world.holds[hold_id] = hold_state


# ---- BR5: state machine ---------------------------------------------------------------------------
@pytest.mark.parametrize("a,b,ok", [
    ("PENDING_PAYMENT", "CONFIRMED", True), ("PENDING_PAYMENT", "CANCELLED", True),
    ("PENDING_PAYMENT", "EXPIRED", True), ("CONFIRMED", "CANCELLED", False),
    ("CONFIRMED", "PENDING_PAYMENT", False), ("CANCELLED", "CONFIRMED", False), ("EXPIRED", "CONFIRMED", False)])
def test_br5_allowed_and_forbidden_transitions(a, b, ok):
    assert state_machine.can_transition(a, b) is ok


def test_br5_move_refuses_illegal_edge():
    with SessionLocal() as db, pytest.raises(ApiError) as e:
        state_machine.move(db, "x", "CONFIRMED", "CANCELLED")
    assert e.value.code == "INVALID_STATE"


def test_br5_paid_booking_cannot_be_cancelled(client, world):
    b = book(client).json()
    assert pay(client, b["id"]).status_code == 200
    r = client.post(f"/api/v1/bookings/{b['id']}/cancel")
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE"
    assert world.holds[b["hold_id"]] == "CONFIRMED"      # the paid seat was never released


# ---- BR3: one active booking per member per class ---------------------------------------------------
def test_br3_second_active_booking_is_rejected_and_takes_no_seat(client, world):
    assert book(client).status_code == 201
    r = book(client)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_BOOKED"
    assert world.seats == 4                               # only one seat taken
    assert book(client, member="m2").status_code == 201   # another member is fine


def test_br3_database_index_is_the_real_guard(client, world):
    b = book(client).json()
    from sqlalchemy.exc import IntegrityError
    with SessionLocal() as db:
        db.add(Booking(member_id="m1", session_id="s1", state="PENDING_PAYMENT", hold_id="h", hold_expires_at=utcnow(), amount_paise=1))
        with pytest.raises(IntegrityError):
            db.commit()


def test_br3_can_rebook_after_cancelling(client, world):
    b = book(client).json()
    assert client.post(f"/api/v1/bookings/{b['id']}/cancel").json()["state"] == "CANCELLED"
    assert world.seats == 5
    assert book(client).status_code == 201


# ---- BR7: notification failure never fails the booking ------------------------------------------------
def test_br7_booking_stays_confirmed_when_notification_is_down(client, world):
    world.notify_down = True
    b = book(client).json()
    r = pay(client, b["id"])
    assert r.status_code == 200 and r.json()["state"] == "CONFIRMED"
    assert world.notifications == []


# ---- orchestration: happy path and failures ----------------------------------------------------------
def test_happy_path(client, world):
    b = book(client).json()
    assert b["state"] == "PENDING_PAYMENT" and b["amount_paise"] == 49900
    paid = pay(client, b["id"]).json()
    assert paid["state"] == "CONFIRMED" and paid["payment_id"] == "pay-1"
    assert world.notifications == [b["id"]]


def test_pay_requires_idempotency_key(client, world):
    b = book(client).json()
    r = client.post(f"/api/v1/bookings/{b['id']}/pay")
    assert r.status_code == 400 and r.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_repeated_pay_with_same_key_returns_same_result_without_new_charge_or_sms(client, world):
    b = book(client).json()
    first, second = pay(client, b["id"]), pay(client, b["id"])
    assert first.json() == second.json() and world.charge_calls == 1 and len(world.notifications) == 1


def test_pay_with_different_key_after_paid_is_conflict(client, world):
    b = book(client).json()
    pay(client, b["id"])
    assert pay(client, b["id"], key="other").json()["error"]["code"] == "ALREADY_PAID"


def test_full_class_is_rejected_with_clear_error(client, world):
    world.seats = 0
    r = book(client)
    assert r.status_code == 409 and r.json()["error"]["code"] == "SESSION_FULL"
    assert client.get("/api/v1/bookings").json() == []   # no orphan booking row


def test_gateway_down_keeps_booking_pending_and_same_key_can_retry(client, world):
    b = book(client).json()
    world.gateway_down = True
    r = pay(client, b["id"])
    assert r.status_code == 503 and r.json()["error"]["code"] == "GATEWAY_UNAVAILABLE"
    assert client.get(f"/api/v1/bookings/{b['id']}").json()["state"] == "PENDING_PAYMENT"
    world.gateway_down = False
    assert pay(client, b["id"]).json()["state"] == "CONFIRMED"


def test_pay_after_hold_expired_is_refused_without_charging(client, world):
    b = book(client).json()
    age_hold(b["id"], world)
    r = pay(client, b["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "HOLD_EXPIRED"
    assert world.charge_calls == 0
    assert client.get(f"/api/v1/bookings/{b['id']}").json()["state"] == "EXPIRED"


def test_seat_lost_while_paying_triggers_refund_compensation(client, world):
    b = book(client).json()
    world.confirm_fails_with = UpstreamError(409, "HOLD_NOT_ACTIVE", "expired meanwhile")
    r = pay(client, b["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "HOLD_EXPIRED_REFUNDED"
    assert world.refunds == ["pay-1"]
    assert client.get(f"/api/v1/bookings/{b['id']}").json()["state"] == "EXPIRED"


def test_validation_and_not_found_use_common_error_format(client, world):
    r = client.post("/api/v1/bookings", json={"member_id": ""})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.get("/api/v1/bookings/nope")
    assert r.status_code == 404 and set(r.json()["error"]) == {"code", "message", "correlation_id", "details"}


def test_correlation_id_is_echoed(client, world):
    r = client.get("/health", headers={"X-Correlation-ID": "abc-123"})
    assert r.headers["x-correlation-id"] == "abc-123"
