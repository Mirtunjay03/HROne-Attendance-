"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience; real env vars win)
"""
import os
import re
from datetime import datetime, timedelta, timezone, date as date_type
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()  # local .env for dev; real env-vars override via os.environ

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.environ.get("MONGO_DB",  "attendance_db")

client = MongoClient(MONGO_URI)
db     = client[MONGO_DB]

IST_OFFSET = timedelta(hours=5, minutes=30)
IST        = timezone(IST_OFFSET)

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# Serve the frontend
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/static/index.html")


# ---------------------------------------------------------------------------
# Startup: create indexes idempotently
# ---------------------------------------------------------------------------
@app.on_event("startup")
def create_indexes() -> None:
    """Create all required indexes idempotently.

    We use explicit index names so that if the database already has an index on
    the same keys (possibly created by sample_seed.py under a different name) we
    skip gracefully instead of raising IndexOptionsConflict.  The pattern is:
    attempt create_index; if it raises because the keys already exist under a
    different name, the index is already there and we're fine.
    """
    from pymongo.errors import OperationFailure

    def safe_create(collection, keys, **kwargs):
        try:
            collection.create_index(keys, **kwargs)
        except OperationFailure as e:
            # code 85 = IndexOptionsConflict: same keys, different name — index exists, fine.
            if e.code == 85:
                pass
            else:
                raise

    # employees
    safe_create(db.employees,
                [("emp_code", ASCENDING)],
                unique=True, name="app_uq_emp_code")
    safe_create(db.employees,
                [("department", ASCENDING)],
                name="app_idx_emp_dept")
    safe_create(db.employees,
                [("joined_on", ASCENDING)],
                name="app_idx_emp_joined")

    # attendance_logs — primary lookup key (unique)
    safe_create(db.attendance_logs,
                [("emp_code", ASCENDING), ("date", ASCENDING)],
                unique=True, name="app_uq_att_emp_date")

    # list endpoint: date desc + emp_code asc  (covers sort + range filter)
    safe_create(db.attendance_logs,
                [("date", DESCENDING), ("emp_code", ASCENDING)],
                name="app_idx_att_date_desc_emp")

    # per-employee queries with descending date
    safe_create(db.attendance_logs,
                [("emp_code", ASCENDING), ("date", DESCENDING)],
                name="app_idx_att_emp_date_desc")

    # analytics: date range + forward scan
    safe_create(db.attendance_logs,
                [("date", ASCENDING), ("emp_code", ASCENDING)],
                name="app_idx_att_date_asc_emp")


# ---------------------------------------------------------------------------
# Epoch-millisecond helpers
# ---------------------------------------------------------------------------
MIN_MS = 100_000_000_000   # ~1973 — anything below looks like seconds
MAX_MS = 4_102_444_800_000  # 2100-01-01


def validate_epoch_ms(v: Any, field_name: str = "punched_at") -> int:
    """Raise 422 if v does not look like a valid epoch-millisecond integer."""
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError(f"{field_name} must be an integer epoch-millisecond value")
    if v < MIN_MS or v > MAX_MS:
        raise ValueError(
            f"{field_name} looks like seconds or is out of range; supply epoch milliseconds"
        )
    return v


def ms_to_dt(ms: int) -> datetime:
    """Epoch-ms → UTC datetime, truncated to whole seconds."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """Datetime → epoch-ms (works for both aware and naive-UTC datetimes)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # PyMongo returns naive UTC datetimes — treat them as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Business-rule helpers
