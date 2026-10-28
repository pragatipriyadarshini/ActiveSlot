#!/usr/bin/env python3
"""Scripted end-to-end test of the main transaction (hold seat -> pay -> confirm -> notify),
including failure paths. Exit code 0 = everything behaved as designed.
Run with the stack up:  python scripts/e2e_main_flow.py"""
import sys
import uuid

from _lib import *  # noqa: F401,F403
from _lib import (BOOKING, NOTIFY, PAYMENT, Report, book, compact, gateway_mode, gateway_reset, gateway_stats, http,
                  new_session, pay, session_detail)

r = Report("tests", "e2e-main-flow")
KEYS = ["id", "state", "amount_paise", "payment_id"]
gateway_reset()

r.section("1. HAPPY PATH: book -> pay -> confirmed -> SMS")
s = new_session(capacity=3)
r.say(f"class created: {s['id']} capacity=3")
resp = book("member-1", s["id"])
r.say(f"POST /bookings            -> {compact(resp, KEYS)}")
b = resp.json()
r.check(resp.status_code == 201 and b["state"] == "PENDING_PAYMENT", "booking created as PENDING_PAYMENT with a seat held")
key = f"pay-{uuid.uuid4()}"
resp = pay(b["id"], key)
r.say(f"POST /bookings/{{id}}/pay   -> {compact(resp, KEYS)}")
r.check(resp.status_code == 200 and resp.json()["state"] == "CONFIRMED", "booking CONFIRMED after payment")
d = session_detail(s["id"])
r.say(f"class state: available={d['available_seats']} held={d['seats_held']} confirmed={d['seats_confirmed']} consistent={d['consistent']}")
r.check(d["available_seats"] == 2 and d["seats_confirmed"] == 1 and d["consistent"], "seat is confirmed and counts are consistent")
notes = http.get(f"{NOTIFY}/api/v1/notifications", params={"booking_id": b["id"]}).json()
r.check(len(notes) == 1 and notes[0]["state"] == "SENT", "exactly one confirmation SMS was sent")

r.section("2. FAILURE PATH: payment gateway DOWN during payment")
s2 = new_session(capacity=2)
b2 = book("member-2", s2["id"]).json()
gateway_mode(payment_mode="fail")
key2 = f"pay-{uuid.uuid4()}"
resp = pay(b2["id"], key2)
r.say(f"gateway=fail, pay            -> {compact(resp)}")
r.check(resp.status_code == 503 and resp.json()["error"]["code"] == "GATEWAY_UNAVAILABLE", "clear 503 error, no crash, no hanging")
state = http.get(f"{BOOKING}/api/v1/bookings/{b2['id']}").json()["state"]
r.check(state == "PENDING_PAYMENT", "booking is still PENDING_PAYMENT (seat still held, nothing charged)")
gateway_mode(payment_mode="ok")
resp = pay(b2["id"], key2)
r.say(f"gateway restored, SAME key   -> {compact(resp, KEYS)}")
r.check(resp.status_code == 200 and resp.json()["state"] == "CONFIRMED", "retry with the same Idempotency-Key succeeds")
pays = http.get(f"{PAYMENT}/api/v1/payments", params={"booking_id": b2["id"]}).json()
r.say(f"payment rows: {[p['state'] for p in pays]}")
r.check(sorted(p["state"] for p in pays) == ["CAPTURED", "FAILED"], "one CAPTURED payment (the FAILED row is the audit trail)")
r.check(gateway_stats()["distinct_charges"] == 2, "gateway holds exactly one charge per paid booking (no double charge)")

r.section("3. FAILURE PATH: card declined, then cancel releases the seat")
s3 = new_session(capacity=1)
b3 = book("member-3", s3["id"]).json()
gateway_mode(payment_mode="decline")
resp = pay(b3["id"], f"pay-{uuid.uuid4()}")
r.say(f"gateway=decline, pay         -> {compact(resp)}")
r.check(resp.status_code == 402 and resp.json()["error"]["code"] == "PAYMENT_DECLINED", "402 PAYMENT_DECLINED")
gateway_mode(payment_mode="ok")
r.check(book("member-4", s3["id"]).status_code == 409, "class (capacity 1) is still full while the seat is held")
resp = http.post(f"{BOOKING}/api/v1/bookings/{b3['id']}/cancel")
r.say(f"POST /bookings/{{id}}/cancel  -> {compact(resp, KEYS)}")
r.check(resp.json()["state"] == "CANCELLED" and session_detail(s3["id"])["available_seats"] == 1, "cancel releases the seat immediately")
r.check(book("member-4", s3["id"]).status_code == 201, "the freed seat can be booked by someone else")

r.section("4. FAILURE PATH: SMS provider down must NOT break the booking (BR7)")
s4 = new_session(capacity=1)
b4 = book("member-5", s4["id"]).json()
gateway_mode(sms_mode="fail")
resp = pay(b4["id"], f"pay-{uuid.uuid4()}")
r.say(f"sms=fail, pay               -> {compact(resp, KEYS)}")
r.check(resp.status_code == 200 and resp.json()["state"] == "CONFIRMED", "booking is CONFIRMED even though the SMS failed")
notes = http.get(f"{NOTIFY}/api/v1/notifications", params={"booking_id": b4["id"]}).json()
r.check(notes and notes[0]["state"] == "FAILED", "notification service recorded the failed delivery")
gateway_mode(sms_mode="ok")

r.section("5. VALIDATION and ERROR FORMAT")
resp = http.post(f"{BOOKING}/api/v1/bookings", json={"member_id": ""})
r.say(f"bad request                  -> {compact(resp)}")
r.check(resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR", "422 with the common error format")
resp = http.post(f"{BOOKING}/api/v1/bookings/{b['id']}/cancel")
r.check(resp.status_code == 409, "cancelling a paid booking is refused (state machine, BR5)")

gateway_reset()
sys.exit(r.finish())
