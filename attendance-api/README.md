# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # or export MONGO_URI / MONGO_DB (real env vars win over .env)
uvicorn app.main:app --port 8000
```
MongoDB 6.0+ is required (`$setWindowFields`, `$rank`). Indexes are created at startup, idempotently.
Docs: `REVIEW.md` (defects in the starter), `DECISIONS.md` (design answers).

## Tests
```bash
pip install -r requirements-dev.txt
pytest tests/test_api_mock.py                  # rules R1-R5, validation, status codes, history, races (mongomock)
(cd tests && npm i mingo@6) && python tests/check_pipelines.py   # 4 analytics pipelines vs a Python reference
python tests/smoke_real_mongo.py               # needs a real MongoDB; checks IXSCAN / no COLLSCAN on ~100k docs
```

## Honest status
- The service was developed **without a MongoDB server available**. Endpoint behaviour was tested on mongomock, and the aggregation
  pipelines were executed with the `mingo` JS engine and compared with a brute-force reference (ties, no-log employees, weekends,
  mid-month joiners, overnight shifts, legacy records). **Index usage (`IXSCAN` / no `COLLSCAN`) and real-server behaviour were
  not verified**: run `tests/smoke_real_mongo.py` against MongoDB 6/7 before relying on them.

## Interpretation choices where the contract is open
- Trend `moving_avg_7d` averages the *reported* (4-decimal) `attendance_rate` values, then rounds half-up.
- Half-up rounding is exact: sums are kept as integers (hundredths / half-days) in the pipelines and rounded with `Decimal`
  or integer arithmetic, because MongoDB's `$round` is banker's rounding.
- `PATCH` with identical status and punch times is 422 even if stored derived values are stale. Concurrent corrections retry
  (optimistic concurrency on the history length) and return 409 only after 5 lost races.
- `punch-out`: an already-closed latest record is reported (409) before the 24-hour window check (422).
