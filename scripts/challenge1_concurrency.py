#!/usr/bin/env python3
"""Challenge 1 - concurrency control. Fires parallel requests for a class with limited seats.

  Phase A (the BUG):  check-then-update in application code  -> over-allocates
  Phase B (the FIX):  real booking flow (atomic conditional UPDATE) -> exactly N succeed

Needs the stack started with:  schedule-service:ENABLE_NAIVE_DEMO=true  (see README)
    python scripts/challenge1_concurrency.py [--requests 20] [--capacity 5]"""
import argparse
import sys
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from _lib import SCHEDULE, SCHEDULE_DB_URL, Report, book, db_query, http, new_session, session_detail

ap = argparse.ArgumentParser()
#requests
ap.add_argument("--requests", type=int, default=30)
#capacity
ap.add_argument("--capacity", type=int, default=10)
args = ap.parse_args()
N, CAP = args.requests, args.capacity
r = Report("challenge-1-concurrency", "concurrency")


def fire(fn):
    """Run fn(i) for N workers released at exactly the same moment."""
    barrier = threading.Barrier(N)

    def worker(i):
        barrier.wait()
        return fn(i)
    with ThreadPoolExecutor(max_workers=N) as pool:
        return list(pool.map(worker, range(N)))


def db_state(session_id):
    sess = db_query(SCHEDULE_DB_URL, "SELECT capacity, available_seats FROM class_sessions WHERE id=%s", (session_id,))
    holds = db_query(SCHEDULE_DB_URL, "SELECT state, count(*) FROM seat_holds WHERE session_id=%s GROUP BY state", (session_id,))
    r.say(f"  DB  class_sessions (capacity, available_seats): {sess}")
    r.say(f"  DB  seat_holds by state                       : {holds}")
    return holds


r.section(f"PHASE A test: {N} parallel requests, capacity {CAP}")
s = new_session(CAP)
naive = fire(lambda i: http.post(f"{SCHEDULE}/api/v1/sessions/{s['id']}/holds-naive",
                                 json={"booking_id": f"naive-{uuid.uuid4()}", "hold_seconds": 300}))
codes = Counter(x.status_code for x in naive)
r.say(f"HTTP status codes: {dict(codes)}")
if codes.get(404):
    r.say("holds-naive endpoint not found: start schedule-service with ENABLE_NAIVE_DEMO=true to run phase A")
else:
    d = session_detail(s["id"])
    over = codes.get(201, 0)
    r.say(f"seats granted: {over} for a class with capacity {CAP}")
    r.say(f"API : available_seats={d['available_seats']} held={d['seats_held']} consistent={d['consistent']}")
    db_state(s["id"])
    if over > CAP:
        r.check(True, f"BUG REPRODUCED: {over} people got a seat in a class of {CAP} (over-allocation)")
    else:  # SQLite (--sqlite quick mode) lets only one writer at a time, which hides this race
        r.say(f"NOTE: bug not reproduced ({over} granted). SQLite serialises writers; use PostgreSQL to capture evidence.")
    r.say("Why: every request READ available_seats>0, then (50 ms later) WROTE a value computed from what it had read.")
    r.say("     Between the read and the write other requests did the same, so the check no longer meant anything.")

r.section(f"PHASE B test: {N} parallel requests, capacity {CAP}")
s = new_session(CAP)
real = fire(lambda i: book(f"member-{i}-{uuid.uuid4().hex[:6]}", s["id"]))
codes = Counter(x.status_code for x in real)
r.say(f"HTTP status codes: {dict(codes)}")
rejected = [x for x in real if x.status_code == 409]
r.say(f"sample rejection: {rejected[0].json()['error'] if rejected else None}")
d = session_detail(s["id"])
r.say(f"API : available_seats={d['available_seats']} held={d['seats_held']} consistent={d['consistent']}")
holds = db_state(s["id"])
r.check(codes.get(201, 0) == CAP, f"exactly {CAP} requests succeeded (got {codes.get(201, 0)})")
r.check(codes.get(409, 0) == N - CAP, f"the other {N - CAP} were rejected with 409 SESSION_FULL (got {codes.get(409, 0)})")
r.check(all(x.json()["error"]["code"] == "SESSION_FULL" for x in rejected), "every rejection carries the meaningful code SESSION_FULL")
r.check(d["available_seats"] == 0 and d["seats_held"] == CAP and d["consistent"], "database state is consistent afterwards (0 free, N held)")
sys.exit(r.finish())
