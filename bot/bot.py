"""Telegram bot with basic user management backed by PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import os
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

# === Проверка доступа пользователей ===
async def user_has_access(tg_id: int) -> bool:
    if db_pool is None:
        return False
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM users WHERE tg_id = $1", tg_id)
    return row is not None

async def user_is_admin(tg_id: int) -> bool:
    if db_pool is None:
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
    if state:
        await state.clear()
    await message.answer("🚫 У вас недостаточно прав для управления настройками.", reply_markup=MAIN_MENU_KB)
    return False

# === Middleware ===
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
        if user_id is None or await user_has_access(user_id):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("🚫 У вас нет доступа к этому боту. Обратитесь к администратору.")
        return None

# === Инициализация базы данных ===
async def init_database(max_attempts: int = 5, retry_delay: float = 2.0) -> None:
    """Initialise database connection pool with retry logic.

    When the application starts inside Docker, the PostgreSQL container might
    need a couple of seconds to accept incoming connections. Without retries
    ``asyncpg.create_pool`` raises an exception and the bot stops before
    polling starts. To make the startup robust we retry the connection several
    times before propagating the error.
    """

    global db_pool

    if db_pool is not None:
        return

    for attempt in range(1, max_attempts + 1):
        try:
            db_pool = await asyncpg.create_pool(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASS,
                database=DB_NAME,
            )
            break
        except Exception as exc:  # pragma: no cover - logged and re-raised
            logging.warning(
                "Failed to connect to PostgreSQL (attempt %s/%s): %s",
                attempt,
                max_attempts,
                exc,
            )
            if attempt == max_attempts:
                raise
            await asyncio.sleep(retry_delay)

    assert db_pool is not None  # for type-checkers

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    tg_id BIGINT UNIQUE NOT NULL,
                    username TEXT NOT NULL,
                    position TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plastic_material_types (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plastic_material_thicknesses (
                    id SERIAL PRIMARY KEY,
                    material_id INTEGER NOT NULL REFERENCES plastic_material_types(id) ON DELETE CASCADE,
                    thickness NUMERIC(10, 2) NOT NULL,
                    UNIQUE(material_id, thickness)
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS plastic_material_colors (
                    id SERIAL PRIMARY KEY,
                    material_id INTEGER NOT NULL REFERENCES plastic_material_types(id) ON DELETE CASCADE,
                    color TEXT NOT NULL,
                    UNIQUE(material_id, color)
                )
            """)

            await conn.execute("""
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
            """)

            await conn.execute("""
                INSERT INTO users (tg_id, username, position, role)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tg_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    position = EXCLUDED.position,
                    role = EXCLUDED.role
            """, 37352491, "DooMka", "Администратор", "администратор с полными правами и доступом")

async def close_database() -> None:
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None

# === Dispatcher и FSM ===
async def on_startup() -> None:
    await init_database()


async def on_shutdown() -> None:
    await close_database()


dp = Dispatcher()
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)
dp.message.outer_middleware(AccessControlMiddleware())

class AddUserStates(StatesGroup):
    waiting_for_tg_id = State()
    waiting_for_username = State()
    waiting_for_position = State()
    waiting_for_role = State()

# === Клавиатуры ===
MAIN_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="⚙️ Настройки")], [KeyboardButton(text="🏢 Склад")]],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_PLASTIC_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить материал")],
        [KeyboardButton(text="➕ Добавить толщину")],
        [KeyboardButton(text="➕ Добавить цвет")],
        [KeyboardButton(text="➖ Удалить материал")],
        [KeyboardButton(text="➖ Удалить толщину")],
        [KeyboardButton(text="➖ Удалить цвет")],
        [KeyboardButton(text="⬅️ Назад к складу")],
    ],
    resize_keyboard=True,
)

CANCEL_TEXT = "❌ Отмена"
CANCEL_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=CANCEL_TEXT)]], resize_keyboard=True)

# === Работа с БД: выборки и вставки ===
async def fetch_materials_with_attributes() -> list[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database not initialized")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                p.name,
                COALESCE((SELECT ARRAY_AGG(t.thickness ORDER BY t.thickness) FROM plastic_material_thicknesses t WHERE t.material_id=p.id), '{}') AS thicknesses,
                COALESCE((SELECT ARRAY_AGG(c.color ORDER BY c.color) FROM plastic_material_colors c WHERE c.material_id=p.id), '{}') AS colors
            FROM plastic_material_types p
            ORDER BY LOWER(p.name)
        """)
    return [dict(row) for row in rows]

def format_thicknesses_list(thicknesses: list[Decimal]) -> str:
    if not thicknesses:
        return "—"
    return ", ".join(f"{t} мм" for t in thicknesses)

def format_colors_list(colors: list[str]) -> str:
    if not colors:
        return "—"
    return ", ".join(colors)

async def send_plastic_settings_overview(message: Message) -> None:
    materials = await fetch_materials_with_attributes()
    if materials:
        lines = []
        for m in materials:
            lines.append(
                f"• {m['name']}\n  Толщины: {format_thicknesses_list(m['thicknesses'])}\n  Цвета: {format_colors_list(m['colors'])}"
            )
        text = "⚙️ Настройки склада → Пластик.\n\n" + "\n\n".join(lines)
    else:
        text = "⚙️ Настройки склада → Пластик.\n\nМатериалы ещё не добавлены."
    await message.answer(text, reply_markup=WAREHOUSE_SETTINGS_PLASTIC_KB)

# === Команды ===
@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer("👋 Привет! Выберите действие:", reply_markup=MAIN_MENU_KB)

@dp.message(F.text == "⚙️ Настройки")
async def handle_settings(message: Message) -> None:
    if await ensure_admin_access(message):
        await message.answer("⚙️ Настройки склада. Выберите действие:", reply_markup=WAREHOUSE_SETTINGS_PLASTIC_KB)

@dp.message(F.text == "🏢 Склад")
async def handle_warehouse(message: Message) -> None:
    await message.answer("🏢 Раздел «Склад» находится в разработке.", reply_markup=MAIN_MENU_KB)

@dp.message(F.text == CANCEL_TEXT)
async def handle_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await send_plastic_settings_overview(message)

# === Запуск ===
async def main() -> None:
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
