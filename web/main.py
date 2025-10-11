from fastapi import FastAPI
import asyncpg, os

app = FastAPI()

DB_HOST = os.getenv("DB_HOST", "db")
DB_NAME = os.getenv("DB_NAME", "botdb")
DB_USER = os.getenv("DB_USER", "botuser")
DB_PASS = os.getenv("DB_PASS", "botpass")

@app.get("/")
async def root():
    conn = await asyncpg.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME
    )
    rows = await conn.fetch(
        "SELECT tg_id, username, position, role FROM users ORDER BY id DESC"
    )
    await conn.close()
    return {"users": [dict(r) for r in rows]}
