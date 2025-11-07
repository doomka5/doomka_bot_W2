from __future__ import annotations

import os
from datetime import date, datetime, time
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import asyncpg
from fastapi import (Depends, FastAPI, File, Form, HTTPException, Request,
                     Response, UploadFile, status)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import Workbook, load_workbook

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Doomka W2 — CRM склад")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _format_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return format(normalized, "f").rstrip("0").rstrip(".") if "." in format(normalized, "f") else format(normalized, "f")
    return str(value)


templates.env.filters["format_value"] = _format_value

DB_SETTINGS = {
    "user": os.getenv("DB_USER", "botuser"),
    "password": os.getenv("DB_PASS", "botpass"),
    "database": os.getenv("DB_NAME", "botdb"),
    "host": os.getenv("DB_HOST", "postgres_bot"),
    "port": int(os.getenv("DB_PORT", 5432)),
}

TABLE_ALIASES: dict[str, list[str]] = {
    "materials": ["materials", "warehouse_plastics"],
    "films": ["films", "warehouse_films"],
}

TEXT_TYPES = {"character varying", "text", "citext"}
INT_TYPES = {"smallint", "integer", "bigint"}
FLOAT_TYPES = {"double precision", "real"}
DECIMAL_TYPES = {"numeric", "decimal", "money"}
DATE_TYPES = {"date"}
TIME_TYPES = {"time without time zone", "time with time zone"}
TIMESTAMP_TYPES = {"timestamp without time zone", "timestamp with time zone"}
BOOL_TYPES = {"boolean"}

COLUMN_LABELS = {
    "id": "ID",
    "article": "Артикул",
    "material": "Материал",
    "thickness": "Толщина",
    "color": "Цвет",
    "color_code": "Код цвета",
    "length": "Длина",
    "width": "Ширина",
    "warehouse": "Склад",
    "comment": "Комментарий",
    "employee_id": "ID сотрудника",
    "employee_name": "Сотрудник",
    "employee_nick": "Ник сотрудника",
    "arrival_date": "Дата поступления",
    "arrival_at": "Дата и время поступления",
    "manufacturer": "Производитель",
    "series": "Серия",
    "recorded_at": "Зарегистрировано",
}

db_pool: asyncpg.Pool | None = None


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def resolve_table(conn: asyncpg.Connection, alias: str) -> dict[str, str]:
    candidates = TABLE_ALIASES.get(alias)
    if not candidates:
        raise HTTPException(status_code=404, detail="Неизвестный раздел")

    for candidate in candidates:
        schema = "public"
        table = candidate
        if "." in candidate:
            schema, table = candidate.split(".", 1)
        exists = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = $1 AND table_name = $2
            )
            """,
            schema,
            table,
        )
        if exists:
            return {
                "schema": schema,
                "table": table,
                "qualified": f"{quote_ident(schema)}.{quote_ident(table)}",
                "alias": alias,
            }
    raise HTTPException(status_code=404, detail=f"Таблица для раздела '{alias}' не найдена")


async def fetch_columns(conn: asyncpg.Connection, schema: str, table: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = $1 AND table_name = $2
        ORDER BY ordinal_position
        """,
        schema,
        table,
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"Нет колонок у таблицы {schema}.{table}")

    columns: list[dict[str, Any]] = []
    for row in rows:
        name = row["column_name"]
        label = COLUMN_LABELS.get(name, name.replace("_", " ").capitalize())
        columns.append(
            {
                "name": name,
                "data_type": row["data_type"],
                "is_nullable": row["is_nullable"] == "YES",
                "has_default": row["column_default"] is not None,
                "title": label,
            }
        )
    return columns


async def fetch_rows(
    conn: asyncpg.Connection,
    table_info: dict[str, str],
    columns: list[dict[str, Any]],
    search: str | None,
    sort: str | None,
    order: str | None,
) -> tuple[list[dict[str, Any]], str, str]:
    column_names = [col["name"] for col in columns]
    if not column_names:
        return [], "", "asc"

    sort_column = sort if sort in column_names else column_names[0]
    order_direction = "desc" if order and order.lower() == "desc" else "asc"

    text_columns = [col for col in columns if col["data_type"] in TEXT_TYPES]

    params: list[Any] = []
    where_clause = ""
    if search and text_columns:
        like_parts = []
        for idx, col in enumerate(text_columns, start=1):
            like_parts.append(f"{quote_ident(col['name'])} ILIKE ${idx}")
            params.append(f"%{search}%")
        where_clause = " WHERE " + " OR ".join(like_parts)

    select_columns = ", ".join(quote_ident(name) for name in column_names)
    query = (
        f"SELECT {select_columns} FROM {table_info['qualified']}"
        f"{where_clause} ORDER BY {quote_ident(sort_column)} {order_direction.upper()}"
    )

    records = await conn.fetch(query, *params)
    rows = [dict(record) for record in records]
    return rows, sort_column, order_direction


