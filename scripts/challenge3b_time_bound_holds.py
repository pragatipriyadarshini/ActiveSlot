#!/usr/bin/env python3
"""Challenge 3B - time-bound holds. Unpaid seats are released automatically after HOLD_SECONDS,
cancellation releases them at once, paid seats are never released, and the expiry job is safe to run twice.

Start the stack with a short hold so the demo is quick:
    python scripts/run_all.py --set booking-service:HOLD_SECONDS=30 --set schedule-service:EXPIRY_SWEEP_INTERVAL_SECONDS=2
    python scripts/challenge3b_time_bound_holds.py"""
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone

from _lib import (BOOKING, SCHEDULE, SCHEDULE_DB_URL, Report, book, compact, db_query, http, new_session, now_utc,
                  parse_ts, pay, session_detail)

r = Report("challenge-3b-time-bound-holds", "holds")


def snapshot(label, sid):
    d = session_detail(sid)
    r.say(f"{label:<42} available={d['available_seats']} held={d['seats_held']} confirmed={d['seats_confirmed']} consistent={d['consistent']}")
    return d


r.section("1. Cancellation releases a held seat immediately")
s = new_session(capacity=2)
x = book("member-x", s["id"]).json()
snapshot("after booking X (unpaid)", s["id"])
resp = http.post(f"{BOOKING}/api/v1/bookings/{x['id']}/cancel")
r.say(f"cancel X -> {compact(resp, ['id', 'state'])}")
d = snapshot("after cancelling X", s["id"])
r.check(d["available_seats"] == 2, "seat returned to the pool at once")

r.section("2. Set up: A pays (permanent seat), B only holds (will expire)")
a = book("member-a", s["id"]).json()
pay(a["id"], f"pay-{uuid.uuid4()}")
booking_response = book("member-b", s["id"])
if booking_response.status_code != 201:
    raise RuntimeError(
        f"Could not create the booking that should expire: "
        f"HTTP {booking_response.status_code} {booking_response.text}"
    )
b = booking_response.json()
d = snapshot("A confirmed, B held", s["id"])
r.check(d["available_seats"] == 0 and d["seats_confirmed"] == 1 and d["seats_held"] == 1, "1 confirmed + 1 held = full")
r.check(book("member-c", s["id"]).status_code == 409, "a third member is rejected: class full")
expires_value = b.get("hold_expires_at")
if not expires_value:
    raise RuntimeError(f"Booking response has no hold_expires_at: {b}")
expires = parse_ts(expires_value)
if expires.tzinfo is None:
    expires = expires.replace(tzinfo=timezone.utc)
now = now_utc()
if now.tzinfo is None:
    now = now.replace(tzinfo=timezone.utc)
wait = (expires - now).total_seconds()
r.say(f"B's hold expires at {b['hold_expires_at']} (in {wait:.0f}s). Waiting for the automatic release ...")
deadline = time.monotonic() + max(wait, 0) + 6
while time.monotonic() < deadline:
    remaining = deadline - time.monotonic()
    time.sleep(min(1, max(remaining, 0.1)))
    r.say(f"  ... {max(int(deadline - time.monotonic()), 0)}s left")

r.section("3. After expiry: B's seat is back, A's paid seat was NOT touched")
d = snapshot("after expiry", s["id"])
r.check(d["available_seats"] == 1 and d["seats_confirmed"] == 1 and d["seats_held"] == 0, "B's seat released, A's paid seat kept")
bb = http.get(f"{BOOKING}/api/v1/bookings/{b['id']}").json()
aa = http.get(f"{BOOKING}/api/v1/bookings/{a['id']}").json()
r.say(f"booking B -> {bb['state']}    booking A -> {aa['state']}")
r.check(bb["state"] == "EXPIRED" and aa["state"] == "CONFIRMED", "B is EXPIRED, A is still CONFIRMED")
resp = pay(b["id"], f"pay-{uuid.uuid4()}")
r.say(f"B tries to pay late -> {compact(resp)}")
r.check(resp.status_code == 409 and resp.json()["error"]["code"] == "HOLD_EXPIRED", "late payment is refused (and nothing is charged)")
r.check(book("member-b", s["id"]).status_code == 201, "B can book again (BR3 only blocks ACTIVE bookings)")

r.section("4. The expiry job is safe to run twice, even at the same moment")
s2 = new_session(capacity=3)
h = http.post(f"{SCHEDULE}/api/v1/sessions/{s2['id']}/holds", json={"booking_id": f"b-{uuid.uuid4()}", "hold_seconds": 1}).json()
snapshot("one 1-second hold taken", s2["id"])
time.sleep(1.3)
barrier = threading.Barrier(3)


def run_expiry(_):
    barrier.wait()
    response = http.post(f"{SCHEDULE}/api/v1/internal/holds/expire")
    if response.status_code != 200:
        raise RuntimeError(
            f"Expiry endpoint failed: HTTP {response.status_code} {response.text}"
        )
    body = response.json()
    if "released" not in body:
        raise RuntimeError(f"Expiry endpoint response has no 'released' field: {body}")
    return body["released"]


with ThreadPoolExecutor(max_workers=3) as pool:   # 2 manual runs racing each other and the background sweeper
    released = list(pool.map(run_expiry, range(3)))
r.say(f"3 simultaneous manual runs released: {released}")
time.sleep(2.5)
d = snapshot("after all runs", s2["id"])
r.check(sum(released) <= 1, "the single expired hold was released at most once by the manual runs")
r.check(d["available_seats"] == 3 and d["consistent"], "available_seats is 3, NOT 4: no double release")
rows = db_query(SCHEDULE_DB_URL, "SELECT state, count(*) FROM seat_holds WHERE session_id=%s GROUP BY state", (s2["id"],))
r.say(f"  DB seat_holds by state: {rows}")
sys.exit(r.finish())
