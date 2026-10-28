# FitSlot

FitSlot is a microservices-based fitness class booking platform that demonstrates distributed system patterns through a realistic booking workflow.

A member can:

- Browse available fitness classes
- Reserve a seat
- Complete payment
- Receive a confirmation notification

The project showcases:

- Seat reservation concurrency handling
- Payment idempotency
- Time-bound seat holds and expiration
- Service-to-service communication
- Failure handling and recovery
- Database isolation between services

---

## Architecture

### Services

| Service | Port | Responsibility |
|----------|------|---------------|
| Schedule Service | 8001 | Manages fitness classes, seat availability, and seat holds |
| Booking Service | 8002 | Coordinates bookings, payments, and confirmations |
| Payment Service | 8003 | Processes payments and prevents duplicate charges |
| Notification Service | 8004 | Sends and records SMS notifications |
| Mock Gateway | 8005 | Simulates payment and SMS providers |

> PostgreSQL runs on port **5432** when using the database-backed deployment.

---

## Booking Workflow

The Booking Service orchestrates the complete reservation flow:

1. Schedule Service places a seat hold.
2. Payment Service processes the payment.
3. Schedule Service confirms the seat reservation.
4. Notification Service sends a confirmation SMS.

Each service owns its own database and communicates exclusively through HTTP APIs. No service directly accesses another service's database.

---

## Running Locally

### Requirements

- Python 3.10+
- Docker (optional)

> Local mode uses SQLite and does not require Docker or PostgreSQL.

### Create a Virtual Environment

#### Windows

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

#### macOS / Linux

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

---

## Run with SQLite (No Docker)

```powershell
python scripts\run_all.py --sqlite
```

This starts all five services and stores data under:

```text
.data/
```

The SQLite databases are recreated each time the launcher starts.

---

## Run with PostgreSQL

For database isolation checks and concurrency demonstrations:

```powershell
docker compose up -d
docker compose ps

python scripts\run_all.py
```

The included Docker Compose setup automatically:

- Starts PostgreSQL on port **5432**
- Creates separate databases for each service
- Creates dedicated database users per service

To use an external PostgreSQL instance, update the `DATABASE_URL` values inside each service's `.env` file.

---

## Environment Configuration

The launcher loads configuration from:

```text
services/<service>/.env
```

If `.env` is not found, it automatically falls back to:

```text
services/<service>/.env.example
```

You can override settings for a single run:

```powershell
python scripts\run_all.py --set PAYMENT:LOG_LEVEL=DEBUG
```

---

## API Documentation

After startup, open:

- Booking Service: http://localhost:8002/docs
- Schedule Service: http://localhost:8001/docs
- Payment Service: http://localhost:8003/docs
- Notification Service: http://localhost:8004/docs

Additional endpoints:

- `/docs` – Interactive Swagger UI
- `/health` – Health check endpoint

---

## Stopping Services

Press:

```text
Ctrl + C
```

in the launcher terminal.

If PostgreSQL was started using Docker Compose:

```powershell
docker compose down
```

To also remove stored database volumes:

```powershell
docker compose down -v
```

---

## Tests & Demo Scenarios

With all services running, execute the following scripts:

### Seed Sample Data

```powershell
python scripts\seed.py
```

### End-to-End Booking Flow

```powershell
python scripts\e2e_main_flow.py
```

### Concurrency Challenge

```powershell
python scripts\challenge1_concurrency.py
```

### Payment Idempotency Challenge

```powershell
python scripts\challenge2_idempotency.py
```

### Time-Bound Seat Hold Challenge

```powershell
python scripts\challenge3b_time_bound_holds.py
```

### Database Isolation Verification

```powershell
python scripts\check_db_isolation.py
```

All scripts generate timestamped outputs inside:

```text
evidence/
```

> For accurate concurrency and isolation demonstrations, use PostgreSQL. SQLite handles write concurrency differently and is intended primarily for local development.

---

## Configuration Reference

Each service reads configuration from environment variables.

Defaults are provided in the corresponding `.env.example` file.

| Service | Key Settings |
|----------|-------------|
| Schedule | `DATABASE_URL`, `EXPIRY_SWEEP_INTERVAL_SECONDS`, `ENABLE_NAIVE_DEMO` |
| Booking | `DATABASE_URL`, service URLs, `HOLD_SECONDS`, request timeouts |
| Payment | `DATABASE_URL`, `GATEWAY_URL`, gateway timeout, idempotency wait settings |
| Notification | `DATABASE_URL`, `PROVIDER_URL`, provider timeout |
| Mock Gateway | Payment and SMS simulation settings |

All services also support:

```text
PORT
SERVICE_NAME
LOG_LEVEL
```

---

## Project Structure

```text
FitSlot/
│
├── services/
│   ├── schedule/
│   ├── booking/
│   ├── payment/
│   ├── notification/
│   └── mock_gateway/
│
├── scripts/
├── docs/
├── evidence/
└── infra/
```

### Service Layout

Each service follows a consistent structure:

- `main.py` – API routes, request validation, startup configuration
- `logic.py` – Core business logic
- `models.py` – Database models and persistence
- `common.py` – Shared utilities, logging, errors, and time helpers

The Mock Gateway service uses only:

- `main.py`
- `common.py`

---

## Supporting Directories

### `scripts/`

Contains:

- Service launcher
- Test and demo scenarios
- Challenge scripts
- OpenAPI exporter

### `infra/postgres/`

Contains:

- `init.sql` for creating service databases and users

### `docs/`

Contains:

- Exported OpenAPI specifications
- Example outputs
- Documentation assets

### `evidence/`

Contains:

- Demo transcripts
- Challenge outputs
- Validation and testing artifacts

---

## Key Design Principles

- Service ownership of data
- Database-per-service architecture
- HTTP-based communication
- Idempotent payment processing
- Concurrent seat reservation handling
- Time-limited reservation holds
- Failure isolation and recovery

Built as a learning project to demonstrate practical microservice architecture patterns in a fitness studio booking domain.