def convert_value(value: Any, data_type: str) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None
        value = stripped

    if data_type in INT_TYPES:
        return int(value)
    if data_type in DECIMAL_TYPES:
        return Decimal(str(value))
    if data_type in FLOAT_TYPES:
        return float(value)
    if data_type in DATE_TYPES:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return date.fromisoformat(str(value))
    if data_type in TIMESTAMP_TYPES:
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        return datetime.fromisoformat(str(value))
    if data_type in TIME_TYPES:
        if isinstance(value, time):
            return value
        return time.fromisoformat(str(value))
    if data_type in BOOL_TYPES:
        if isinstance(value, bool):
            return value
        lowered = str(value).strip().lower()
        if lowered in {"1", "true", "t", "yes", "y"}:
            return True
        if lowered in {"0", "false", "f", "no", "n"}:
            return False
        raise ValueError("ожидалось булево значение (true/false)")
    return str(value)


async def get_pool() -> asyncpg.Pool:
    if db_pool is None:
        raise HTTPException(status_code=500, detail="База данных недоступна")
    return db_pool


@app.on_event("startup")
async def startup() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(**DB_SETTINGS)


@app.on_event("shutdown")
async def shutdown() -> None:
    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


def _prepare_form_columns(raw_columns: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    for column in raw_columns:
        name = column["name"]
        if name == "id":
            continue
        data_type = column["data_type"]
        input_type = "text"
        step = None
        if data_type in INT_TYPES:
            input_type = "number"
            step = "1"
        elif data_type in DECIMAL_TYPES | FLOAT_TYPES:
            input_type = "number"
            step = "0.01"
        elif data_type in DATE_TYPES:
            input_type = "date"
        elif data_type in TIMESTAMP_TYPES:
            input_type = "datetime-local"
        elif data_type in TIME_TYPES:
            input_type = "time"
        elif data_type == "text":
            input_type = "textarea"
        prepared.append(
            {
                "name": name,
                "title": column["title"],
                "input_type": input_type,
                "step": step,
                "required": not column["is_nullable"] and not column["has_default"],
            }
        )
    return prepared


async def _render_table(
    request: Request,
    alias: str,
    template_name: str,
    search: str,
    sort: str | None,
    order: str | None,
    message: str | None,
    error: str | None,
) -> HTMLResponse:
    pool = await get_pool()
    async with pool.acquire() as conn:
        table_info = await resolve_table(conn, alias)
        columns = await fetch_columns(conn, table_info["schema"], table_info["table"])
        rows, applied_sort, applied_order = await fetch_rows(conn, table_info, columns, search, sort, order)

    context = {
        "request": request,
        "columns": columns,
        "rows": rows,
        "search": search,
        "sort": applied_sort,
        "order": applied_order,
        "message": message,
        "error": error,
    }
    return templates.TemplateResponse(template_name, context)


@app.get("/materials", response_class=HTMLResponse, name="materials_page")
async def materials_page(
    request: Request,
    search: str = "",
    sort: str | None = None,
    order: str | None = None,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return await _render_table(request, "materials", "materials.html", search, sort, order, message, error)


@app.get("/films", response_class=HTMLResponse, name="films_page")
async def films_page(
    request: Request,
    search: str = "",
    sort: str | None = None,
    order: str | None = None,
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    return await _render_table(request, "films", "films.html", search, sort, order, message, error)


@app.get("/add", response_class=HTMLResponse, name="add_item_form")
async def add_item_form(
    request: Request,
    type: str = "materials",
    message: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    item_type = type if type in TABLE_ALIASES else "materials"
    pool = await get_pool()
    columns: list[dict[str, Any]] = []
    try:
        async with pool.acquire() as conn:
            table_info = await resolve_table(conn, item_type)
            raw_columns = await fetch_columns(conn, table_info["schema"], table_info["table"])
            columns = _prepare_form_columns(raw_columns)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise
        error = exc.detail

    context = {
        "request": request,
        "columns": columns,
        "item_type": item_type,
        "message": message,
        "error": error,
    }
    return templates.TemplateResponse("add_item.html", context)


@app.post("/add")
async def add_item(
    request: Request,
    item_type: str = Form(...),
) -> Response:
    item_type = item_type if item_type in TABLE_ALIASES else "materials"
    form = await request.form()
    pool = await get_pool()
    async with pool.acquire() as conn:
        table_info = await resolve_table(conn, item_type)
        raw_columns = await fetch_columns(conn, table_info["schema"], table_info["table"])
        columns_for_form = _prepare_form_columns(raw_columns)

        values: list[Any] = []
        columns_to_insert: list[str] = []
        placeholders: list[str] = []
        param_idx = 1

        for column in raw_columns:
            name = column["name"]
            if name == "id":
                continue
            raw_value = form.get(name)
            try:
                converted = convert_value(raw_value, column["data_type"])
            except (ValueError, TypeError) as exc:
                context = {
                    "request": request,
                    "columns": columns_for_form,
                    "item_type": item_type,
                    "error": f"Ошибка в поле '{column['title']}': {exc}",
                    "message": None,
                }
                return templates.TemplateResponse("add_item.html", context, status_code=400)

            if converted is None:
                if not column["is_nullable"] and not column["has_default"]:
                    context = {
                        "request": request,
                        "columns": columns_for_form,
                        "item_type": item_type,
                        "error": f"Поле '{column['title']}' обязательно для заполнения.",
                        "message": None,
                    }
                    return templates.TemplateResponse("add_item.html", context, status_code=400)
                continue

            columns_to_insert.append(name)
            values.append(converted)
            placeholders.append(f"${param_idx}")
            param_idx += 1

        if not columns_to_insert:
            context = {
                "request": request,
                "columns": columns_for_form,
                "item_type": item_type,
                "error": "Не заполнено ни одно поле для сохранения.",
                "message": None,
            }
            return templates.TemplateResponse("add_item.html", context, status_code=400)

        insert_query = (
            f"INSERT INTO {table_info['qualified']} "
            f"({', '.join(quote_ident(col) for col in columns_to_insert)}) "
            f"VALUES ({', '.join(placeholders)})"
        )
        await conn.execute(insert_query, *values)

    target_route = "materials_page" if item_type == "materials" else "films_page"
    target_url = request.url_for(target_route)
    redirect_url = f"{target_url}?" + urlencode({"message": "Запись успешно добавлена."})
    return RedirectResponse(redirect_url, status_code=status.HTTP_303_SEE_OTHER)


def _create_workbook(columns: list[dict[str, Any]], rows: list[dict[str, Any]]) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Данные"
    ws.append([col["title"] for col in columns])

    for row in rows:
        ws.append([row.get(col["name"]) for col in columns])

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output


@app.get("/{table_alias}/export", response_class=StreamingResponse, name="export_table")
async def export_table(
    table_alias: str,
    search: str = "",
    sort: str | None = None,
    order: str | None = None,
) -> StreamingResponse:
    if table_alias not in TABLE_ALIASES:
        raise HTTPException(status_code=404, detail="Неизвестный раздел")
    pool = await get_pool()
    async with pool.acquire() as conn:
        table_info = await resolve_table(conn, table_alias)
        columns = await fetch_columns(conn, table_info["schema"], table_info["table"])
        rows, _, _ = await fetch_rows(conn, table_info, columns, search, sort, order)

    file_buffer = _create_workbook(columns, rows)
    filename = f"{table_alias}.xlsx"
    headers = {"Content-Disposition": f"attachment; filename={filename}"}
    return StreamingResponse(file_buffer, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers=headers)


@app.post("/{table_alias}/import", name="import_table")
async def import_table(
    request: Request,
    table_alias: str,
    file: UploadFile = File(...),
) -> RedirectResponse:
    if table_alias not in TABLE_ALIASES:
        raise HTTPException(status_code=404, detail="Неизвестный раздел")

    try:
        data = await file.read()
        workbook = load_workbook(BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        target_route = "materials_page" if table_alias == "materials" else "films_page"
        target_url = request.url_for(target_route)
        params = urlencode({"error": f"Не удалось прочитать файл: {exc}"})
        return RedirectResponse(f"{target_url}?{params}", status_code=status.HTTP_303_SEE_OTHER)

    sheet = workbook.active
    rows_iter = sheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        target_route = "materials_page" if table_alias == "materials" else "films_page"
        target_url = request.url_for(target_route)
        params = urlencode({"error": "Пустой файл, импорт невозможен."})
        return RedirectResponse(f"{target_url}?{params}", status_code=status.HTTP_303_SEE_OTHER)

    pool = await get_pool()
    async with pool.acquire() as conn:
        table_info = await resolve_table(conn, table_alias)
        raw_columns = await fetch_columns(conn, table_info["schema"], table_info["table"])
        column_lookup: dict[str, dict[str, Any]] = {}
        for col in raw_columns:
            if col["name"] == "id":
                continue
            column_lookup.setdefault(col["name"].lower(), col)
            column_lookup.setdefault(col["title"].lower(), col)

        header_mapping: list[str | None] = []
        for cell_value in header_row:
            if cell_value is None:
                header_mapping.append(None)
                continue
            key = str(cell_value).strip().lower()
            column = column_lookup.get(key)
            header_mapping.append(column["name"] if column else None)

        if not any(header_mapping):
            target_route = "materials_page" if table_alias == "materials" else "films_page"
            target_url = request.url_for(target_route)
            params = urlencode({"error": "Не найдено подходящих столбцов для импорта."})
            return RedirectResponse(f"{target_url}?{params}", status_code=status.HTTP_303_SEE_OTHER)

        rows_to_insert: list[dict[str, Any]] = []
        for row_values in rows_iter:
            row_data: dict[str, Any] = {}
            for idx, raw_value in enumerate(row_values):
                column_name = header_mapping[idx] if idx < len(header_mapping) else None
                if not column_name:
                    continue
                column_info = column_lookup[column_name.lower()]
                try:
                    converted = convert_value(raw_value, column_info["data_type"])
                except (ValueError, TypeError) as exc:  # noqa: BLE001
                    workbook.close()
                    target_route = "materials_page" if table_alias == "materials" else "films_page"
                    target_url = request.url_for(target_route)
                    params = urlencode({"error": f"Ошибка импорта в столбце '{column_info['title']}': {exc}"})
                    return RedirectResponse(f"{target_url}?{params}", status_code=status.HTTP_303_SEE_OTHER)
                if converted is None:
                    if not column_info["is_nullable"] and not column_info["has_default"]:
                        workbook.close()
                        target_route = "materials_page" if table_alias == "materials" else "films_page"
                        target_url = request.url_for(target_route)
                        params = urlencode({"error": f"Столбец '{column_info['title']}' обязателен для заполнения."})
                        return RedirectResponse(f"{target_url}?{params}", status_code=status.HTTP_303_SEE_OTHER)
                    continue
                row_data[column_name] = converted
            if row_data:
                rows_to_insert.append(row_data)

        workbook.close()

        if not rows_to_insert:
            target_route = "materials_page" if table_alias == "materials" else "films_page"
            target_url = request.url_for(target_route)
            params = urlencode({"error": "Файл не содержит данных для импорта."})
            return RedirectResponse(f"{target_url}?{params}", status_code=status.HTTP_303_SEE_OTHER)

        async with conn.transaction():
            for row_data in rows_to_insert:
                col_names = list(row_data.keys())
                placeholders = [f"${idx}" for idx in range(1, len(col_names) + 1)]
                values = [row_data[name] for name in col_names]
                query = (
                    f"INSERT INTO {table_info['qualified']} "
                    f"({', '.join(quote_ident(name) for name in col_names)}) "
                    f"VALUES ({', '.join(placeholders)})"
                )
                await conn.execute(query, *values)

    target_route = "materials_page" if table_alias == "materials" else "films_page"
    target_url = request.url_for(target_route)
    params = urlencode({"message": f"Импортировано записей: {len(rows_to_insert)}"})
    return RedirectResponse(f"{target_url}?{params}", status_code=status.HTTP_303_SEE_OTHER)
