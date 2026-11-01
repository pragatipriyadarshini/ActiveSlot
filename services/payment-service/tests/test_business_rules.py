"""BR4 - a payment is charged at most once per Idempotency-Key; one CAPTURED payment per booking."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.exc import IntegrityError

from app import logic
from app.models import SessionLocal
from app.models import CAPTURED, Payment

BODY = {"booking_id": "bk-1", "member_id": "m-1", "amount_paise": 49900}


class FakeGateway:
    def __init__(self, delay=0.0, fail_times=0, decline=False):
        self.calls, self.delay, self.fail_times, self.decline = 0, delay, fail_times, decline
        self.lock = threading.Lock()

    def charge(self, reference, amount_paise):
        with self.lock:
            self.calls += 1
            attempt = self.calls
        time.sleep(self.delay)
        if self.decline:
            raise logic.GatewayDeclined("declined")
        if attempt <= self.fail_times:
            raise logic.GatewayUnavailable("timeout")
        return {"gateway_ref": f"gw_{reference}", "status": "captured"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeGateway()
    monkeypatch.setattr(logic, "gateway_charge", f.charge)
    return f


def pay(client, key="k1", body=BODY):
    return client.post("/api/v1/payments", json=body, headers={"Idempotency-Key": key})


def test_br4_missing_key_is_rejected(client, fake):
    r = client.post("/api/v1/payments", json=BODY)
    assert r.status_code == 400 and r.json()["error"]["code"] == "MISSING_IDEMPOTENCY_KEY"


def test_br4_sequential_repeat_replays_and_charges_once(client, fake):
    first, second = pay(client), pay(client)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()                      # identical result
    assert second.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert fake.calls == 1                                    # the gateway was hit once
    assert len(client.get("/api/v1/payments?booking_id=bk-1").json()) == 1


def test_br4_same_key_different_payload_is_rejected(client, fake):
    pay(client)
    r = pay(client, body={**BODY, "amount_paise": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert fake.calls == 1


def test_br4_two_identical_requests_at_the_same_moment_charge_once(client, monkeypatch):
    slow = FakeGateway(delay=0.4)
    monkeypatch.setattr(logic, "gateway_charge", slow.charge)
    barrier = threading.Barrier(8)

    def call(_):
        barrier.wait()
        return pay(client)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(8)))
    assert {r.status_code for r in results} == {201}
    assert len({r.json()["id"] for r in results}) == 1        # everybody got the same payment
    assert slow.calls == 1
    assert sum(r.headers.get("Idempotent-Replayed") == "true" for r in results) == 7


def test_br4_gateway_outage_is_retryable_with_same_key_and_never_double_charges(client, monkeypatch):
    flaky = FakeGateway(fail_times=1)
    monkeypatch.setattr(logic, "gateway_charge", flaky.charge)
    r1 = pay(client)
    assert r1.status_code == 503 and r1.json()["error"]["code"] == "GATEWAY_UNAVAILABLE"
    r2 = pay(client)                                          # same key: allowed because nothing was charged
    assert r2.status_code == 201
    payments = client.get("/api/v1/payments?booking_id=bk-1").json()
    assert sorted(p["state"] for p in payments) == ["CAPTURED", "FAILED"]   # audit trail, one real charge


def test_br4_decline_is_a_final_answer_and_is_replayed(client, monkeypatch):
    d = FakeGateway(decline=True)
    monkeypatch.setattr(logic, "gateway_charge", d.charge)
    assert pay(client).status_code == 402
    again = pay(client)
    assert again.status_code == 402 and again.headers["Idempotent-Replayed"] == "true"
    assert d.calls == 1


def test_br4_database_allows_only_one_captured_payment_per_booking(client, fake):
    with SessionLocal() as db:
        db.add(Payment(booking_id="bk-9", member_id="m", amount_paise=1, state=CAPTURED, idempotency_key="a"))
        db.commit()
        db.add(Payment(booking_id="bk-9", member_id="m", amount_paise=1, state=CAPTURED, idempotency_key="b"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_run_once_waiter_times_out_with_409_if_owner_never_finishes(monkeypatch):
    from app.common import ApiError
    release = threading.Event()
    def slow_work():
        release.wait(5)
        return 201, {}, False

    t = threading.Thread(target=lambda:     logic.run_once("slow", {"a": 1}, slow_work))
    t.start()
    time.sleep(0.2)
    with pytest.raises(ApiError) as e:
        logic.run_once("slow", {"a": 1}, lambda: (201, {}, False), wait_seconds=0.3)
    assert e.value.code == "REQUEST_IN_PROGRESS"
    release.set()
    t.join()


def test_refund_is_repeat_safe(client, fake, monkeypatch):
    monkeypatch.setattr(logic, "gateway_refund", lambda ref: None)
    pid = pay(client).json()["id"]
    assert client.post(f"/api/v1/payments/{pid}/refund").json()["state"] == "REFUNDED"
    assert client.post(f"/api/v1/payments/{pid}/refund").json()["state"] == "REFUNDED"
