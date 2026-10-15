"""Checks the four analytics aggregation pipelines of app/main.py against a brute-force Python reference, on an
awkward generated dataset (ties, no-log employees, weekends, mid-month joiners, overnight shifts, half days,
legacy records without half_day, logs of unknown employees, records outside the month).

There is no MongoDB server in the environment this was written in, so the pipelines are executed by the JS
engine "mingo" (a MongoDB query-language implementation) through tests/mingo_runner.js. mingo is NOT MongoDB:
it validates the pipeline LOGIC, not index usage or server-side quirks. Run tests/smoke_real_mongo.py against a
real MongoDB 6/7 for those.

    cd tests && npm i mingo@6 && cd ..          # once
    python tests/check_pipelines.py
"""
import json
import os
import random
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app.main as m  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- engine shim
def _enc(o):
    if isinstance(o, datetime):
        return {"__date__": o.astimezone(m.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    raise TypeError(type(o))


class Coll:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def aggregate(self, pipeline):
        job = {"pipeline": pipeline, "collection": self.name, "collections": self.store}
        path = os.path.join(HERE, "_job.json")
        with open(path, "w") as f:
            json.dump(job, f, default=_enc)
        env = dict(os.environ, NODE_PATH=os.environ.get("NODE_PATH", os.path.join(HERE, "node_modules")))
        res = subprocess.run(["node", os.path.join(HERE, "mingo_runner.js"), path], capture_output=True, text=True, env=env)
        if res.returncode:
            raise RuntimeError(res.stderr[-1500:])
        return iter(json.loads(res.stdout))

    def find_one(self, flt, proj=None):
        for d in self.store[self.name]:
            if all(d.get(k) == v for k, v in flt.items()):
                return {k: v for k, v in d.items() if k != "_id"}

    def count_documents(self, flt, limit=0):
        return sum(1 for d in self.store[self.name] if all(d.get(k) == v for k, v in flt.items()))


class FakeDB:
    def __init__(self, store):
        self.employees, self.attendance_logs = Coll(store, "employees"), Coll(store, "attendance_logs")


# --------------------------------------------------------------------------- dataset
def build_dataset(seed=7):
    rnd = random.Random(seed)
    employees, logs = [], []
    depts = ["Engineering", "Sales", "Support", "Empty-Logs", "Late-Joiners"]

    def add_emp(code, dept, joined, ss="09:30", se="18:30"):
        employees.append({"emp_code": code, "name": f"N-{code}", "email": f"{code}@x.com", "department": dept,
                          "shift_start": ss, "shift_end": se, "joined_on": joined})

    n = 1
    for dept in depts[:3]:
        for _ in range(8):
            ss, se = ("22:00", "06:00") if dept == "Support" and n % 3 == 0 else ("09:30", "18:30")
            add_emp(f"EMP{n:04d}", dept, rnd.choice(["2026-01-05", "2026-03-01", "2026-07-01", "2026-07-15", "2026-07-20", "2026-08-10"]), ss, se)
            n += 1
    add_emp(f"EMP{n:04d}", "Empty-Logs", "2026-02-01"); n += 1  # employee with zero logs
    add_emp(f"EMP{n:04d}", "Empty-Logs", "2026-02-01"); n += 1
    add_emp(f"EMP{n:04d}", "Late-Joiners", "2026-09-01"); n += 1  # joins after the tested months

    statuses = ["PRESENT"] * 6 + ["WFH", "ON_DUTY", "LEAVE", "ABSENT"]
    d0 = date(2026, 6, 28)
    for e in employees:
        if e["department"] in ("Empty-Logs", "Late-Joiners"):
            continue
        for i in range(0, 70):  # 28 Jun - 5 Sep, includes weekends
            day = d0 + timedelta(days=i)
            if rnd.random() < 0.3:
                continue  # missing day (no record at all)
            if day.weekday() >= 5 and rnd.random() < 0.6:
                continue
            st = rnd.choice(statuses)
            rec = {"emp_code": e["emp_code"], "date": day.isoformat(), "status": st}
            if st in m.PRESENCE:
                sh, sm = divmod(m.hhmm_minutes(e["shift_start"]), 60)
                start, end = m.shift_bounds(day, e["shift_start"], e["shift_end"])
                p_in = start + timedelta(seconds=rnd.randint(-900, 4000))
                dur = rnd.choice([rnd.randint(2 * 3600, 5 * 3600), rnd.randint(8 * 3600, 11 * 3600)])
                p_out = p_in + timedelta(seconds=dur) if rnd.random() < 0.9 else None
                rec.update(punch_in=p_in, punch_out=p_out, **m.derive_fields(day, st, p_in, p_out, e))
            else:
                rec.update(punch_in=None, punch_out=None, work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
            if rnd.random() < 0.1:  # legacy shapes
                rec.pop("half_day", None)
                rec.pop("history", None)
            logs.append(rec)
    # a log whose employee does not exist
    logs.append({"emp_code": "EMP9999", "date": "2026-07-08", "status": "PRESENT", "late_minutes": 500, "work_hours": 9.0,
                 "overtime_minutes": 0, "half_day": False, "punch_in": None, "punch_out": None})
    # forced ties on late minutes for the leaderboard (two employees in one department)
    for code in ("EMP0001", "EMP0002", "EMP0003"):
        logs[:] = [l for l in logs if not (l["emp_code"] == code and l["date"].startswith("2026-07"))]
        for d in ("2026-07-06", "2026-07-07"):
            logs.append({"emp_code": code, "date": d, "status": "PRESENT", "late_minutes": 20, "work_hours": 8.0,
                         "overtime_minutes": 0, "half_day": False, "punch_in": None, "punch_out": None})
    return employees, logs


# --------------------------------------------------------------------------- reference (brute force, Decimal)
def hu(x, places):
    return float(Decimal(x).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def is_present(l):
    return l["status"] in m.PRESENCE


def weight(l):
    return (Decimal("0.5") if l.get("half_day") else Decimal(1)) if is_present(l) else Decimal(0)


def wd(d: date):
    return d.weekday() < 5


def ref_month_logs(logs, month, code=None):
    return [l for l in logs if l["date"].startswith(month) and (code is None or l["emp_code"] == code)]


def ref_monthly(emps, logs, code, month):
    first, last = m.parse_month(month)
    e = next(e for e in emps if e["emp_code"] == code)
    start = max(first, date.fromisoformat(e["joined_on"]))
    days = sum(1 for i in range((last - start).days + 1) if wd(start + timedelta(days=i))) if start <= last else 0
    L = ref_month_logs(logs, month, code)
    present = sum((weight(l) for l in L if wd(date.fromisoformat(l["date"]))), Decimal(0))
    return {
        "emp_code": code, "month": month, "working_days": days, "present_days": hu(present, 2),
        "leave_days": sum(1 for l in L if l["status"] == "LEAVE"),
        "late_count": sum(1 for l in L if (l.get("late_minutes") or 0) > 0),
        "total_late_minutes": sum(l.get("late_minutes") or 0 for l in L),
        "total_overtime_minutes": sum(l.get("overtime_minutes") or 0 for l in L),
        "attendance_pct": hu(present * 100 / days, 2) if days else None,
    }


def ref_summary(emps, logs, month, dept=None):
    first, last = m.parse_month(month)
    out = {}
    for e in emps:
        if e["joined_on"] > last.isoformat() or (dept and e["department"] != dept):
            continue
        o = out.setdefault(e["department"], {"h": 0, "p": Decimal(0), "wh": [], "lc": 0, "lm": 0, "lv": 0, "od": 0})
        o["h"] += 1
        for l in ref_month_logs(logs, month, e["emp_code"]):
            if wd(date.fromisoformat(l["date"])):
                o["p"] += weight(l)
            if is_present(l) and l.get("work_hours") is not None:
                o["wh"].append(Decimal(str(l["work_hours"])))
            if (l.get("late_minutes") or 0) > 0:
                o["lc"] += 1
                o["lm"] += l["late_minutes"]
            o["lv"] += l["status"] == "LEAVE"
            o["od"] += l["status"] == "ON_DUTY"
    return {"month": month, "items": [
        {"department": d, "headcount": o["h"], "present_days": hu(o["p"], 2),
         "avg_work_hours": hu(sum(o["wh"]) / len(o["wh"]), 2) if o["wh"] else None,
         "late_count": o["lc"], "total_late_minutes": o["lm"], "leave_count": o["lv"], "on_duty_count": o["od"]}
        for d, o in sorted(out.items())]}


def ref_leaderboard(emps, logs, month, limit, dept=None):
    by = {e["emp_code"]: e for e in emps}
    tot = {}
    for l in ref_month_logs(logs, month):
        if l["emp_code"] in by and (not dept or by[l["emp_code"]]["department"] == dept):
            t = tot.setdefault(l["emp_code"], [0, 0])
            t[0] += l.get("late_minutes") or 0
            t[1] += (l.get("late_minutes") or 0) > 0
    rows = sorted(((c, t) for c, t in tot.items() if t[0] > 0), key=lambda x: (-x[1][0], x[0]))
    items = []
    for c, t in rows:
        rank = 1 + sum(1 for _, u in rows if u[0] > t[0])
        if rank <= limit:
            items.append({"rank": rank, "emp_code": c, "name": by[c]["name"], "department": by[c]["department"],
                          "total_late_minutes": t[0], "late_count": t[1]})
    return {"month": month, "items": items}


def ref_trend(emps, logs, dept, d_from, d_to):
    E = [e for e in emps if e["department"] == dept]
    codes = {e["emp_code"] for e in E}
    rows, rates = [], []
    day = d_from
    while day <= d_to:
        iso = day.isoformat()
        L = [l for l in logs if l["date"] == iso and l["emp_code"] in codes]
        present = sum((weight(l) for l in L), Decimal(0))
        head = sum(1 for e in E if e["joined_on"] <= iso)
        working = wd(day)
        rate = hu(present / head, 4) if working and head else None
        rates.append(rate)
        window = [r for r in rates[-7:] if r is not None]
        mov = hu(sum(Decimal(str(r)) for r in window) / len(window), 4) if window else None
        rows.append({"date": iso, "is_working_day": working, "headcount": head, "present_count": float(present),
                     "late_count": sum(1 for l in L if (l.get("late_minutes") or 0) > 0),
                     "attendance_rate": rate, "moving_avg_7d": mov})
        day += timedelta(days=1)
    return {"department": dept, "items": rows}


# --------------------------------------------------------------------------- run
def main():
    emps, logs = build_dataset()
    store = {"employees": emps, "attendance_logs": logs}
    for e in emps:
        pass
    m.db = FakeDB(store)
    bad = 0

    def check(name, got, want):
        nonlocal bad
        if got != want:
            bad += 1
            print("MISMATCH", name)
            print("  got :", json.dumps(got)[:600])
            print("  want:", json.dumps(want)[:600])
        else:
            print("ok", name)

    for month in ("2026-07", "2026-08", "2026-06", "2026-09"):
        for code in ("EMP0001", "EMP0005", "EMP0009", "EMP0017", "EMP0025", "EMP0026", "EMP0027"):
            if any(e["emp_code"] == code for e in emps):
                check(f"monthly {code} {month}", m.employee_monthly(code, month), ref_monthly(emps, logs, code, month))
        check(f"summary {month}", m.department_summary(month, None), ref_summary(emps, logs, month))
        check(f"summary {month} Sales", m.department_summary(month, "Sales"), ref_summary(emps, logs, month, "Sales"))
        for limit in (1, 2, 3, 10, 50):
            check(f"leaderboard {month} limit={limit}", m.late_leaderboard(month, limit, None), ref_leaderboard(emps, logs, month, limit))
            check(f"leaderboard {month} limit={limit} Engineering", m.late_leaderboard(month, limit, "Engineering"), ref_leaderboard(emps, logs, month, limit, "Engineering"))
    for dept in ("Engineering", "Sales", "Support", "Empty-Logs", "Late-Joiners"):
        for a, b in (("2026-07-01", "2026-07-31"), ("2026-07-10", "2026-08-20"), ("2026-07-04", "2026-07-04"), ("2026-06-29", "2026-07-05"), ("2026-09-01", "2026-09-10")):
            got = m.department_trend(dept, a, b)
            check(f"trend {dept} {a}..{b}", got, ref_trend(emps, logs, dept, date.fromisoformat(a), date.fromisoformat(b)))
    # hand-built tie: totals 100, 80, 80, 50, 30 -> ranks 1, 2, 2, 4, 5 ; a never-late employee is not listed
    te = [{"emp_code": f"EMP{i:04d}", "name": f"T{i}", "email": "t@x.com", "department": "T", "joined_on": "2026-01-01"} for i in range(1, 7)]
    totals = {"EMP0001": 50, "EMP0002": 80, "EMP0003": 100, "EMP0004": 80, "EMP0005": 30, "EMP0006": 0}
    tl = [{"emp_code": c, "date": "2026-07-07", "status": "PRESENT", "late_minutes": v} for c, v in totals.items()]
    m.db = FakeDB({"employees": te, "attendance_logs": tl})
    for limit, want in ((1, ["EMP0003"]), (2, ["EMP0003", "EMP0002", "EMP0004"]), (3, ["EMP0003", "EMP0002", "EMP0004"]),
                        (4, ["EMP0003", "EMP0002", "EMP0004", "EMP0001"]), (10, ["EMP0003", "EMP0002", "EMP0004", "EMP0001", "EMP0005"])):
        got = m.late_leaderboard("2026-07", limit, None)["items"]
        check(f"tie limit={limit}", [(i["emp_code"], i["rank"]) for i in got],
              [(c, 1 + sum(1 for o in totals.values() if o > totals[c])) for c in want])
    print("FAILED" if bad else "ALL PIPELINE CHECKS PASSED", bad)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
