"""Unit tests for the schedule service business rules (BR1, BR2, BR6). Each test name carries its rule ID
so the report can trace  rule -> code (app/inventory.py) -> test (this file)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import update

from app import logic as inventory
from app.common import ApiError, utcnow
from app.models import SessionLocal
from app.models import CONFIRMED, EXPIRED, HELD, RELEASED, ClassSession, SeatHold


def make_session(db, capacity=2, starts_in_hours=24):
    s = ClassSession(title="HIIT", trainer="Arjun", starts_at=utcnow() + timedelta(hours=starts_in_hours),
                     capacity=capacity, available_seats=capacity, price_paise=49900)
    db.add(s)
    db.commit()
    return s


# ---- BR1: never over-allocate -------------------------------------------------------------------
def test_br1_stops_at_capacity(db):
    s = make_session(db, capacity=2)
    inventory.hold_seat(db, s.id, "b1", 60)
    inventory.hold_seat(db, s.id, "b2", 60)
    with pytest.raises(ApiError) as e:
        inventory.hold_seat(db, s.id, "b3", 60)
    assert (e.value.status, e.value.code) == (409, "SESSION_FULL")


def test_br1_parallel_requests_never_over_allocate(db):
    s = make_session(db, capacity=3)

    def attempt(i):
        with SessionLocal() as own_db:  # every request has its own DB session, like the real API
            try:
                inventory.hold_seat(own_db, s.id, f"b{i}", 60)
                return True
            except ApiError:
                return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(12)))
    assert sum(results) == 3
    db.expire_all()
    assert db.get(ClassSession, s.id).available_seats == 0


def test_br1_same_booking_gets_one_seat_only(db):
    s = make_session(db, capacity=2)
    h1, created1 = inventory.hold_seat(db, s.id, "same", 60)
    h2, created2 = inventory.hold_seat(db, s.id, "same", 60)
    assert (created1, created2) == (True, False) and h1.id == h2.id
    assert db.get(ClassSession, s.id).available_seats == 1


def test_br1_database_rejects_out_of_range_even_if_code_is_wrong(db):
    s = make_session(db, capacity=1)
    with pytest.raises(Exception):
        db.execute(update(ClassSession).where(ClassSession.id == s.id).values(available_seats=-1))
        db.commit()
    db.rollback()


# ---- BR2: holds expire, paid seats never do, job is repeat-safe -----------------------------------
def _age(db, hold_id):
    db.execute(update(SeatHold).where(SeatHold.id == hold_id).values(expires_at=utcnow() - timedelta(seconds=1)))
    db.commit()


def test_br2_expired_hold_returns_seat(db):
    s = make_session(db, capacity=1)
    hold, _ = inventory.hold_seat(db, s.id, "b1", 30)
    assert inventory.expire_holds(db) == 0  # not yet due
    _age(db, hold.id)
    assert inventory.expire_holds(db) == 1
    db.expire_all()
    assert db.get(SeatHold, hold.id).state == EXPIRED
    assert db.get(ClassSession, s.id).available_seats == 1


def test_br2_expiry_restores_capacity_after_naive_demo_overbooking(db):
    s = make_session(db, capacity=2)
    expired_at = utcnow() - timedelta(seconds=1)
    db.add_all(
        SeatHold(
            session_id=s.id,
            booking_id=f"naive-{index}",
            state=HELD,
            expires_at=expired_at,
        )
        for index in range(4)
    )
    db.execute(
        update(ClassSession)
        .where(ClassSession.id == s.id)
        .values(available_seats=1)
    )
    db.commit()

    assert inventory.expire_holds(db) == 4
    db.expire_all()
    assert db.get(ClassSession, s.id).available_seats == s.capacity


def test_br2_never_releases_a_paid_seat(db):
    s = make_session(db, capacity=1)
    hold, _ = inventory.hold_seat(db, s.id, "b1", 30)
    inventory.confirm_hold(db, hold.id)
    _age(db, hold.id)
    assert inventory.expire_holds(db) == 0
    db.expire_all()
    assert db.get(SeatHold, hold.id).state == CONFIRMED
    assert db.get(ClassSession, s.id).available_seats == 0


def test_br2_expiry_job_is_safe_to_run_twice_at_once(db):
    s = make_session(db, capacity=3)
    ids = [inventory.hold_seat(db, s.id, f"b{i}", 30)[0].id for i in range(3)]
    for hid in ids:
        _age(db, hid)

    def run(_):
        with SessionLocal() as own_db:
            return inventory.expire_holds(own_db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        released = list(pool.map(run, range(2)))
    assert sum(released) == 3  # each hold released exactly once across both runs
    db.expire_all()
    assert db.get(ClassSession, s.id).available_seats == 3  # not 6


def test_br2_cancel_releases_but_not_after_payment(db):
    s = make_session(db, capacity=2)
    h1, _ = inventory.hold_seat(db, s.id, "b1", 60)
    h2, _ = inventory.hold_seat(db, s.id, "b2", 60)
    assert inventory.release_hold(db, h1.id).state == RELEASED
    assert inventory.release_hold(db, h1.id).state == RELEASED  # repeat is harmless
    inventory.confirm_hold(db, h2.id)
    with pytest.raises(ApiError) as e:
        inventory.release_hold(db, h2.id)
    assert e.value.code == "HOLD_ALREADY_CONFIRMED"
    db.expire_all()
    assert db.get(ClassSession, s.id).available_seats == 1


def test_confirm_after_expiry_is_refused(db):
    s = make_session(db, capacity=1)
    hold, _ = inventory.hold_seat(db, s.id, "b1", 30)
    _age(db, hold.id)
    inventory.expire_holds(db)
    with pytest.raises(ApiError) as e:
        inventory.confirm_hold(db, hold.id)
    assert e.value.code == "HOLD_NOT_ACTIVE"


# ---- BR6: no booking for a class that has started ---------------------------------------------------
def test_br6_cannot_book_started_class(db):
    s = make_session(db, capacity=5, starts_in_hours=-1)
    with pytest.raises(ApiError) as e:
        inventory.hold_seat(db, s.id, "b1", 60)
    assert e.value.code == "SESSION_STARTED"
    assert db.get(ClassSession, s.id).available_seats == 5


# ---- API level: validation + consistent error format ---------------------------------------------
def test_api_validation_error_format(client):
    r = client.post("/api/v1/sessions", json={"title": "", "capacity": 0})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "VALIDATION_ERROR" and body["correlation_id"] and body["details"]


def test_api_full_flow_and_health(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.post("/api/v1/sessions", json={"title": "Yoga", "trainer": "Mira", "capacity": 1,
                                              "starts_at": (utcnow() + timedelta(days=1)).isoformat() + "Z",
                                              "price_paise": 30000})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert client.post(f"/api/v1/sessions/{sid}/holds", json={"booking_id": "a"}).status_code == 201
    full = client.post(f"/api/v1/sessions/{sid}/holds", json={"booking_id": "b"})
    assert full.status_code == 409 and full.json()["error"]["code"] == "SESSION_FULL"
    detail = client.get(f"/api/v1/sessions/{sid}").json()
    assert detail["available_seats"] == 0 and detail["seats_held"] == 1 and detail["consistent"] is True
    assert client.get("/api/v1/sessions/nope").status_code == 404
