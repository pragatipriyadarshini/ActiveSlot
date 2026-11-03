# FitSlot — fitness class booking system

A CultFit-style application: members browse fitness classes, **hold a seat**, **pay**, get **confirmed**
and receive an **SMS**.

## Services

schedule-service -8001
booking-service -8002
payment-service -8003
notification-service -8004
mock-gateway -8005

## Run it from a fresh clone

Prerequisites: **Python 3.10+** and **Docker** (Docker only runs PostgreSQL). 

```bash
# 1. Python environment (one venv for all services, tests and scripts)
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

# 2. PostgreSQL with 4 databases and 4 separate users (see infra/postgres/init.sql)
docker compose up -d
docker compose ps                    # wait until "healthy"

# 3. Start the 4 services + mock gateway (logs go to logs/<service>.log)
python scripts/run_all.py
```

Leave that terminal running. In a **second terminal** (same venv) you can now:

```bash
python scripts/seed.py                       # a few demo classes; then open http://localhost:8002/docs
python scripts/e2e_main_flow.py              # main transaction + failure paths
python scripts/challenge1_concurrency.py     # Challenge 1: concurrency
python scripts/challenge2_idempotency.py     # Challenge 2: idempotency
python scripts/challenge3b_time_bound_holds.py   # Challenge 3B: time-bound holds (~40 s)
python scripts/check_db_isolation.py         # each service user can only open its own database
```

Each script prints PASS/FAIL per check and saves a timestamped transcript in `evidence/`.

**Unit tests** (no running services needed; they use temporary SQLite files and fake upstreams):

```bash
cd services/schedule-service     && python -m pytest -q
cd services/booking-service      && python -m pytest -q
cd services/payment-service      && python -m pytest -q
cd services/notification-service && python -m pytest -q
```

**Quick mode without Docker:** `python scripts/run_all.py --sqlite` uses SQLite files in `.data/`.
Everything works except the *naive-code race* in Challenge 1 phase A, which SQLite hides because it lets
one writer in at a time. Collect final evidence on PostgreSQL.

**Stop:** Ctrl+C in the first terminal, then `docker compose down` (add `-v` to wipe the data).

### Ports and configuration (all via environment variables)

Each service reads its settings from the environment; `scripts/run_all.py` loads `services/<name>/.env`
(or `.env.example` if there is no `.env`). Copy `.env.example` to `.env` to change something.

schedule-service | 8001 | `DATABASE_URL`, `EXPIRY_SWEEP_INTERVAL_SECONDS` (5), `ENABLE_NAIVE_DEMO` (false) |
booking-service | 8002 | `DATABASE_URL`, `SCHEDULE_SERVICE_URL`, `PAYMENT_SERVICE_URL`, `NOTIFICATION_SERVICE_URL`, `HOLD_SECONDS` (300), `*_TIMEOUT_SECONDS` |
payment-service | 8003 | `DATABASE_URL`, `GATEWAY_URL`, `GATEWAY_TIMEOUT_SECONDS` (3), `IDEMPOTENCY_WAIT_SECONDS` (10) |
notification-service | 8004 | `DATABASE_URL`, `PROVIDER_URL`, `PROVIDER_TIMEOUT_SECONDS` (2) |
mock-gateway | 8005 | `PAYMENT_MODE`, `PAYMENT_DELAY_MS`, `SMS_MODE`, `SMS_DELAY_MS` |

Common to all: `PORT`, `SERVICE_NAME`, `LOG_LEVEL`.

### Every service provides
* `GET /health`, versioned APIs under `/api/v1/...`, and Swagger UI at `/docs` (OpenAPI JSON at `/openapi.json`;
  exported copies in `docs/openapi/`, regenerate with `python scripts/export_openapi.py`)
* One error format: `{"error": {"code", "message", "correlation_id", "details"}}`
* Structured JSON logs; the `X-Correlation-ID` header is created at the edge and forwarded on every
  service-to-service call, so one booking can be followed across all four log files:
  `grep <correlation-id> logs/*.log`

### API Calls

```bash
SESSION=$(curl -s localhost:8001/api/v1/sessions | python -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
BOOKING=$(curl -s -X POST localhost:8002/api/v1/bookings -H 'Content-Type: application/json' \
  -d "{\"member_id\":\"member-1\",\"session_id\":\"$SESSION\"}" | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

curl -s -X POST localhost:8002/api/v1/bookings/$BOOKING/pay -H 'Idempotency-Key: my-first-key'
# make the gateway fail, then pay a second booking:
curl -s -X PUT localhost:8005/admin/mode -H 'Content-Type: application/json' -d '{"payment_mode":"fail"}'
```

## Repository layout

```
README.md  docker-compose.yml  requirements-dev.txt
docs/                 REPORT.md (report draft + diagrams), openapi/*.json, sample-runs/
infra/postgres/       init-db.sh  (4 databases, 4 users)
services/<name>/      app/ (api, business logic, models), tests/, requirements.txt, .env.example
scripts/              run_all.py, e2e + challenge scripts, seed, db-isolation check, openapi export
evidence/             one sub-folder per challenge (+ README linking files to claims)
```
