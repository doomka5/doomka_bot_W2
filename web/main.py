import html

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
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
            arrival_date DATE,
            arrival_at TIMESTAMPTZ
        )
        """
    )
    await conn.execute(
        """
        ALTER TABLE warehouse_plastics
        ADD COLUMN IF NOT EXISTS arrival_at TIMESTAMPTZ
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
            color TEXT NOT NULL,
            UNIQUE(material_id, color)
        )
        """
    )
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plastic_storage_locations (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
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


@app.get("/plastics", response_class=HTMLResponse)
async def plastics_page() -> HTMLResponse:
    """Выводит страницу со списком добавленных пластиков."""
    conn = await asyncpg.connect(**DB_SETTINGS)
    records = await conn.fetch(
        """
        SELECT
            article,
            material,
            thickness,
            color,
            length,
            width,
            warehouse,
            comment,
            employee_name,
            arrival_at
        FROM warehouse_plastics
        ORDER BY arrival_at DESC NULLS LAST, id DESC
        """
    )
    await conn.close()

    def _format_decimal(value):
        if value is None:
            return "—"
        as_str = format(value, "f").rstrip("0").rstrip(".")
        return as_str or "0"

    if records:
        rows_html = []
        for record in records:
            arrival_at = record["arrival_at"]
            if arrival_at:
                arrival_tz = (
                    arrival_at.astimezone()
                    if getattr(arrival_at, "tzinfo", None)
                    else arrival_at
                )
                arrival_formatted = arrival_tz.strftime("%Y-%m-%d %H:%M")
            else:
                arrival_formatted = "—"
            comment = record["comment"] or "—"
            rows_html.append(
                "<tr>"
                f"<td>{html.escape(record['article'] or '—')}</td>"
                f"<td>{html.escape(record['material'] or '—')}</td>"
                f"<td>{_format_decimal(record['thickness'])}</td>"
                f"<td>{html.escape(record['color'] or '—')}</td>"
                f"<td>{_format_decimal(record['length'])}</td>"
                f"<td>{_format_decimal(record['width'])}</td>"
                f"<td>{html.escape(record['warehouse'] or '—')}</td>"
                f"<td>{html.escape(comment)}</td>"
                f"<td>{html.escape(record['employee_name'] or '—')}</td>"
                f"<td>{arrival_formatted}</td>"
                "</tr>"
            )
        table_body = "".join(rows_html)
    else:
        table_body = (
            "<tr><td colspan=\"10\" style=\"text-align:center;\">"
            "Нет добавленных записей"
            "</td></tr>"
        )

    html = f"""
    <html>
        <head>
            <meta charset=\"utf-8\" />
            <title>Склад пластика</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 2rem; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ border: 1px solid #ccc; padding: 0.5rem; }}
                th {{ background-color: #f5f5f5; text-align: left; }}
            </style>
        </head>
        <body>
            <h1>Список добавленного пластика</h1>
            <table>
                <thead>
                    <tr>
                        <th>Артикул</th>
                        <th>Материал</th>
                        <th>Толщина</th>
                        <th>Цвет</th>
                        <th>Длина, мм</th>
                        <th>Ширина, мм</th>
                        <th>Склад</th>
                        <th>Комментарий</th>
                        <th>Сотрудник</th>
                        <th>Дата поступления</th>
                    </tr>
                </thead>
                <tbody>
                    {table_body}
                </tbody>
            </table>
        </body>
    </html>
    """
    return HTMLResponse(content=html)
