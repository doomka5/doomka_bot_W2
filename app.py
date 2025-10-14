import os
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

DATABASE_POOL: Optional[asyncpg.pool.Pool] = None

app = FastAPI(title="Warehouse Plastics API")


async def create_pool() -> asyncpg.pool.Pool:
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT")
    db_name = os.getenv("DB_NAME")
    db_user = os.getenv("DB_USER")
    db_pass = os.getenv("DB_PASS")

    if not all([db_host, db_port, db_name, db_user, db_pass]):
        missing = [
            name
            for name, value in (
                ("DB_HOST", db_host),
                ("DB_PORT", db_port),
                ("DB_NAME", db_name),
                ("DB_USER", db_user),
                ("DB_PASS", db_pass),
            )
            if not value
        ]
        raise RuntimeError(
            "Missing required database environment variables: " + ", ".join(missing)
        )

    return await asyncpg.create_pool(
        host=db_host,
        port=int(db_port),
        database=db_name,
        user=db_user,
        password=db_pass,
        min_size=1,
        max_size=10,
    )


@app.on_event("startup")
async def startup() -> None:
    global DATABASE_POOL
    DATABASE_POOL = await create_pool()


@app.on_event("shutdown")
async def shutdown() -> None:
    global DATABASE_POOL
    if DATABASE_POOL:
        await DATABASE_POOL.close()
        DATABASE_POOL = None


def build_filters(
    article: Optional[str] = None,
    material: Optional[str] = None,
    color: Optional[str] = None,
    warehouse: Optional[str] = None,
    thickness: Optional[float] = None,
    thickness_min: Optional[float] = None,
    thickness_max: Optional[float] = None,
) -> Tuple[str, List[Any]]:
    conditions: List[str] = []
    params: List[Any] = []

    def add_ilike_condition(field: str, value: Optional[str]) -> None:
        if value:
            params.append(f"%{value}%")
            conditions.append(f"{field} ILIKE ${len(params)}")

    add_ilike_condition("article", article)
    add_ilike_condition("material", material)
    add_ilike_condition("color", color)
    add_ilike_condition("warehouse", warehouse)

    if thickness is not None:
        params.append(thickness)
        conditions.append(f"thickness = ${len(params)}")

    if thickness_min is not None:
        params.append(thickness_min)
        conditions.append(f"thickness >= ${len(params)}")

    if thickness_max is not None:
        params.append(thickness_max)
        conditions.append(f"thickness <= ${len(params)}")

    where_clause = ""
    if conditions:
        where_clause = " WHERE " + " AND ".join(conditions)

    return where_clause, params


async def fetch_plastics(
    article: Optional[str] = None,
    material: Optional[str] = None,
    color: Optional[str] = None,
    warehouse: Optional[str] = None,
    thickness: Optional[float] = None,
    thickness_min: Optional[float] = None,
    thickness_max: Optional[float] = None,
) -> List[Dict[str, Any]]:
    if DATABASE_POOL is None:
        raise HTTPException(status_code=503, detail="Database pool is not initialized")

    where_clause, params = build_filters(
        article=article,
        material=material,
        color=color,
        warehouse=warehouse,
        thickness=thickness,
        thickness_min=thickness_min,
        thickness_max=thickness_max,
    )

    query = (
        "SELECT id, article, material, thickness, color, length, width, warehouse, "
        "comment, employee_id, employee_name, arrival_date, arrival_at "
        "FROM warehouse_plastics"
        f"{where_clause}"
        " ORDER BY arrival_at DESC NULLS LAST, id DESC"
        " LIMIT 100"
    )

    async with DATABASE_POOL.acquire() as connection:
        records = await connection.fetch(query, *params)

    results: List[Dict[str, Any]] = []
    for record in records:
        arrival_at = record["arrival_at"]
        formatted_arrival_at = (
            arrival_at.strftime("%Y-%m-%d %H:%M") if arrival_at is not None else None
        )
        arrival_date = record["arrival_date"]
        formatted_arrival_date = (
            arrival_date.isoformat() if arrival_date is not None else None
        )
        results.append(
            {
                "id": record["id"],
                "article": record["article"],
                "material": record["material"],
                "thickness": float(record["thickness"]) if record["thickness"] is not None else None,
                "color": record["color"],
                "length": float(record["length"]) if record["length"] is not None else None,
                "width": float(record["width"]) if record["width"] is not None else None,
                "warehouse": record["warehouse"],
                "comment": record["comment"],
                "employee_id": record["employee_id"],
                "employee_name": record["employee_name"],
                "arrival_date": formatted_arrival_date,
                "arrival_at": formatted_arrival_at,
            }
        )

    return results


@app.get("/api/plastics")
async def api_plastics(
    article: Optional[str] = Query(default=None),
    material: Optional[str] = Query(default=None),
    color: Optional[str] = Query(default=None),
    warehouse: Optional[str] = Query(default=None),
    thickness: Optional[float] = Query(default=None),
    thickness_min: Optional[float] = Query(default=None),
    thickness_max: Optional[float] = Query(default=None),
) -> JSONResponse:
    data = await fetch_plastics(
        article=article,
        material=material,
        color=color,
        warehouse=warehouse,
        thickness=thickness,
        thickness_min=thickness_min,
        thickness_max=thickness_max,
    )
    return JSONResponse(content={"data": data})


