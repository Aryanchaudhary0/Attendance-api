"""Run this against a REAL MongoDB 6/7 before submitting. It seeds ~100k attendance records, starts the app
in-process, then checks: every endpoint answers without a 500, /admin/explain plans contain IXSCAN and no
COLLSCAN, and the endpoints are fast.

    docker run -d -p 27017:27017 mongo:7
    MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_smoke python tests/smoke_real_mongo.py

WARNING: it DROPS the database named in MONGO_DB (default: attendance_smoke). Do not point it at real data.
"""
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGO_DB", "attendance_smoke")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402

N_EMP, DAYS = 400, 250  # ~100k records
rnd = random.Random(1)
m.client.drop_database(m.db.name)
depts = ["Engineering", "Sales", "Support", "HR", "Finance"]
emps = [{"emp_code": f"EMP{i:04d}", "name": f"Name {i}", "email": f"e{i}@x.com", "department": depts[i % 5],
         "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01",
         "created_at": datetime.now(timezone.utc)} for i in range(1, N_EMP + 1)]
m.db.employees.insert_many(emps)
start = datetime(2026, 1, 1).date()
docs = []
for e in emps:
    for d in range(DAYS):
        day = start + timedelta(days=d)
        if rnd.random() < 0.2:
            continue
        late = rnd.choice([0, 0, 0, 15, 25, 40])
        docs.append({"emp_code": e["emp_code"], "date": day.isoformat(), "status": rnd.choice(["PRESENT"] * 8 + ["WFH", "LEAVE"]),
                     "punch_in": None, "punch_out": None, "work_hours": round(rnd.uniform(3, 10), 2), "late_minutes": late,
                     "overtime_minutes": 0, "half_day": False, "history": []})
m.db.attendance_logs.insert_many(docs, ordered=False)
print(f"seeded {len(docs)} attendance records")

failures = 0
with TestClient(m.app) as c:  # runs the lifespan -> creates the indexes
    def check(label, ok, extra=""):
        global failures
        failures += (not ok)
        print(("ok  " if ok else "FAIL"), label, extra)

    check("health", c.get("/health").status_code == 200)
    print("indexes:", {k: v["key"] for k, v in m.db.attendance_logs.index_information().items()})
    calls = {
        "attendance_list": {"emp_code": "EMP0007", "date_from": "2026-03-01", "date_to": "2026-04-30", "page": 2, "page_size": 20},
        "attendance_list#2": {"status": "WFH", "page": 1},
        "attendance_list#3": {},
        "employee_monthly": {"emp_code": "EMP0007", "month": "2026-03"},
        "department_summary": {"month": "2026-03"},
        "late_leaderboard": {"month": "2026-03", "limit": 5},
        "department_trend": {"department": "Sales", "from": "2026-03-01", "to": "2026-05-31"},
    }
    for name, params in calls.items():
        ep = name.split("#")[0]
        t = time.time()
        r = c.get(f"/admin/explain/{ep}", params=params)
        if r.status_code != 200:
            check(f"explain {name}", False, f"-> {r.status_code} {r.text[:200]}")
            continue
        plan = json.dumps(r.json()["explain"])
        check(f"explain {name}", "IXSCAN" in plan and "COLLSCAN" not in plan,
              f"IXSCAN={'IXSCAN' in plan} COLLSCAN={'COLLSCAN' in plan} ({time.time() - t:.2f}s)")
    urls = [
        ("/attendance", {"page": 3}), ("/attendance", {"emp_code": "EMP0007", "date_from": "2026-03-01"}),
        ("/analytics/employees/EMP0007/monthly", {"month": "2026-03"}), ("/analytics/departments/summary", {"month": "2026-03"}),
        ("/analytics/leaderboard/late", {"month": "2026-03"}), ("/analytics/departments/Sales/trend", {"from": "2026-03-01", "to": "2026-05-31"}),
    ]
    for url, params in urls:
        t = time.time()
        r = c.get(url, params=params)
        check(f"GET {url} {params}", r.status_code == 200, f"{r.status_code} in {time.time() - t:.2f}s")
print("FAILED" if failures else "SMOKE OK", failures)
sys.exit(1 if failures else 0)
