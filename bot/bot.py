"""Telegram bot with basic user management backed by PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Dict, Optional

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    KeyboardButton,
    Message,
    TelegramObject,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO)

# === Настройки окружения ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "botdb")
DB_USER = os.getenv("DB_USER", "botuser")
DB_PASS = os.getenv("DB_PASS", "botpass")

db_pool: Optional[asyncpg.Pool] = None

WARSAW_TZ = ZoneInfo("Europe/Warsaw")


# === Проверка доступа пользователей ===
async def user_has_access(tg_id: int) -> bool:
    if db_pool is None:
        logging.warning("Database pool is not initialised when checking access")
        return False
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM users WHERE tg_id = $1", tg_id)
    return row is not None


async def user_is_admin(tg_id: int) -> bool:
    if db_pool is None:
        logging.warning("Database pool is not initialised when checking admin role")
        return False
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT role FROM users WHERE tg_id = $1", tg_id)
    if not row:
        return False
    role = (row["role"] or "").lower()
    return "админист" in role or "admin" in role


async def ensure_admin_access(message: Message, state: Optional[FSMContext] = None) -> bool:
    if not message.from_user:
        return False
    if await user_is_admin(message.from_user.id):
        return True
    if state is not None:
        await state.clear()
    await message.answer("🚫 У вас недостаточно прав для управления настройками.", reply_markup=MAIN_MENU_KB)
    return False


# === Мидлварь доступа ===
class AccessControlMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user_id: Optional[int] = None
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        if user_id is None:
            return await handler(event, data)
        if await user_has_access(user_id):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("🚫 У вас нет доступа к этому боту. Обратитесь к администратору.")
        return None


# === Инициализация базы данных ===
async def init_database() -> None:
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS, database=DB_NAME
    )

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Таблица пользователей
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    tg_id BIGINT UNIQUE NOT NULL,
                    username TEXT NOT NULL,
                    position TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            # Таблица склада пластиков
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
            # Таблица типов пластиков
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
            # Добавляем администратора
            await conn.execute(
                """
                INSERT INTO users (tg_id, username, position, role)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tg_id) DO UPDATE
                SET username = EXCLUDED.username,
                    position = EXCLUDED.position,
                    role = EXCLUDED.role
                """,
                37352491,
                "DooMka",
                "Администратор",
                "администратор с полными правами и доступом",
            )


async def close_database() -> None:
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None


# === События запуска и остановки ===
async def on_startup(bot: Bot) -> None:
    await init_database()
    logging.info("✅ Бот запущен и подключён к базе данных.")
    print("✅ Бот запущен и подключён к базе данных.")


async def on_shutdown(bot: Bot) -> None:
    await close_database()


# === Dispatcher ===
dp = Dispatcher()
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)
dp.message.outer_middleware(AccessControlMiddleware())


# === FSM ===
class AddUserStates(StatesGroup):
    waiting_for_tg_id = State()
    waiting_for_username = State()
    waiting_for_position = State()
    waiting_for_role = State()


class ManagePlasticMaterialStates(StatesGroup):
    waiting_for_new_material_name = State()
    waiting_for_material_name_to_delete = State()
    waiting_for_material_name_to_add_thickness = State()
    waiting_for_thickness_value_to_add = State()
    waiting_for_material_name_to_delete_thickness = State()
    waiting_for_thickness_value_to_delete = State()
    waiting_for_material_name_to_add_color = State()
    waiting_for_color_value_to_add = State()
    waiting_for_material_name_to_delete_color = State()
    waiting_for_color_value_to_delete = State()
    waiting_for_new_storage_location_name = State()
    waiting_for_storage_location_to_delete = State()


class AddWarehousePlasticStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_material = State()
    waiting_for_thickness = State()
    waiting_for_color = State()
    waiting_for_length = State()
    waiting_for_width = State()
    waiting_for_storage = State()
    waiting_for_comment = State()


class SearchWarehousePlasticStates(StatesGroup):
    waiting_for_query = State()


class CommentWarehousePlasticStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_comment = State()


# === Клавиатуры ===
MAIN_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="⚙️ Настройки"),
            KeyboardButton(text="Тест"),
            KeyboardButton(text="Тест 2"),
        ],
        [KeyboardButton(text="🏢 Склад")],
    ],
    resize_keyboard=True,
)

SETTINGS_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👥 Пользователи")],
        [KeyboardButton(text="⬅️ Главное меню")],
    ],
    resize_keyboard=True,
)

USERS_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить пользователя")],
        [KeyboardButton(text="📋 Посмотреть всех пользователей")],
        [KeyboardButton(text="⬅️ Назад в настройки")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧱 Пластики")],
        [KeyboardButton(text="⚙️ Настройки склада")],
        [KeyboardButton(text="⬅️ Главное меню")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧱 Пластик")],
        [KeyboardButton(text="⬅️ Назад к складу")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_PLASTIC_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📦 Материал")],
        [KeyboardButton(text="📏 Толщина")],
        [KeyboardButton(text="🎨 Цвет")],
        [KeyboardButton(text="🏷️ Место хранения")],
        [KeyboardButton(text="⬅️ Назад к складу")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_PLASTIC_MATERIALS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить материал")],
        [KeyboardButton(text="➖ Удалить материал")],
        [KeyboardButton(text="⬅️ Назад к пластику")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить толщину")],
        [KeyboardButton(text="➖ Удалить толщину")],
        [KeyboardButton(text="⬅️ Назад к пластику")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить цвет")],
        [KeyboardButton(text="➖ Удалить цвет")],
        [KeyboardButton(text="⬅️ Назад к пластику")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_PLASTIC_STORAGE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить место хранения")],
        [KeyboardButton(text="➖ Удалить место хранения")],
        [KeyboardButton(text="⬅️ Назад к пластику")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_PLASTICS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="➖ Списать")],
        [KeyboardButton(text="💬 Комментировать")],
        [KeyboardButton(text="🔁 Переместить"), KeyboardButton(text="🔍 Найти")],
        [KeyboardButton(text="⬅️ Назад к складу")],
    ],
    resize_keyboard=True,
)

CANCEL_TEXT = "❌ Отмена"
SKIP_TEXT = "Пропустить"

CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=CANCEL_TEXT)]],
    resize_keyboard=True,
)

SKIP_OR_CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=SKIP_TEXT)], [KeyboardButton(text=CANCEL_TEXT)]],
    resize_keyboard=True,
)


async def _process_cancel_if_requested(message: Message, state: FSMContext) -> bool:
    if (message.text or "").strip() != CANCEL_TEXT:
        return False
    await handle_cancel(message, state)
    return True


async def _cancel_add_plastic_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Добавление пластика отменено.", reply_markup=WAREHOUSE_PLASTICS_KB
    )


async def _cancel_search_plastic_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Поиск отменён.", reply_markup=WAREHOUSE_PLASTICS_KB)


async def _cancel_comment_plastic_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Изменение комментария отменено.", reply_markup=WAREHOUSE_PLASTICS_KB
    )


# === Работа с БД ===
async def upsert_user_in_db(tg_id: int, username: str, position: str, role: str) -> None:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (tg_id, username, position, role)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tg_id) DO UPDATE
            SET username = EXCLUDED.username,
                position = EXCLUDED.position,
                role = EXCLUDED.role
            """,
            tg_id, username, position, role,
        )


