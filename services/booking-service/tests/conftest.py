import os
import tempfile
from datetime import timedelta

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/booking_test.db"
os.environ["HOLD_SECONDS"] = "30"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.logic import UpstreamError, UpstreamUnavailable  # noqa: E402
from app.common import iso, utcnow  # noqa: E402
from app.models import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


class FakeWorld:
    """In-memory stand-ins for schedule, payment and notification services (no network in unit tests)."""
    def __init__(self):
        self.seats = 5
        self.holds = {}            # hold_id -> state
        self.expires = {}
        self.charges = {}          # key -> payment
        self.charge_calls = 0
        self.refunds = []
        self.notifications = []
        self.gateway_down = False
        self.notify_down = False
        self.confirm_fails_with = None

    def create_hold(self, session_id, booking_id, hold_seconds):
        if self.seats <= 0:
            raise UpstreamError(409, "SESSION_FULL", "full")
        self.seats -= 1
        hid = f"hold-{len(self.holds) + 1}"
        self.holds[hid] = "HELD"
        self.expires[hid] = utcnow() + timedelta(seconds=hold_seconds)
        return {"id": hid, "state": "HELD", "price_paise": 49900, "expires_at": iso(self.expires[hid])}

    def get_hold(self, hold_id):
        return {"id": hold_id, "state": self.holds[hold_id]}

    def confirm_hold(self, hold_id):
        if self.confirm_fails_with:
            raise self.confirm_fails_with
        if self.holds[hold_id] not in ("HELD", "CONFIRMED"):
            raise UpstreamError(409, "HOLD_NOT_ACTIVE", "gone")
        self.holds[hold_id] = "CONFIRMED"
        return {"id": hold_id, "state": "CONFIRMED"}

    def release_hold(self, hold_id):
        if self.holds[hold_id] == "CONFIRMED":
            raise UpstreamError(409, "HOLD_ALREADY_CONFIRMED", "paid")
        if self.holds[hold_id] == "HELD":
            self.seats += 1
        self.holds[hold_id] = "RELEASED"
        return {"id": hold_id, "state": "RELEASED"}

    def charge(self, key, booking_id, member_id, amount_paise):
        if self.gateway_down:
            raise UpstreamError(503, "GATEWAY_UNAVAILABLE", "down")
        if key not in self.charges:
            self.charge_calls += 1
            self.charges[key] = {"id": f"pay-{len(self.charges) + 1}", "state": "CAPTURED"}
        return self.charges[key]

    def refund(self, payment_id):
        self.refunds.append(payment_id)
        return {}

    def notify(self, booking_id, member_id, type_, message):
        if self.notify_down:
            raise UpstreamUnavailable("ConnectError")
        self.notifications.append(booking_id)
        return {}


@pytest.fixture
def world(monkeypatch):
    w = FakeWorld()
    for name in ("create_hold", "get_hold", "confirm_hold", "release_hold", "charge", "refund", "notify"):
        monkeypatch.setattr("app.logic." + name, getattr(w, name))
    return w


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
