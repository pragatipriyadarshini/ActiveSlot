# Evidence

The scripts in `scripts/` save timestamped transcripts here. Run them yourself to produce fresh evidence for your report.

| Folder | Script | What it checks |
|---|---|---|
| `challenge-1-concurrency/` | `challenge1_concurrency.py` | Seat allocation under concurrent requests, including the intentionally unsafe demo |
| `challenge-2-idempotency/` | `challenge2_idempotency.py` | Repeated and concurrent payment requests do not cause duplicate charges |
| `challenge-3b-time-bound-holds/` | `challenge3b_time_bound_holds.py` | Holds expire, cancellation frees a seat, and confirmed seats stay allocated |
| `tests/` | `e2e_main_flow.py`, `check_db_isolation.py`, and pytest | End-to-end behavior, database separation, and service unit tests |

Use PostgreSQL for the concurrency and database-isolation checks. SQLite is convenient for local development, but its write behavior can hide the over-allocation race that the first challenge is meant to demonstrate.

Add your own screenshots or run transcripts to the relevant folder, then update the claim table below with the file names you used.

| Claim | Evidence file |
|---|---|
| Exactly the available number of seats are allocated under contention | `challenge-1-concurrency/` |
| Reusing an idempotency key does not repeat a charge | `challenge-2-idempotency/` |
| Expired holds release seats without releasing confirmed seats | `challenge-3b-time-bound-holds/` |
