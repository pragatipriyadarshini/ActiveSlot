# Containers: Docker and Minikube

Each service reads its settings from environment variables, so the same code runs from Python in Docker Compose; only the values
change.

Prerequisites: Docker Desktop (or Docker Engine)

 `services/<name>/Dockerfile`, `.dockerignore` | image of one service (python:3.12-slim, non-root user, health check) 
 `docker-compose.yml` | PostgreSQL + (with `docker compose up --build`) all five application containers 

---
## Part 1

### 1.1 Build and run one image
```bash
docker build -t fitslot/mock-gateway:1.0 services/mock-gateway
docker run --rm -p 8005:8005 fitslot/mock-gateway:1.0       # open http://localhost:8005/docs, Ctrl+C to stop
docker images | grep fitslot
```

### 1.2 The whole system with Compose
```bash
docker compose up --build
docker compose ps                         # postgres + 5 containers; "healthy" after ~20 s
curl localhost:8002/health
python scripts/e2e_main_flow.py           # scripts use localhost ports, which Compose publishes
python scripts/challenge1_concurrency.py
python scripts/challenge2_idempotency.py
python scripts/challenge3b_time_bound_holds.py   # ~40 s (HOLD_SECONDS defaults to 30 in Compose)
docker compose logs -f booking-service    # JSON logs; follow one X-Correlation-ID across services:
docker compose logs | grep <correlation-id>
```
Failure demos that only containers give you:
```bash
docker compose stop payment-service       # pay now returns 503 PAYMENT_UNAVAILABLE, booking stays PENDING_PAYMENT
docker compose start payment-service      # retry with the SAME Idempotency-Key succeeds
```
Stop: `docker compose --profile app down` (data kept) or `down -v` 

### 1.3 Test it
```bash
python scripts/k8s_port_forward.py                   # keep running in terminal 1 (forwards 8001-8005 and 5432)
# terminal 2:
python scripts/e2e_main_flow.py
python scripts/challenge1_concurrency.py
python scripts/challenge2_idempotency.py
python scripts/challenge3b_time_bound_holds.py
```

### Known limitations (state them honestly)
Tables are created at service start-up (`create_all`)
all four databases live on one PostgreSQL pod(logically separate, separate credentials; production would use separate instances)
Secrets are demo values in git