# ---------------------------------------------------------------------------
def half_up(value: float, decimals: int = 2) -> float:
    """Round a float using ROUND_HALF_UP (not banker's rounding)."""
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def attendance_date_for_punch(punch_dt: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Return the IST calendar date string (YYYY-MM-DD) for a punch-in.

    Overnight shift (shift_end <= shift_start):
      If the IST time of the punch is *strictly before* shift_end,
      the record belongs to the shift that started the previous calendar day.
    """
    ist_dt   = punch_dt.astimezone(IST)
    h_end, m_end = map(int, shift_end.split(":"))
    h_st,  m_st  = map(int, shift_start.split(":"))

    is_overnight = (h_end * 60 + m_end) <= (h_st * 60 + m_st)

    if is_overnight:
        punch_time_minutes = ist_dt.hour * 60 + ist_dt.minute
        end_minutes        = h_end * 60 + m_end
        if punch_time_minutes < end_minutes:
            # Punch is in the early hours — belongs to the *previous* day's shift
            return (ist_dt.date() - timedelta(days=1)).isoformat()

    return ist_dt.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, shift_start: str) -> int:
    """
    R2: Grace period 10 min. Late only if strictly > 10:00 after shift_start.
    late_minutes = floor minutes since shift_start (not since grace period).
    All arithmetic in IST.
    """
    ist = punch_in_dt.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    # Build shift-start on the same IST calendar date
    shift_start_dt = ist.replace(hour=h, minute=m, second=0, microsecond=0)
    elapsed_secs   = int((ist - shift_start_dt).total_seconds())
    if elapsed_secs <= 600:          # <= 10:00 → on time (or early)
        return 0
    return elapsed_secs // 60        # floor minutes since shift_start


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals."""
    diff_secs = (punch_out_dt - punch_in_dt).total_seconds()
    return half_up(diff_secs / 3600, 2)


def compute_overtime(punch_out_dt: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    """
    R3: Floor minutes from shift_end to punch_out; 0 if < 30 minutes.
    For overnight shifts shift_end is on the *next* calendar day.
    """
    h, m = map(int, shift_end.split(":"))
    h_st, m_st = map(int, shift_start.split(":"))

    is_overnight = (h * 60 + m) <= (h_st * 60 + m_st)

    base_date = datetime.fromisoformat(attendance_date)   # naive
    if is_overnight:
        # shift_end is next calendar day
        end_naive = base_date.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=1)
    else:
        end_naive = base_date.replace(hour=h, minute=m, second=0, microsecond=0)

    # Convert to IST-aware
    shift_end_dt = end_naive.replace(tzinfo=IST)

    # punch_out_dt may be UTC-aware or naive-UTC
    if punch_out_dt.tzinfo is None:
        punch_out_dt = punch_out_dt.replace(tzinfo=timezone.utc)

    diff_secs = (punch_out_dt - shift_end_dt).total_seconds()
    if diff_secs < 30 * 60:   # < 30 minutes
        return 0
    return int(diff_secs // 60)


def compute_derived(punch_in_dt: datetime, punch_out_dt: Optional[datetime],
                    shift_start: str, shift_end: str, attendance_date: str) -> dict:
    """Compute all four derived fields from final punch times."""
    late_minutes     = compute_late_minutes(punch_in_dt, shift_start)
    work_hours       = None
    overtime_minutes = 0
    half_day         = False

    if punch_out_dt is not None:
        work_hours       = compute_work_hours(punch_in_dt, punch_out_dt)
        overtime_minutes = compute_overtime(punch_out_dt, shift_end, attendance_date, shift_start)
        half_day         = work_hours < 4.50

    return {
        "late_minutes":     late_minutes,
        "work_hours":       work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day":         half_day,
    }


# ---------------------------------------------------------------------------
# Response serialisation helpers
# ---------------------------------------------------------------------------
def fmt_employee(doc: dict) -> dict:
    """Strip _id and convert created_at datetime → epoch-ms."""
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


def fmt_attendance(doc: dict) -> dict:
    """Strip _id, convert BSON dates → epoch-ms, normalise missing fields."""
    doc.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        if isinstance(doc.get(field), datetime):
            doc[field] = dt_to_ms(doc[field])

    # Normalise optional fields that may be absent in seeded data
    doc.setdefault("late_minutes",     0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day",         False)
    doc.setdefault("history",          [])

    # Convert BSON dates inside history entries
    for entry in doc.get("history", []):
        if isinstance(entry.get("at"), datetime):
            entry["at"] = dt_to_ms(entry["at"])
        for _field, change in entry.get("changes", {}).items():
            if isinstance(change.get("from"), datetime):
                change["from"] = dt_to_ms(change["from"])
            if isinstance(change.get("to"), datetime):
                change["to"] = dt_to_ms(change["to"])

    return doc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
_EMP_CODE_RE   = re.compile(r"^EMP\d{4,6}$")
_SHIFT_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MONTH_RE      = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_RE       = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class EmployeeCreate(BaseModel):
    emp_code:    str = Field(..., pattern=r"^EMP\d{4,6}$")
    name:        str = Field(..., min_length=1, max_length=100)
    email:       str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department:  str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end:   str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on:   str  # validated below

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def shift_times_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v, "punched_at")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH or ON_DUTY for punch-in")
        return v


class PunchOutRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        return validate_epoch_ms(v, "punched_at")


class RegularizeRequest(BaseModel):
    status:          Optional[str] = None
    punch_in:        Optional[int] = None
    punch_out:       Optional[int] = None
    reason:          str = Field(..., min_length=5, max_length=200)
    regularized_by:  str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_times(cls, v, info):
        if v is None:
            return v
        return validate_epoch_ms(v, info.field_name)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            raise ValueError("invalid status")
        return v


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


# ---------------------------------------------------------------------------
# /employees
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(tz=timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return fmt_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(1,  ge=1),
    page_size:  int = Query(20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip  = (page - 1) * page_size
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        fmt_employee(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# /attendance/punch-in
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # R1: truncate to whole seconds, then determine attendance date
    raw_ms   = body.punched_at if body.punched_at is not None else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    truncated_ms = (raw_ms // 1000) * 1000
    punch_dt = ms_to_dt(truncated_ms)

    att_date = attendance_date_for_punch(punch_dt, emp["shift_start"], emp["shift_end"])

    late_min = compute_late_minutes(punch_dt, emp["shift_start"])

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         punch_dt,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late_min,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return fmt_attendance(doc)


# ---------------------------------------------------------------------------
# /attendance/punch-out
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    raw_ms       = body.punched_at if body.punched_at is not None else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    truncated_ms = (raw_ms // 1000) * 1000
    punch_out_dt = ms_to_dt(truncated_ms)

    # Find the most recent record whose punch_in <= punched_at and punch_out is null
    record = db.attendance_logs.find_one(
        {
            "emp_code":  body.emp_code,
            "punch_in":  {"$lte": punch_out_dt},
            "punch_out": None,
            "status":    {"$in": ["PRESENT", "WFH", "ON_DUTY"]},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        # Check if there's a record that's already punched out
        already_out = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": punch_out_dt},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if already_out:
            raise HTTPException(409, "record is already punched out")
        raise HTTPException(404, "no open punch-in record found")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    # 422: punch_out must be after punch_in
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(422, "punched_at must be after punch_in")

    # 422: must be within 24 hours
    if (punch_out_dt - punch_in_dt).total_seconds() > 86400:
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    derived = compute_derived(
        punch_in_dt, punch_out_dt,
        emp["shift_start"], emp["shift_end"],
        record["date"]
    )

    # Concurrency-safe: use the known punch_out=null as the filter condition
    result = db.attendance_logs.find_one_and_update(
        {
            "emp_code":  body.emp_code,
            "date":      record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out":        punch_out_dt,
                "work_hours":       derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day":         derived["half_day"],
            }
        },
        return_document=True,
    )

    if result is None:
        raise HTTPException(409, "record is already punched out")

    return fmt_attendance(result)


# ---------------------------------------------------------------------------
# /attendance  (list)
# ---------------------------------------------------------------------------
@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(1,  ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # Validate dates
    for d, name in ((date_from, "date_from"), (date_to, "date_to")):
        if d and not _DATE_RE.match(d):
            raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(422, "invalid status")
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip  = (page - 1) * page_size
    items = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    for item in items:
        fmt_attendance(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# /attendance/{emp_code}/{date}  (PATCH regularize)
# ---------------------------------------------------------------------------
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    # Validate path date
    if not _DATE_RE.match(date):
        raise HTTPException(422, "date must be YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "no attendance record for that date")

    # ---- validate request ----
    new_status    = body.status
    new_punch_in  = ms_to_dt(body.punch_in)  if body.punch_in  else None
    new_punch_out = ms_to_dt(body.punch_out) if body.punch_out else None

    # ABSENT/LEAVE: cannot also supply punch times
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE records cannot have punch times")

    # Resolve final values
    final_status = new_status if new_status is not None else record["status"]

    # For presence statuses, punch_in is required (supplied or already stored)
    cur_punch_in = record.get("punch_in")
    if cur_punch_in is not None and cur_punch_in.tzinfo is None:
        cur_punch_in = cur_punch_in.replace(tzinfo=timezone.utc)

    cur_punch_out = record.get("punch_out")
    if cur_punch_out is not None and cur_punch_out.tzinfo is None:
        cur_punch_out = cur_punch_out.replace(tzinfo=timezone.utc)

    if final_status in ("ABSENT", "LEAVE"):
        final_punch_in  = None
        final_punch_out = None
    else:
        # presence status
        final_punch_in  = new_punch_in  if new_punch_in  is not None else cur_punch_in
        final_punch_out = new_punch_out if new_punch_out is not None else cur_punch_out

        if final_punch_in is None:
            raise HTTPException(422, "a presence status requires punch_in")

        # punch_in must stay on the record's attendance date (R1)
        pi_date = attendance_date_for_punch(final_punch_in, emp["shift_start"], emp["shift_end"])
        if pi_date != date:
            raise HTTPException(422, "punch_in date does not match the record's attendance date (R1)")

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (final_punch_out - final_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Compute derived fields for the new state
    if final_status in ("ABSENT", "LEAVE"):
        derived = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    else:
        derived = compute_derived(
            final_punch_in, final_punch_out,
            emp["shift_start"], emp["shift_end"], date
        )

    # Build $set payload and detect changes
    def _cur_ms(field):
        v = record.get(field)
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return dt_to_ms(v)
        return v  # None

    cur_vals = {
        "status":           record.get("status"),
        "punch_in":         _cur_ms("punch_in"),
        "punch_out":        _cur_ms("punch_out"),
        "work_hours":       record.get("work_hours"),
        "late_minutes":     record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day":         record.get("half_day", False),
    }

    new_vals = {
        "status":           final_status,
        "punch_in":         dt_to_ms(final_punch_in),
        "punch_out":        dt_to_ms(final_punch_out),
        "work_hours":       derived["work_hours"],
        "late_minutes":     derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day":         derived["half_day"],
    }

    changes: dict = {}
    for field in ("status", "punch_in", "punch_out", "work_hours",
                  "late_minutes", "overtime_minutes", "half_day"):
        if cur_vals[field] != new_vals[field]:
            changes[field] = {"from": cur_vals[field], "to": new_vals[field]}

    if not changes:
        raise HTTPException(422, "request changes nothing")

    # Build the $set document with proper datetime values for Mongo storage
    set_doc: dict = {
        "status":           final_status,
        "punch_in":         final_punch_in,
        "punch_out":        final_punch_out,
        "work_hours":       derived["work_hours"],
        "late_minutes":     derived["late_minutes"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day":         derived["half_day"],
    }

    # Convert epoch-ms changes back to BSON dates for history storage
    history_changes: dict = {}
    for field, chg in changes.items():
        if field in ("punch_in", "punch_out"):
            history_changes[field] = {
                "from": ms_to_dt(chg["from"]) if chg["from"] is not None else None,
                "to":   ms_to_dt(chg["to"])   if chg["to"]   is not None else None,
            }
        else:
            history_changes[field] = chg

    history_entry = {
        "at":      datetime.now(tz=timezone.utc),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": history_changes,
    }

    # Concurrency-safe: optimistic version via array length check
    current_history_len = len(record.get("history") or [])

    result = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date":     date,
            # Only succeed if history hasn't grown (concurrent edit guard)
            f"history.{current_history_len}": {"$exists": False},
        },
        {
            "$set":  set_doc,
            "$push": {"history": history_entry},
        },
        return_document=True,
    )

    if result is None:
        raise HTTPException(409, "concurrent edit detected; please retry")

    return fmt_attendance(result)


# ---------------------------------------------------------------------------
# /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    year, mon = map(int, month.split("-"))
    import calendar as _cal
    last_dom    = _cal.monthrange(year, mon)[1]
    month_start = f"{year:04d}-{mon:02d}-01"
    last_day    = f"{year:04d}-{mon:02d}-{last_dom:02d}"
    if mon == 12:
        month_end = f"{year+1:04d}-01-01"
    else:
        month_end = f"{year:04d}-{mon+1:02d}-01"

    # R7 working days: Mon-Fri from max(joined_on, month_start) to last_day
    joined_on  = emp["joined_on"]
    count_from = max(joined_on, month_start)

    working_days = _count_working_days(count_from, last_day)

    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date":     {"$gte": month_start, "$lt": month_end},
        }},
        {"$group": {
            "_id": None,
            # present_days: R5/R6/R7 — count Mon-Fri presence records; half_day=0.5
            # We accumulate per-record and filter weekday in $group using $dayOfWeek
            # Instead use $sum with conditional
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2,3,4,5,6]]},
                    ]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                    0
                ]
            }},
            "leave_days":          {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count":          {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes":  {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days           = r["present_days"]
        leave_days             = int(r["leave_days"])
        late_count             = int(r["late_count"])
        total_late_minutes     = int(r["total_late_minutes"])
        total_overtime_minutes = int(r["total_overtime_minutes"])
    else:
        present_days = leave_days = late_count = total_late_minutes = total_overtime_minutes = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = half_up((present_days / working_days) * 100, 2)

    # Render present_days: keep as float if it has a .5 component, else as int
    present_days_out = half_up(float(present_days), 2)
    if present_days_out == int(present_days_out):
        present_days_out = int(present_days_out)

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days_out,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":         attendance_pct,
    }


def _count_working_days(from_date: str, to_date: str) -> int:
    """Count Mon-Fri days between from_date and to_date inclusive."""
    d1 = datetime.strptime(from_date, "%Y-%m-%d").date()
    d2 = datetime.strptime(to_date,   "%Y-%m-%d").date()
    if d1 > d2:
        return 0
    count = 0
    cur = d1
    while cur <= d2:
        if cur.weekday() < 5:  # Mon=0 … Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# /analytics/departments/summary
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/summary")
def department_summary(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    import calendar as _cal
    year, mon = map(int, month.split("-"))
    month_start = f"{year:04d}-{mon:02d}-01"
    last_dom    = _cal.monthrange(year, mon)[1]
    last_day    = f"{year:04d}-{mon:02d}-{last_dom:02d}"
    if mon == 12:
        month_end = f"{year+1:04d}-01-01"
    else:
        month_end = f"{year:04d}-{mon+1:02d}-01"

    # ------------------------------------------------------------------
    # Step 1: headcount per department (R9) — employees joined_on <= last_day
    # ------------------------------------------------------------------
    emp_match: dict = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    headcount_pipeline = [
        {"$match": emp_match},
        {"$group": {
            "_id":       "$department",
            "headcount": {"$sum": 1},
            "emp_codes": {"$push": "$emp_code"},
        }},
    ]
    headcount_by_dept = {
        row["_id"]: {"headcount": row["headcount"], "emp_codes": row["emp_codes"]}
        for row in db.employees.aggregate(headcount_pipeline)
    }

    if not headcount_by_dept:
        return {"month": month, "items": []}

    # ------------------------------------------------------------------
    # Step 2: aggregate logs for those employees in that month
    # ------------------------------------------------------------------
    all_emp_codes = [ec for dept in headcount_by_dept.values() for ec in dept["emp_codes"]]

    log_pipeline = [
        {"$match": {
            "emp_code": {"$in": all_emp_codes},
            "date":     {"$gte": month_start, "$lt": month_end},
        }},
        # Join employee to get department
        {"$lookup": {
            "from":         "employees",
            "localField":   "emp_code",
            "foreignField": "emp_code",
            "as":           "emp_info",
        }},
        {"$unwind": "$emp_info"},
        {"$group": {
            "_id": "$emp_info.department",
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2,3,4,5,6]]},
                    ]},
                    {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                    0
                ]
            }},
            # avg_work_hours: over records with presence status AND work_hours not null
            "wh_sum":   {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$work_hours", None]},
                    ]},
                    "$work_hours",
                    0
                ]
            }},
            "wh_count": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne": ["$work_hours", None]},
                    ]},
                    1, 0
                ]
            }},
            "late_count":          {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes":  {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count":         {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count":       {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]

    log_stats = {row["_id"]: row for row in db.attendance_logs.aggregate(log_pipeline)}

    # ------------------------------------------------------------------
    # Step 3: merge and build response
    # ------------------------------------------------------------------
    items = []
    for dept, hc_info in sorted(headcount_by_dept.items()):
        stats = log_stats.get(dept, {})
        wh_count = stats.get("wh_count", 0)
        wh_sum   = stats.get("wh_sum",   0.0)
        avg_wh   = half_up(wh_sum / wh_count, 2) if wh_count > 0 else None

        pd = stats.get("present_days", 0)
        present_days_val = half_up(float(pd), 2)
        if present_days_val == int(present_days_val):
            present_days_val = int(present_days_val)

        items.append({
            "department":          dept,
            "headcount":           hc_info["headcount"],
            "present_days":        present_days_val,
            "avg_work_hours":      avg_wh,
            "late_count":          int(stats.get("late_count", 0)),
            "total_late_minutes":  int(stats.get("total_late_minutes", 0)),
            "leave_count":         int(stats.get("leave_count", 0)),
            "on_duty_count":       int(stats.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# /analytics/leaderboard/late
# ---------------------------------------------------------------------------
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month:      str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    import calendar as _cal
    year, mon = map(int, month.split("-"))
    month_start = f"{year:04d}-{mon:02d}-01"
    if mon == 12:
        month_end = f"{year+1:04d}-01-01"
    else:
        month_end = f"{year:04d}-{mon+1:02d}-01"

    pipeline: list = [
        {"$match": {
            "date": {"$gte": month_start, "$lt": month_end},
        }},
        # Group per employee
        {"$group": {
            "_id":                "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        }},
        # Only employees with total > 0
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # Join employee info
        {"$lookup": {
            "from":         "employees",
            "localField":   "_id",
            "foreignField": "emp_code",
            "as":           "emp_info",
        }},
        # Drop logs whose employee doesn't exist
        {"$match": {"emp_info": {"$ne": []}}},
        {"$unwind": "$emp_info"},
    ]

    if department:
        pipeline.append({"$match": {"emp_info.department": department}})

    pipeline += [
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        # Standard competition ranking (1,2,2,4) using $setWindowFields
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {
                    "$rank": {}
                }
            }
        }},
        # Apply limit: keep rows where rank <= limit
        {"$match": {"rank": {"$lte": limit}}},
        # Final sort: total_late_minutes desc, emp_code asc
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "_id":                0,
            "emp_code":           "$_id",
            "name":               "$emp_info.name",
            "department":         "$emp_info.department",
            "total_late_minutes": 1,
            "rank":               1,
        }},
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------
@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date:  str = Query(..., alias="from"),
    to_date:    str = Query(..., alias="to"),
):
    # Validate dates
    for d, name in ((from_date, "from"), (to_date, "to")):
        if not _DATE_RE.match(d):
            raise HTTPException(422, f"{name} must be YYYY-MM-DD")

    if to_date < from_date:
        raise HTTPException(422, "to must not be before from")

    d1 = datetime.strptime(from_date, "%Y-%m-%d").date()
    d2 = datetime.strptime(to_date,   "%Y-%m-%d").date()
    if (d2 - d1).days > 91:   # > 92 days means >= 93 days span (0-indexed: day 0 to day 92 = 93 days)
        raise HTTPException(422, "range must not exceed 92 days")

    # Check department exists
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "department not found")

    # Use $densify for gap-filling in the pipeline
    # MongoDB $densify works on numeric/date fields; we use dates as strings
    # Approach: generate all dates in Python (range is ≤ 92 days, so ≤ 93 dates — trivial)

    # Build per-day headcount lookup (R9): employees joined_on <= that day
    # We'll embed this as a $lookup on employees per day using a pipeline lookup

    # The main aggregation:
    # 1. Match logs for the department employees in range
    # 2. Densify to fill missing days
    # 3. Compute daily metrics
    # 4. Moving average in a $setWindowFields stage

    # Since $densify on string dates is awkward, we generate the date spine in Python
    # and use a $facet / union approach — but the problem says "work done in the database".
    # We'll use a $documents stage (MongoDB 6.0+) to inject the date spine.

    # Build date spine
    dates = []
    cur = d1
    while cur <= d2:
        dates.append(cur.isoformat())
        cur += timedelta(days=1)

    # Aggregate: $documents must run as a database-level aggregate (MongoDB 6.0+)
    # db.aggregate() accepts pipelines starting with $documents.
    pipeline = [
        # Inject the date spine as a virtual collection
        {"$documents": [{"date": d} for d in dates]},

        # For each date, look up attendance logs for department employees
        {"$lookup": {
            "from": "attendance_logs",
            "let":  {"d": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
                # Join employee to filter by department
                {"$lookup": {
                    "from":         "employees",
                    "localField":   "emp_code",
                    "foreignField": "emp_code",
                    "as":           "emp_info",
                }},
                {"$match": {"emp_info": {"$ne": []}}},
                {"$unwind": "$emp_info"},
                {"$match": {"emp_info.department": department}},
            ],
            "as": "logs",
        }},

        # Compute headcount for this date: department employees joined_on <= date (R9)
        {"$lookup": {
            "from": "employees",
            "let":  {"d": "$date"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq":  ["$department", department]},
                        {"$lte": ["$joined_on",  "$$d"]},
                    ]
                }}},
            ],
            "as": "dept_emps",
        }},

        # Project daily metrics
        {"$addFields": {
            "headcount": {"$size": "$dept_emps"},
            "is_working_day": {"$in": [
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                [2, 3, 4, 5, 6]   # Mon=2 … Fri=6 in MongoDB $dayOfWeek
            ]},
            "present_count": {"$sum": {
                "$map": {
                    "input": "$logs",
                    "as":    "log",
                    "in": {"$cond": [
                        {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$eq": ["$$log.half_day", True]}, 0.5, 1.0]},
                        0
                    ]}
                }
            }},
            "late_count": {"$size": {
                "$filter": {
                    "input": "$logs",
                    "as":    "log",
                    "cond":  {"$gt": ["$$log.late_minutes", 0]}
                }
            }},
        }},

        # attendance_rate: only on working days with headcount > 0; 0.0 when working day but no records
        {"$addFields": {
            "attendance_rate": {"$cond": {
                "if":   {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                "then": {"$divide": ["$present_count", "$headcount"]},
                "else": None,
            }}
        }},

        # 7-day moving average over non-null attendance_rate values in the window
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d_raw": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }
            }
        }},

        {"$project": {
            "_id":            0,
            "date":           1,
            "is_working_day": 1,
            "headcount":      1,
            "present_count":  1,
            "late_count":     1,
            "attendance_rate": 1,
            "moving_avg_7d":  "$moving_avg_7d_raw",
        }},

        {"$sort": {"date": 1}},
    ]

    # $documents requires a database-level aggregate, not a collection-level one
    rows = list(db.aggregate(pipeline))

    # Apply half-up rounding to rates (4 decimals) in Python
    for row in rows:
        if row.get("attendance_rate") is not None:
            row["attendance_rate"] = half_up(row["attendance_rate"], 4)
        if row.get("moving_avg_7d") is not None:
            row["moving_avg_7d"] = half_up(row["moving_avg_7d"], 4)
        # present_count may be 0.5 increments
        pc = row.get("present_count", 0)
        row["present_count"] = half_up(pc, 2) if pc != int(pc) else int(pc)

    return {"department": department, "from": from_date, "to": to_date, "items": rows}


