"""Behavioural tests that run WITHOUT a MongoDB server (mongomock). They cover R1-R5, validation, status codes,
history and the race guards. Aggregation pipelines are covered by tests/check_pipelines.* and tests/smoke_real_mongo.py.

    pip install pytest mongomock httpx
    pytest tests/test_api_mock.py
"""
import threading
from datetime import datetime, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as m

UTC = timezone.utc


def ms(y, mo, d, h, mi, s=0, ms_=0):
    """IST wall-clock -> epoch ms."""
    from datetime import timedelta
    dt = datetime(y, mo, d, h, mi, s, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    return int(dt.timestamp()) * 1000 + ms_


@pytest.fixture()
def client(monkeypatch):
    mc = mongomock.MongoClient(tz_aware=True)
    monkeypatch.setattr(m, "client", mc)
    monkeypatch.setattr(m, "db", mc["t"])
    m.ensure_indexes()
    return TestClient(m.app)


def emp(c, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "A", "email": "a@x.com", "department": "Eng", "joined_on": "2026-01-05", **kw}
    return c.post("/employees", json=body)


# ---------------------------------------------------------------- pure rules
def test_late_boundaries():
    start, _ = m.shift_bounds(datetime(2026, 7, 6).date(), "09:30", "18:30")
    f = lambda h, mi, s: m.compute_late_minutes(datetime(2026, 7, 6, h, mi, s, tzinfo=m.IST), start)
    assert f(9, 40, 0) == 0
    assert f(9, 40, 1) == 10
    assert f(10, 15, 59) == 45
    assert f(9, 0, 0) == 0


def test_overtime_boundaries():
    _, end = m.shift_bounds(datetime(2026, 7, 6).date(), "09:30", "18:30")
    f = lambda h, mi, s: m.compute_overtime(datetime(2026, 7, 6, h, mi, s, tzinfo=m.IST), end)
    assert f(19, 0, 0) == 30
    assert f(18, 59, 59) == 0
    assert f(19, 10, 59) == 40
    assert f(17, 0, 0) == 0


def test_work_hours_half_up():
    t0 = datetime(2026, 7, 6, 9, 0, tzinfo=m.IST)
    # 0.125 h = 450 s -> 0.13 (half-up), float round() would give 0.12
    assert m.compute_work_hours(t0, t0.replace(minute=7, second=30)) == m.Decimal("0.13")
    assert m.compute_work_hours(t0, t0.replace(hour=13, minute=30)) == m.Decimal("4.50")


def test_overnight_date_and_overtime():
    # shift 22:00-06:00. punch-in 02:00 IST on the 7th belongs to the shift of the 6th.
    t = datetime(2026, 7, 7, 2, 0, tzinfo=m.IST)
    assert m.attendance_date(t, "22:00", "06:00").isoformat() == "2026-07-06"
    assert m.attendance_date(datetime(2026, 7, 6, 21, 55, tzinfo=m.IST), "22:00", "06:00").isoformat() == "2026-07-06"
    assert m.attendance_date(datetime(2026, 7, 7, 6, 0, tzinfo=m.IST), "22:00", "06:00").isoformat() == "2026-07-07"
    _, end = m.shift_bounds(datetime(2026, 7, 6).date(), "22:00", "06:00")
    assert m.compute_overtime(datetime(2026, 7, 7, 6, 40, tzinfo=m.IST), end) == 40


# ---------------------------------------------------------------- employees
def test_employee_crud_and_validation(client):
    r = emp(client)
    assert r.status_code == 201 and "_id" not in r.json() and isinstance(r.json()["created_at"], int)
    assert emp(client).status_code == 409
    assert emp(client, "EMP12").status_code == 422
    assert emp(client, "EMP0002", shift_start="09:00", shift_end="09:00").status_code == 422
    assert emp(client, "EMP0003", joined_on="2026-13-01").status_code == 422
    assert emp(client, "EMP0004", email="nope").status_code == 422
    for i in range(5, 12):
        emp(client, f"EMP{i:04d}", department="Ops" if i % 2 else "Eng")
    r = client.get("/employees", params={"department": "Ops", "page": 1, "page_size": 2}).json()
    assert r["total"] == 4 and [e["emp_code"] for e in r["items"]] == ["EMP0005", "EMP0007"]
    r2 = client.get("/employees", params={"department": "Ops", "page": 2, "page_size": 2}).json()
    assert [e["emp_code"] for e in r2["items"]] == ["EMP0009", "EMP0011"]
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


def test_concurrent_employee_create(client):
    codes = []

    def go():
        codes.append(emp(client, "EMP0099").status_code)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [201] + [409] * 7


# ---------------------------------------------------------------- punch in / out
def test_punch_flow(client):
    emp(client)
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 40, 1, 900)})
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["date"] == "2026-07-06" and b["late_minutes"] == 10 and b["history"] == []
    assert b["punch_in"] == ms(2026, 7, 6, 9, 40, 1)  # truncated to whole seconds
    assert b["punch_out"] is None and b["work_hours"] is None and b["half_day"] is False and "id" not in b
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 11, 0)}).status_code == 409
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 10, 59)})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["overtime_minutes"] == 40 and b["work_hours"] == 9.52 and b["half_day"] is False
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 19, 20)}).status_code == 409


