import os
from datetime import date
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="台股選股系統")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise HTTPException(status_code=500, detail="Supabase not configured")
    return create_client(url, key)


@app.get("/api/results")
async def get_results(date: str = None):
    sb = get_supabase()

    if not date:
        latest = sb.table("screening_results") \
            .select("date") \
            .order("date", desc=True) \
            .limit(1) \
            .execute()
        date = latest.data[0]["date"] if latest.data else None

    if not date:
        return {"date": None, "count": 0, "results": []}

    result = sb.table("screening_results") \
        .select("*") \
        .eq("date", date) \
        .order("relative_volume", desc=True) \
        .execute()

    return {"date": date, "count": len(result.data), "results": result.data}


@app.get("/api/dates")
async def get_dates():
    sb = get_supabase()
    result = sb.table("screening_results") \
        .select("date") \
        .order("date", desc=True) \
        .execute()
    dates = list(dict.fromkeys(r["date"] for r in result.data))
    return {"dates": dates[:60]}
