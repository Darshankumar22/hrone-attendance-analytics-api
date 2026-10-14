"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from pydantic import BaseModel, ConfigDict, StrictInt, Field, field_validator, model_validator
import os
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictInt, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from datetime import datetime, timezone, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from typing import Optional, Literal
from pydantic import BaseModel, StrictInt

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")
from contextlib import asynccontextmanager


def create_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )

    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )

    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined",
    )

    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )

    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )

    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING)],
        name="ix_attendance_status_date",
    )


@app.on_event("startup")
def initialize_database():
    create_indexes()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    punch_in = truncate_seconds(punch_in)

    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(
        hour=h, minute=m, second=0, microsecond=0
    )

    # For an overnight shift, early-morning punch-ins
    # belong to the shift that started the previous day.
    if punch_in.hour * 60 + punch_in.minute < h * 60 + m:
        start -= timedelta(days=1)

    elapsed_seconds = (punch_in - start).total_seconds()

    # Strictly more than 10 minutes late.
    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int(
        (truncate_seconds(punch_out) - truncate_seconds(punch_in))
        .total_seconds()
    )

    hours = Decimal(seconds) / Decimal(3600)

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: Optional[str] = None,
) -> int:
    h, m = map(int, shift_end.split(":"))

    end = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=h, minute=m, tzinfo=IST
    )

    # If shift_end <= shift_start, the shift ends next day.
    if shift_start is not None:
        sh, sm = map(int, shift_start.split(":"))
        if (h, m) <= (sh, sm):
            end += timedelta(days=1)

    elapsed_seconds = (
        truncate_seconds(punch_out) - end
    ).total_seconds()

    minutes = int(elapsed_seconds // 60)

    return minutes if minutes >= 30 else 0

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


from typing import Literal
from pydantic import StrictInt

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeRequest(BaseModel):
    regularized_by: str
    reason: str
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        )


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # Convert epoch milliseconds to an IST datetime.
    if body.punched_at is None:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        try:
            ts_utc = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            ).replace(microsecond=0)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="Invalid punched_at timestamp",
            )

    ts_ist = ts_utc.astimezone(IST)

    # Attendance date is based on the shift's start date.
    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    overnight = (eh, em) <= (sh, sm)
    attendance_day = ts_ist.date()

    if overnight and (ts_ist.hour, ts_ist.minute) < (eh, em):
        attendance_day -= timedelta(days=1)

    date_str = attendance_day.isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_ist, shift_start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        result = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    doc["id"] = str(result.inserted_id)
    doc.pop("_id", None)

    # Return instants as epoch milliseconds.
    doc["punch_in"] = int(ts_utc.timestamp() * 1000)
    doc["punch_out"] = None

    return doc


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # Use the current instant when punched_at is omitted.
    if body.punched_at is None:
        ts_utc = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        if not 100000000000 <= body.punched_at <= 4102444800000:
            raise HTTPException(
                status_code=422,
                detail="Invalid punched_at timestamp",
            )

        try:
            ts_utc = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            ).replace(microsecond=0)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="Invalid punched_at timestamp",
            )

    # Find the latest record that started no later than punch-out.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts_utc},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Record already punched out",
        )

    # MongoDB may return naive datetimes representing UTC.
    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    elapsed_seconds = (ts_utc - punch_in_utc).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_utc, ts_utc)

    employee_shift_start = emp["shift_start"]
    employee_shift_end = emp["shift_end"]

    overtime_minutes = compute_overtime(
        ts_utc,
        employee_shift_end,
        record["date"],
        employee_shift_start,
    )

    half_day = work_hours < 4.50

    # Atomic conditional update: simultaneous requests cannot both win.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=True,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Record already punched out",
        )

    # Return the public API fields; never expose MongoDB's _id.
    return {
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": int(punch_in_utc.timestamp() * 1000),
        "punch_out": int(ts_utc.timestamp() * 1000),
        "work_hours": updated["work_hours"],
        "late_minutes": updated["late_minutes"],
        "overtime_minutes": updated["overtime_minutes"],
        "half_day": updated["half_day"],
        "history": updated.get("history", []),
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest,
):
    # Validate request metadata.
    if not 1 <= len(body.regularized_by) <= 50:
        raise HTTPException(422, "regularized_by must be 1-50 characters")

    if not 5 <= len(body.reason) <= 200:
        raise HTTPException(422, "reason must be 5-200 characters")

    try:
        attendance_date = datetime.strptime(date, "%Y-%m-%d").date()
        if attendance_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    query = {"emp_code": emp_code, "date": date}
    old = db.attendance_logs.find_one(query)

    if old is None:
        raise HTTPException(404, "Attendance record not found")

    old_status = old["status"]
    new_status = body.status if body.status is not None else old_status

    def from_ms(value):
        if value is None:
            return None
        if not 100000000000 <= value <= 4102444800000:
            raise HTTPException(422, "Invalid epoch-millisecond timestamp")
        try:
            return datetime.fromtimestamp(
                value / 1000, tz=timezone.utc
            ).replace(microsecond=0)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, "Invalid timestamp")

    old_in = old.get("punch_in")
    old_out = old.get("punch_out")

    if old_in and old_in.tzinfo is None:
        old_in = old_in.replace(tzinfo=timezone.utc)
    if old_out and old_out.tzinfo is None:
        old_out = old_out.replace(tzinfo=timezone.utc)

    new_in = (
        from_ms(body.punch_in)
        if body.punch_in is not None
        else old_in
    )
    new_out = (
        from_ms(body.punch_out)
        if body.punch_out is not None
        else old_out
    )

    # ABSENT and LEAVE cannot have punch times.
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422, "ABSENT and LEAVE cannot include punch times"
            )
        new_in = None
        new_out = None

    # Presence statuses require a punch-in.
    elif new_in is None:
        raise HTTPException(
            422, "Presence status requires punch_in"
        )

    # Punch-in must belong to this attendance date in IST.
    if new_in is not None:
        if new_in.astimezone(IST).date().isoformat() != date:
            raise HTTPException(
                422, "punch_in must fall on the attendance date"
            )

    if new_out is not None:
        if new_in is None:
            raise HTTPException(422, "punch_out requires punch_in")

        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 86400:
            raise HTTPException(
                422, "punch_out must be after punch_in and within 24 hours"
            )

    # Calculate derived fields from the final timestamps.
    late_minutes = 0
    work_hours = None
    overtime_minutes = 0
    half_day = False

    if new_in is not None:
        local_in = new_in.astimezone(IST)
        shift_start = emp["shift_start"]
        shift_end = emp["shift_end"]

        sh, sm = map(int, shift_start.split(":"))
        start_dt = local_in.replace(
            hour=sh, minute=sm, second=0, microsecond=0
        )
        late_seconds = (local_in - start_dt).total_seconds()
        late_minutes = (
            int(late_seconds // 60)
            if late_seconds > 600
            else 0
        )

    if new_in is not None and new_out is not None:
        seconds = (new_out - new_in).total_seconds()
        work_hours = float(
            Decimal(str(seconds / 3600)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        half_day = work_hours < 4.50

        eh, em = map(int, emp["shift_end"].split(":"))
        local_in = new_in.astimezone(IST)
        local_out = new_out.astimezone(IST)

        end_dt = local_in.replace(
            hour=eh, minute=em, second=0, microsecond=0
        )
        if (eh, em) <= tuple(map(int, emp["shift_start"].split(":"))):
            end_dt += timedelta(days=1)

        overtime_seconds = (local_out - end_dt).total_seconds()
        overtime_minutes = (
            int(overtime_seconds // 60)
            if overtime_seconds >= 1800
            else 0
        )

    # Record only fields that actually changed.
    def ms(value):
        if value is None:
            return None
        return int(value.timestamp() * 1000)

    changes = {}

    for field, before, after in [
        ("status", old_status, new_status),
        ("punch_in", old.get("punch_in"), new_in),
        ("punch_out", old.get("punch_out"), new_out),
        ("late_minutes", old.get("late_minutes", 0), late_minutes),
        ("work_hours", old.get("work_hours"), work_hours),
        ("overtime_minutes", old.get("overtime_minutes", 0), overtime_minutes),
        ("half_day", old.get("half_day", False), half_day),
    ]:
        if field in ("punch_in", "punch_out"):
            before = ms(before)
            after = ms(after)

        if before != after:
            changes[field] = {"from": before, "to": after}

    if not changes:
        raise HTTPException(422, "Request does not change the record")

    history_entry = {
        "at": int(datetime.now(timezone.utc).timestamp() * 1000),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare the old history and values to prevent lost concurrent edits.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": old["_id"],
            "history": old.get("history", []),
            "status": old_status,
            "punch_in": old.get("punch_in"),
            "punch_out": old.get("punch_out"),
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "late_minutes": late_minutes,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            409, "Attendance record changed concurrently; retry"
        )

    return {
        "emp_code": updated["emp_code"],
        "date": updated["date"],
        "status": updated["status"],
        "punch_in": ms(updated.get("punch_in")),
        "punch_out": ms(updated.get("punch_out")),
        "work_hours": updated.get("work_hours"),
        "late_minutes": updated.get("late_minutes", 0),
        "overtime_minutes": updated.get("overtime_minutes", 0),
        "half_day": updated.get("half_day", False),
        "history": updated.get("history", []),
    }

@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    for item in items:
        created_at = item.get("created_at")
        if isinstance(created_at, datetime):
            item["created_at"] = int(
                created_at.replace(tzinfo=timezone.utc).timestamp() * 1000
            )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    ts = datetime.fromtimestamp(body.punched_at / 1000) if body.punched_at else datetime.now()
    d = ts.date().isoformat()
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from fastapi import HTTPException, Query


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    employee = db.employees.find_one({"emp_code": emp_code})

    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, month_num = map(int, month.split("-"))
    start_date = f"{year:04d}-{month_num:02d}-01"

    if month_num == 12:
        next_date = f"{year + 1:04d}-01-01"
    else:
        next_date = f"{year:04d}-{month_num + 1:02d}-01"

    joined_on = employee["joined_on"]
    effective_start = max(start_date, joined_on)

    # Count weekdays from the employee's joining date.
    working_days = 0
    current = datetime.strptime(effective_start, "%Y-%m-%d").date()
    end = datetime.strptime(next_date, "%Y-%m-%d").date()

    while current < end:
        if current.weekday() < 5:
            working_days += 1
        from datetime import timedelta
        current += timedelta(days=1)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lt": next_date}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$lt": [{"$dayOfWeek": {
                                        "$dateFromString": {"dateString": "$date"}
                                    }}, 7]},
                                    {"$gt": [{"$dayOfWeek": {
                                        "$dateFromString": {"dateString": "$date"}
                                    }}, 1]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    summary = result[0] if result else {}

    present_days = summary.get("present_days", 0)
    attendance_pct = (
        float(
            (Decimal(str(present_days)) * Decimal("100") /
             Decimal(working_days)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        if working_days else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct
    }

from datetime import date, timedelta
from fastapi import Query, HTTPException
from decimal import Decimal, ROUND_HALF_UP


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, month_num = map(int, month.split("-"))

    start_date = f"{year:04d}-{month_num:02d}-01"

    if month_num == 12:
        next_date = f"{year + 1:04d}-01-01"
    else:
        next_date = f"{year:04d}-{month_num + 1:02d}-01"

    month_end = (
        date.fromisoformat(next_date) - timedelta(days=1)
    ).isoformat()

    employee_match = {"joined_on": {"$lte": month_end}}

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$code"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lt": ["$date", next_date]},
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_weekday": {
                                "$dayOfWeek": {
                                    "$dateFromString": {
                                        "dateString": "$date",
                                        "timezone": "Asia/Kolkata",
                                    }
                                }
                            }
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$in": ["$logs._weekday", [2, 3, 4, 5, 6]]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "work_hour_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            "$logs.work_hours",
                            None,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$avg": "$work_hour_values"
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))

    # Apply half-up rounding to reported averages.
    for item in results:
        avg = item.get("avg_work_hours")
        item["avg_work_hours"] = (
            float(Decimal(str(avg)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ))
            if avg is not None
            else None
        )

    return {"month": month, "items": results}



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    # Calculate the start and end of the requested month.
    year, month_num = map(int, month.split("-"))
    start_date = f"{year:04d}-{month_num:02d}-01"

    if month_num == 12:
        next_date = f"{year + 1:04d}-01-01"
    else:
        next_date = f"{year:04d}-{month_num + 1:02d}-01"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lt": next_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    # Apply the department filter before ranking.
    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }



from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fastapi import Query, HTTPException


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    # Validate dates and range.
    try:
        start_day = date.fromisoformat(from_date)
        end_day = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(422, "Dates must use YYYY-MM-DD format")

    if start_day.isoformat() != from_date or end_day.isoformat() != to_date:
        raise HTTPException(422, "Dates must use YYYY-MM-DD format")

    if end_day < start_day:
        raise HTTPException(422, "'to' must be on or after 'from'")

    days = (end_day - start_day).days + 1
    if days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Unknown department")

    start_dt = datetime.combine(start_day, datetime.min.time())
    end_exclusive = datetime.combine(
        end_day + timedelta(days=1), datetime.min.time()
    )

    pipeline = [
        # Seed the date series so it also works when no attendance exists.
        {"$documents": [{"day": start_dt}]},
        {
            "$densify": {
                "field": "day",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$day",
                        "timezone": "Asia/Kolkata",
                    }
                },
                "weekday": {"$dayOfWeek": "$day"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day_string": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$day_string"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "employee.department": department
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [
                                            {"$ifNull": ["$late_minutes", 0]},
                                            0,
                                        ]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_string": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_string"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "staff",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$staff.count", 0]},
                        0,
                    ]
                },
                "present_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.present_count", 0]},
                        0,
                    ]
                },
                "late_count": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily.late_count", 0]},
                        0,
                    ]
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$weekday", 2]},
                        {"$lte": ["$weekday", 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    # Database-level aggregation, including gap filling and rolling average.
    rows = list(db.aggregate(pipeline))

    # Apply the contract's half-up rounding to four decimal places.
    def round_half_up(value):
        if value is None:
            return None
        return float(
            Decimal(str(value)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        )

    for row in rows:
        row["attendance_rate"] = round_half_up(row["attendance_rate"])
        row["moving_avg_7d"] = round_half_up(row["moving_avg_7d"])

    return {"department": department, "items": rows}



from fastapi import Path
from bson import json_util
import json


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(
        ...,
        pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$",
    ),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(
        None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"
    ),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def required(*names):
        missing = [name for name in names if not values.get(name)]
        if missing:
            raise HTTPException(
                422, f"Missing required parameter(s): {', '.join(missing)}"
            )

    values = {
        "emp_code": emp_code,
        "month": month,
        "department": department,
        "from": from_date,
        "to": to_date,
    }

    if endpoint == "attendance_list":
        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to
        if status:
            query["status"] = status

        operation = {
            "find": "attendance_logs",
            "filter": query,
            "sort": {"date": -1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = "attendance_logs"
        explain = db.command(
            "explain", operation, verbosity="executionStats"
        )

    elif endpoint == "employee_monthly":
        required("emp_code", "month")
        year, m = map(int, month.split("-"))
        start = f"{year:04d}-{m:02d}-01"
        end = (
            f"{year + 1:04d}-01-01" if m == 12
            else f"{year:04d}-{m + 1:02d}-01"
        )
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": start, "$lt": end},
            }},
            {"$sort": {"date": 1}},
        ]
        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "department_summary":
        required("month")
        year, m = map(int, month.split("-"))
        start = f"{year:04d}-{m:02d}-01"
        end = (
            f"{year + 1:04d}-01-01" if m == 12
            else f"{year:04d}-{m + 1:02d}-01"
        )
        match = {"date": {"$gte": start, "$lt": end}}
        pipeline = [{"$match": match}]
        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    elif endpoint == "late_leaderboard":
        required("month")
        year, m = map(int, month.split("-"))
        start = f"{year:04d}-{m:02d}-01"
        end = (
            f"{year + 1:04d}-01-01" if m == 12
            else f"{year:04d}-{m + 1:02d}-01"
        )
        pipeline = [
            {"$match": {
                "date": {"$gte": start, "$lt": end},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
            }},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
        ]
        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    else:  # department_trend
        required("department", "from", "to")
        try:
            start_day = date.fromisoformat(from_date)
            end_day = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(422, "Dates must use YYYY-MM-DD format")

        if end_day < start_day or (end_day - start_day).days + 1 > 92:
            raise HTTPException(422, "Invalid trend date range")

        # Explain the indexed attendance lookup used by the trend.
        pipeline = [
            {"$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }},
            {"$match": {"employee.department": department}},
        ]
        collection = "attendance_logs"
        explain = db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    # Convert BSON-specific values to standard JSON-compatible values.
    explain = json.loads(json_util.dumps(explain))

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain,
    }




# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
