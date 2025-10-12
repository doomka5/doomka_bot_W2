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
    """Создание необходимых таблиц (если не существуют)."""
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
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS warehouse_plastics (
            id SERIAL PRIMARY KEY,
            article TEXT NOT NULL,
            material TEXT,
            thickness NUMERIC(10, 2),
            color TEXT,
            length NUMERIC(10, 2),
            width NUMERIC(10, 2),
            warehouse TEXT,
            comment TEXT,
            employee_id BIGINT,
            employee_name TEXT,
            arrival_date DATE
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plastic_material_types (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plastic_material_thicknesses (
            id SERIAL PRIMARY KEY,
            material_id INTEGER NOT NULL REFERENCES plastic_material_types(id) ON DELETE CASCADE,
            thickness NUMERIC(10, 2) NOT NULL,
            UNIQUE(material_id, thickness)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plastic_material_colors (
            id SERIAL PRIMARY KEY,
            material_id INTEGER NOT NULL REFERENCES plastic_material_types(id) ON DELETE CASCADE,
            color TEXT NOT NULL
        )
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS plastic_material_colors_material_id_color_idx
        ON plastic_material_colors (material_id, LOWER(color))
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