def test_punch_validation(client):
    emp(client)
    bad = [
        {"emp_code": "EMP0001", "punched_at": 1783312500},  # seconds
        {"emp_code": "EMP0001", "punched_at": 1783312500000.5},
        {"emp_code": "EMP0001", "punched_at": "1783312500000"},
        {"emp_code": "EMP0001", "punched_at": 4102444800001},
        {"emp_code": "EMP0001", "punched_at": True},
        {"emp_code": "EMP0001", "status": "ABSENT"},
        {"emp_code": "EMP0001", "status": "BOGUS"},
        {},
    ]
    for body in bad:
        assert client.post("/attendance/punch-in", json=body).status_code == 422, body
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404  # no punch-in
    # derived fields sent by a client are ignored
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0), "late_minutes": 99, "work_hours": 5})
    assert r.status_code == 201 and r.json()["late_minutes"] == 0 and r.json()["work_hours"] is None


def test_punch_out_window(client):
    emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)})
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0, 1)}).status_code == 422
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 13, 30)})
    assert r.status_code == 200 and r.json()["work_hours"] == 4.5 and r.json()["half_day"] is False
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 13, 29, 59)})
    assert r.json()["work_hours"] == 4.5 and r.json()["half_day"] is False  # rounded value is 4.50, not < 4.50
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 8, 9, 0)})
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 8, 13, 29)})
    assert r.json()["work_hours"] == 4.48 and r.json()["half_day"] is True


def test_overnight_punch_across_midnight(client):
    emp(client, "EMP0005", shift_start="22:00", shift_end="06:00")
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 6, 21, 55)})
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 7, 6, 40)})
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 40 and r.json()["work_hours"] == 8.75
    # late arrival after midnight still belongs to the previous day's shift
    r = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(2026, 7, 8, 2, 0)})
    assert r.json()["date"] == "2026-07-07" and r.json()["late_minutes"] == 240


