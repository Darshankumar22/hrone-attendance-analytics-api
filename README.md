# Employee Attendance & Analytics API

FastAPI + MongoDB backend for employee attendance, punch-in/punch-out, attendance regularization, and analytics.

## Requirements
- Python 3.11+
- MongoDB 6.0+
- Dependencies in `requirements.txt`

## Run locally
From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for Swagger UI. Readiness endpoint: `GET /health`.

The application should read `MONGO_URI` and `MONGO_DB` from environment variables and create indexes at startup. Do not commit `.env`, credentials, virtual environments, or database dumps.

## API areas
- Health and employee create/list endpoints
- Attendance punch-in, punch-out, listing, and regularization
- Monthly employee analytics, department summary, late leaderboard, and department trend
- Explain endpoint for the five supported query names

## Before submission
Run from a clean environment and test every endpoint against the supplied OpenAPI contract. Specifically verify concurrency, timestamp validation, overnight shifts, rounding, pagination totals, analytics edge cases, and explain plans on representative data. A successful Swagger response does not by itself prove all requirements pass. Confirm `app/main.py` has no duplicate route registrations and each explain branch uses the same query/pipeline and filters as its corresponding endpoint.