def format_dimension(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.2f} мм"


@app.get("/plastics", response_class=HTMLResponse)
async def html_plastics(
    article: Optional[str] = Query(default=None),
    material: Optional[str] = Query(default=None),
    color: Optional[str] = Query(default=None),
    warehouse: Optional[str] = Query(default=None),
    thickness: Optional[float] = Query(default=None),
    thickness_min: Optional[float] = Query(default=None),
    thickness_max: Optional[float] = Query(default=None),
) -> HTMLResponse:
    data = await fetch_plastics(
        article=article,
        material=material,
        color=color,
        warehouse=warehouse,
        thickness=thickness,
        thickness_min=thickness_min,
        thickness_max=thickness_max,
    )

    rows = ""
    for item in data:
        rows += f"""
        <tr>
            <td>{item['id']}</td>
            <td>{item['article'] or '—'}</td>
            <td>{item['material'] or '—'}</td>
            <td>{format_dimension(item['thickness'])}</td>
            <td>{item['color'] or '—'}</td>
            <td>{format_dimension(item['length'])}</td>
            <td>{format_dimension(item['width'])}</td>
            <td>{item['warehouse'] or '—'}</td>
            <td>{item['comment'] or '—'}</td>
            <td>{item['employee_id'] or '—'}</td>
            <td>{item['employee_name'] or '—'}</td>
            <td>{item['arrival_date'] or '—'}</td>
            <td>{item['arrival_at'] or '—'}</td>
        </tr>
        """

    if not rows:
        rows = "<tr><td colspan=\"13\">Нет данных</td></tr>"

    html_content = f"""
    <!DOCTYPE html>
    <html lang=\"ru\">
    <head>
        <meta charset=\"UTF-8\">
        <title>Склад пластика</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            h1 {{ margin-bottom: 16px; }}
            form {{ margin-bottom: 20px; display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }}
            form div {{ display: flex; flex-direction: column; }}
            form label {{ font-weight: bold; margin-bottom: 4px; }}
            form input {{ padding: 6px; border: 1px solid #ccc; border-radius: 4px; }}
            form .buttons {{ grid-column: 1 / -1; display: flex; gap: 10px; }}
            form button {{ padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; }}
            form button[type=\"submit\"] {{ background-color: #4CAF50; color: white; }}
            form button[type=\"reset\"] {{ background-color: #f0f0f0; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            tr:nth-child(even) {{ background-color: #fafafa; }}
        </style>
    </head>
    <body>
        <h1>Склад пластика</h1>
        <form method=\"get\" action=\"/plastics\">
            <div>
                <label for=\"article\">Артикул</label>
                <input type=\"text\" id=\"article\" name=\"article\" value=\"{article or ''}\" />
            </div>
            <div>
                <label for=\"material\">Материал</label>
                <input type=\"text\" id=\"material\" name=\"material\" value=\"{material or ''}\" />
            </div>
            <div>
                <label for=\"color\">Цвет</label>
                <input type=\"text\" id=\"color\" name=\"color\" value=\"{color or ''}\" />
            </div>
            <div>
                <label for=\"warehouse\">Место хранения</label>
                <input type=\"text\" id=\"warehouse\" name=\"warehouse\" value=\"{warehouse or ''}\" />
            </div>
            <div>
                <label for=\"thickness\">Толщина (точно)</label>
                <input type=\"number\" step=\"0.01\" id=\"thickness\" name=\"thickness\" value=\"{'' if thickness is None else thickness}\" />
            </div>
            <div>
                <label for=\"thickness_min\">Толщина от</label>
                <input type=\"number\" step=\"0.01\" id=\"thickness_min\" name=\"thickness_min\" value=\"{'' if thickness_min is None else thickness_min}\" />
            </div>
            <div>
                <label for=\"thickness_max\">Толщина до</label>
                <input type=\"number\" step=\"0.01\" id=\"thickness_max\" name=\"thickness_max\" value=\"{'' if thickness_max is None else thickness_max}\" />
            </div>
            <div class=\"buttons\">
                <button type=\"submit\">Фильтр</button>
                <button type=\"reset\" onclick=\"window.location='/plastics'; return false;\">Сброс</button>
            </div>
        </form>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Артикул</th>
                    <th>Материал</th>
                    <th>Толщина</th>
                    <th>Цвет</th>
                    <th>Длина</th>
                    <th>Ширина</th>
                    <th>Склад</th>
                    <th>Комментарий</th>
                    <th>ID сотрудника</th>
                    <th>Имя сотрудника</th>
                    <th>Дата прихода</th>
                    <th>Прибыло</th>
                </tr>
            </thead>
            <tbody>
                {rows}
            </tbody>
        </table>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)


__all__ = ["app"]
