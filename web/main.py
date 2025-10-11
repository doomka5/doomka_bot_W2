from fastapi import FastAPI
import asyncpg, os

app = FastAPI()
DB_SETTINGS = {
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASS"),
    "database": os.getenv("DB_NAME"),
    "host": os.getenv("DB_HOST"),
    "port": os.getenv("DB_PORT", 5432),
}

@app.on_event("startup")
async def startup():
    conn = await asyncpg.connect(**DB_SETTINGS)
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            tg_id BIGINT UNIQUE,
            name TEXT
        )
    ''')
    await conn.close()

@app.get("/")
async def root():
    conn = await asyncpg.connect(**DB_SETTINGS)
    rows = await conn.fetch("SELECT tg_id, name FROM users ORDER BY id DESC")
    await conn.close()
    return {"users": [dict(r) for r in rows]}