async def fetch_plastic_material_types() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM plastic_material_types ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def fetch_plastic_storage_locations() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM plastic_storage_locations ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def insert_plastic_material_type(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO plastic_material_types (name)
            VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_plastic_material_type(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM plastic_material_types WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def insert_plastic_storage_location(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        existing_id = await conn.fetchval(
            "SELECT id FROM plastic_storage_locations WHERE LOWER(name) = LOWER($1)",
            name,
        )
        if existing_id:
            return False
        row = await conn.fetchrow(
            """
            INSERT INTO plastic_storage_locations (name)
            VALUES ($1)
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_plastic_storage_location(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM plastic_storage_locations WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def fetch_materials_with_thicknesses() -> list[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.name,
                   COALESCE(
                       (
                           SELECT ARRAY_AGG(t.thickness ORDER BY t.thickness)
                           FROM plastic_material_thicknesses t
                           WHERE t.material_id = p.id
                       ),
                       ARRAY[]::NUMERIC[]
                   ) AS thicknesses,
                   COALESCE(
                       (
                           SELECT ARRAY_AGG(c.color ORDER BY LOWER(c.color))
                           FROM plastic_material_colors c
                           WHERE c.material_id = p.id
                       ),
                       ARRAY[]::TEXT[]
                   ) AS colors
            FROM plastic_material_types p
            ORDER BY LOWER(p.name)
            """
        )
    return [dict(row) for row in rows]


async def fetch_material_thicknesses(material_name: str) -> list[Decimal]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.thickness
            FROM plastic_material_thicknesses t
            JOIN plastic_material_types p ON p.id = t.material_id
            WHERE LOWER(p.name) = LOWER($1)
            ORDER BY t.thickness
            """,
            material_name,
        )
    return [row["thickness"] for row in rows]


async def insert_material_thickness(material_name: str, thickness: Decimal) -> str:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        material_id = await conn.fetchval(
            "SELECT id FROM plastic_material_types WHERE LOWER(name) = LOWER($1)",
            material_name,
        )
        if material_id is None:
            return "material_not_found"
        row = await conn.fetchrow(
            """
            INSERT INTO plastic_material_thicknesses (material_id, thickness)
            VALUES ($1, $2)
            ON CONFLICT (material_id, thickness) DO NOTHING
            RETURNING id
            """,
            material_id,
            thickness,
        )
        if row:
            return "added"
        return "exists"


async def delete_material_thickness(material_name: str, thickness: Decimal) -> str:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        material_id = await conn.fetchval(
            "SELECT id FROM plastic_material_types WHERE LOWER(name) = LOWER($1)",
            material_name,
        )
        if material_id is None:
            return "material_not_found"
        result = await conn.execute(
            """
            DELETE FROM plastic_material_thicknesses
            WHERE material_id = $1 AND thickness = $2
            """,
            material_id,
            thickness,
        )
    if result.endswith(" 1"):
        return "deleted"
    return "not_found"


async def fetch_material_colors(material_name: str) -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.color
            FROM plastic_material_colors c
            JOIN plastic_material_types p ON p.id = c.material_id
            WHERE LOWER(p.name) = LOWER($1)
            ORDER BY LOWER(c.color)
            """,
            material_name,
        )
    return [row["color"] for row in rows]


async def insert_material_color(material_name: str, color: str) -> str:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        material_id = await conn.fetchval(
            "SELECT id FROM plastic_material_types WHERE LOWER(name) = LOWER($1)",
            material_name,
        )
        if material_id is None:
            return "material_not_found"
        exists = await conn.fetchval(
            """
            SELECT 1
            FROM plastic_material_colors
            WHERE material_id = $1 AND LOWER(color) = LOWER($2)
            """,
            material_id,
            color,
        )
        if exists:
            return "exists"
        await conn.execute(
            """
            INSERT INTO plastic_material_colors (material_id, color)
            VALUES ($1, $2)
            """,
            material_id,
            color,
        )
    return "added"


async def delete_material_color(material_name: str, color: str) -> str:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        material_id = await conn.fetchval(
            "SELECT id FROM plastic_material_types WHERE LOWER(name) = LOWER($1)",
            material_name,
        )
        if material_id is None:
            return "material_not_found"
        result = await conn.execute(
            """
            DELETE FROM plastic_material_colors
            WHERE material_id = $1 AND LOWER(color) = LOWER($2)
            """,
            material_id,
            color,
        )
    if result.endswith(" 1"):
        return "deleted"
    return "not_found"


async def insert_warehouse_plastic_record(
    article: str,
    material: str,
    thickness: Decimal,
    color: str,
    length_mm: Decimal,
    width_mm: Decimal,
    warehouse: str,
    comment: Optional[str],
    employee_id: Optional[int],
    employee_name: Optional[str],
) -> Dict[str, Any]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    now_warsaw = datetime.now(WARSAW_TZ)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO warehouse_plastics (
                article,
                material,
                thickness,
                color,
                length,
                width,
                warehouse,
                comment,
                employee_id,
                employee_name,
                arrival_date,
                arrival_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING
                id,
                article,
                material,
                thickness,
                color,
                length,
                width,
                warehouse,
                comment,
                employee_id,
                employee_name,
                arrival_date,
                arrival_at
            """,
            article,
            material,
            thickness,
            color,
            length_mm,
            width_mm,
            warehouse,
            comment,
            employee_id,
            employee_name,
            now_warsaw.date(),
            now_warsaw,
        )
    if row is None:
        return {}
    return dict(row)


async def search_warehouse_plastic_records(query: str, limit: int = 5) -> list[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    pattern = f"%{query}%"
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
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
            WHERE article ILIKE $1
               OR material ILIKE $1
               OR color ILIKE $1
               OR warehouse ILIKE $1
               OR comment ILIKE $1
            ORDER BY arrival_at DESC NULLS LAST, id DESC
            LIMIT $2
            """,
            pattern,
            limit,
        )
    return [dict(row) for row in rows]


async def fetch_warehouse_plastic_by_article(article: str) -> Optional[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id,
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
            WHERE article = $1
            ORDER BY arrival_at DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            article,
        )
    if row is None:
        return None
    return dict(row)


async def update_warehouse_plastic_comment(
    record_id: int, comment: Optional[str]
) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE warehouse_plastics
            SET comment = $2
            WHERE id = $1
            """,
            record_id,
            comment,
        )
    return result.endswith(" 1")


def format_materials_list(materials: list[str]) -> str:
    if not materials:
        return "—"
    return "\n".join(f"• {item}" for item in materials)


def format_thickness_value(thickness: Decimal) -> str:
    as_str = format(thickness, "f").rstrip("0").rstrip(".")
    if not as_str:
        as_str = "0"
    return f"{as_str} мм"


def format_dimension_value(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    as_str = format(value, "f").rstrip("0").rstrip(".")
    if not as_str:
        as_str = "0"
    return f"{as_str} мм"


def format_thicknesses_list(thicknesses: list[Decimal]) -> str:
    if not thicknesses:
        return "—"
    return ", ".join(format_thickness_value(value) for value in thicknesses)


def format_colors_list(colors: list[str]) -> str:
    if not colors:
        return "—"
    return ", ".join(colors)


def format_storage_locations_list(locations: list[str]) -> str:
    if not locations:
        return "—"
    return "\n".join(f"• {item}" for item in locations)


def format_plastic_record_for_message(record: Dict[str, Any]) -> str:
    thickness = record.get("thickness")
    arrival_at = record.get("arrival_at")
    if arrival_at:
        try:
            arrival_local = arrival_at.astimezone(WARSAW_TZ)
        except Exception:
            arrival_local = arrival_at
        arrival_text = arrival_local.strftime("%Y-%m-%d %H:%M")
    else:
        arrival_text = "—"
    return (
        f"Артикул: {record.get('article') or '—'}\n"
        f"Материал: {record.get('material') or '—'}\n"
        f"Толщина: {format_thickness_value(thickness) if thickness is not None else '—'}\n"
        f"Цвет: {record.get('color') or '—'}\n"
        f"Длина: {format_dimension_value(record.get('length'))}\n"
        f"Ширина: {format_dimension_value(record.get('width'))}\n"
        f"Склад: {record.get('warehouse') or '—'}\n"
        f"Комментарий: {record.get('comment') or '—'}\n"
        f"Добавил: {record.get('employee_name') or '—'}\n"
        f"Добавлено: {arrival_text}"
    )


def parse_thickness_input(raw_text: str) -> Optional[Decimal]:
    if raw_text is None:
        return None
    cleaned = raw_text.strip().lower()
    for suffix in ("мм", "mm"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    cleaned = cleaned.replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return value.quantize(Decimal("0.01"))


def parse_positive_integer(raw_text: str) -> Optional[int]:
    if raw_text is None:
        return None
    cleaned = (raw_text or "").strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    if value <= 0:
        return None
    return value


def build_materials_keyboard(materials: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for name in materials:
        rows.append([KeyboardButton(text=name)])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_thickness_keyboard(thicknesses: list[Decimal]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for value in thicknesses:
        rows.append([KeyboardButton(text=format_thickness_value(value))])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_colors_keyboard(colors: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for value in colors:
        rows.append([KeyboardButton(text=value)])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_storage_locations_keyboard(locations: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for location in locations:
        rows.append([KeyboardButton(text=location)])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


# === Сервисные функции ===
async def send_plastic_settings_overview(message: Message) -> None:
    materials = await fetch_materials_with_thicknesses()
    storage_locations = await fetch_plastic_storage_locations()
    if materials:
        lines = []
        for material in materials:
            name = material["name"]
            thicknesses = material.get("thicknesses") or []
            formatted_thicknesses = format_thicknesses_list(thicknesses)
            colors = material.get("colors") or []
            formatted_colors = format_colors_list(colors)
            lines.append(
                "\n".join(
                    [
                        f"• {name}",
                        f"   Толщины: {formatted_thicknesses}",
                        f"   Цвета: {formatted_colors}",
                    ]
                )
            )
        materials_list = "\n".join(lines)
        text = (
            "⚙️ Настройки склада → Пластик.\n\n"
            "Доступные материалы, толщины и цвета:\n"
            f"{materials_list}"
        )
    else:
        text = (
            "⚙️ Настройки склада → Пластик.\n\n"
            "Материалы ещё не добавлены. Используйте кнопки ниже."
        )
    storage_text = format_storage_locations_list(storage_locations)
    text = f"{text}\n\nМеста хранения:\n{storage_text}"
    await message.answer(text, reply_markup=WAREHOUSE_SETTINGS_PLASTIC_KB)


async def send_storage_locations_overview(message: Message) -> None:
    locations = await fetch_plastic_storage_locations()
    formatted = format_storage_locations_list(locations)
    await message.answer(
        "⚙️ Настройки склада → Пластик → Места хранения.\n\n"
        "Доступные места хранения:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить место.",
        reply_markup=WAREHOUSE_SETTINGS_PLASTIC_STORAGE_KB,
    )


# === Команды ===
@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer("👋 Привет! Выберите действие:", reply_markup=MAIN_MENU_KB)


@dp.message(F.text == "Тест")
async def handle_test(message: Message) -> None:
    await message.answer("тест ок")


@dp.message(F.text == "Тест 2")
async def handle_test_two(message: Message) -> None:
    await message.answer("тест 2 ок")


@dp.message(Command("settings"))
@dp.message(F.text == "⚙️ Настройки")
async def handle_settings(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await message.answer("⚙️ Настройки. Выберите действие:", reply_markup=SETTINGS_MENU_KB)


@dp.message(F.text == "⚙️ Настройки склада")
async def handle_warehouse_settings(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await message.answer("⚙️ Настройки склада. Выберите действие:", reply_markup=WAREHOUSE_SETTINGS_MENU_KB)


@dp.message(F.text == "👥 Пользователи")
async def handle_users_menu(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await message.answer("👥 Пользователи. Выберите действие:", reply_markup=USERS_MENU_KB)


@dp.message(F.text == "⬅️ Главное меню")
async def handle_back_to_main(message: Message) -> None:
    await message.answer("Главное меню.", reply_markup=MAIN_MENU_KB)


@dp.message(F.text == "⬅️ Назад в настройки")
async def handle_back_to_settings(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await handle_settings(message)


@dp.message(F.text == "⬅️ Назад к складу")
async def handle_back_to_warehouse(message: Message, state: FSMContext) -> None:
    await state.clear()
    await handle_warehouse_menu(message)


# === Склад ===
@dp.message(F.text == "🏢 Склад")
async def handle_warehouse_menu(message: Message) -> None:
    await message.answer("🏢 Склад. Выберите раздел:", reply_markup=WAREHOUSE_MENU_KB)


@dp.message(F.text == "🧱 Пластики")
async def handle_warehouse_plastics(message: Message) -> None:
    await message.answer("📦 Раздел «Пластики». Выберите действие:", reply_markup=WAREHOUSE_PLASTICS_KB)


@dp.message(F.text == "🔍 Найти")
async def handle_search_warehouse_plastic(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchWarehousePlasticStates.waiting_for_query)
    await message.answer(
        "Введите часть артикула, материала, цвета или комментария для поиска",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "💬 Комментировать")
async def handle_comment_warehouse_plastic(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CommentWarehousePlasticStates.waiting_for_article)
    await message.answer(
        "Введите номер артикула, чтобы просмотреть и изменить комментарий.",
        reply_markup=CANCEL_KB,
    )


@dp.message(SearchWarehousePlasticStates.waiting_for_query)
async def process_search_warehouse_plastic(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_plastic_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Запрос не может быть пустым. Введите текст для поиска.",
            reply_markup=CANCEL_KB,
        )
        return
    results = await search_warehouse_plastic_records(text, limit=5)
    if not results:
        await message.answer(
            "Ничего не найдено. Попробуйте другой запрос.", reply_markup=CANCEL_KB
        )
        return
    formatted = "\n\n".join(format_plastic_record_for_message(item) for item in results)
    await message.answer(
        f"🔍 Найдено записей: {len(results)}\n\n{formatted}",
        reply_markup=CANCEL_KB,
    )
    await message.answer(
        "Введите новый запрос для продолжения поиска или нажмите «❌ Отмена».",
        reply_markup=CANCEL_KB,
    )


@dp.message(CommentWarehousePlasticStates.waiting_for_article)
async def process_comment_article(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_comment_plastic_flow(message, state)
        return
    article = (message.text or "").strip()
    if not article.isdigit():
        await message.answer(
            "⚠️ Артикул должен содержать только цифры. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    record = await fetch_warehouse_plastic_by_article(article)
    if record is None:
        await message.answer(
            "ℹ️ Пластик с таким артикулом не найден. Попробуйте другой артикул.",
            reply_markup=CANCEL_KB,
        )
        return
    previous_comment = record.get("comment")
    await state.update_data(
        plastic_id=record["id"],
        article=record.get("article"),
        previous_comment=previous_comment,
    )
    await message.answer(
        "Найдена запись:\n\n"
        f"{format_plastic_record_for_message(record)}\n\n"
        f"Текущий комментарий: {previous_comment or '—'}",
        reply_markup=CANCEL_KB,
    )
    await state.set_state(CommentWarehousePlasticStates.waiting_for_comment)
    await message.answer(
        "Введите новый комментарий. Пустое сообщение удалит существующий комментарий.",
        reply_markup=CANCEL_KB,
    )


@dp.message(CommentWarehousePlasticStates.waiting_for_comment)
async def process_comment_update(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_comment_plastic_flow(message, state)
        return
    data = await state.get_data()
    record_id = data.get("plastic_id")
    article = data.get("article")
    previous_comment = data.get("previous_comment")
    if record_id is None or article is None:
        await _cancel_comment_plastic_flow(message, state)
        return
    new_comment_raw = (message.text or "").strip()
    new_comment: Optional[str]
    if new_comment_raw:
        new_comment = new_comment_raw
    else:
        new_comment = None
    updated = await update_warehouse_plastic_comment(record_id, new_comment)
    if not updated:
        await message.answer(
            "⚠️ Не удалось обновить комментарий. Попробуйте позже.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        await state.clear()
        return
    await state.clear()
    await message.answer(
        "✅ Комментарий обновлён.\n"
        f"Артикул: {article}\n"
        f"Старый комментарий: {previous_comment or '—'}\n"
        f"Новый комментарий: {new_comment or '—'}",
        reply_markup=WAREHOUSE_PLASTICS_KB,
    )


@dp.message(F.text == "➕ Добавить")
async def handle_add_warehouse_plastic(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddWarehousePlasticStates.waiting_for_article)
    await message.answer(
        "Введите номер артикула (только цифры).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehousePlasticStates.waiting_for_article)
async def process_plastic_article(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_flow(message, state)
        return
    article = (message.text or "").strip()
    if not article.isdigit():
        await message.answer(
            "⚠️ Артикул должен содержать только цифры. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    materials = await fetch_plastic_material_types()
    if not materials:
        await state.clear()
        await message.answer(
            "Справочник материалов пуст. Добавьте материалы в настройках склада.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    await state.update_data(article=article)
    await state.set_state(AddWarehousePlasticStates.waiting_for_material)
    await message.answer(
        "Выберите тип материала:",
        reply_markup=build_materials_keyboard(materials),
    )


@dp.message(AddWarehousePlasticStates.waiting_for_material)
async def process_plastic_material(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_flow(message, state)
        return
    materials = await fetch_plastic_material_types()
    raw = (message.text or "").strip()
    match = next((item for item in materials if item.lower() == raw.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Такой материал не найден. Выберите один из списка.",
            reply_markup=build_materials_keyboard(materials),
        )
        return
    thicknesses = await fetch_material_thicknesses(match)
    if not thicknesses:
        await state.clear()
        await message.answer(
            "Для выбранного материала не указаны толщины. Добавьте их в настройках склада.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    await state.update_data(material=match)
    await state.set_state(AddWarehousePlasticStates.waiting_for_thickness)
    await message.answer(
        "Выберите толщину из списка:",
        reply_markup=build_thickness_keyboard(thicknesses),
    )


@dp.message(AddWarehousePlasticStates.waiting_for_thickness)
async def process_plastic_thickness(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_flow(message, state)
        return
    data = await state.get_data()
    material = data.get("material")
    if not material:
        await _cancel_add_plastic_flow(message, state)
        return
    thicknesses = await fetch_material_thicknesses(material)
    value = parse_thickness_input(message.text or "")
    if value is None or all(item != value for item in thicknesses):
        await message.answer(
            "⚠️ Выберите толщину, используя кнопки ниже.",
            reply_markup=build_thickness_keyboard(thicknesses),
        )
        return
    colors = await fetch_material_colors(material)
    if not colors:
        await state.clear()
        await message.answer(
            "Для выбранного материала не указаны цвета. Добавьте их в настройках склада.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    await state.update_data(thickness=value)
    await state.set_state(AddWarehousePlasticStates.waiting_for_color)
    await message.answer(
        "Выберите цвет:",
        reply_markup=build_colors_keyboard(colors),
    )


@dp.message(AddWarehousePlasticStates.waiting_for_color)
async def process_plastic_color(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_flow(message, state)
        return
    data = await state.get_data()
    material = data.get("material")
    if not material:
        await _cancel_add_plastic_flow(message, state)
        return
    colors = await fetch_material_colors(material)
    raw = (message.text or "").strip()
    match = next((item for item in colors if item.lower() == raw.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Цвет не найден. Выберите один из списка.",
            reply_markup=build_colors_keyboard(colors),
        )
        return
    await state.update_data(color=match)
    await state.set_state(AddWarehousePlasticStates.waiting_for_length)
    await message.answer(
        "Укажите длину листа в миллиметрах (только число).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehousePlasticStates.waiting_for_length)
async def process_plastic_length(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_flow(message, state)
        return
    value = parse_positive_integer(message.text or "")
    if value is None:
        await message.answer(
            "⚠️ Длина должна быть положительным числом. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(length=value)
    await state.set_state(AddWarehousePlasticStates.waiting_for_width)
    await message.answer(
        "Укажите ширину листа в миллиметрах (только число).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehousePlasticStates.waiting_for_width)
async def process_plastic_width(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_flow(message, state)
        return
    value = parse_positive_integer(message.text or "")
    if value is None:
        await message.answer(
            "⚠️ Ширина должна быть положительным числом. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    locations = await fetch_plastic_storage_locations()
    if not locations:
        await state.clear()
        await message.answer(
            "Справочник мест хранения пуст. Добавьте места в настройках склада.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    await state.update_data(width=value)
    await state.set_state(AddWarehousePlasticStates.waiting_for_storage)
    await message.answer(
        "Выберите место хранения:",
        reply_markup=build_storage_locations_keyboard(locations),
    )


@dp.message(AddWarehousePlasticStates.waiting_for_storage)
async def process_plastic_storage(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_flow(message, state)
        return
    locations = await fetch_plastic_storage_locations()
    raw = (message.text or "").strip()
    match = next((item for item in locations if item.lower() == raw.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Место хранения не найдено. Выберите одно из списка.",
            reply_markup=build_storage_locations_keyboard(locations),
        )
        return
    await state.update_data(storage=match)
    await state.set_state(AddWarehousePlasticStates.waiting_for_comment)
    await message.answer(
        "Добавьте комментарий (необязательно) или нажмите «Пропустить».",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


@dp.message(AddWarehousePlasticStates.waiting_for_comment)
async def process_plastic_comment(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_plastic_flow(message, state)
        return
    comment: Optional[str]
    if text == SKIP_TEXT:
        comment = None
    else:
        comment = text or None
    data = await state.get_data()
    article = data.get("article")
    material = data.get("material")
    thickness: Optional[Decimal] = data.get("thickness")
    color = data.get("color")
    length = data.get("length")
    width = data.get("width")
    storage = data.get("storage")
    if not all([article, material, thickness, color, length, width, storage]):
        await _cancel_add_plastic_flow(message, state)
        return
    employee_id = message.from_user.id if message.from_user else None
    employee_name = message.from_user.full_name if message.from_user else None
    record = await insert_warehouse_plastic_record(
        article=article,
        material=material,
        thickness=thickness,
        color=color,
        length_mm=Decimal(length),
        width_mm=Decimal(width),
        warehouse=storage,
        comment=comment,
        employee_id=employee_id,
        employee_name=employee_name,
    )
    await state.clear()
    summary_comment = (record.get("comment") if record else comment) or "—"
    if record and record.get("employee_name"):
        summary_employee = record.get("employee_name") or "—"
    else:
        summary_employee = employee_name or "—"
    arrival_at = record.get("arrival_at") if record else None
    if arrival_at:
        try:
            arrival_local = arrival_at.astimezone(WARSAW_TZ)
        except Exception:
            arrival_local = arrival_at
        arrival_formatted = arrival_local.strftime("%Y-%m-%d %H:%M")
    else:
        arrival_formatted = datetime.now(WARSAW_TZ).strftime("%Y-%m-%d %H:%M")
    await message.answer(
        "✅ Пластик добавлен на склад.\n\n"
        f"Артикул: {article}\n"
        f"Материал: {material}\n"
        f"Толщина: {format_thickness_value(thickness)}\n"
        f"Цвет: {color}\n"
        f"Длина: {length} мм\n"
        f"Ширина: {width} мм\n"
        f"Место хранения: {storage}\n"
        f"Комментарий: {summary_comment}\n"
        f"Добавил: {summary_employee}\n"
        f"Добавлено: {arrival_formatted}",
        reply_markup=WAREHOUSE_PLASTICS_KB,
    )


@dp.message(F.text == "🧱 Пластик")
async def handle_warehouse_settings_plastic(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await send_plastic_settings_overview(message)


@dp.message(F.text == "📦 Материал")
async def handle_plastic_materials_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await message.answer(
        "Выберите действие с материалами:",
        reply_markup=WAREHOUSE_SETTINGS_PLASTIC_MATERIALS_KB,
    )


@dp.message(F.text == "📏 Толщина")
async def handle_plastic_thickness_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await message.answer(
        "Выберите действие с толщинами:",
        reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
    )


@dp.message(F.text == "🎨 Цвет")
async def handle_plastic_colors_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await message.answer(
        "Выберите действие с цветами:",
        reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
    )


@dp.message(F.text == "🏷️ Место хранения")
async def handle_plastic_storage_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_storage_locations_overview(message)


@dp.message(F.text == "⬅️ Назад к пластику")
async def handle_back_to_plastic_settings(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_plastic_settings_overview(message)


@dp.message(F.text == "➕ Добавить материал")
async def handle_add_plastic_material_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(ManagePlasticMaterialStates.waiting_for_new_material_name)
    materials = await fetch_plastic_material_types()
    existing_text = format_materials_list(materials)
    await message.answer(
        "Введите название материала (например, Дибонд, Акрил, ПВХ).\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_new_material_name)
async def process_new_plastic_material(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_plastic_material_type(name):
        await message.answer(f"✅ Материал «{name}» добавлен.")
    else:
        await message.answer(f"ℹ️ Материал «{name}» уже есть в списке.")
    await state.clear()
    await send_plastic_settings_overview(message)


@dp.message(F.text == "➖ Удалить материал")
async def handle_remove_plastic_material_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    materials = await fetch_plastic_material_types()
    if not materials:
        await message.answer(
            "Список материалов пуст. Добавьте материалы перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_MATERIALS_KB,
        )
        await state.clear()
        return
    await state.set_state(ManagePlasticMaterialStates.waiting_for_material_name_to_delete)
    await message.answer(
        "Выберите материал, который нужно удалить:",
        reply_markup=build_materials_keyboard(materials),
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_material_name_to_delete)
async def process_remove_plastic_material(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await delete_plastic_material_type(name):
        await message.answer(f"🗑 Материал «{name}» удалён.")
    else:
        await message.answer(f"ℹ️ Материал «{name}» не найден в списке.")
    await state.clear()
    await send_plastic_settings_overview(message)


@dp.message(F.text == "➕ Добавить место хранения")
async def handle_add_storage_location_button(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(
        ManagePlasticMaterialStates.waiting_for_new_storage_location_name
    )
    locations = await fetch_plastic_storage_locations()
    existing_text = format_storage_locations_list(locations)
    await message.answer(
        "Введите название места хранения (например, Полка А1, Стеллаж 3).\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_new_storage_location_name)
async def process_new_storage_location(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_plastic_storage_location(name):
        await message.answer(f"✅ Место хранения «{name}» добавлено.")
    else:
        await message.answer(
            f"ℹ️ Место хранения «{name}» уже есть в списке."
        )
    await state.clear()
    await send_storage_locations_overview(message)


@dp.message(F.text == "➖ Удалить место хранения")
async def handle_remove_storage_location_button(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    locations = await fetch_plastic_storage_locations()
    if not locations:
        await message.answer(
            "Список мест хранения пуст. Добавьте места перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_STORAGE_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManagePlasticMaterialStates.waiting_for_storage_location_to_delete
    )
    await message.answer(
        "Выберите место хранения, которое нужно удалить:",
        reply_markup=build_storage_locations_keyboard(locations),
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_storage_location_to_delete)
async def process_remove_storage_location(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await delete_plastic_storage_location(name):
        await message.answer(f"🗑 Место хранения «{name}» удалено.")
    else:
        await message.answer(
            f"ℹ️ Место хранения «{name}» не найдено в списке."
        )
    await state.clear()
    await send_storage_locations_overview(message)


@dp.message(F.text == "➕ Добавить толщину")
async def handle_add_thickness_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    materials = await fetch_plastic_material_types()
    if not materials:
        await message.answer(
            "Сначала добавьте материал, чтобы можно было указать толщины.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManagePlasticMaterialStates.waiting_for_material_name_to_add_thickness
    )
    await message.answer(
        "Выберите материал, для которого нужно добавить толщину:",
        reply_markup=build_materials_keyboard(materials),
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_material_name_to_add_thickness)
async def process_add_thickness_material_selection(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    materials = await fetch_plastic_material_types()
    match = next((item for item in materials if item.lower() == name.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Такой материал не найден. Выберите один из списка.",
            reply_markup=build_materials_keyboard(materials),
        )
        return
    await state.update_data(selected_material=match)
    await state.set_state(ManagePlasticMaterialStates.waiting_for_thickness_value_to_add)
    existing_thicknesses = await fetch_material_thicknesses(match)
    existing_text = format_thicknesses_list(existing_thicknesses)
    await message.answer(
        "Введите толщину в миллиметрах (например, 3 или 3.5).\n"
        "Допустимы значения с точкой или запятой, можно указывать 'мм'.\n\n"
        f"Текущие толщины для «{match}»: {existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_thickness_value_to_add)
async def process_add_thickness_value(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    data = await state.get_data()
    material = data.get("selected_material")
    if not material:
        await state.clear()
        await message.answer(
            "ℹ️ Материал не найден. Попробуйте начать заново.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
        return
    value = parse_thickness_input(message.text or "")
    if value is None:
        await message.answer(
            "⚠️ Не удалось распознать толщину. Укажите число, например 3 или 3.5 мм.",
            reply_markup=CANCEL_KB,
        )
        return
    status = await insert_material_thickness(material, value)
    if status == "material_not_found":
        await message.answer(
            "ℹ️ Материал больше не существует. Попробуйте снова.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
    elif status == "exists":
        await message.answer(
            f"ℹ️ Толщина {format_thickness_value(value)} уже добавлена для «{material}».",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
    else:
        await message.answer(
            f"✅ Толщина {format_thickness_value(value)} добавлена для «{material}».",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
    await state.clear()
    await send_plastic_settings_overview(message)


@dp.message(F.text == "➕ Добавить цвет")
async def handle_add_color_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    materials = await fetch_plastic_material_types()
    if not materials:
        await message.answer(
            "Сначала добавьте материалы, чтобы указать для них цвета.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
        await state.clear()
        return
    await state.set_state(ManagePlasticMaterialStates.waiting_for_material_name_to_add_color)
    await message.answer(
        "Выберите материал, для которого нужно добавить цвет:",
        reply_markup=build_materials_keyboard(materials),
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_material_name_to_add_color)
async def process_add_color_material_selection(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    materials = await fetch_plastic_material_types()
    match = next((item for item in materials if item.lower() == name.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Такой материал не найден. Выберите один из списка.",
            reply_markup=build_materials_keyboard(materials),
        )
        return
    await state.update_data(selected_material=match)
    await state.set_state(ManagePlasticMaterialStates.waiting_for_color_value_to_add)
    existing_colors = await fetch_material_colors(match)
    existing_text = format_colors_list(existing_colors)
    await message.answer(
        "Введите название цвета (например, Белый, Чёрный, Красный).\n\n"
        f"Текущие цвета для «{match}»: {existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_color_value_to_add)
async def process_add_color_value(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    data = await state.get_data()
    material = data.get("selected_material")
    if not material:
        await state.clear()
        await message.answer(
            "ℹ️ Материал не найден. Попробуйте начать заново.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
        return
    color = (message.text or "").strip()
    if not color:
        await message.answer(
            "⚠️ Цвет не может быть пустым. Укажите название цвета.",
            reply_markup=CANCEL_KB,
        )
        return
    status = await insert_material_color(material, color)
    if status == "material_not_found":
        await message.answer(
            "ℹ️ Материал больше не существует. Попробуйте снова.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
    elif status == "exists":
        await message.answer(
            f"ℹ️ Цвет «{color}» уже добавлен для «{material}».",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
    else:
        await message.answer(
            f"✅ Цвет «{color}» добавлен для «{material}».",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
    await state.clear()
    await send_plastic_settings_overview(message)


@dp.message(F.text == "➖ Удалить толщину")
async def handle_remove_thickness_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    materials = await fetch_materials_with_thicknesses()
    materials_with_data = [
        item["name"]
        for item in materials
        if item.get("thicknesses") and len(item["thicknesses"]) > 0
    ]
    if not materials_with_data:
        await message.answer(
            "Пока нет материалов с толщинами для удаления.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManagePlasticMaterialStates.waiting_for_material_name_to_delete_thickness
    )
    await message.answer(
        "Выберите материал, у которого нужно удалить толщину:",
        reply_markup=build_materials_keyboard(materials_with_data),
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_material_name_to_delete_thickness)
async def process_remove_thickness_material_selection(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    materials = await fetch_materials_with_thicknesses()
    match = next(
        (
            item
            for item in materials
            if item["name"].lower() == name.lower()
            and item.get("thicknesses")
            and len(item["thicknesses"]) > 0
        ),
        None,
    )
    if match is None:
        options = [
            item["name"]
            for item in materials
            if item.get("thicknesses") and len(item["thicknesses"]) > 0
        ]
        await message.answer(
            "ℹ️ Материал не найден или у него нет толщин. Выберите из списка.",
            reply_markup=build_materials_keyboard(options),
        )
        return
    await state.update_data(selected_material=match["name"])
    await state.set_state(ManagePlasticMaterialStates.waiting_for_thickness_value_to_delete)
    await message.answer(
        "Выберите толщину, которую нужно удалить:",
        reply_markup=build_thickness_keyboard(match["thicknesses"]),
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_thickness_value_to_delete)
async def process_remove_thickness_value(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    data = await state.get_data()
    material = data.get("selected_material")
    if not material:
        await state.clear()
        await message.answer(
            "ℹ️ Материал не найден. Попробуйте начать заново.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
        return
    value = parse_thickness_input(message.text or "")
    if value is None:
        await message.answer(
            "⚠️ Не удалось распознать толщину. Укажите число, например 3 или 3.5 мм.",
            reply_markup=build_thickness_keyboard(await fetch_material_thicknesses(material)),
        )
        return
    status = await delete_material_thickness(material, value)
    if status == "material_not_found":
        await message.answer(
            "ℹ️ Материал больше не существует. Попробуйте снова.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
    elif status == "deleted":
        await message.answer(
            f"🗑 Толщина {format_thickness_value(value)} удалена у «{material}».",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
    else:
        await message.answer(
            f"ℹ️ Толщина {format_thickness_value(value)} не найдена у «{material}».",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_THICKNESS_KB,
        )
    await state.clear()
    await send_plastic_settings_overview(message)


@dp.message(F.text == "➖ Удалить цвет")
async def handle_remove_color_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    materials = await fetch_materials_with_thicknesses()
    materials_with_colors = [
        item["name"]
        for item in materials
        if item.get("colors") and len(item["colors"]) > 0
    ]
    if not materials_with_colors:
        await message.answer(
            "Пока нет материалов с добавленными цветами для удаления.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManagePlasticMaterialStates.waiting_for_material_name_to_delete_color
    )
    await message.answer(
        "Выберите материал, у которого нужно удалить цвет:",
        reply_markup=build_materials_keyboard(materials_with_colors),
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_material_name_to_delete_color)
async def process_remove_color_material_selection(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    materials = await fetch_materials_with_thicknesses()
    match = next(
        (
            item
            for item in materials
            if item["name"].lower() == name.lower()
            and item.get("colors")
            and len(item["colors"]) > 0
        ),
        None,
    )
    if match is None:
        options = [
            item["name"]
            for item in materials
            if item.get("colors") and len(item["colors"]) > 0
        ]
        await message.answer(
            "ℹ️ Материал не найден или у него нет цветов. Выберите из списка.",
            reply_markup=build_materials_keyboard(options),
        )
        return
    await state.update_data(selected_material=match["name"])
    await state.set_state(ManagePlasticMaterialStates.waiting_for_color_value_to_delete)
    await message.answer(
        "Выберите цвет, который нужно удалить:",
        reply_markup=build_colors_keyboard(match["colors"]),
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_color_value_to_delete)
async def process_remove_color_value(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    data = await state.get_data()
    material = data.get("selected_material")
    if not material:
        await state.clear()
        await message.answer(
            "ℹ️ Материал не найден. Попробуйте начать заново.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
        return
    color = (message.text or "").strip()
    if not color:
        await message.answer(
            "⚠️ Не удалось распознать цвет. Укажите название цвета.",
            reply_markup=build_colors_keyboard(await fetch_material_colors(material)),
        )
        return
    status = await delete_material_color(material, color)
    if status == "material_not_found":
        await message.answer(
            "ℹ️ Материал больше не существует. Попробуйте снова.",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
    elif status == "deleted":
        await message.answer(
            f"🗑 Цвет «{color}» удалён у «{material}».",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
    else:
        await message.answer(
            f"ℹ️ Цвет «{color}» не найден у «{material}».",
            reply_markup=WAREHOUSE_SETTINGS_PLASTIC_COLORS_KB,
        )
    await state.clear()
    await send_plastic_settings_overview(message)


@dp.message(F.text == CANCEL_TEXT)
async def handle_cancel(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_plastic_settings_overview(message)


# === Пользователи (добавление/просмотр) можно вернуть сюда позже ===


async def main() -> None:
    """Запускает поллинг Telegram-бота."""
    bot = Bot(BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
