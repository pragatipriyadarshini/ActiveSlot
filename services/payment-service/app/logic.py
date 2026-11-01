"""Payment workflows, idempotency handling, and calls to the payment gateway."""
import hashlib
import json
import os
import time
from typing import Callable, NamedTuple

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .common import ApiError, error_body, log, outbound_headers, utcnow
from .models import (
    CAPTURED,
    DECLINED,
    FAILED,
    PENDING,
    REFUNDED,
    IdempotencyRecord,
    Payment,
    SessionLocal,
)


# Gateway connection details can be changed without editing service code.
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8005")
GATEWAY_TIMEOUT = float(os.environ.get("GATEWAY_TIMEOUT_SECONDS", "3"))
IDEMPOTENCY_WAIT_SECONDS = float(os.environ.get("IDEMPOTENCY_WAIT_SECONDS", "10"))


class GatewayDeclined(Exception):
    """The gateway rejected the payment."""


class GatewayUnavailable(Exception):
    """The gateway timed out or returned a server error."""


# These functions keep network details out of the payment workflow.
def gateway_charge(reference: str, amount_paise: int) -> dict:
    try:
        response = httpx.post(
            f"{GATEWAY_URL}/charge",
            json={"reference": reference, "amount_paise": amount_paise},
            headers=outbound_headers(),
            timeout=GATEWAY_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise GatewayUnavailable(type(exc).__name__) from exc
    if response.status_code == 402:
        raise GatewayDeclined(response.json().get("error", {}).get("message", "declined"))
    if response.status_code >= 500:
        raise GatewayUnavailable(f"gateway returned {response.status_code}")
    response.raise_for_status()
    return response.json()


def gateway_refund(gateway_ref: str) -> None:
    try:
        httpx.post(
            f"{GATEWAY_URL}/refund",
            json={"gateway_ref": gateway_ref},
            headers=outbound_headers(),
            timeout=GATEWAY_TIMEOUT,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        raise GatewayUnavailable(type(exc).__name__) from exc


# Store the request fingerprint and final response to make retries safe.
def fingerprint(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _claim(key: str, request_fingerprint: str) -> bool:
    with SessionLocal() as db:
        db.add(
            IdempotencyRecord(
                key=key,
                fingerprint=request_fingerprint,
                status="IN_PROGRESS",
            )
        )
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False


class _Snapshot(NamedTuple):
    fingerprint: str
    status: str
    response_status: int | None
    response_body: str | None


def _read(key: str) -> _Snapshot | None:
    with SessionLocal() as db:
        record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.key == key))
        snapshot = None if record is None else _Snapshot(
            record.fingerprint,
            record.status,
            record.response_status,
            record.response_body,
        )
        db.rollback()
        return snapshot


def run_once(
    key: str,
    payload: dict,
    work: Callable[[], tuple[int, dict, bool]],
    wait_seconds: float = IDEMPOTENCY_WAIT_SECONDS,
) -> tuple[int, dict, bool]:
    """Run a payment once, replay its result, or release a retryable failure."""
    request_fingerprint = fingerprint(payload)
    deadline = time.monotonic() + wait_seconds

    while True:
        if _claim(key, request_fingerprint):
            try:
                status, body, retryable = work()
            except Exception:
                _release(key)
                raise
            if retryable:
                _release(key)
                return status, body, False
            with SessionLocal() as db:
                db.execute(
                    update(IdempotencyRecord)
                    .where(IdempotencyRecord.key == key)
                    .values(
                        status="COMPLETED",
                        response_status=status,
                        response_body=json.dumps(body),
                        completed_at=utcnow(),
                    )
                )
                db.commit()
            return status, body, False

        record = _read(key)
        if record is None:
            continue
        if record.fingerprint != request_fingerprint:
            raise ApiError(
                422,
                "IDEMPOTENCY_KEY_REUSED",
                "This Idempotency-Key was already used with a different request payload",
            )
        if record.status == "COMPLETED":
            log.info(
                "idempotent_replay",
                extra={"idempotency_key": key, "stored_status": record.response_status},
            )
            return record.response_status, json.loads(record.response_body), True
        if time.monotonic() > deadline:
            raise ApiError(
                409,
                "REQUEST_IN_PROGRESS",
                "The original request with this key is still running; retry shortly",
            )
        time.sleep(0.05)


def _release(key: str) -> None:
    with SessionLocal() as db:
        db.execute(delete(IdempotencyRecord).where(IdempotencyRecord.key == key))
        db.commit()


# Payment operations write an audit record for each gateway attempt.
def payment_dict(payment: Payment) -> dict:
    return {
        "id": payment.id,
        "booking_id": payment.booking_id,
        "member_id": payment.member_id,
        "amount_paise": payment.amount_paise,
        "state": payment.state,
        "gateway_ref": payment.gateway_ref,
        "created_at": payment.created_at.isoformat(timespec="seconds") + "Z",
    }


def charge_payment(key: str, booking_id: str, member_id: str, amount_paise: int):
    """Charge once and return the HTTP status, response body, and retryable flag."""
    with SessionLocal() as db:
        payment = Payment(
            booking_id=booking_id,
            member_id=member_id,
            amount_paise=amount_paise,
            state=PENDING,
            idempotency_key=key,
        )
        db.add(payment)
        db.commit()
        try:
            result = gateway_charge(reference=key, amount_paise=amount_paise)
        except GatewayDeclined as exc:
            _set_state(db, payment, DECLINED)
            log.info("payment_declined", extra={"payment_id": payment.id, "booking_id": booking_id})
            return 402, error_body("PAYMENT_DECLINED", str(exc)), False
        except GatewayUnavailable as exc:
            _set_state(db, payment, FAILED)
            log.warning("gateway_unavailable", extra={"payment_id": payment.id, "reason": str(exc)})
            return 503, error_body(
                "GATEWAY_UNAVAILABLE",
                "Payment gateway is unavailable; nothing was charged, retry with the same Idempotency-Key",
            ), True
        payment.gateway_ref = result["gateway_ref"]
        _set_state(db, payment, CAPTURED)
        log.info(
            "payment_captured",
            extra={"payment_id": payment.id, "booking_id": booking_id, "amount_paise": amount_paise},
        )
        return 201, payment_dict(payment), False


def _set_state(db: Session, payment: Payment, state: str) -> None:
    payment.state = state
    payment.updated_at = utcnow()
    db.commit()


def refund_payment(db: Session, payment_id: str) -> Payment:
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise ApiError(404, "PAYMENT_NOT_FOUND", "Payment does not exist")
    if payment.state == REFUNDED:
        return payment
    if payment.state != CAPTURED:
        raise ApiError(
            409,
            "INVALID_STATE",
            f"Only CAPTURED payments can be refunded (this one is {payment.state})",
        )
    try:
        gateway_refund(payment.gateway_ref)
    except GatewayUnavailable:
        raise ApiError(503, "GATEWAY_UNAVAILABLE", "Gateway unavailable; refund not done, retry later")
    changed = db.execute(
        update(Payment)
        .where(Payment.id == payment_id, Payment.state == CAPTURED)
        .values(state=REFUNDED, updated_at=utcnow())
    ).rowcount
    db.commit()
    db.refresh(payment)
    if changed:
        log.info("payment_refunded", extra={"payment_id": payment_id})
    return payment
