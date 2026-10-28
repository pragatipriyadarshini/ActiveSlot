"""Small helpers shared by the demo / evidence scripts (not part of any service)."""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = os.environ.get("SCHEDULE_URL", "http://localhost:8001")
BOOKING = os.environ.get("BOOKING_URL", "http://localhost:8002")
PAYMENT = os.environ.get("PAYMENT_URL", "http://localhost:8003")
NOTIFY = os.environ.get("NOTIFICATION_URL", "http://localhost:8004")
GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8005")
SCHEDULE_DB_URL = os.environ.get("SCHEDULE_DB_URL", "postgresql://schedule_user:schedule_pw@localhost:5432/schedule_db")
BOOKING_DB_URL = os.environ.get("BOOKING_DB_URL", "postgresql://booking_user:booking_pw@localhost:5432/booking_db")
PAYMENT_DB_URL = os.environ.get("PAYMENT_DB_URL", "postgresql://payment_user:payment_pw@localhost:5432/payment_db")

http = httpx.Client(timeout=30, limits=httpx.Limits(max_connections=100))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Report:
    """Prints to the terminal AND saves a timestamped transcript under evidence/<folder>/ so the output
    can be attached as evidence (command + output + timestamp)."""

    def __init__(self, folder: str, name: str):
        self.dir = ROOT / "evidence" / folder
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"{name}-{stamp}.txt"
        self.fh = open(self.path, "w", encoding="utf-8")
        self.failures: list[str] = []
        self.say(f"$ python scripts/{Path(sys.argv[0]).name} {' '.join(sys.argv[1:])}".rstrip())
        self.say(f"# started {now_utc().isoformat(timespec='seconds')}")

    def say(self, text: str = "") -> None:
        line = f"[{now_utc().strftime('%H:%M:%S')}] {text}" if text else ""
        print(line)
        self.fh.write(line + "\n")
        self.fh.flush()

    def section(self, title: str) -> None:
        self.say()
        self.say("=" * 78)
        self.say(title)
        self.say("=" * 78)

    def check(self, condition: bool, message: str) -> None:
        self.say(f"  {'PASS' if condition else 'FAIL'}  {message}")
        if not condition:
            self.failures.append(message)

    def finish(self) -> int:
        self.say()
        self.say(f"RESULT: {'ALL CHECKS PASSED' if not self.failures else str(len(self.failures)) + ' CHECK(S) FAILED'}")
        self.say(f"# transcript saved to {self.path.relative_to(ROOT)}")
        self.fh.close()
        return 1 if self.failures else 0


def new_session(capacity: int, title="HIIT 6 AM", price_paise=49900) -> dict:
    r = http.post(f"{SCHEDULE}/api/v1/sessions", json={
        "title": f"{title} ({uuid.uuid4().hex[:4]})", "trainer": "Coach Arjun", "capacity": capacity,
        "price_paise": price_paise, "starts_at": (now_utc() + timedelta(days=2)).isoformat()})
    r.raise_for_status()
    return r.json()


def session_detail(session_id: str) -> dict:
    return http.get(f"{SCHEDULE}/api/v1/sessions/{session_id}").json()


def book(member: str, session_id: str) -> httpx.Response:
    return http.post(f"{BOOKING}/api/v1/bookings", json={"member_id": member, "session_id": session_id})


def pay(booking_id: str, key: str) -> httpx.Response:
    return http.post(f"{BOOKING}/api/v1/bookings/{booking_id}/pay", headers={"Idempotency-Key": key})


def gateway_reset() -> None:
    http.post(f"{GATEWAY}/admin/reset")


def gateway_mode(**kw) -> None:
    http.put(f"{GATEWAY}/admin/mode", json=kw).raise_for_status()


def gateway_stats() -> dict:
    return http.get(f"{GATEWAY}/admin/stats").json()


def compact(resp: httpx.Response, keys=None) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    if keys and isinstance(body, dict) and "error" not in body:
        body = {k: body.get(k) for k in keys}
    if isinstance(body, dict) and "error" in body:
        body = {"error": {"code": body["error"]["code"], "message": body["error"]["message"]}}
    return f"HTTP {resp.status_code} {json.dumps(body)}"


def db_query(url: str, sql: str, params=None):
    """Optional direct look at a service's own database (for evidence). Returns rows or None if unreachable."""
    try:
        import psycopg2
        with psycopg2.connect(url, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        return f"(database not reachable from here: {type(exc).__name__})"
