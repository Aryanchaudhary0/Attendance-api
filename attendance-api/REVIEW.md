# REVIEW.md

Defects found in the starter `app/main.py`. Everything below is fixed in the submitted file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds the shift start with `punch_in.replace(hour, minute)`, i.e. on the punch's own UTC/server date, not in IST. | Punch-in 09:35 IST is 04:05 UTC, so "shift start" becomes 09:30 UTC and the result is nonsense (0 for real late arrivals). | Convert to IST first; build the shift start for the attendance date with `shift_bounds()`. |
| 2 | `compute_late_minutes` | Grace check is `int(seconds / 60) > 10`. The int truncation happens before the comparison. | 09:40:01 and 09:40:59 give 10 minutes, `10 > 10` is false, so 0. R2 says 09:40:01 is late (10). | Compare seconds: late only if `delta > 600`, then `delta // 60`. |
| 3 | `compute_late_minutes` | No whole-second truncation; overnight shifts ignored (shift start belongs to the previous day). | Shift 22:00-06:00, punch-in 02:00 next day: late should be 240 min, starter says negative / 0. | Truncate to seconds at the API boundary; `attendance_date()` implements R1 incl. the overnight rule. |
| 4 | `compute_work_hours` | `round(x, 2)` is float, banker's-style rounding. | 450 s = 0.125 h returns 0.12; R4 wants 0.13 (half-up). Naive/aware datetimes would also raise `TypeError`. | `Decimal(seconds) / 3600` quantized with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Shift end is always put on the attendance date, and there is no 30-minute minimum. | Overnight 22:00-06:00, out 06:40 next day gives 1480 min instead of 40. Punch-out 18:40 on a 18:30 shift returns 10 instead of 0. | End is next day for overnight shifts; floor to minutes; return 0 below 30. |
| 6 | `punch_in` | `emp` can be `None`; `emp["shift_start"]` raises `TypeError`. | POST an unknown `emp_code`: 500 instead of 404. | `get_employee()` raises 404. |
| 7 | `punch_in` | `punched_at` handling: `if body.punched_at` (0 is falsy), `fromtimestamp(ms / 1000)` gives a **naive server-local** datetime, no range or type validation, sub-second part kept. | Send seconds (`1783312500`) and get a 1970 date, 201. Send `1.5` or `"x"`: accepted or 500. | `StrictInt` with bounds 1e11..4102444800000 (422 otherwise), `from_ms()` gives aware UTC truncated to seconds. |
| 8 | `punch_in` | `date` is `ts.date()` (server-local date), not the IST attendance date, and ignores the overnight rule. | Server in UTC: punch-in 00:30 IST gets the previous day's date. Overnight early-morning punch gets the wrong day. | `attendance_date()` (R1). |
| 9 | `punch_in` | Check-then-insert race and no unique index. | Fire 10 identical punch-ins in parallel: several 201, duplicate documents for one (emp, date). | Unique index `(emp_code, date)`; `insert_one` and `DuplicateKeyError` -> 409. |
| 10 | `punch_in` / `PunchInIn` | `status` is a free string. | `status: "ABSENT"` or `"banana"` with a punch-in is stored. Contract: only PRESENT / WFH / ON_DUTY, else 422. | `Literal["PRESENT","WFH","ON_DUTY"]`. |
| 11 | `punch_in`, `list_attendance` | Response adds `id` (stringified `_id`), returns raw `datetime` objects, and legacy docs have no `history`/`half_day`. | `_id` leaks, `punch_in` is an ISO string instead of epoch ms. | `record_out()` maps to the contract: no ids, epoch ms, defaults for missing fields. |
| 12 | `create_employee` | Check-then-insert race, no unique index; no validation (emp_code format, HH:MM, email, `joined_on`, `shift_start == shift_end`); `created_at = datetime.now()` is naive local time and returned as a datetime. | Parallel creates of one code give two 201; `shift_start: "9:30pm"` is stored and later crashes punch-in with 500. | Unique index + `DuplicateKeyError` -> 409; Pydantic patterns and validators -> 422; `created_at` is UTC and returned as epoch ms. |
| 13 | `list_employees` | `skip = page * page_size` (page 1 skips the first page); `total` ignores the department filter; no sort; `page=0` gives negative skip -> 500; unbounded `page_size`. | `GET /employees?page=1` misses the first 20 employees; `?department=X` shows `total` of all employees. | `(page - 1) * page_size`, `count_documents(filter)`, `sort(emp_code)`, `page >= 1`, `1 <= page_size <= 100`. |
| 14 | `list_attendance` | Loads every matching document and sorts in Python; `total = len(docs)`; sort lacks the `emp_code` tie-breaker; no index. | 100k records: slow and memory heavy; equal dates come back in arbitrary order. | Database sort `(date desc, emp_code asc)` + `skip/limit`, `count_documents`, and the indexes below. |
| 15 | `list_attendance` | No validation: `date_from > date_to`, malformed dates, unknown `status`, `page=0`, huge `page_size`. | `date_from=2026-07-10&date_to=2026-07-01` returns an empty page instead of 422; `page=0` slices from the end. | Strict date parsing, 422 on inverted range, `Literal` status, bounded paging. |
| 16 | `health` | Always `ok`, never pings MongoDB. | Stop MongoDB: `/health` still 200; the runner starts testing too early. | `ping`; 503 on failure. |
| 17 | `MongoClient(...)` | Not `tz_aware`, so PyMongo returns naive datetimes; no indexes are created at startup. | Subtracting aware and naive datetimes raises `TypeError`; every list/analytics query is a collection scan. | `tz_aware=True`; idempotent `ensure_indexes()` in the app lifespan (retried from `/health`). |

## Looked at and judged fine
- Endpoints are plain `def`, so PyMongo's blocking calls run in FastAPI's thread pool; no need for Motor.
- `load_dotenv()` does not override variables already in the environment, so real `MONGO_URI` / `MONGO_DB` win, as required.
- Pydantic ignores unknown fields, which is exactly the contract for client-sent derived fields.
- `201` for creation and `409` for a duplicate `emp_code` are the right codes; `doc.pop("_id")` correctly keeps `_id` out of the response (but the other fixes above replace that code).
- The default shifts 09:30 / 18:30 match the contract.
