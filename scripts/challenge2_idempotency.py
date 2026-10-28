#!/usr/bin/env python3
"""Challenge 2 - idempotency. Proves a repeated or simultaneous payment request is charged ONCE.

    python scripts/challenge2_idempotency.py"""
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from _lib import (BOOKING, NOTIFY, PAYMENT, PAYMENT_DB_URL, ROOT, Report, book, compact, db_query, gateway_reset,
                  gateway_stats, http, new_session, pay)

r = Report("challenge-2-idempotency", "idempotency")
gateway_reset()


def parallel(n, fn):
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        return fn(i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(worker, range(n)))


def charge(body, key):
    return http.post(f"{PAYMENT}/api/v1/payments", json=body, headers={"Idempotency-Key": key})


s = new_session(capacity=50)

r.section("A. Sequential repeat (payment-service, same key twice)")
body = {"booking_id": f"bk-{uuid.uuid4().hex[:8]}", "member_id": "member-1", "amount_paise": 49900}
key = f"key-{uuid.uuid4()}"
first, second = charge(body, key), charge(body, key)
r.say(f"1st call  : {compact(first)}   Idempotent-Replayed={first.headers.get('idempotent-replayed')}")
r.say(f"2nd call  : {compact(second)}   Idempotent-Replayed={second.headers.get('idempotent-replayed')}")
r.check(first.status_code == second.status_code == 201 and first.json() == second.json(), "same key -> identical response")
r.check(second.headers.get("idempotent-replayed") == "true", "second response is flagged as a replay")
r.check(gateway_stats()["charge_calls"] == 1, f"the gateway was called ONCE (charge_calls={gateway_stats()['charge_calls']})")

r.section("B. Same key, DIFFERENT payload must be rejected")
other = charge({**body, "amount_paise": 100}, key)
r.say(f"reuse key with other amount: {compact(other)}")
r.check(other.status_code == 422 and other.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED", "422 IDEMPOTENCY_KEY_REUSED")
r.check(gateway_stats()["charge_calls"] == 1, "still only one gateway call")

r.section("C. TWO-OR-MORE IDENTICAL REQUESTS AT THE SAME MOMENT (payment-service, 10 in parallel)")
gateway_reset()
body = {"booking_id": f"bk-{uuid.uuid4().hex[:8]}", "member_id": "member-2", "amount_paise": 49900}
key = f"key-{uuid.uuid4()}"
results = parallel(10, lambda i: charge(body, key))
ids = {x.json().get("id") for x in results}
replays = sum(x.headers.get("idempotent-replayed") == "true" for x in results)
r.say(f"status codes: {sorted(x.status_code for x in results)}   distinct payment ids: {len(ids)}   replayed responses: {replays}")
r.check(all(x.status_code == 201 for x in results) and len(ids) == 1, "all 10 callers received the SAME payment")
r.check(replays == 9, "1 request did the work, 9 were served the stored result")
st = gateway_stats()
r.check(st["charge_calls"] == 1, f"gateway charge_calls={st['charge_calls']} (must be 1)")
rows = db_query(PAYMENT_DB_URL, "SELECT state, count(*) FROM payments WHERE booking_id=%s GROUP BY state", (body["booking_id"],))
r.say(f"  DB payments for this booking by state: {rows}")
api_rows = http.get(f"{PAYMENT}/api/v1/payments", params={"booking_id": body["booking_id"]}).json()
r.check(len(api_rows) == 1, "exactly ONE payment record exists for the booking")

r.section("D. END TO END: 10 parallel 'Pay' clicks on one booking (booking-service -> payment-service)")
gateway_reset()
b = book("member-3", s["id"]).json()
key = f"key-{uuid.uuid4()}"
results = parallel(10, lambda i: pay(b["id"], key))
r.say(f"status codes: {sorted(x.status_code for x in results)}")
bodies = {x.json().get("payment_id") for x in results}
r.check(all(x.status_code == 200 and x.json()["state"] == "CONFIRMED" for x in results), "all 10 responses: 200 CONFIRMED")
r.check(len(bodies) == 1, f"all 10 report the same payment_id {bodies}")
r.check(gateway_stats()["charge_calls"] == 1, f"gateway charge_calls={gateway_stats()['charge_calls']} (must be 1)")
pays = http.get(f"{PAYMENT}/api/v1/payments", params={"booking_id": b["id"]}).json()
r.check(len(pays) == 1 and pays[0]["state"] == "CAPTURED", "database shows a single CAPTURED payment")
notes = http.get(f"{NOTIFY}/api/v1/notifications", params={"booking_id": b["id"]}).json()
r.check(len(notes) == 1, f"member received exactly one SMS (got {len(notes)})")
r.say("sequential repeat later:")
again = pay(b["id"], key)
r.say(f"  {compact(again, ['id', 'state', 'payment_id'])}")
r.check(again.status_code == 200 and gateway_stats()["charge_calls"] == 1, "a late repeat is also replayed, no new charge")

r.section("E. Log evidence: replays are visible in payment-service's structured log")
log_path = ROOT / "logs" / "payment-service.log"
if log_path.exists():
    lines = [ln for ln in log_path.read_text().splitlines() if "idempotent_replay" in ln]
    out = r.dir / "payment-service-replay-log-lines.txt"
    out.write_text("\n".join(lines) + "\n")
    r.say(f"{len(lines)} 'idempotent_replay' log lines (saved to {out.relative_to(ROOT)}); last 2:")
    for ln in lines[-2:]:
        r.say("  " + ln)
    r.check(len(lines) >= 10, "replay events are logged")
else:
    r.say("logs/payment-service.log not found (services were not started with scripts/run_all.py); skip")
gateway_reset()
sys.exit(r.finish())
