# Employee Attendance & Analytics API

FastAPI + PyMongo backend for the HROne engineering assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local or Atlas)

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in the repository root (real environment variables override it):

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

## Run

```bash
uvicorn app.main:app --port 8000
```

The app is ready when `GET /health` returns `{"status": "ok"}`.

## Load sample data (optional)

```bash
python sample_seed.py
```

## Interactive docs

Once running: http://localhost:8000/docs

## Notes

- All indexes are created automatically at startup (idempotent).
- `MONGO_URI` and `MONGO_DB` are read from environment variables; nothing is hard-coded.
- The department trend endpoint uses MongoDB `$documents` (requires MongoDB 6.0+).
- All instants in requests and responses are **epoch milliseconds** (integers). Values that look like seconds are rejected with 422.
