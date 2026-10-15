"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real env vars win)

Layout of this single file:
  1. configuration, database, indexes
  2. time helpers + business rules R1-R5  (pure functions, no I/O)
  3. serialisation (BSON <-> API) and validation helpers
  4. request models
  5. endpoints: system, employees, attendance
  6. analytics pipelines + endpoints
  7. admin explain
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration, database, indexes
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent: create_index is a no-op when the same index already exists."""
    global _indexes_ready
    emp, logs = db.employees, db.attendance_logs
    # Uniqueness of the business key; also the lookup key for every employee query.
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    # GET /employees?department= (sorted by emp_code), trend / summary with a department.
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp")
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
    # Department summary over all departments filters on joined_on alone.
    emp.create_index([("joined_on", ASCENDING)], name="joined")
    # At most one record per employee and day: this is what makes punch-in race-safe.
    # Also serves: employee filter + date range + sort(date desc), employee monthly, $lookup by emp_code.
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    # Global listing sort (date desc, emp_code asc), month range scans, per-day lookups in the trend.
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the app up so /health can answer 503 and retry later
        log.warning("index creation failed at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time helpers and business rules (R1 - R5)
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_BELOW = Decimal("4.50")  # R5
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

EPOCH_MIN, EPOCH_MAX = 100_000_000_000, 4_102_444_800_000
# StrictInt rejects floats, strings and booleans; the bounds reject seconds-looking values.
EpochMs = Annotated[StrictInt, Field(ge=EPOCH_MIN, le=EPOCH_MAX)]


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=UTC)
    delta = dt - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    dt = datetime.now(UTC)
    return dt.replace(microsecond=dt.microsecond // 1000 * 1000)


def hhmm_minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm_minutes(shift_end) <= hhmm_minutes(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; an overnight shift's early-morning punch-in belongs to the day before."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end):
        seconds_of_day = ist.hour * 3600 + ist.minute * 60 + ist.second
        if seconds_of_day < hhmm_minutes(shift_end) * 60:
            day -= timedelta(days=1)
    return day


def shift_bounds(day: date, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Shift start / end as aware datetimes for the shift that began on `day` (end is next day if overnight)."""
    sh, sm = divmod(hhmm_minutes(shift_start), 60)
    eh, em = divmod(hhmm_minutes(shift_end), 60)
    start = datetime.combine(day, time(sh, sm), tzinfo=IST)
    end = datetime.combine(day, time(eh, em), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    delta = int((punch_in - shift_start_dt).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: whole minutes after shift end, counted only when >= 30."""
    delta = int((punch_out - shift_end_dt).total_seconds())
    minutes = delta // 60 if delta > 0 else 0
    return minutes if minutes >= OVERTIME_MIN_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600 rounded half-up to 2 decimals (Decimal, so no binary-float or banker's rounding)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive_fields(
    day: date, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], emp: dict
) -> dict:
    """All stored derived values for a record, from its final status and punch times (R2 - R5)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    shift_start = emp.get("shift_start", "09:30")
    shift_end = emp.get("shift_end", "18:30")
    start_dt, end_dt = shift_bounds(day, shift_start, shift_end)
    out["late_minutes"] = compute_late_minutes(punch_in, start_dt)
    if punch_out is not None:
        hours = compute_work_hours(punch_in, punch_out)
        out["work_hours"] = float(hours)
        out["overtime_minutes"] = compute_overtime(punch_out, end_dt)
        out["half_day"] = hours < HALF_DAY_BELOW
    return out


def round_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# 3. Serialisation and validation helpers
# --------------------------------------------------------------------------- #
def _validation_error(where: str, name: str, msg: str, value: Any) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": [where, name], "msg": msg, "input": value}])


def parse_date(value: str, name: str, where: str = "query") -> date:
    """Strict YYYY-MM-DD (date.fromisoformat alone would also accept other ISO spellings)."""
    ok = len(value) == 10 and value[4] == "-" and value[7] == "-" and value.replace("-", "").isdigit()
    if ok:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise _validation_error(where, name, "must be a valid date in YYYY-MM-DD format", value)


def parse_month(value: str) -> tuple[date, date]:
    """'YYYY-MM' -> (first day, last day)."""
    ok = len(value) == 7 and value[4] == "-" and value[:4].isdigit() and value[5:].isdigit()
    if ok:
        year, month = int(value[:4]), int(value[5:])
        if 1 <= year <= 9999 and 1 <= month <= 12:
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    raise _validation_error("query", "month", "must be a month in YYYY-MM format", value)


def require(value: Any, name: str) -> Any:
    if value is None or value == "":
        raise _validation_error("query", name, "field required", None)
    return value


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def _history_value(key: str, value: Any) -> Any:
    return to_ms(value) if key in ("punch_in", "punch_out") else value


def record_out(doc: dict) -> dict:
    """Stored attendance document -> API shape. Never exposes _id; missing legacy fields get their defaults."""
    history = []
    for entry in doc.get("history") or []:
        changes = {
            key: {"from": _history_value(key, ch.get("from")), "to": _history_value(key, ch.get("to"))}
            for key, ch in (entry.get("changes") or {}).items()
        }
        history.append({"at": to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def check_punch_window(punch_in: datetime, punch_out: datetime) -> None:
    if punch_out <= punch_in:
        raise HTTPException(422, "punch_out must be after punch_in")
    if punch_out - punch_in > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_date(cls, v: str) -> str:
        parse_date(v, "joined_on", "body")
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


Page = Annotated[int, Query(ge=1, le=10_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]


# --------------------------------------------------------------------------- #
# 5. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides, not a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    shift_start, shift_end = emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    day = attendance_date(ts, shift_start, shift_end)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(day, body.status, ts, None, emp),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent insert wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    # Most recent record that started at or before the punch-out. Records without a
    # punch_in (ABSENT / LEAVE) never match a date comparison, so they are skipped.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = rec["punch_in"].replace(microsecond=0)
    check_punch_window(p_in, ts)
    day = date.fromisoformat(rec["date"])
    derived = derive_fields(day, rec["status"], p_in, ts, emp)
    # Compare-and-set on punch_out: of two simultaneous requests only one matches.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_find_args(emp_code, date_from, date_to, status, page, page_size):
    """Filter / sort / skip / limit of GET /attendance, shared with the explain endpoint."""
    q: dict = {}
    d_from = parse_date(date_from, "date_from") if date_from is not None else None
    d_to = parse_date(date_to, "date_to") if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        raise _validation_error("query", "date_from", "date_from must not be after date_to", date_from)
    if emp_code is not None:
        q["emp_code"] = emp_code
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from.isoformat()
        if d_to:
            q["date"]["$lte"] = d_to.isoformat()
    if status is not None:
        q["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q, sort, skip, limit = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(q)  # exact, reflects the applied filters (R10)
    docs = db.attendance_logs.find(q).sort(sort).skip(skip).limit(limit)  # the database sorts and pages
    return {"items": [record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


def plan_regularization(rec: dict, emp: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Validate a correction against the current record. Returns (new field values, history `changes`)."""
    status = body.status or rec["status"]
    new_in, new_out = rec.get("punch_in"), rec.get("punch_out")
    if new_in is not None:
        new_in = new_in.replace(microsecond=0)
    if new_out is not None:
        new_out = new_out.replace(microsecond=0)

    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"status {status} clears the punch times; do not send punch_in / punch_out")
        new_in = new_out = None
    else:
        if body.punch_in is not None:
            new_in = from_ms(body.punch_in)
        if body.punch_out is not None:
            new_out = from_ms(body.punch_out)
        if new_in is None:
            raise HTTPException(422, f"status {status} requires a punch_in")
        day_of_in = attendance_date(new_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
        if day_of_in.isoformat() != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            check_punch_window(new_in, new_out)

    old_in = rec.get("punch_in")
    old_out = rec.get("punch_out")
    old_in = old_in.replace(microsecond=0) if old_in is not None else None
    old_out = old_out.replace(microsecond=0) if old_out is not None else None
    if status == rec["status"] and new_in == old_in and new_out == old_out:
        raise HTTPException(422, "the request changes nothing")

    new = {"status": status, "punch_in": new_in, "punch_out": new_out}
    new.update(derive_fields(date.fromisoformat(rec["date"]), status, new_in, new_out, emp))
    old = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    body: RegularizeIn,
    emp_code: str,
    att_date: Annotated[str, Path(alias="date")],
):
    parse_date(att_date, "date", "path")
    emp = get_employee(emp_code)
    for _ in range(5):  # optimistic concurrency: retry if another correction got in between
        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
        if rec is None:
            raise HTTPException(404, "no attendance record for that date")
        new, changes = plan_regularization(rec, emp, body)
        entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
        guard = {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            # the history length is the version: a concurrent append makes this filter miss
            "history": {"$size": len(rec["history"])} if rec.get("history") is not None else {"$exists": False},
        }
        updated = db.attendance_logs.find_one_and_update(
            guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
        )
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "the record is being modified concurrently; retry")


# --------------------------------------------------------------------------- #
# 6. Analytics (aggregation pipelines)
# --------------------------------------------------------------------------- #
def _int_sum(expr: Any) -> dict:
    return {"$sum": expr}


# Per-log numbers, computed from the STORED values (no recomputation from punch times).
#   ph  presence in half-days, Mon-Fri only (full day 2, half day 1)        -> present_days
#   whc work_hours in hundredths, presence records with a value (any day)    -> avg_work_hours
#   whn how many such records
#   late / lc  late minutes and 1 if late                                    (any day, weekends included)
#   ot  overtime minutes, lv LEAVE records, od ON_DUTY records
_IS_PRESENT = {"$in": ["$status", PRESENCE]}
_DOW = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None, "onNull": None}}}
_LATE = {"$ifNull": ["$late_minutes", 0]}
LOG_METRICS_STAGE = {
    "$project": {
        "_id": 0,
        "ph": {
            "$cond": [
                {"$and": [_IS_PRESENT, {"$in": [_DOW, [1, 2, 3, 4, 5]]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]},
                0,
            ]
        },
        "whc": {
            "$cond": [
                {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]},
                {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}},
                0,
            ]
        },
        "whn": {"$cond": [{"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}, 1, 0]},
        "late": _LATE,
        "lc": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]},
        "ot": {"$ifNull": ["$overtime_minutes", 0]},
        "lv": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
        "od": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
    }
}
METRIC_FIELDS = ["ph", "whc", "whn", "late", "lc", "ot", "lv", "od"]


def metric_sums(prefix: str = "") -> dict:
    return {f: {"$sum": f"${prefix}{f}"} for f in METRIC_FIELDS}


def count_weekdays(first: date, last: date) -> int:
    """Mon-Fri days in [first, last] (R7, no holiday calendar)."""
    if first > last:
        return 0
    days = (last - first).days + 1
    full_weeks, extra = divmod(days, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (first + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def present_days(halves: int) -> float:
    return round_half_up(Decimal(halves) / 2, 2)


# ---- employee monthly -------------------------------------------------------
def monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        LOG_METRICS_STAGE,
        {"$group": {"_id": None, **metric_sums()}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query()]):
    first, last = parse_month(month)
    emp = get_employee(emp_code)
    row = next(db.attendance_logs.aggregate(monthly_pipeline(emp_code, first, last)), None) or {}
    start = max(first, date.fromisoformat(emp["joined_on"])) if emp.get("joined_on") else first
    working_days = count_weekdays(start, last)
    halves = int(row.get("ph", 0))
    pct = None
    if working_days > 0:
        pct = round_half_up(Decimal(halves) * 100 / (2 * working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days(halves),
        "leave_days": int(row.get("lv", 0)),
        "late_count": int(row.get("lc", 0)),
        "total_late_minutes": int(row.get("late", 0)),
        "total_overtime_minutes": int(row.get("ot", 0)),
        "attendance_pct": pct,
    }


# ---- department summary -----------------------------------------------------
def summary_pipeline(first: date, last: date, department: Optional[str]) -> list:
    match: dict = {"joined_on": {"$lte": last.isoformat()}}  # R9: joined by the end of the month
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        # Start from employees so that people with zero logs are still in the pipeline.
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    LOG_METRICS_STAGE,
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},  # keeps employees with no logs
        {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"}, **metric_sums("logs.")}},
        {"$group": {"_id": "$_id.department", "headcount": {"$sum": 1}, **metric_sums()}},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Annotated[str, Query()], department: Optional[str] = None):
    first, last = parse_month(month)
    items = []
    for r in db.employees.aggregate(summary_pipeline(first, last, department)):
        whn = int(r["whn"])
        avg = round_half_up(Decimal(int(r["whc"])) / whn / 100, 2) if whn else None
        items.append(
            {
                "department": r["_id"],
                "headcount": int(r["headcount"]),
                "present_days": present_days(int(r["ph"])),
                "avg_work_hours": avg,
                "late_count": int(r["lc"]),
                "total_late_minutes": int(r["late"]),
                "leave_count": int(r["lv"]),
                "on_duty_count": int(r["od"]),
            }
        )
    return {"month": month, "items": items}


# ---- late leaderboard -------------------------------------------------------
def leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list:
    stages: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total": {"$sum": _LATE},
                "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        stages.append({"$match": {"emp.department": department}})  # rank inside the department only
    stages += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1, 2, 2, 4).
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies to the rank, so ties at the cutoff are all kept
        {"$sort": {"total": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total",
                "late_count": 1,
            }
        },
    ]
    return stages


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query()],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    first, last = parse_month(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


# ---- department trend -------------------------------------------------------
MAX_TREND_DAYS = 92


def _half_up_div(numer: Any, denom: Any) -> dict:
    """floor(numer/denom + 1/2) for non-negative integers, exactly (no float rounding): (n - n mod d) / d."""
    n = {"$add": [{"$multiply": [2, numer]}, denom]}
    d = {"$multiply": [2, denom]}
    return {"$divide": [{"$subtract": [n, {"$mod": [n, d]}]}, d]}


def trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    window = {"documents": [-6, 0]}  # current row + up to 6 preceding rows, inside the requested range only
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        # One row per calendar day, built inside the database (weekends and empty days included).
        {"$set": {"day": {"$range": [0, days]}}},
        {"$unwind": "$day"},
        {"$set": {"dt": {"$add": [start, {"$multiply": ["$day", 86400000]}]}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date", "codes": "$codes"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$d"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                    {
                        "$group": {
                            "_id": None,
                            "halves": {
                                "$sum": {
                                    "$cond": [
                                        _IS_PRESENT,
                                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]},
                                        0,
                                    ]
                                }
                            },
                            "late": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
                        }
                    },
                ],
                "as": "s",
            }
        },
        {"$set": {"s": {"$arrayElemAt": ["$s", 0]}}},  # missing when the day has no records
        {
            "$set": {
                "halves": {"$ifNull": ["$s.halves", 0]},
                "late_count": {"$ifNull": ["$s.late", 0]},
                "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
                "working": {"$in": [{"$isoDayOfWeek": "$dt"}, [1, 2, 3, 4, 5]]},
            }
        },
        # attendance_rate in 1/10000 units, half-up, using integer arithmetic only.
        {
            "$set": {
                "k": {
                    "$cond": [
                        {"$and": ["$working", {"$gt": ["$headcount", 0]}]},
                        _half_up_div({"$multiply": ["$halves", 10000]}, {"$multiply": [2, "$headcount"]}),
                        None,
                    ]
                }
            }
        },
        {"$set": {"has_rate": {"$cond": [{"$eq": [{"$type": "$k"}, "null"]}, 0, 1]}}},
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "k_sum": {"$sum": "$k", "window": window},
                    "k_cnt": {"$sum": "$has_rate", "window": window},
                },
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": "$working",
                "headcount": 1,
                "present_count": {"$divide": ["$halves", 2]},
                "late_count": 1,
                "attendance_rate": {"$cond": [{"$eq": ["$has_rate", 1]}, {"$divide": ["$k", 10000]}, None]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$k_cnt", 0]},
                        {"$divide": [_half_up_div("$k_sum", "$k_cnt"), 10000]},
                        None,
                    ]
                },
            }
        },
    ]


def parse_trend_range(frm: Optional[str], to: Optional[str]) -> tuple[date, date]:
    d_from = parse_date(require(frm, "from"), "from")
    d_to = parse_date(require(to, "to"), "to")
    if d_to < d_from:
        raise _validation_error("query", "to", "'to' must not be before 'from'", to)
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        raise _validation_error("query", "to", f"range must not exceed {MAX_TREND_DAYS} days", to)
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    frm: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
):
    d_from, d_to = parse_trend_range(frm, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    items = list(db.employees.aggregate(trend_pipeline(department, d_from, d_to)))
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Admin: explain
# --------------------------------------------------------------------------- #
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _bson_to_json(value: Any) -> Any:
    return json.loads(json_util.dumps(value, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    frm: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    # Each branch builds the query with the very same functions the real endpoint uses.
    if endpoint == "attendance_list":
        q, sort, skip, lim = attendance_find_args(emp_code, date_from, date_to, status, page, page_size)
        coll = "attendance_logs"
        command = {"find": coll, "filter": q, "sort": dict(sort), "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            first, last = parse_month(require(month, "month"))
            coll, pipeline = "attendance_logs", monthly_pipeline(require(emp_code, "emp_code"), first, last)
        elif endpoint == "department_summary":
            first, last = parse_month(require(month, "month"))
            coll, pipeline = "employees", summary_pipeline(first, last, department)
        elif endpoint == "late_leaderboard":
            first, last = parse_month(require(month, "month"))
            coll, pipeline = "attendance_logs", leaderboard_pipeline(first, last, limit, department)
        else:  # department_trend
            d_from, d_to = parse_trend_range(frm, to)
            coll, pipeline = "employees", trend_pipeline(require(department, "department"), d_from, d_to)
        command = {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": _bson_to_json(result)}