# ---------------------------------------------------------------------------
# /admin/explain/{endpoint}
# ---------------------------------------------------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int = Query(10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_date:  Optional[str] = Query(None, alias="from"),
    to_date:    Optional[str] = Query(None, alias="to"),
    page:       int = Query(1,  ge=1),
    page_size:  int = Query(20, ge=1, le=100),
):
    import calendar as _cal

    valid = {"attendance_list", "employee_monthly", "department_summary",
             "late_leaderboard", "department_trend"}
    if endpoint not in valid:
        raise HTTPException(422, f"endpoint must be one of {sorted(valid)}")

    # ------- attendance_list -------
    if endpoint == "attendance_list":
        q: dict = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to:   q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        explain_doc = db.attendance_logs.find(q).sort(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        ).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _clean_explain(explain_doc)}

    # ------- employee_monthly -------
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        year, mon = map(int, month.split("-"))
        month_start = f"{year:04d}-{mon:02d}-01"
        if mon == 12:
            month_end = f"{year+1:04d}-01-01"
        else:
            month_end = f"{year:04d}-{mon+1:02d}-01"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lt": month_end}}},
            {"$group": {"_id": None, "total": {"$sum": 1}}},
        ]
        explain_doc = db.command(
            "aggregate", "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _clean_explain(explain_doc)}

    # ------- department_summary -------
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        year, mon = map(int, month.split("-"))
        month_start = f"{year:04d}-{mon:02d}-01"
        last_dom    = _cal.monthrange(year, mon)[1]
        last_day    = f"{year:04d}-{mon:02d}-{last_dom:02d}"
        if mon == 12:
            month_end = f"{year+1:04d}-01-01"
        else:
            month_end = f"{year:04d}-{mon+1:02d}-01"
        emp_match: dict = {"joined_on": {"$lte": last_day}}
        if department:
            emp_match["department"] = department
        explain_doc = db.employees.find(emp_match).explain()
        return {"endpoint": endpoint, "collection": "employees", "explain": _clean_explain(explain_doc)}

    # ------- late_leaderboard -------
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        year, mon = map(int, month.split("-"))
        month_start = f"{year:04d}-{mon:02d}-01"
        if mon == 12:
            month_end = f"{year+1:04d}-01-01"
        else:
            month_end = f"{year:04d}-{mon+1:02d}-01"
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lt": month_end}}},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}},
            {"$match": {"total_late_minutes": {"$gt": 0}}},
        ]
        explain_doc = db.command(
            "aggregate", "attendance_logs",
            pipeline=pipeline,
            explain=True,
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _clean_explain(explain_doc)}

    # ------- department_trend -------
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        # Explain the logs lookup by date
        q = {"date": {"$gte": from_date, "$lte": to_date}}
        explain_doc = db.attendance_logs.find(q).sort("date", ASCENDING).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": _clean_explain(explain_doc)}


def _clean_explain(raw) -> dict:
    """Convert the explain result to a JSON-safe dict."""
    import bson
    if isinstance(raw, dict):
        result = {}
        for k, v in raw.items():
            result[k] = _clean_explain(v)
        return result
    elif isinstance(raw, list):
        return [_clean_explain(i) for i in raw]
    elif isinstance(raw, bson.ObjectId):
        return str(raw)
    elif isinstance(raw, bytes):
        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.hex()
    elif isinstance(raw, datetime):
        return raw.isoformat()
    elif isinstance(raw, float) and (raw != raw):  # NaN
        return None
    else:
        return raw
