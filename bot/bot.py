"""Telegram bot with basic user management backed by PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import os
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
                    arrival_date DATE
                )
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


# === Клавиатуры ===
MAIN_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="Тест")],
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
        [KeyboardButton(text="➕ Добавить материал")],
        [KeyboardButton(text="➖ Удалить материал")],
        [KeyboardButton(text="⬅️ Назад к складу")],
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


CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
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


# === Сервисные функции ===
async def send_plastic_settings_overview(message: Message) -> None:
    materials = await fetch_plastic_material_types()
    if materials:
        materials_list = "\n".join(f"• {item}" for item in materials)
        text = (
            "⚙️ Настройки склада → Пластик.\n\n"
            "Доступные материалы для кнопок заполнения:\n"
            f"{materials_list}"
        )
    else:
        text = (
            "⚙️ Настройки склада → Пластик.\n\n"
            "Материалы ещё не добавлены. Используйте кнопки ниже, чтобы начать."
        )
    await message.answer(text, reply_markup=WAREHOUSE_SETTINGS_PLASTIC_KB)


# === Команды ===
@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer("👋 Привет! Выберите действие:", reply_markup=MAIN_MENU_KB)


@dp.message(F.text == "Тест")
async def handle_test(message: Message) -> None:
    await message.answer("тест ок")


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


@dp.message(F.text == "🧱 Пластик")
async def handle_warehouse_settings_plastic(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await send_plastic_settings_overview(message)


@dp.message(F.text == "➕ Добавить материал")
async def handle_add_plastic_material_button(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(ManagePlasticMaterialStates.waiting_for_new_material_name)
    await message.answer(
        "Введите название материала (например, Дибонд, Акрил, ПВХ):",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_new_material_name)
async def process_new_plastic_material(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_plastic_material_type(name):
        await message.answer(f"✅ Материал «{name}» добавлен.")
    else:
        await message.answer(
            f"ℹ️ Материал «{name}» уже есть в списке.",
        )
    await state.clear()
    await send_plastic_settings_overview(message)


@dp.message(F.text == "➖ Удалить материал")
async def handle_remove_plastic_material_button(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(ManagePlasticMaterialStates.waiting_for_material_name_to_delete)
    await message.answer(
        "Введите название материала, который нужно удалить:",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManagePlasticMaterialStates.waiting_for_material_name_to_delete)
async def process_remove_plastic_material(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await delete_plastic_material_type(name):
        await message.answer(f"🗑 Материал «{name}» удалён.")
    else:
        await message.answer(
            f"ℹ️ Материал «{name}» не найден в списке.",
        )
    await state.clear()
    await send_plastic_settings_overview(message)


@dp.message(F.text == "❌ Отмена")
async def handle_cancel(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_plastic_settings_overview(message)


# === Пользователи ===
# ... (остальной код добавления и просмотра пользователей не менялся)


async def main() -> None:
    """Запускает поллинг Telegram-бота."""

    bot = Bot(BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
