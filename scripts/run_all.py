#!/usr/bin/env python3
"""Start all five processes (4 services + mock gateway) with one command and stop them with Ctrl+C.

    python scripts/run_all.py              # PostgreSQL (docker compose up -d first)
    python scripts/run_all.py --sqlite     # no Docker/Postgres needed: quick try-out mode
    python scripts/run_all.py --set booking-service:HOLD_SECONDS=30 --set schedule-service:EXPIRY_SWEEP_INTERVAL_SECONDS=2

Configuration comes from each service's `.env` (or `.env.example` if there is no `.env`); --set overrides it.
Logs go to logs/<service>.log (structured JSON, one line per event)."""
import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = (
    "mock-gateway",
    "schedule-service",
    "payment-service",
    "notification-service",
    "booking-service",
)


def load_env(service_dir: Path) -> dict[str, str]:
    path = service_dir / ".env"
    if not path.exists():
        path = service_dir / ".env.example"
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def wait_healthy(port: str, name: str, timeout: int = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise SystemExit(f"{name} did not become healthy on port {port}; see logs/{name}.log")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", action="store_true", help="use SQLite files in .data/ instead of PostgreSQL")
    ap.add_argument("--set", action="append", default=[], metavar="SERVICE:KEY=VALUE")
    args = ap.parse_args()

    overrides: dict[str, dict] = {}
    for item in args.set:
        svc, kv = item.split(":", 1)
        k, v = kv.split("=", 1)
        overrides.setdefault(svc, {})[k] = v

    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / ".data").mkdir(exist_ok=True)
    procs = []

    def stop_processes() -> None:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

    def request_stop(*_) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        for name in SERVICES:
            sdir = ROOT / "services" / name
            env = {**os.environ, **load_env(sdir), **overrides.get(name, {})}
            if args.sqlite and "DATABASE_URL" in env:
                db_file = ROOT / ".data" / f"{name}.db"
                if db_file.exists():
                    db_file.unlink()
                env["DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"

            with (ROOT / "logs" / f"{name}.log").open("w") as log_file:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "app.main:app",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        env["PORT"],
                        "--log-level",
                        "warning",
                    ],
                    cwd=sdir,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
            procs.append(process)
            wait_healthy(env["PORT"], name)
            print(f"  up   {name:<21} http://localhost:{env['PORT']}   (docs: /docs)", flush=True)

        print("\nAll services are running. Press Ctrl+C to stop.", flush=True)
        while True:
            for process, name in zip(procs, SERVICES):
                if process.poll() is not None:
                    raise SystemExit(f"{name} exited unexpectedly; see logs/{name}.log")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping services...", flush=True)
    finally:
        stop_processes()
        print("All services stopped.", flush=True)


if __name__ == "__main__":
    main()
