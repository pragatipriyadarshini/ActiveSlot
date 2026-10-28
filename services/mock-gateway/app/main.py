"""Mock payment gateway + SMS provider (assignment requirement: payment and notification systems
must be our own mocks). Stateless apart from in-memory counters; behaviour is switchable at runtime
through /admin/mode so demos can make it slow, failing or declining on demand."""
import asyncio
import os
import threading
import uuid

from fastapi import FastAPI
from pydantic import BaseModel, Field

from . import common
from .common import ApiError

app = FastAPI(title="Mock Gateway", version="1.0.0",
              description="Simulates a payment gateway and an SMS provider that can be slow or fail.")
common.install(app)

_lock = threading.Lock()
_state = {
    "payment_mode": os.environ.get("PAYMENT_MODE", "ok"),        # ok | fail | decline
    "payment_delay_ms": int(os.environ.get("PAYMENT_DELAY_MS", "300")),
    "sms_mode": os.environ.get("SMS_MODE", "ok"),                # ok | fail
    "sms_delay_ms": int(os.environ.get("SMS_DELAY_MS", "50")),
}
_charges: dict[str, dict] = {}   # reference -> result (gateways de-duplicate on their reference)
_calls = {"charge_calls": 0, "refund_calls": 0, "sms_calls": 0}


class ChargeIn(BaseModel):
    reference: str = Field(min_length=1, description="Caller's unique reference (we send our Idempotency-Key)")
    amount_paise: int = Field(ge=1)


class RefundIn(BaseModel):
    gateway_ref: str


class SmsIn(BaseModel):
    to: str
    text: str


class ModeIn(BaseModel):
    payment_mode: str | None = Field(default=None, pattern="^(ok|fail|decline)$")
    payment_delay_ms: int | None = Field(default=None, ge=0, le=60000)
    sms_mode: str | None = Field(default=None, pattern="^(ok|fail)$")
    sms_delay_ms: int | None = Field(default=None, ge=0, le=60000)


# Health check for the launcher.
@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok", "service": "mock-gateway"}


# Simulate a payment success, decline, or temporary outage.
@app.post("/charge", tags=["gateway"])
async def charge(body: ChargeIn):
    with _lock:
        _calls["charge_calls"] += 1
        mode, delay = _state["payment_mode"], _state["payment_delay_ms"]
    await asyncio.sleep(delay / 1000)
    if mode == "fail":
        raise ApiError(503, "GATEWAY_DOWN", "Simulated gateway outage")
    if mode == "decline":
        raise ApiError(402, "CARD_DECLINED", "Simulated card decline")
    with _lock:
        result = _charges.setdefault(body.reference, {"gateway_ref": f"gw_{uuid.uuid4().hex[:12]}",
                                                      "amount_paise": body.amount_paise, "status": "captured"})
    return result


# Refunds are accepted by the mock provider for compensation flows.
@app.post("/refund", tags=["gateway"])
async def refund(body: RefundIn):
    with _lock:
        _calls["refund_calls"] += 1
    return {"gateway_ref": body.gateway_ref, "status": "refunded"}


# Simulate SMS latency or provider failure.
@app.post("/sms/send", tags=["sms"])
async def send_sms(body: SmsIn):
    with _lock:
        _calls["sms_calls"] += 1
        mode, delay = _state["sms_mode"], _state["sms_delay_ms"]
    await asyncio.sleep(delay / 1000)
    if mode == "fail":
        raise ApiError(503, "SMS_PROVIDER_DOWN", "Simulated SMS provider outage")
    return {"status": "sent", "message_id": f"sms_{uuid.uuid4().hex[:10]}"}


# Admin routes let demo scripts change behavior and inspect/reset counters.
@app.get("/admin/mode", tags=["admin"])
def get_mode():
    return _state


@app.put("/admin/mode", tags=["admin"])
def set_mode(body: ModeIn):
    with _lock:
        _state.update({k: v for k, v in body.model_dump().items() if v is not None})
        return _state


@app.get("/admin/stats", tags=["admin"])
def stats():
    """How many times the gateway was really called, and how many distinct charges exist.
    Evidence for Challenge 2: N repeated requests must still show exactly one charge call."""
    with _lock:
        return {**_calls, "distinct_charges": len(_charges), "references": sorted(_charges)}


@app.post("/admin/reset", tags=["admin"])
def reset():
    with _lock:
        _charges.clear()
        for k in _calls:
            _calls[k] = 0
        _state.update(payment_mode="ok", payment_delay_ms=300, sms_mode="ok", sms_delay_ms=50)
    return {"status": "reset"}
