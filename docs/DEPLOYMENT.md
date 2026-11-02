# Containers: Docker and Minikube

Everything here is **config only**: the application code is unchanged. Each service reads its settings from
environment variables, so the same code runs from Python, in Docker Compose and on Kubernetes; only the values
change (database host = `postgres`, other services = their Service names such as `http://payment-service:8003`).

Prerequisites: Docker Desktop (or Docker Engine), and for Part 2 `minikube` and `kubectl`.

| File | Purpose |
|---|---|
| `services/<name>/Dockerfile`, `.dockerignore` | image of one service (python:3.12-slim, non-root user, health check) |
| `docker-compose.yml` | PostgreSQL + (with `docker compose up --build`) all five application containers |
| `k8s/*.yaml` | namespace, config + secret, PostgreSQL, one Deployment + Service per service |
| `scripts/k8s_port_forward.py` | forwards all services to localhost so the test scripts run unchanged |

---
## Part 1: Docker (and Docker Compose)

### 1.1 Build and run one image (to understand the pieces)
```bash
docker build -t fitslot/mock-gateway:1.0 services/mock-gateway
docker run --rm -p 8005:8005 fitslot/mock-gateway:1.0       # open http://localhost:8005/docs, Ctrl+C to stop
docker images | grep fitslot
```
Why these Dockerfile lines: the `requirements.txt` layer is copied before the code so rebuilds are fast; the
process runs as a non-root user; `HEALTHCHECK` calls `/health`; the port and `SERVICE_NAME` are environment
variables; **nothing secret is baked into the image**.

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
Stop: `docker compose --profile app down` (data kept) or `down -v` (wipes the database; needed if you change
`infra/postgres/init.sql`, which only runs on an empty volume).
Do not run `scripts/run_all.py` at the same time (same ports).

---
## Part 2: Minikube (Kubernetes)

### 2.1 Start the cluster and build the images *inside* it
```bash
minikube start --driver=docker --cpus=2 --memory=4096
kubectl get nodes                                    # Ready

# make your docker CLI talk to Minikube's Docker daemon (images built now exist inside the cluster)
eval $(minikube docker-env)                          # bash / zsh
#   PowerShell:  & minikube -p minikube docker-env --shell powershell | Invoke-Expression
for s in schedule-service booking-service payment-service notification-service mock-gateway; do
  docker build -t fitslot/$s:1.0 services/$s
done
docker images | grep fitslot
```
Alternative without `docker-env`: build normally, then `minikube image load fitslot/<service>:1.0` for each.
The manifests use `imagePullPolicy: IfNotPresent` because the images are not in any registry.

### 2.2 Deploy
```bash
kubectl apply -f k8s/                                # applies 00-namespace first, then config, postgres, services
kubectl -n fitslot get pods -w                       # wait until every pod is Running and READY 1/1
kubectl -n fitslot get svc,deploy
```
What each part does:
* `01-config.yaml`: a **ConfigMap** (URLs, hold duration, log level) and a **Secret** (database URLs) injected as environment variables.
* `02-postgres.yaml`: PostgreSQL with a PersistentVolumeClaim; the init script (4 databases, 4 users) is mounted from a ConfigMap.
* `1x-*.yaml`: per service a **Deployment** (an init container waits for PostgreSQL; startup/readiness probes call `/health`;
  liveness is TCP-only so a database outage does not restart healthy pods; resource requests/limits) and a **Service**
  (the stable DNS name other pods use, e.g. `payment-service`).

### 2.3 Test it
```bash
python scripts/k8s_port_forward.py                   # keep running in terminal 1 (forwards 8001-8005 and 5432)
# terminal 2:
python scripts/e2e_main_flow.py
python scripts/challenge1_concurrency.py
python scripts/challenge2_idempotency.py
python scripts/challenge3b_time_bound_holds.py
```
`scripts/challenge2_idempotency.py` reads the replay log lines from `logs/payment-service.log`, which does not exist
in Kubernetes; use `kubectl -n fitslot logs deploy/payment-service | grep idempotent_replay` as the evidence instead.

### 2.4 Scale and break things (this is where Kubernetes pays off)
```bash
# scale AFTER the first start (two pods creating tables on an empty database at the same instant can race)
kubectl -n fitslot scale deploy/schedule-service deploy/booking-service deploy/payment-service --replicas=2
kubectl -n fitslot get pods -o wide
python scripts/challenge1_concurrency.py             # booking -> schedule/payment calls now spread over both pods
kubectl -n fitslot logs -l app=payment-service --prefix --tail=20     # logs of all payment pods

kubectl -n fitslot delete pod -l app=payment-service  # kill pods: Kubernetes restarts them; retry with the same key
kubectl -n fitslot scale deploy/payment-service --replicas=0   # payment "down": pay returns 503, nothing is lost
kubectl -n fitslot scale deploy/payment-service --replicas=2   # recovery
```
Why this is safe: correctness lives in the database (atomic seat UPDATE, idempotency key primary key, conditional state
changes), not in process memory, so any number of replicas can serve requests; every `schedule-service` replica runs the
expiry sweeper and BR2 makes that harmless. **Keep `mock-gateway` at 1 replica** (it keeps its mode and counters in memory).
Scaling note for the report: the one hot row per class (Challenge 1) is the real limit, not the number of pods.

### 2.5 Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `ErrImagePull` / `ImagePullBackOff` | image not inside Minikube: redo the `docker-env` step and rebuild (or `minikube image load`) |
| pod `Init:0/1` for long | PostgreSQL not ready: `kubectl -n fitslot logs deploy/postgres` |
| `CrashLoopBackOff` | `kubectl -n fitslot logs <pod> --previous`; usually a wrong environment variable or database URL |
| pod Running but `0/1` ready | `/health` fails: database unreachable; `kubectl -n fitslot describe pod <pod>` |
| scripts cannot connect | port-forward died after a pod restart: stop and re-run `scripts/k8s_port_forward.py` |
| changed code but old behaviour | rebuild the image **with the same tag**, then `kubectl -n fitslot rollout restart deploy/<service>` |
| changed `init.sql` | databases are created only once: `kubectl delete namespace fitslot` and deploy again |

### 2.6 Clean up
```bash
kubectl delete namespace fitslot        # removes everything including the database volume
minikube stop                           # or: minikube delete
```

### Evidence to capture for the report
`docker compose ps`, `docker images`, `kubectl -n fitslot get pods,svc,deploy`, `kubectl describe pod` (probes, env),
the scripts' transcripts in `evidence/`, a replica-scaling run, and the "payment down -> 503 -> recover" sequence.

### Known limitations (state them honestly)
Tables are created at service start-up (`create_all`), not by migrations; all four databases live on one PostgreSQL pod
(logically separate, separate credentials; production would use separate instances); Secrets are demo values in git;
no Ingress, autoscaling or network policies; the manifests were written for Minikube and not load-tested.
