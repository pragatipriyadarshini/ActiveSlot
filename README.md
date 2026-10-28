# FitSlot

FitSlot is a fitness-class booking demo built from four services and a mock gateway. A member can find a class, reserve a seat, pay, and receive a confirmation notification.

The project demonstrates seat concurrency, payment idempotency, expiring holds, service-to-service requests, and failure handling.

## Services

Schedule (8001) - Owns classes, seat availability, and seat holds 
Booking (8002) - Coordinates booking, payment, and confirmation 
Payment (8003) - Records payments and prevents duplicate charges
Notification (8004) - Sends and records SMS notifications
Mock gateway (8005) - Simulates payment and SMS providers 

PostgreSQL uses port 5432 when you choose the database-backed setup below.

The booking service coordinates the main flow:

1. Schedule holds a seat.
2. Payment charges the member.
3. Schedule confirms the seat.
4. Notification sends a confirmation SMS.

Each service owns its database. Services communicate over HTTP and do not read one another's tables.

## Run locally

Requirements:
Python 3.10 or later. 
Docker (optional): the local mode uses SQLite and does not need Docker or PostgreSQL.

Create a virtual environment and install the project dependencies:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

On macOS or Linux, activate the environment with `source .venv/bin/activate`.

### Without Docker

From the project directory, run:
--- python scripts\run_all.py --sqlite

This starts all five processes and stores each service's data in `.data\`. The local databases are reset each time this command starts.

### Start with PostgreSQL
For database-isolation checks and concurrency evidence:
docker compose up -d
docker compose ps
python scripts\run_all.py

Compose starts PostgreSQL on port 5432. 
The initialization script creates a separate database and user for each service. To use a PostgreSQL server other than the included Compose instance, set the connection URLs in each service's `.env` file.

The launcher reads `services\<service>\.env`, or `.env.example` if no `.env` exists. Use `--set SERVICE:KEY=VALUE` to override a setting for one run. For example:


Open the API documentation at `http://localhost:8002/docs`. Each service also has a `/docs` page and a `/health` endpoint.

Press Ctrl+C in the launcher terminal to stop the services. If using Compose, stop PostgreSQL separately with `docker compose down`; add `-v` only if you also want to remove its stored data.

## Tests and demo scripts

With the services running, these scripts exercise the end-to-end flow and distributed-systems scenarios:

python scripts\seed.py ---- generate clsses
python scripts\e2e_main_flow.py - end-to-end-service-runs
python scripts\challenge1_concurrency.py
python scripts\challenge2_idempotency.py
python scripts\challenge3b_time_bound_holds.py
python scripts\check_db_isolation.py

The scripts save timestamped output under `evidence\`. 
Use PostgreSQL for concurrency and database-isolation evidence. 
SQLite serializes writes differently, so it is useful for local development but not for demonstrating the naive over-allocation race.

## Configuration

Each service reads its settings from environment variables. Defaults are in the service's `.env.example`; copy that file to `.env` to customize it.

| Service | Main settings |
|---|---|
| Schedule | `DATABASE_URL`, `EXPIRY_SWEEP_INTERVAL_SECONDS`, `ENABLE_NAIVE_DEMO` |
| Booking | `DATABASE_URL`, service URLs, `HOLD_SECONDS`, request timeouts |
| Payment | `DATABASE_URL`, `GATEWAY_URL`, gateway timeout, idempotency wait |
| Notification | `DATABASE_URL`, `PROVIDER_URL`, provider timeout |
| Mock gateway | Payment/SMS modes and simulated delays |

All services also support `PORT`, `SERVICE_NAME`, and `LOG_LEVEL`. The default ports are listed above.

## Project layout

- Each service's `app/` keeps the code in a few modules: `main.py` defines validation, HTTP routes, and startup; `logic.py` handles service behavior; `models.py` contains database setup and tables; `common.py` holds logging, errors, and time helpers. The mock gateway only needs `main.py` and `common.py`.
- `services/` also contains each service's tests and `.env.example`.
- `scripts/` contains the launcher, demo flows, challenge checks, and OpenAPI exporter.
- `infra/postgres/init.sql` creates the four service databases and users.
- `docs/` contains exported API specifications and sample run outputs.
- `evidence/` contains example outputs and transcripts from demo runs.