def test_concurrent_punch_in_and_out(client):
    emp(client)
    codes = []

    def pin():
        codes.append(client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 9, 0)}).status_code)

    ts = [threading.Thread(target=pin) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(codes) == [201] + [409] * 9
    outs = []

    def pout():
        outs.append(client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 6, 18, 0)}).status_code)

    ts = [threading.Thread(target=pout) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(outs) == [200] + [409] * 9


# ---------------------------------------------------------------- list
def test_list_attendance(client):
    emp(client, "EMP0001")
    emp(client, "EMP0002")
    for code in ("EMP0002", "EMP0001"):
        for d in (6, 7, 8):
            client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(2026, 7, d, 9, 0)})
    r = client.get("/attendance", params={"page_size": 4}).json()
    assert r["total"] == 6
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [("2026-07-08", "EMP0001"), ("2026-07-08", "EMP0002"), ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002")]
    r = client.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-07", "date_to": "2026-07-07"}).json()
    assert r["total"] == 1
    assert client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-07"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "2026-7-8"}).status_code == 422
    assert client.get("/attendance", params={"status": "NOPE"}).status_code == 422
    assert client.get("/attendance", params={"status": "WFH"}).json()["total"] == 0


# ---------------------------------------------------------------- regularize
def test_regularize(client):
    emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 10, 5)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 18, 30)})
    url = "/attendance/EMP0001/2026-07-07"
    hdr = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    r = client.patch(url, json={"punch_in": ms(2026, 7, 7, 9, 28), **hdr})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["late_minutes"] == 0 and b["work_hours"] == 9.03
    assert len(b["history"]) == 1
    ch = b["history"][0]["changes"]
    assert set(ch) == {"punch_in", "late_minutes", "work_hours"}  # only what actually changed
    assert ch["punch_in"] == {"from": ms(2026, 7, 7, 10, 5), "to": ms(2026, 7, 7, 9, 28)}
    assert ch["late_minutes"] == {"from": 35, "to": 0}
    assert isinstance(b["history"][0]["at"], int) and b["history"][0]["by"] == "hr.admin"
    # second correction appends
    r = client.patch(url, json={"status": "LEAVE", **hdr}).json()
    assert len(r["history"]) == 2 and r["punch_in"] is None and r["punch_out"] is None and r["work_hours"] is None
    assert r["history"][1]["changes"]["status"] == {"from": "PRESENT", "to": "LEAVE"}
    # rules
    assert client.patch(url, json={"status": "ABSENT", "punch_in": ms(2026, 7, 7, 9, 0), **hdr}).status_code == 422
    assert client.patch(url, json={"status": "LEAVE", **hdr}).status_code == 422  # changes nothing
    assert client.patch(url, json={"status": "WFH", **hdr}).status_code == 422  # presence needs a punch_in
    assert client.patch(url, json={"status": "WFH", "punch_in": ms(2026, 7, 8, 9, 0), **hdr}).status_code == 422  # wrong date
    assert client.patch(url, json={"status": "WFH", "punch_in": ms(2026, 7, 7, 9, 0), "punch_out": ms(2026, 7, 7, 9, 0), **hdr}).status_code == 422
    assert client.patch(url, json={"status": "WFH", "punch_in": ms(2026, 7, 7, 9, 0), "punch_out": ms(2026, 7, 8, 9, 0, 1), **hdr}).status_code == 422
    r = client.patch(url, json={"status": "WFH", "punch_in": ms(2026, 7, 7, 9, 0), "punch_out": ms(2026, 7, 7, 13, 0), **hdr})
    assert r.status_code == 200 and r.json()["half_day"] is True and r.json()["work_hours"] == 4.0 and len(r.json()["history"]) == 3
    assert client.patch(url, json={"status": "WFH", "reason": "x", "regularized_by": "a"}).status_code == 422
    assert client.patch(url, json={"reason": "valid reason", "regularized_by": "a", "work_hours": 3}).status_code == 422  # nothing changed (derived ignored)
    assert client.patch("/attendance/EMP0001/2026-07-09", json={"status": "LEAVE", **hdr}).status_code == 404
    assert client.patch("/attendance/EMP9999/2026-07-07", json={"status": "LEAVE", **hdr}).status_code == 404
    assert client.patch("/attendance/EMP0001/2026-7-7", json={"status": "LEAVE", **hdr}).status_code == 422


def test_regularize_legacy_record(client):
    """Seeded record without history / half_day (DATA_MODEL sample 9)."""
    emp(client, "EMP0002")
    m.db.attendance_logs.insert_one({
        "emp_code": "EMP0002", "date": "2026-07-14", "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 4, 2, tzinfo=UTC), "punch_out": datetime(2026, 7, 14, 13, 1, tzinfo=UTC),
        "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0,
    })
    rec = client.get("/attendance", params={"emp_code": "EMP0002"}).json()["items"][0]
    assert rec["history"] == [] and rec["half_day"] is False
    r = client.patch("/attendance/EMP0002/2026-07-14", json={"status": "ON_DUTY", "reason": "official visit", "regularized_by": "hr"})
    assert r.status_code == 200 and len(r.json()["history"]) == 1 and list(r.json()["history"][0]["changes"]) == ["status"]


def test_concurrent_regularize_keeps_every_entry(client):
    emp(client)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(2026, 7, 7, 9, 0)})
    codes = []

    def patch(i):
        codes.append(client.patch("/attendance/EMP0001/2026-07-07", json={"punch_in": ms(2026, 7, 7, 9, 0) + (i + 1) * 60000, "reason": f"edit {i}", "regularized_by": "hr"}).status_code)

    ts = [threading.Thread(target=patch, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rec = client.get("/attendance", params={"emp_code": "EMP0001"}).json()["items"][0]
    assert all(c in (200, 409) for c in codes)
    assert len(rec["history"]) == codes.count(200)  # nothing lost, nothing double-counted


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
