from fastapi import FastAPI
import asyncpg
import os

app = FastAPI()

DB_SETTINGS = {
    "user": os.getenv("DB_USER", "botuser"),
    "password": os.getenv("DB_PASS", "botpass"),
    "database": os.getenv("DB_NAME", "botdb"),
    "host": os.getenv("DB_HOST", "postgres_bot"),
    "port": int(os.getenv("DB_PORT", 5432)),
}


@app.on_event("startup")
async def startup():
    """Создание таблицы users при старте (если не существует)."""
    conn = await asyncpg.connect(**DB_SETTINGS)
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            tg_id BIGINT UNIQUE,
            username TEXT,
            position TEXT,
            role TEXT,
            created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
        )
        """
    )
    await conn.close()


@app.get("/")
async def root():
    """Возвращает список пользователей из таблицы users."""
    conn = await asyncpg.connect(**DB_SETTINGS)
    rows = await conn.fetch(
        "SELECT tg_id, username, position, role FROM users ORDER BY id DESC"
    )
    await conn.close()
    return {"users": [dict(r) for r in rows]}
