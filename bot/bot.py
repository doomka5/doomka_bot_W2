"""Telegram bot with basic user management backed by PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from io import BytesIO
from pathlib import Path
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

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
    BufferedInputFile,
)
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

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

def _resolve_update_script_path() -> Path:
    env_path = os.getenv("UPDATE_SCRIPT_PATH")
    if env_path:
        return Path(env_path)

    default_path = Path("/share/3D/doomka_bot_W2/update.sh")
    if default_path.exists():
        return default_path

    return Path(__file__).resolve().parent.parent / "update.sh"


UPDATE_SCRIPT_PATH = _resolve_update_script_path()

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
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS written_off_plastics (
                    id SERIAL PRIMARY KEY,
                    source_id INTEGER,
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
                    arrival_at TIMESTAMPTZ,
                    project TEXT,
                    written_off_by_id BIGINT,
                    written_off_by_name TEXT,
                    written_off_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                ALTER TABLE written_off_plastics
                ADD COLUMN IF NOT EXISTS written_off_at TIMESTAMPTZ DEFAULT timezone('utc', now())
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
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS film_manufacturers (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS led_module_manufacturers (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS led_module_storage_locations (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS led_module_series (
                    id SERIAL PRIMARY KEY,
                    manufacturer_id INTEGER NOT NULL REFERENCES led_module_manufacturers(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now()),
                    UNIQUE(manufacturer_id, name)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS led_module_colors (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS led_module_power_options (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS led_module_voltage_options (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS led_module_lens_counts (
                    id SERIAL PRIMARY KEY,
                    value INTEGER UNIQUE NOT NULL CHECK (value > 0),
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS generated_led_modules (
                    id SERIAL PRIMARY KEY,
                    article TEXT UNIQUE NOT NULL,
                    manufacturer_id INTEGER NOT NULL REFERENCES led_module_manufacturers(id) ON DELETE RESTRICT,
                    series_id INTEGER NOT NULL REFERENCES led_module_series(id) ON DELETE RESTRICT,
                    color_id INTEGER NOT NULL REFERENCES led_module_colors(id) ON DELETE RESTRICT,
                    lens_count_id INTEGER NOT NULL REFERENCES led_module_lens_counts(id) ON DELETE RESTRICT,
                    power_option_id INTEGER NOT NULL REFERENCES led_module_power_options(id) ON DELETE RESTRICT,
                    voltage_option_id INTEGER NOT NULL REFERENCES led_module_voltage_options(id) ON DELETE RESTRICT,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS warehouse_led_modules (
                    id SERIAL PRIMARY KEY,
                    led_module_id INTEGER NOT NULL REFERENCES generated_led_modules(id) ON DELETE RESTRICT,
                    article TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    added_by_id BIGINT,
                    added_by_name TEXT,
                    added_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                ALTER TABLE warehouse_led_modules
                DROP CONSTRAINT IF EXISTS warehouse_led_modules_quantity_check
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS written_off_led_modules (
                    id SERIAL PRIMARY KEY,
                    led_module_id INTEGER NOT NULL REFERENCES generated_led_modules(id) ON DELETE RESTRICT,
                    article TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0),
                    project TEXT,
                    written_off_by_id BIGINT,
                    written_off_by_name TEXT,
                    written_off_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS led_strip_manufacturers (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS power_supply_manufacturers (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS film_series (
                    id SERIAL PRIMARY KEY,
                    manufacturer_id INTEGER NOT NULL REFERENCES film_manufacturers(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now()),
                    UNIQUE(manufacturer_id, name)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS film_storage_locations (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_types (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS clients (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    phone TEXT,
                    contact_person TEXT,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS client_addresses (
                    id SERIAL PRIMARY KEY,
                    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                    address TEXT,
                    google_maps_link TEXT,
                    created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                CREATE SEQUENCE IF NOT EXISTS order_number_seq START 1
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    order_number INTEGER NOT NULL UNIQUE DEFAULT nextval('order_number_seq'),
                    client_id INTEGER NOT NULL REFERENCES clients(id),
                    client_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    folder_path TEXT NOT NULL,
                    due_date DATE NOT NULL,
                    is_urgent BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
                    created_by_id BIGINT,
                    created_by_name TEXT
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS warehouse_films (
                    id SERIAL PRIMARY KEY,
                    article TEXT NOT NULL,
                    manufacturer TEXT,
                    series TEXT,
                    color_code TEXT,
                    color TEXT,
                    width NUMERIC(10, 2),
                    length NUMERIC(10, 2),
                    warehouse TEXT,
                    comment TEXT,
                    employee_id BIGINT,
                    employee_nick TEXT,
                    recorded_at TIMESTAMPTZ
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS written_off_films (
                    id SERIAL PRIMARY KEY,
                    source_id INTEGER,
                    article TEXT NOT NULL,
                    manufacturer TEXT,
                    series TEXT,
                    color_code TEXT,
                    color TEXT,
                    width NUMERIC(10, 2),
                    length NUMERIC(10, 2),
                    warehouse TEXT,
                    comment TEXT,
                    employee_id BIGINT,
                    employee_nick TEXT,
                    recorded_at TIMESTAMPTZ,
                    project TEXT,
                    written_off_by_id BIGINT,
                    written_off_by_name TEXT,
                    written_off_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
                )
                """
            )
            await conn.execute(
                """
                ALTER TABLE written_off_films
                ADD COLUMN IF NOT EXISTS written_off_at TIMESTAMPTZ DEFAULT timezone('utc', now())
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
    waiting_for_created_at = State()


class ManageOrderTypeStates(StatesGroup):
    waiting_for_new_type_name = State()


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


class ManageFilmManufacturerStates(StatesGroup):
    waiting_for_new_manufacturer_name = State()
    waiting_for_manufacturer_name_to_delete = State()


class ManageFilmSeriesStates(StatesGroup):
    waiting_for_manufacturer_for_new_series = State()
    waiting_for_new_series_name = State()
    waiting_for_manufacturer_for_series_deletion = State()
    waiting_for_series_name_to_delete = State()


class ManageFilmStorageStates(StatesGroup):
    waiting_for_new_storage_location_name = State()
    waiting_for_storage_location_to_delete = State()


class ManageLedModuleManufacturerStates(StatesGroup):
    waiting_for_new_manufacturer_name = State()
    waiting_for_manufacturer_name_to_delete = State()


class ManageLedModuleSeriesStates(StatesGroup):
    waiting_for_manufacturer_for_new_series = State()
    waiting_for_new_series_name = State()
    waiting_for_manufacturer_for_series_deletion = State()
    waiting_for_series_name_to_delete = State()


class ManageLedModuleStorageStates(StatesGroup):
    waiting_for_new_storage_location_name = State()
    waiting_for_storage_location_to_delete = State()


class ManageLedModuleLensStates(StatesGroup):
    waiting_for_new_lens_count = State()
    waiting_for_lens_count_to_delete = State()


class ManageLedModuleColorStates(StatesGroup):
    waiting_for_new_color_name = State()
    waiting_for_color_name_to_delete = State()


class ManageLedModulePowerStates(StatesGroup):
    waiting_for_new_power_value = State()
    waiting_for_power_value_to_delete = State()


class ManageLedModuleVoltageStates(StatesGroup):
    waiting_for_new_voltage_value = State()
    waiting_for_voltage_value_to_delete = State()


class GenerateLedModuleStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_manufacturer = State()
    waiting_for_series = State()
    waiting_for_color = State()
    waiting_for_lens_count = State()
    waiting_for_power = State()
    waiting_for_voltage = State()


class ManageLedStripManufacturerStates(StatesGroup):
    waiting_for_new_manufacturer_name = State()
    waiting_for_manufacturer_name_to_delete = State()


class ManagePowerSupplyManufacturerStates(StatesGroup):
    waiting_for_new_manufacturer_name = State()
    waiting_for_manufacturer_name_to_delete = State()


class AddWarehouseFilmStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_manufacturer = State()
    waiting_for_series = State()
    waiting_for_color_code = State()
    waiting_for_color = State()
    waiting_for_width = State()
    waiting_for_length = State()
    waiting_for_storage = State()
    waiting_for_comment = State()


class CommentWarehouseFilmStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_comment = State()


class MoveWarehouseFilmStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_new_location = State()


class WriteOffWarehouseFilmStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_project = State()


class SearchWarehouseFilmStates(StatesGroup):
    choosing_mode = State()
    waiting_for_article = State()
    waiting_for_number = State()
    waiting_for_color = State()


class AddWarehousePlasticStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_material = State()
    waiting_for_thickness = State()
    waiting_for_color = State()
    waiting_for_length = State()
    waiting_for_width = State()
    waiting_for_storage = State()
    waiting_for_comment = State()


class AddWarehousePlasticBatchStates(StatesGroup):
    waiting_for_quantity = State()
    waiting_for_material = State()
    waiting_for_thickness = State()
    waiting_for_color = State()
    waiting_for_length = State()
    waiting_for_width = State()
    waiting_for_storage = State()
    waiting_for_comment = State()


class AddWarehouseLedModuleStates(StatesGroup):
    waiting_for_module = State()
    waiting_for_quantity = State()


class WriteOffWarehouseLedModuleStates(StatesGroup):
    waiting_for_module = State()
    waiting_for_quantity = State()
    waiting_for_project = State()


class SearchWarehousePlasticStates(StatesGroup):
    choosing_mode = State()
    waiting_for_article = State()
    waiting_for_material = State()
    waiting_for_thickness = State()
    waiting_for_color = State()
    waiting_for_min_length = State()
    waiting_for_min_width = State()


class AddClientStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_contact_person = State()
    waiting_for_address = State()
    waiting_for_map_link = State()


class SearchClientStates(StatesGroup):
    waiting_for_query = State()


class CreateOrderStates(StatesGroup):
    waiting_for_client_query = State()
    waiting_for_client_selection = State()
    waiting_for_order_name = State()
    waiting_for_order_type = State()
    waiting_for_folder_path = State()
    waiting_for_due_date = State()
    waiting_for_urgency = State()


class CommentWarehousePlasticStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_comment = State()


class MoveWarehousePlasticStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_new_location = State()


class WriteOffWarehousePlasticStates(StatesGroup):
    waiting_for_article = State()
    waiting_for_project = State()


# === Клавиатуры ===
MAIN_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🏢 Склад"),
            KeyboardButton(text="⚙️ Настройки"),
        ],
        [
            KeyboardButton(text="Клиенты"),
            KeyboardButton(text="Заказы"),
        ],
    ],
    resize_keyboard=True,
)

ORDERS_NEW_ORDER_TEXT = "🆕 Новый заказ"
ORDERS_IN_PROGRESS_TEXT = "📋 Заказы в работе"
ORDERS_SETTINGS_TEXT = "⚙️ Настройки заказов"
ORDERS_SETTINGS_ORDER_TYPE_TEXT = "🗂️ Тип заказа"
ORDERS_SETTINGS_BACK_TEXT = "⬅️ Назад к заказам"
ORDER_TYPE_ADD_TEXT = "➕ Добавить тип заказа"
ORDER_TYPE_DELETE_TEXT = "➖ Удалить тип заказа"
ORDER_TYPE_BACK_TEXT = "⬅️ Назад к настройкам заказов"

ORDERS_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=ORDERS_NEW_ORDER_TEXT)],
        [KeyboardButton(text=ORDERS_IN_PROGRESS_TEXT)],
        [KeyboardButton(text=ORDERS_SETTINGS_TEXT)],
        [KeyboardButton(text="⬅️ Главное меню")],
    ],
    resize_keyboard=True,
)

ORDERS_SETTINGS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=ORDERS_SETTINGS_ORDER_TYPE_TEXT)],
        [KeyboardButton(text=ORDERS_SETTINGS_BACK_TEXT)],
    ],
    resize_keyboard=True,
)

ORDERS_ORDER_TYPE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=ORDER_TYPE_ADD_TEXT)],
        [KeyboardButton(text=ORDER_TYPE_DELETE_TEXT)],
        [KeyboardButton(text=ORDER_TYPE_BACK_TEXT)],
    ],
    resize_keyboard=True,
)

CLIENTS_ADD_CLIENT_TEXT = "➕ Добавить клиента"
CLIENTS_SEARCH_CLIENT_TEXT = "🔍 Поиск клиента"

CLIENTS_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=CLIENTS_ADD_CLIENT_TEXT)],
        [KeyboardButton(text=CLIENTS_SEARCH_CLIENT_TEXT)],
        [KeyboardButton(text="⬅️ Главное меню")],
    ],
    resize_keyboard=True,
)

SETTINGS_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👥 Пользователи")],
        [KeyboardButton(text="🔄 Перезагрузить")],
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

WAREHOUSE_ELECTRICS_TEXT = "⚡ Электрика"
WAREHOUSE_ELECTRICS_LED_STRIPS_TEXT = "💡 Led лента"
WAREHOUSE_ELECTRICS_LED_MODULES_TEXT = "🧩 Led модули"
WAREHOUSE_ELECTRICS_POWER_SUPPLIES_TEXT = "🔌 Блоки питания"
WAREHOUSE_LED_MODULES_ADD_TEXT = "➕ Добавить Led модули"
WAREHOUSE_LED_MODULES_STOCK_TEXT = "📦 Остаток Led модулей на складе"
WAREHOUSE_LED_MODULES_WRITE_OFF_TEXT = "➖ Списать Led модули"
WAREHOUSE_LED_MODULES_BACK_TO_ELECTRICS_TEXT = "⬅️ Назад к разделу «Электрика»"

WAREHOUSE_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧱 Пластики")],
        [KeyboardButton(text="🎞️ Пленки")],
        [KeyboardButton(text=WAREHOUSE_ELECTRICS_TEXT)],
        [KeyboardButton(text="⚙️ Настройки склада")],
        [KeyboardButton(text="⬅️ Главное меню")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_ELECTRICS_TEXT = "⚡ Электрика ⚙️"
WAREHOUSE_SETTINGS_ELECTRICS_LED_STRIPS_TEXT = "💡 Led лента ⚙️"
WAREHOUSE_SETTINGS_ELECTRICS_LED_MODULES_TEXT = "🧩 Led модули ⚙️"
LED_MODULES_BASE_MENU_TEXT = "Led модули baza"
WAREHOUSE_SETTINGS_ELECTRICS_POWER_SUPPLIES_TEXT = "🔌 Блоки питания ⚙️"
WAREHOUSE_SETTINGS_BACK_TO_ELECTRICS_TEXT = "⬅️ Назад к электрике"

WAREHOUSE_SETTINGS_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🧱 Пластик")],
        [KeyboardButton(text="🎞️ Пленки ⚙️")],
        [KeyboardButton(text=WAREHOUSE_SETTINGS_ELECTRICS_TEXT)],
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

WAREHOUSE_SETTINGS_FILM_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏭 Производитель")],
        [KeyboardButton(text="🎬 Серия")],
        [KeyboardButton(text="🏬 Склад")],
        [KeyboardButton(text="⬅️ Назад к складу")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_ELECTRICS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=WAREHOUSE_SETTINGS_ELECTRICS_LED_STRIPS_TEXT)],
        [KeyboardButton(text=WAREHOUSE_SETTINGS_ELECTRICS_LED_MODULES_TEXT)],
        [KeyboardButton(text=WAREHOUSE_SETTINGS_ELECTRICS_POWER_SUPPLIES_TEXT)],
        [KeyboardButton(text=WAREHOUSE_SETTINGS_BACK_TO_ELECTRICS_TEXT)],
        [KeyboardButton(text="⬅️ Назад к складу")],
    ],
    resize_keyboard=True,
)

LED_MODULES_MANUFACTURERS_MENU_TEXT = "🏭 Производитель Led модулей"
LED_MODULES_SERIES_MENU_TEXT = "🎬 Серия Led модулей"
LED_MODULES_STORAGE_MENU_TEXT = "🏬 Места хранения Led модулей"
LED_MODULES_POWER_MENU_TEXT = "⚡ Мощность модулей"
LED_MODULES_LENS_MENU_TEXT = "🔢 Количество линз"
LED_MODULES_COLORS_MENU_TEXT = "🎨 Цвет модулей"
LED_MODULES_VOLTAGE_MENU_TEXT = "🔌 Напряжение модулей"
LED_MODULES_BACK_TEXT = "⬅️ Назад к Led модулям"
LED_MODULES_GENERATE_TEXT = "Сгенерировать Led модуль"
LED_MODULES_DELETE_TEXT = "Удалить Led модуль"
LED_MODULES_ADD_MANUFACTURER_TEXT = "➕ Добавить производителя Led модулей"
LED_MODULES_REMOVE_MANUFACTURER_TEXT = "➖ Удалить производителя Led модулей"
LED_MODULES_ADD_SERIES_TEXT = "➕ Добавить серию Led модулей"
LED_MODULES_REMOVE_SERIES_TEXT = "➖ Удалить серию Led модулей"
LED_MODULES_ADD_STORAGE_TEXT = "➕ Добавить место хранения Led модулей"
LED_MODULES_REMOVE_STORAGE_TEXT = "➖ Удалить место хранения Led модулей"
LED_MODULES_ADD_POWER_TEXT = "➕ Добавить мощность модулей"
LED_MODULES_REMOVE_POWER_TEXT = "➖ Удалить мощность модулей"
LED_MODULES_ADD_VOLTAGE_TEXT = "➕ Добавить напряжение модулей"
LED_MODULES_REMOVE_VOLTAGE_TEXT = "➖ Удалить напряжение модулей"
LED_MODULES_ADD_LENS_COUNT_TEXT = "➕ Добавить количество линз"
LED_MODULES_REMOVE_LENS_COUNT_TEXT = "➖ Удалить количество линз"
LED_MODULES_ADD_COLOR_TEXT = "➕ Добавить цвет модулей"
LED_MODULES_REMOVE_COLOR_TEXT = "➖ Удалить цвет модулей"
LED_STRIPS_ADD_MANUFACTURER_TEXT = "➕ Добавить производителя Led ленты"
LED_STRIPS_REMOVE_MANUFACTURER_TEXT = "➖ Удалить производителя Led ленты"
POWER_SUPPLIES_ADD_MANUFACTURER_TEXT = "➕ Добавить производителя блоков питания"
POWER_SUPPLIES_REMOVE_MANUFACTURER_TEXT = "➖ Удалить производителя блоков питания"

WAREHOUSE_SETTINGS_LED_MODULES_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_MODULES_MANUFACTURERS_MENU_TEXT)],
        [KeyboardButton(text=LED_MODULES_SERIES_MENU_TEXT)],
        [KeyboardButton(text=LED_MODULES_STORAGE_MENU_TEXT)],
        [KeyboardButton(text=LED_MODULES_BASE_MENU_TEXT)],
        [KeyboardButton(text=LED_MODULES_COLORS_MENU_TEXT)],
        [KeyboardButton(text=LED_MODULES_POWER_MENU_TEXT)],
        [KeyboardButton(text=LED_MODULES_VOLTAGE_MENU_TEXT)],
        [KeyboardButton(text=LED_MODULES_LENS_MENU_TEXT)],
        [KeyboardButton(text=WAREHOUSE_SETTINGS_BACK_TO_ELECTRICS_TEXT)],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_LED_MODULES_BASE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_MODULES_GENERATE_TEXT)],
        [KeyboardButton(text=LED_MODULES_DELETE_TEXT)],
        [KeyboardButton(text=LED_MODULES_BACK_TEXT)],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_LED_MODULES_MANUFACTURERS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_MODULES_ADD_MANUFACTURER_TEXT)],
        [KeyboardButton(text=LED_MODULES_REMOVE_MANUFACTURER_TEXT)],
        [KeyboardButton(text=LED_MODULES_BACK_TEXT)],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_LED_MODULES_SERIES_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_MODULES_ADD_SERIES_TEXT)],
        [KeyboardButton(text=LED_MODULES_REMOVE_SERIES_TEXT)],
        [KeyboardButton(text=LED_MODULES_BACK_TEXT)],
    ],
    resize_keyboard=True,
)


WAREHOUSE_SETTINGS_LED_MODULES_STORAGE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_MODULES_ADD_STORAGE_TEXT)],
        [KeyboardButton(text=LED_MODULES_REMOVE_STORAGE_TEXT)],
        [KeyboardButton(text=LED_MODULES_BACK_TEXT)],
    ],
    resize_keyboard=True,
)


WAREHOUSE_SETTINGS_LED_MODULES_COLORS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_MODULES_ADD_COLOR_TEXT)],
        [KeyboardButton(text=LED_MODULES_REMOVE_COLOR_TEXT)],
        [KeyboardButton(text=LED_MODULES_BACK_TEXT)],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_LED_MODULES_POWER_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_MODULES_ADD_POWER_TEXT)],
        [KeyboardButton(text=LED_MODULES_REMOVE_POWER_TEXT)],
        [KeyboardButton(text=LED_MODULES_BACK_TEXT)],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_LED_MODULES_VOLTAGE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_MODULES_ADD_VOLTAGE_TEXT)],
        [KeyboardButton(text=LED_MODULES_REMOVE_VOLTAGE_TEXT)],
        [KeyboardButton(text=LED_MODULES_BACK_TEXT)],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_LED_MODULES_LENS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_MODULES_ADD_LENS_COUNT_TEXT)],
        [KeyboardButton(text=LED_MODULES_REMOVE_LENS_COUNT_TEXT)],
        [KeyboardButton(text=LED_MODULES_BACK_TEXT)],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_LED_STRIPS_MANUFACTURERS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LED_STRIPS_ADD_MANUFACTURER_TEXT)],
        [KeyboardButton(text=LED_STRIPS_REMOVE_MANUFACTURER_TEXT)],
        [KeyboardButton(text=WAREHOUSE_SETTINGS_BACK_TO_ELECTRICS_TEXT)],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_POWER_SUPPLIES_MANUFACTURERS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=POWER_SUPPLIES_ADD_MANUFACTURER_TEXT)],
        [KeyboardButton(text=POWER_SUPPLIES_REMOVE_MANUFACTURER_TEXT)],
        [KeyboardButton(text=WAREHOUSE_SETTINGS_BACK_TO_ELECTRICS_TEXT)],
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

WAREHOUSE_SETTINGS_FILM_MANUFACTURERS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить производителя")],
        [KeyboardButton(text="➖ Удалить производителя")],
        [KeyboardButton(text="⬅️ Назад к пленкам")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_FILM_SERIES_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить серию")],
        [KeyboardButton(text="➖ Удалить серию")],
        [KeyboardButton(text="⬅️ Назад к пленкам")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_SETTINGS_FILM_STORAGE_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить место хранения пленки")],
        [KeyboardButton(text="➖ Удалить место хранения пленки")],
        [KeyboardButton(text="⬅️ Назад к пленкам")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_FILMS_ADD_TEXT = "➕ Добавить пленку"
WAREHOUSE_FILMS_WRITE_OFF_TEXT = "➖ Списать пленку"
WAREHOUSE_FILMS_COMMENT_TEXT = "💬 Комментировать пленку"
WAREHOUSE_FILMS_MOVE_TEXT = "🔁 Переместить пленку"
WAREHOUSE_FILMS_SEARCH_TEXT = "🔍 Найти пленку"
WAREHOUSE_FILMS_EXPORT_TEXT = "📤 Экспорт пленок"

WAREHOUSE_FILMS_SEARCH_BY_ARTICLE_TEXT = "По артикулу"
WAREHOUSE_FILMS_SEARCH_BY_NUMBER_TEXT = "По номеру"
WAREHOUSE_FILMS_SEARCH_BY_COLOR_TEXT = "По цвету"
WAREHOUSE_FILMS_SEARCH_BACK_TEXT = "⬅️ Назад к пленкам"
FILM_SEARCH_RESULTS_LIMIT = 15

WAREHOUSE_FILMS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text=WAREHOUSE_FILMS_ADD_TEXT),
            KeyboardButton(text=WAREHOUSE_FILMS_WRITE_OFF_TEXT),
        ],
        [
            KeyboardButton(text=WAREHOUSE_FILMS_COMMENT_TEXT),
            KeyboardButton(text=WAREHOUSE_FILMS_MOVE_TEXT),
        ],
        [
            KeyboardButton(text=WAREHOUSE_FILMS_SEARCH_TEXT),
            KeyboardButton(text=WAREHOUSE_FILMS_EXPORT_TEXT),
        ],
        [KeyboardButton(text="⬅️ Назад к складу")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_FILMS_SEARCH_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=WAREHOUSE_FILMS_SEARCH_BY_ARTICLE_TEXT)],
        [KeyboardButton(text=WAREHOUSE_FILMS_SEARCH_BY_NUMBER_TEXT)],
        [KeyboardButton(text=WAREHOUSE_FILMS_SEARCH_BY_COLOR_TEXT)],
        [KeyboardButton(text=WAREHOUSE_FILMS_SEARCH_BACK_TEXT)],
    ],
    resize_keyboard=True,
)

WAREHOUSE_PLASTICS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="++добавить пачку")],
        [KeyboardButton(text="➖ Списать"), KeyboardButton(text="💬 Комментировать")],
        [KeyboardButton(text="🔁 Переместить"), KeyboardButton(text="🔍 Найти")],
        [KeyboardButton(text="📤 Экспорт")],
        [KeyboardButton(text="⬅️ Назад к складу")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_ELECTRICS_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=WAREHOUSE_ELECTRICS_LED_STRIPS_TEXT)],
        [KeyboardButton(text=WAREHOUSE_ELECTRICS_LED_MODULES_TEXT)],
        [KeyboardButton(text=WAREHOUSE_ELECTRICS_POWER_SUPPLIES_TEXT)],
        [KeyboardButton(text="⬅️ Назад к складу")],
    ],
    resize_keyboard=True,
)

WAREHOUSE_LED_MODULES_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=WAREHOUSE_LED_MODULES_ADD_TEXT)],
        [KeyboardButton(text=WAREHOUSE_LED_MODULES_STOCK_TEXT)],
        [KeyboardButton(text=WAREHOUSE_LED_MODULES_WRITE_OFF_TEXT)],
        [KeyboardButton(text=WAREHOUSE_LED_MODULES_BACK_TO_ELECTRICS_TEXT)],
    ],
    resize_keyboard=True,
)

SEARCH_BY_ARTICLE_TEXT = "🔢 Поиск по артикулу"
ADVANCED_SEARCH_TEXT = "🧭 Расширенный поиск"
BACK_TO_PLASTICS_MENU_TEXT = "⬅️ Назад к меню пластика"

WAREHOUSE_PLASTICS_SEARCH_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=SEARCH_BY_ARTICLE_TEXT)],
        [KeyboardButton(text=ADVANCED_SEARCH_TEXT)],
        [KeyboardButton(text=BACK_TO_PLASTICS_MENU_TEXT)],
    ],
    resize_keyboard=True,
)

ADVANCED_SEARCH_SKIP_MATERIAL_TEXT = "➡️ Далее"
ADVANCED_SEARCH_ALL_THICKNESSES_TEXT = "📏 Все толщины"
ADVANCED_SEARCH_ALL_COLORS_TEXT = "🎨 Все цвета"

CANCEL_TEXT = "❌ Отмена"
SKIP_TEXT = "Пропустить"

def build_article_input_keyboard(
    suggested_article: Optional[str] = None,
) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = []
    if suggested_article:
        keyboard.append([KeyboardButton(text=suggested_article)])
    keyboard.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


CANCEL_KB = build_article_input_keyboard()

SKIP_OR_CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=SKIP_TEXT)], [KeyboardButton(text=CANCEL_TEXT)]],
    resize_keyboard=True,
)

ORDER_URGENCY_YES_TEXT = "Да"
ORDER_URGENCY_NO_TEXT = "Нет"

ORDER_URGENCY_KB = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text=ORDER_URGENCY_YES_TEXT),
            KeyboardButton(text=ORDER_URGENCY_NO_TEXT),
        ],
        [KeyboardButton(text=CANCEL_TEXT)],
    ],
    resize_keyboard=True,
)


async def _process_cancel_if_requested(message: Message, state: FSMContext) -> bool:
    if (message.text or "").strip() != CANCEL_TEXT:
        return False
    await handle_cancel(message, state)
    return True


async def _cancel_add_user_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Добавление пользователя отменено.", reply_markup=USERS_MENU_KB
    )


async def _cancel_add_client_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Добавление клиента отменено.", reply_markup=CLIENTS_MENU_KB
    )


async def _cancel_search_client_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Поиск клиента отменён.", reply_markup=CLIENTS_MENU_KB
    )


async def _cancel_create_order_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Создание заказа отменено.", reply_markup=ORDERS_MENU_KB
    )


async def _cancel_add_plastic_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Добавление пластика отменено.", reply_markup=WAREHOUSE_PLASTICS_KB
    )


async def _cancel_add_plastic_batch_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Добавление пачки пластика отменено.", reply_markup=WAREHOUSE_PLASTICS_KB
    )


async def _cancel_add_led_module_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Добавление Led модуля отменено.", reply_markup=WAREHOUSE_LED_MODULES_KB
    )


async def _cancel_write_off_led_module_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Списание Led модулей отменено.", reply_markup=WAREHOUSE_LED_MODULES_KB
    )


async def _cancel_search_plastic_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Поиск отменён.", reply_markup=WAREHOUSE_PLASTICS_KB)


async def _cancel_search_film_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Поиск отменён.", reply_markup=WAREHOUSE_FILMS_KB)


async def _cancel_comment_plastic_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Изменение комментария отменено.", reply_markup=WAREHOUSE_PLASTICS_KB
    )


async def _cancel_move_plastic_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Перемещение отменено.", reply_markup=WAREHOUSE_PLASTICS_KB)


async def _cancel_write_off_plastic_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Списание отменено.", reply_markup=WAREHOUSE_PLASTICS_KB)


async def _cancel_add_film_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Добавление пленки отменено.", reply_markup=WAREHOUSE_FILMS_KB
    )


async def _cancel_comment_film_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Изменение комментария отменено.", reply_markup=WAREHOUSE_FILMS_KB
    )


async def _cancel_move_film_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Перемещение пленки отменено.", reply_markup=WAREHOUSE_FILMS_KB
    )


async def _cancel_write_off_film_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("❌ Списание пленки отменено.", reply_markup=WAREHOUSE_FILMS_KB)


async def _cancel_generate_led_module_flow(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "❌ Генерация Led модуля отменена.",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_BASE_KB,
    )


# === Работа с БД ===
async def upsert_user_in_db(
    tg_id: int,
    username: str,
    position: str,
    role: str,
    created_at: Optional[datetime] = None,
) -> None:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (tg_id, username, position, role, created_at)
            VALUES ($1, $2, $3, $4, COALESCE($5, timezone('utc', now())))
            ON CONFLICT (tg_id) DO UPDATE
            SET username = EXCLUDED.username,
                position = EXCLUDED.position,
                role = EXCLUDED.role,
                created_at = CASE
                    WHEN $5 IS NULL THEN users.created_at
                    ELSE EXCLUDED.created_at
                END
            """,
            tg_id,
            username,
            position,
            role,
            created_at,
        )


async def fetch_all_users_from_db() -> list[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tg_id, username, position, role, created_at
            FROM users
            ORDER BY created_at DESC NULLS LAST, id DESC
            """
        )
    return [dict(row) for row in rows]


async def create_client_in_db(
    name: str,
    phone: Optional[str] = None,
    contact_person: Optional[str] = None,
) -> int:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO clients (name, phone, contact_person)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            name,
            phone,
            contact_person,
        )
    return int(row["id"])


async def add_client_address_in_db(
    client_id: int,
    address: Optional[str] = None,
    google_maps_link: Optional[str] = None,
) -> int:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO client_addresses (client_id, address, google_maps_link)
            VALUES ($1, $2, $3)
            RETURNING id
            """,
            client_id,
            address,
            google_maps_link,
        )
    return int(row["id"])


async def search_clients_by_name(
    query: str,
    limit: int = 10,
) -> Tuple[list[Dict[str, Any]], bool]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        client_rows = await conn.fetch(
            """
            SELECT id, name, phone, contact_person
            FROM clients
            WHERE name ILIKE $1
            ORDER BY LOWER(name), id
            LIMIT $2
            """,
            f"%{query}%",
            limit + 1,
        )
        has_more = len(client_rows) > limit
        trimmed_rows = list(client_rows[:limit])
        if not trimmed_rows:
            return [], False
        client_ids = [row["id"] for row in trimmed_rows]
        address_rows = await conn.fetch(
            """
            SELECT client_id, address, google_maps_link
            FROM client_addresses
            WHERE client_id = ANY($1::int[])
            ORDER BY client_id, created_at DESC NULLS LAST, id DESC
            """,
            client_ids,
        )
    addresses_map: Dict[int, list[Dict[str, Optional[str]]]] = {}
    for row in address_rows:
        address_value = row.get("address")
        map_link_value = row.get("google_maps_link")
        if address_value is None and map_link_value is None:
            continue
        addresses_map.setdefault(row["client_id"], []).append(
            {
                "address": address_value,
                "google_maps_link": map_link_value,
            }
        )
    results: list[Dict[str, Any]] = []
    for row in trimmed_rows:
        results.append(
            {
                "id": row["id"],
                "name": row["name"],
                "phone": row.get("phone"),
                "contact_person": row.get("contact_person"),
                "addresses": addresses_map.get(row["id"], []),
            }
        )
    return results, has_more


async def fetch_order_types() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM order_types ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def fetch_next_order_number() -> int:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(MAX(order_number), 0) + 1 AS next_number FROM orders"
        )
    return int(row["next_number"] or 1)


async def fetch_all_orders() -> list[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                order_number,
                client_name,
                title,
                order_type,
                folder_path,
                due_date,
                is_urgent,
                created_at,
                created_by_name
            FROM orders
            ORDER BY due_date ASC, order_number ASC
            """
        )
    return [dict(row) for row in rows]


async def create_order_in_db(
    client_id: int,
    client_name: str,
    title: str,
    order_type: str,
    folder_path: str,
    due_date: date,
    is_urgent: bool,
    created_by_id: Optional[int],
    created_by_name: Optional[str],
) -> Dict[str, Any]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO orders (
                client_id,
                client_name,
                title,
                order_type,
                folder_path,
                due_date,
                is_urgent,
                created_by_id,
                created_by_name
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id, order_number, client_id, client_name, title, order_type,
                      folder_path, due_date, is_urgent, created_at, created_by_id,
                      created_by_name
            """,
            client_id,
            client_name,
            title,
            order_type,
            folder_path,
            due_date,
            is_urgent,
            created_by_id,
            created_by_name,
        )
    if row is None:
        raise RuntimeError("Failed to insert order")
    return dict(row)


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


async def fetch_film_manufacturers() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM film_manufacturers ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def fetch_led_module_manufacturers() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM led_module_manufacturers ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def fetch_led_module_storage_locations() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM led_module_storage_locations ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def fetch_led_strip_manufacturers() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM led_strip_manufacturers ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def fetch_power_supply_manufacturers() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM power_supply_manufacturers ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def get_led_module_manufacturer_by_name(
    name: str,
) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name
            FROM led_module_manufacturers
            WHERE LOWER(name) = LOWER($1)
            """,
            name,
        )
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"]}


async def fetch_led_module_manufacturers_with_series() -> list[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        manufacturers_rows = await conn.fetch(
            "SELECT id, name FROM led_module_manufacturers ORDER BY LOWER(name)"
        )
        series_rows = await conn.fetch(
            """
            SELECT manufacturer_id, name
            FROM led_module_series
            ORDER BY manufacturer_id, LOWER(name)
            """
        )
    series_map: dict[int, list[str]] = {}
    for row in series_rows:
        series_map.setdefault(row["manufacturer_id"], []).append(row["name"])
    result: list[dict[str, Any]] = []
    for row in manufacturers_rows:
        result.append(
            {
                "id": row["id"],
                "name": row["name"],
                "series": series_map.get(row["id"], []),
            }
        )
    return result


async def fetch_led_module_colors() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM led_module_colors ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def fetch_led_module_power_options() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM led_module_power_options ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def fetch_led_module_voltage_options() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM led_module_voltage_options ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def fetch_generated_led_modules_with_details() -> list[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                glm.article,
                manufacturer.name AS manufacturer,
                series.name AS series,
                color.name AS color,
                lens.value AS lens_count,
                power.name AS power,
                voltage.name AS voltage
            FROM generated_led_modules AS glm
            JOIN led_module_manufacturers AS manufacturer ON manufacturer.id = glm.manufacturer_id
            JOIN led_module_series AS series ON series.id = glm.series_id
            JOIN led_module_colors AS color ON color.id = glm.color_id
            JOIN led_module_lens_counts AS lens ON lens.id = glm.lens_count_id
            JOIN led_module_power_options AS power ON power.id = glm.power_option_id
            JOIN led_module_voltage_options AS voltage ON voltage.id = glm.voltage_option_id
            ORDER BY glm.created_at DESC NULLS LAST, glm.id DESC
            """
        )
    return [dict(row) for row in rows]


async def fetch_led_module_stock_summary() -> list[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                glm.article,
                manufacturer.name AS manufacturer,
                series.name AS series,
                color.name AS color,
                lens.value AS lens_count,
                power.name AS power,
                voltage.name AS voltage,
                COALESCE(SUM(wlm.quantity), 0) AS total_quantity
            FROM generated_led_modules AS glm
            JOIN led_module_manufacturers AS manufacturer ON manufacturer.id = glm.manufacturer_id
            JOIN led_module_series AS series ON series.id = glm.series_id
            JOIN led_module_colors AS color ON color.id = glm.color_id
            JOIN led_module_lens_counts AS lens ON lens.id = glm.lens_count_id
            JOIN led_module_power_options AS power ON power.id = glm.power_option_id
            JOIN led_module_voltage_options AS voltage ON voltage.id = glm.voltage_option_id
            LEFT JOIN warehouse_led_modules AS wlm ON wlm.led_module_id = glm.id
            GROUP BY
                glm.id,
                glm.article,
                manufacturer.name,
                series.name,
                color.name,
                lens.value,
                power.name,
                voltage.name
            HAVING COALESCE(SUM(wlm.quantity), 0) > 0
            ORDER BY total_quantity DESC, LOWER(glm.article)
            """,
        )
    return [dict(row) for row in rows]


async def get_generated_led_module_details(module_id: int) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                glm.id,
                glm.article,
                manufacturer.name AS manufacturer,
                series.name AS series,
                color.name AS color,
                lens.value AS lens_count,
                power.name AS power,
                voltage.name AS voltage
            FROM generated_led_modules AS glm
            JOIN led_module_manufacturers AS manufacturer ON manufacturer.id = glm.manufacturer_id
            JOIN led_module_series AS series ON series.id = glm.series_id
            JOIN led_module_colors AS color ON color.id = glm.color_id
            JOIN led_module_lens_counts AS lens ON lens.id = glm.lens_count_id
            JOIN led_module_power_options AS power ON power.id = glm.power_option_id
            JOIN led_module_voltage_options AS voltage ON voltage.id = glm.voltage_option_id
            WHERE glm.id = $1
            """,
            module_id,
        )
    if row is None:
        return None
    return dict(row)


async def fetch_led_module_lens_counts() -> list[int]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT value FROM led_module_lens_counts ORDER BY value"
        )
    return [row["value"] for row in rows]


async def fetch_led_module_series_by_manufacturer(
    manufacturer_name: str,
) -> list[str]:
    manufacturer = await get_led_module_manufacturer_by_name(manufacturer_name)
    if manufacturer is None:
        return []
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name
            FROM led_module_series
            WHERE manufacturer_id = $1
            ORDER BY LOWER(name)
            """,
            manufacturer["id"],
        )
    return [row["name"] for row in rows]


async def get_led_module_series_by_name(
    manufacturer_id: int, name: str
) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, manufacturer_id, name
            FROM led_module_series
            WHERE manufacturer_id = $1 AND LOWER(name) = LOWER($2)
            """,
            manufacturer_id,
            name,
        )
    if row is None:
        return None
    return {"id": row["id"], "manufacturer_id": row["manufacturer_id"], "name": row["name"]}


async def get_led_module_color_by_name(name: str) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name FROM led_module_colors WHERE LOWER(name) = LOWER($1)",
            name,
        )
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"]}


async def get_led_module_power_option_by_name(name: str) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name FROM led_module_power_options WHERE LOWER(name) = LOWER($1)",
            name,
        )
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"]}


async def get_led_module_voltage_option_by_name(name: str) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name FROM led_module_voltage_options WHERE LOWER(name) = LOWER($1)",
            name,
        )
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"]}


async def get_led_module_lens_count_by_value(value: int) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, value FROM led_module_lens_counts WHERE value = $1",
            value,
        )
    if row is None:
        return None
    return {"id": row["id"], "value": row["value"]}


async def get_generated_led_module_by_article(article: str) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, article, manufacturer_id, series_id, color_id,
                   lens_count_id, power_option_id, voltage_option_id, created_at
            FROM generated_led_modules
            WHERE LOWER(article) = LOWER($1)
            """,
            article,
        )
    if row is None:
        return None
    return dict(row)


async def insert_generated_led_module(
    *,
    article: str,
    manufacturer_id: int,
    series_id: int,
    color_id: int,
    lens_count_id: int,
    power_option_id: int,
    voltage_option_id: int,
) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO generated_led_modules (
                article,
                manufacturer_id,
                series_id,
                color_id,
                lens_count_id,
                power_option_id,
                voltage_option_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (article) DO NOTHING
            RETURNING id, article, manufacturer_id, series_id, color_id,
                      lens_count_id, power_option_id, voltage_option_id, created_at
            """,
            article,
            manufacturer_id,
            series_id,
            color_id,
            lens_count_id,
            power_option_id,
            voltage_option_id,
        )
    if row is None:
        return None
    return dict(row)


async def insert_warehouse_led_module_record(
    *,
    led_module_id: int,
    article: str,
    quantity: int,
    added_by_id: Optional[int],
    added_by_name: Optional[str],
) -> Dict[str, Any]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    added_at = datetime.now(WARSAW_TZ)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO warehouse_led_modules (
                led_module_id,
                article,
                quantity,
                added_by_id,
                added_by_name,
                added_at
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id, led_module_id, article, quantity, added_by_id, added_by_name, added_at
            """,
            led_module_id,
            article,
            quantity,
            added_by_id,
            added_by_name,
            added_at,
        )
    if row is None:
        return {}
    return dict(row)


async def get_led_module_stock_quantity(led_module_id: int) -> int:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT COALESCE(SUM(quantity), 0) FROM warehouse_led_modules WHERE led_module_id = $1",
            led_module_id,
        )
    return int(value or 0)


async def write_off_led_module_stock(
    *,
    led_module_id: int,
    article: str,
    quantity: int,
    project: str,
    written_off_by_id: Optional[int],
    written_off_by_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    if quantity <= 0:
        raise ValueError("Quantity for write-off must be positive")
    now_warsaw = datetime.now(WARSAW_TZ)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            available = await conn.fetchval(
                "SELECT COALESCE(SUM(quantity), 0) FROM warehouse_led_modules WHERE led_module_id = $1",
                led_module_id,
            )
            if available is None or int(available) < quantity:
                return None
            ledger_row = await conn.fetchrow(
                """
                INSERT INTO warehouse_led_modules (
                    led_module_id,
                    article,
                    quantity,
                    added_by_id,
                    added_by_name,
                    added_at
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                led_module_id,
                article,
                -quantity,
                written_off_by_id,
                written_off_by_name,
                now_warsaw,
            )
            if ledger_row is None:
                return None
            written_off_row = await conn.fetchrow(
                """
                INSERT INTO written_off_led_modules (
                    led_module_id,
                    article,
                    quantity,
                    project,
                    written_off_by_id,
                    written_off_by_name,
                    written_off_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING
                    id,
                    led_module_id,
                    article,
                    quantity,
                    project,
                    written_off_by_id,
                    written_off_by_name,
                    written_off_at
                """,
                led_module_id,
                article,
                quantity,
                project,
                written_off_by_id,
                written_off_by_name,
                now_warsaw,
            )
    if written_off_row is None:
        return None
    return dict(written_off_row)


async def fetch_film_storage_locations() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT name FROM film_storage_locations ORDER BY LOWER(name)"
        )
    return [row["name"] for row in rows]


async def get_film_manufacturer_by_name(
    name: str,
) -> Optional[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name FROM film_manufacturers WHERE LOWER(name) = LOWER($1)",
            name,
        )
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"]}


async def fetch_film_manufacturers_with_series() -> list[dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        manufacturers_rows = await conn.fetch(
            "SELECT id, name FROM film_manufacturers ORDER BY LOWER(name)"
        )
        series_rows = await conn.fetch(
            """
            SELECT manufacturer_id, name
            FROM film_series
            ORDER BY manufacturer_id, LOWER(name)
            """
        )
    series_map: dict[int, list[str]] = {}
    for row in series_rows:
        series_map.setdefault(row["manufacturer_id"], []).append(row["name"])
    result: list[dict[str, Any]] = []
    for row in manufacturers_rows:
        result.append(
            {
                "id": row["id"],
                "name": row["name"],
                "series": series_map.get(row["id"], []),
            }
        )
    return result


async def fetch_film_series_by_manufacturer(manufacturer_name: str) -> list[str]:
    manufacturer = await get_film_manufacturer_by_name(manufacturer_name)
    if manufacturer is None:
        return []
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name
            FROM film_series
            WHERE manufacturer_id = $1
            ORDER BY LOWER(name)
            """,
            manufacturer["id"],
        )
    return [row["name"] for row in rows]


async def fetch_max_plastic_article() -> Optional[int]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT MAX(article::BIGINT)
            FROM warehouse_plastics
            WHERE article ~ '^[0-9]+$'
            """
        )
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def fetch_max_film_article() -> Optional[int]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT MAX(article::BIGINT)
            FROM warehouse_films
            WHERE article ~ '^[0-9]+$'
            """
        )
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def insert_order_type(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO order_types (name)
            VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            name,
        )
    return row is not None


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


async def insert_film_manufacturer(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO film_manufacturers (name)
            VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_film_manufacturer(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM film_manufacturers WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def insert_led_module_manufacturer(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO led_module_manufacturers (name)
            VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            name,
        )
    return row is not None


async def insert_led_module_storage_location(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        existing_id = await conn.fetchval(
            "SELECT id FROM led_module_storage_locations WHERE LOWER(name) = LOWER($1)",
            name,
        )
        if existing_id:
            return False
        row = await conn.fetchrow(
            """
            INSERT INTO led_module_storage_locations (name)
            VALUES ($1)
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_led_module_manufacturer(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM led_module_manufacturers WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def delete_led_module_storage_location(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM led_module_storage_locations WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def insert_led_module_color(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT 1 FROM led_module_colors WHERE LOWER(name) = LOWER($1)",
            name,
        )
        if existing:
            return False
        row = await conn.fetchrow(
            """
            INSERT INTO led_module_colors (name)
            VALUES ($1)
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_led_module_color(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM led_module_colors WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def insert_led_module_lens_count(value: int) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO led_module_lens_counts (value)
            VALUES ($1)
            ON CONFLICT (value) DO NOTHING
            RETURNING id
            """,
            value,
        )
    return row is not None


async def insert_led_module_series(
    manufacturer_name: str, series_name: str
) -> str:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        manufacturer_row = await conn.fetchrow(
            """
            SELECT id, name
            FROM led_module_manufacturers
            WHERE LOWER(name) = LOWER($1)
            """,
            manufacturer_name,
        )
        if manufacturer_row is None:
            return "manufacturer_not_found"
        manufacturer_id = manufacturer_row["id"]
        existing_id = await conn.fetchval(
            """
            SELECT id
            FROM led_module_series
            WHERE manufacturer_id = $1 AND LOWER(name) = LOWER($2)
            """,
            manufacturer_id,
            series_name,
        )
        if existing_id:
            return "already_exists"
        row = await conn.fetchrow(
            """
            INSERT INTO led_module_series (manufacturer_id, name)
            VALUES ($1, $2)
            RETURNING id
            """,
            manufacturer_id,
            series_name,
        )
    return "inserted" if row else "error"


async def delete_led_module_series(
    manufacturer_name: str, series_name: str
) -> str:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        manufacturer_row = await conn.fetchrow(
            """
            SELECT id
            FROM led_module_manufacturers
            WHERE LOWER(name) = LOWER($1)
            """,
            manufacturer_name,
        )
        if manufacturer_row is None:
            return "manufacturer_not_found"
        result = await conn.execute(
            """
            DELETE FROM led_module_series
            WHERE manufacturer_id = $1 AND LOWER(name) = LOWER($2)
            """,
            manufacturer_row["id"],
            series_name,
        )
    return "deleted" if result.endswith(" 1") else "not_found"


async def delete_led_module_lens_count(value: int) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM led_module_lens_counts WHERE value = $1",
            value,
        )
    return result.endswith(" 1")


async def insert_led_module_power_option(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO led_module_power_options (name)
            VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_led_module_power_option(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM led_module_power_options WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def insert_led_module_voltage_option(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO led_module_voltage_options (name)
            VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_led_module_voltage_option(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM led_module_voltage_options WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def insert_led_strip_manufacturer(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO led_strip_manufacturers (name)
            VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_led_strip_manufacturer(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM led_strip_manufacturers WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def insert_power_supply_manufacturer(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO power_supply_manufacturers (name)
            VALUES ($1)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_power_supply_manufacturer(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM power_supply_manufacturers WHERE LOWER(name) = LOWER($1)",
            name,
        )
    return result.endswith(" 1")


async def insert_film_series(
    manufacturer_name: str, series_name: str
) -> str:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        manufacturer_row = await conn.fetchrow(
            "SELECT id, name FROM film_manufacturers WHERE LOWER(name) = LOWER($1)",
            manufacturer_name,
        )
        if manufacturer_row is None:
            return "manufacturer_not_found"
        manufacturer_id = manufacturer_row["id"]
        existing_id = await conn.fetchval(
            """
            SELECT id FROM film_series
            WHERE manufacturer_id = $1 AND LOWER(name) = LOWER($2)
            """,
            manufacturer_id,
            series_name,
        )
        if existing_id:
            return "already_exists"
        row = await conn.fetchrow(
            """
            INSERT INTO film_series (manufacturer_id, name)
            VALUES ($1, $2)
            RETURNING id
            """,
            manufacturer_id,
            series_name,
        )
    return "inserted" if row else "error"


async def delete_film_series(
    manufacturer_name: str, series_name: str
) -> str:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        manufacturer_row = await conn.fetchrow(
            "SELECT id FROM film_manufacturers WHERE LOWER(name) = LOWER($1)",
            manufacturer_name,
        )
        if manufacturer_row is None:
            return "manufacturer_not_found"
        result = await conn.execute(
            """
            DELETE FROM film_series
            WHERE manufacturer_id = $1 AND LOWER(name) = LOWER($2)
            """,
            manufacturer_row["id"],
            series_name,
        )
    return "deleted" if result.endswith(" 1") else "not_found"


async def insert_film_storage_location(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        existing_id = await conn.fetchval(
            "SELECT id FROM film_storage_locations WHERE LOWER(name) = LOWER($1)",
            name,
        )
        if existing_id:
            return False
        row = await conn.fetchrow(
            """
            INSERT INTO film_storage_locations (name)
            VALUES ($1)
            RETURNING id
            """,
            name,
        )
    return row is not None


async def delete_film_storage_location(name: str) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM film_storage_locations WHERE LOWER(name) = LOWER($1)",
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


async def fetch_all_material_thicknesses() -> list[Decimal]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT thickness
            FROM plastic_material_thicknesses
            ORDER BY thickness
            """
        )
    return [row["thickness"] for row in rows]


async def fetch_all_material_colors() -> list[str]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT color
            FROM plastic_material_colors
            ORDER BY LOWER(color)
            """
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


async def insert_warehouse_film_record(
    article: str,
    manufacturer: str,
    series: str,
    color_code: str,
    color: str,
    width_mm: Decimal,
    length_mm: Decimal,
    warehouse: str,
    comment: Optional[str],
    employee_id: Optional[int],
    employee_nick: Optional[str],
) -> Dict[str, Any]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    recorded_at = datetime.now(WARSAW_TZ)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO warehouse_films (
                article,
                manufacturer,
                series,
                color_code,
                color,
                width,
                length,
                warehouse,
                comment,
                employee_id,
                employee_nick,
                recorded_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            RETURNING
                id,
                article,
                manufacturer,
                series,
                color_code,
                color,
                width,
                length,
                warehouse,
                comment,
                employee_id,
                employee_nick,
                recorded_at
            """,
            article,
            manufacturer,
            series,
            color_code,
            color,
            width_mm,
            length_mm,
            warehouse,
            comment,
            employee_id,
            employee_nick,
            recorded_at,
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


async def fetch_all_warehouse_plastics() -> list[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
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
                arrival_date,
                arrival_at
            FROM warehouse_plastics
            ORDER BY arrival_at DESC NULLS LAST, id DESC
            """
        )
    return [dict(row) for row in rows]


async def fetch_all_warehouse_films() -> list[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                article,
                manufacturer,
                series,
                color_code,
                color,
                width,
                length,
                warehouse,
                comment,
                employee_id,
                employee_nick,
                recorded_at
            FROM warehouse_films
            ORDER BY recorded_at DESC NULLS LAST, id DESC
            """
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


async def fetch_warehouse_film_by_article(article: str) -> Optional[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id,
                article,
                manufacturer,
                series,
                color_code,
                color,
                width,
                length,
                warehouse,
                comment,
                employee_id,
                employee_nick,
                recorded_at
            FROM warehouse_films
            WHERE article = $1
            ORDER BY recorded_at DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            article,
        )
    if row is None:
        return None
    return dict(row)


async def write_off_warehouse_film(
    record_id: int,
    project: str,
    written_off_by_id: Optional[int],
    written_off_by_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    now_warsaw = datetime.now(WARSAW_TZ)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            original_row = await conn.fetchrow(
                """
                DELETE FROM warehouse_films
                WHERE id = $1
                RETURNING
                    id,
                    article,
                    manufacturer,
                    series,
                    color_code,
                    color,
                    width,
                    length,
                    warehouse,
                    comment,
                    employee_id,
                    employee_nick,
                    recorded_at
                """,
                record_id,
            )
            if original_row is None:
                return None
            inserted_row = await conn.fetchrow(
                """
                INSERT INTO written_off_films (
                    source_id,
                    article,
                    manufacturer,
                    series,
                    color_code,
                    color,
                    width,
                    length,
                    warehouse,
                    comment,
                    employee_id,
                    employee_nick,
                    recorded_at,
                    project,
                    written_off_by_id,
                    written_off_by_name,
                    written_off_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
                )
                RETURNING
                    id,
                    source_id,
                    article,
                    manufacturer,
                    series,
                    color_code,
                    color,
                    width,
                    length,
                    warehouse,
                    comment,
                    employee_id,
                    employee_nick,
                    recorded_at,
                    project,
                    written_off_by_id,
                    written_off_by_name,
                    written_off_at
                """,
                original_row["id"],
                original_row["article"],
                original_row["manufacturer"],
                original_row["series"],
                original_row["color_code"],
                original_row["color"],
                original_row["width"],
                original_row["length"],
                original_row["warehouse"],
                original_row["comment"],
                original_row["employee_id"],
                original_row["employee_nick"],
                original_row["recorded_at"],
                project,
                written_off_by_id,
                written_off_by_name,
                now_warsaw,
            )
    if inserted_row is None:
        return None
    return dict(inserted_row)


async def fetch_warehouse_film_by_id(record_id: int) -> Optional[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                id,
                article,
                manufacturer,
                series,
                color_code,
                color,
                width,
                length,
                warehouse,
                comment,
                employee_id,
                employee_nick,
                recorded_at
            FROM warehouse_films
            WHERE id = $1
            """,
            record_id,
        )
    if row is None:
        return None
    return dict(row)


async def search_warehouse_films_by_color_code(
    color_code: str, limit: int = FILM_SEARCH_RESULTS_LIMIT
) -> list[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                article,
                manufacturer,
                series,
                color_code,
                color,
                width,
                length,
                warehouse,
                comment,
                employee_id,
                employee_nick,
                recorded_at
            FROM warehouse_films
            WHERE color_code ILIKE '%' || $1 || '%'
            ORDER BY recorded_at DESC NULLS LAST, id DESC
            LIMIT $2
            """,
            color_code,
            limit,
        )
    return [dict(row) for row in rows]


async def search_warehouse_films_by_color(
    color_query: str, limit: int = FILM_SEARCH_RESULTS_LIMIT
) -> list[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                article,
                manufacturer,
                series,
                color_code,
                color,
                width,
                length,
                warehouse,
                comment,
                employee_id,
                employee_nick,
                recorded_at
            FROM warehouse_films
            WHERE color ILIKE '%' || $1 || '%'
            ORDER BY recorded_at DESC NULLS LAST, id DESC
            LIMIT $2
            """,
            color_query,
            limit,
        )
    return [dict(row) for row in rows]


async def search_warehouse_plastics_advanced(
    material: Optional[str] = None,
    thickness: Optional[Decimal] = None,
    color: Optional[str] = None,
    min_length: Optional[Decimal] = None,
    min_width: Optional[Decimal] = None,
) -> list[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    conditions: list[str] = []
    params: list[Any] = []
    param_index = 1
    if material:
        conditions.append(f"LOWER(material) = LOWER(${param_index})")
        params.append(material)
        param_index += 1
    if thickness is not None:
        conditions.append(f"thickness = ${param_index}")
        params.append(thickness)
        param_index += 1
    if color:
        conditions.append(f"LOWER(color) = LOWER(${param_index})")
        params.append(color)
        param_index += 1
    if min_length is not None:
        conditions.append(f"length >= ${param_index}")
        params.append(min_length)
        param_index += 1
    if min_width is not None:
        conditions.append(f"width >= ${param_index}")
        params.append(min_width)
        param_index += 1
    where_clause = ""
    if conditions:
        where_clause = " WHERE " + " AND ".join(conditions)
    query = (
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
        """
        + where_clause
        + " ORDER BY length DESC NULLS LAST, width DESC NULLS LAST, arrival_at DESC NULLS LAST, id DESC"
    )
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(query, *params)
    return [dict(row) for row in rows]


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


async def update_warehouse_film_comment(
    record_id: int, comment: Optional[str]
) -> bool:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE warehouse_films
            SET comment = $2
            WHERE id = $1
            """,
            record_id,
            comment,
        )
    return result.endswith(" 1")


async def update_warehouse_film_location(
    record_id: int,
    new_location: str,
    employee_id: Optional[int],
    employee_nick: Optional[str],
) -> Optional[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE warehouse_films
            SET warehouse = $2,
                employee_id = COALESCE($3, employee_id),
                employee_nick = COALESCE($4, employee_nick)
            WHERE id = $1
            RETURNING
                id,
                article,
                manufacturer,
                series,
                color_code,
                color,
                width,
                length,
                warehouse,
                comment,
                employee_id,
                employee_nick,
                recorded_at
            """,
            record_id,
            new_location,
            employee_id,
            employee_nick,
        )
    if row is None:
        return None
    return dict(row)


async def update_warehouse_plastic_location(
    record_id: int,
    new_location: str,
    employee_id: Optional[int],
    employee_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE warehouse_plastics
            SET warehouse = $2,
                employee_id = COALESCE($3, employee_id),
                employee_name = COALESCE($4, employee_name)
            WHERE id = $1
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
                employee_name,
                arrival_at
            """,
            record_id,
            new_location,
            employee_id,
            employee_name,
        )
    if row is None:
        return None
    return dict(row)


async def write_off_warehouse_plastic(
    record_id: int,
    project: str,
    written_off_by_id: Optional[int],
    written_off_by_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")
    now_warsaw = datetime.now(WARSAW_TZ)
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            original_row = await conn.fetchrow(
                """
                DELETE FROM warehouse_plastics
                WHERE id = $1
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
                record_id,
            )
            if original_row is None:
                return None
            inserted_row = await conn.fetchrow(
                """
                INSERT INTO written_off_plastics (
                    source_id,
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
                    arrival_at,
                    project,
                    written_off_by_id,
                    written_off_by_name,
                    written_off_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17
                )
                RETURNING
                    id,
                    source_id,
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
                    arrival_at,
                    project,
                    written_off_by_id,
                    written_off_by_name,
                    written_off_at
                """,
                original_row["id"],
                original_row["article"],
                original_row["material"],
                original_row["thickness"],
                original_row["color"],
                original_row["length"],
                original_row["width"],
                original_row["warehouse"],
                original_row["comment"],
                original_row["employee_id"],
                original_row["employee_name"],
                original_row["arrival_date"],
                original_row["arrival_at"],
                project,
                written_off_by_id,
                written_off_by_name,
                now_warsaw,
            )
    if inserted_row is None:
        return None
    return dict(inserted_row)


def format_materials_list(materials: list[str]) -> str:
    if not materials:
        return "—"
    return "\n".join(f"• {item}" for item in materials)


def format_order_types_list(order_types: list[str]) -> str:
    if not order_types:
        return "—"
    return "\n".join(f"• {name}" for name in order_types)


def _format_datetime(value: Optional[datetime]) -> str:
    if value is None:
        return "—"
    try:
        localised = value.astimezone(WARSAW_TZ)
    except Exception:
        localised = value
    return localised.strftime("%Y-%m-%d %H:%M")


def _format_date(value: Optional[date]) -> str:
    if value is None:
        return "—"
    return value.strftime("%d.%m.%Y")


def _parse_due_date_input(value: str) -> Optional[date]:
    cleaned = value.strip()
    if not cleaned:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _decimal_to_excel_number(value: Optional[Decimal]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, InvalidOperation):
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError):
            return None


def _format_date_for_excel(value: Optional[date], fallback: Optional[datetime] = None) -> str:
    if value is not None:
        return value.strftime("%Y-%m-%d")
    if fallback is None:
        return ""
    try:
        localised = fallback.astimezone(WARSAW_TZ)
    except Exception:
        localised = fallback
    return localised.strftime("%Y-%m-%d")


def _format_datetime_for_excel(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    try:
        localised = value.astimezone(WARSAW_TZ)
    except Exception:
        localised = value
    return localised.strftime("%Y-%m-%d %H:%M")


def parse_user_created_at_input(text: str) -> Optional[datetime]:
    cleaned = text.strip()
    if not cleaned:
        return None

    datetime_formats = [
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y %H:%M",
    ]
    date_formats = ["%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"]

    for fmt in datetime_formats:
        try:
            parsed = datetime.strptime(cleaned, fmt)
            return parsed.replace(tzinfo=WARSAW_TZ)
        except ValueError:
            continue

    for fmt in date_formats:
        try:
            parsed_date = datetime.strptime(cleaned, fmt).date()
            combined = datetime.combine(parsed_date, time.min, tzinfo=WARSAW_TZ)
            return combined
        except ValueError:
            continue

    return None


def format_user_record_for_message(record: Dict[str, Any], index: int) -> str:
    tg_id = record.get("tg_id") or "—"
    username = record.get("username") or "—"
    position = record.get("position") or "—"
    role = record.get("role") or "—"
    created_at = record.get("created_at")
    created_text = _format_datetime(created_at)
    return (
        f"{index}. 👤 {username}\n"
        f"   • TG ID: {tg_id}\n"
        f"   • Должность: {position}\n"
        f"   • Роль: {role}\n"
        f"   • Добавлен: {created_text}"
    )


def split_text_into_messages(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for part in parts:
        candidate = part if not current else f"{current}\n\n{part}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(part) > limit:
            for start in range(0, len(part), limit):
                chunks.append(part[start : start + limit])
            current = ""
        else:
            current = part
    if current:
        chunks.append(current)
    return chunks


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


def format_series_list(series: list[str]) -> str:
    if not series:
        return "—"
    return ", ".join(series)


def format_storage_locations_list(locations: list[str]) -> str:
    if not locations:
        return "—"
    return "\n".join(f"• {item}" for item in locations)


def build_plastics_export_file(records: list[Dict[str, Any]]) -> BufferedInputFile:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Plastics"

    headers = [
        "Артикул",
        "Материал",
        "Толщина (мм)",
        "Цвет",
        "Длина (мм)",
        "Ширина (мм)",
        "Место хранения",
        "Комментарий",
        "Ответственный",
        "Дата прибытия",
        "Дата и время прибытия",
    ]
    sheet.append(headers)

    for record in records:
        arrival_at: Optional[datetime] = record.get("arrival_at")
        arrival_date: Optional[date] = record.get("arrival_date")
        row = [
            record.get("article"),
            record.get("material"),
            _decimal_to_excel_number(record.get("thickness")),
            record.get("color"),
            _decimal_to_excel_number(record.get("length")),
            _decimal_to_excel_number(record.get("width")),
            record.get("warehouse"),
            record.get("comment"),
            record.get("employee_name"),
            _format_date_for_excel(arrival_date, arrival_at),
            _format_datetime_for_excel(arrival_at),
        ]
        sheet.append(row)

    for column_index, column_cells in enumerate(sheet.columns, start=1):
        max_length = 0
        for cell in column_cells:
            value = cell.value
            if value is None:
                continue
            max_length = max(max_length, len(str(value)))
        adjusted_width = min(max(12, max_length + 2), 40)
        column_letter = get_column_letter(column_index)
        sheet.column_dimensions[column_letter].width = adjusted_width

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    timestamp = datetime.now(WARSAW_TZ).strftime("%Y%m%d_%H%M%S")
    filename = f"plastics_export_{timestamp}.xlsx"
    return BufferedInputFile(buffer.getvalue(), filename=filename)


def build_films_export_file(records: list[Dict[str, Any]]) -> BufferedInputFile:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Films"

    headers = [
        "Артикул",
        "Производитель",
        "Серия",
        "Код цвета",
        "Цвет",
        "Ширина (мм)",
        "Длина (мм)",
        "Место хранения",
        "Комментарий",
        "Ник сотрудника",
        "ID сотрудника",
        "Дата и время записи",
    ]
    sheet.append(headers)

    for record in records:
        row = [
            record.get("article"),
            record.get("manufacturer"),
            record.get("series"),
            record.get("color_code"),
            record.get("color"),
            _decimal_to_excel_number(record.get("width")),
            _decimal_to_excel_number(record.get("length")),
            record.get("warehouse"),
            record.get("comment"),
            record.get("employee_nick"),
            record.get("employee_id"),
            _format_datetime_for_excel(record.get("recorded_at")),
        ]
        sheet.append(row)

    for column_index, column_cells in enumerate(sheet.columns, start=1):
        max_length = 0
        for cell in column_cells:
            value = cell.value
            if value is None:
                continue
            max_length = max(max_length, len(str(value)))
        adjusted_width = min(max(12, max_length + 2), 40)
        column_letter = get_column_letter(column_index)
        sheet.column_dimensions[column_letter].width = adjusted_width

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    timestamp = datetime.now(WARSAW_TZ).strftime("%Y%m%d_%H%M%S")
    filename = f"films_export_{timestamp}.xlsx"
    return BufferedInputFile(buffer.getvalue(), filename=filename)


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


def format_film_record_for_message(record: Dict[str, Any]) -> str:
    recorded_at = record.get("recorded_at")
    if recorded_at:
        try:
            recorded_local = recorded_at.astimezone(WARSAW_TZ)
        except Exception:
            recorded_local = recorded_at
        recorded_text = recorded_local.strftime("%Y-%m-%d %H:%M")
    else:
        recorded_text = "—"
    return (
        f"Артикул: {record.get('article') or '—'}\n"
        f"Производитель: {record.get('manufacturer') or '—'}\n"
        f"Серия: {record.get('series') or '—'}\n"
        f"Код цвета: {record.get('color_code') or '—'}\n"
        f"Цвет: {record.get('color') or '—'}\n"
        f"Ширина: {format_dimension_value(record.get('width'))}\n"
        f"Длина: {format_dimension_value(record.get('length'))}\n"
        f"Склад: {record.get('warehouse') or '—'}\n"
        f"Комментарий: {record.get('comment') or '—'}\n"
        f"Ник: {record.get('employee_nick') or '—'}\n"
        f"ID: {record.get('employee_id') or '—'}\n"
        f"Дата и время: {recorded_text}"
    )


def format_film_records_list_for_message(records: list[Dict[str, Any]]) -> str:
    parts: list[str] = []
    for index, record in enumerate(records, start=1):
        formatted = format_film_record_for_message(record)
        parts.append(f"{index}.\n{formatted}")
    return "\n\n".join(parts)


def format_written_off_film_record(record: Dict[str, Any]) -> str:
    base_info = format_film_record_for_message(record)
    project = record.get("project") or "—"
    written_off_at = record.get("written_off_at")
    if written_off_at:
        try:
            written_off_local = written_off_at.astimezone(WARSAW_TZ)
        except Exception:
            written_off_local = written_off_at
        written_off_text = written_off_local.strftime("%Y-%m-%d %H:%M")
    else:
        written_off_text = "—"
    written_off_by_name = record.get("written_off_by_name") or "—"
    written_off_by_id = record.get("written_off_by_id")
    written_off_by_id_text = "—" if written_off_by_id is None else str(written_off_by_id)
    return (
        f"{base_info}\n"
        f"Проект: {project}\n"
        f"Списал: {written_off_by_name}\n"
        f"ID списавшего: {written_off_by_id_text}\n"
        f"Списано: {written_off_text}"
    )


def format_written_off_plastic_record(record: Dict[str, Any]) -> str:
    base_info = format_plastic_record_for_message(record)
    project = record.get("project") or "—"
    written_off_at = record.get("written_off_at")
    if written_off_at:
        try:
            written_off_local = written_off_at.astimezone(WARSAW_TZ)
        except Exception:
            written_off_local = written_off_at
        written_off_text = written_off_local.strftime("%Y-%m-%d %H:%M")
    else:
        written_off_text = "—"
    written_off_by_name = record.get("written_off_by_name") or "—"
    written_off_by_id = record.get("written_off_by_id")
    if written_off_by_id is None:
        written_off_by_id_text = "—"
    else:
        written_off_by_id_text = str(written_off_by_id)
    return (
        f"{base_info}\n"
        f"Проект: {project}\n"
        f"Списал: {written_off_by_name}\n"
        f"ID списавшего: {written_off_by_id_text}\n"
        f"Списано: {written_off_text}"
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


def parse_dimension_filter_value(raw_text: str) -> Optional[Decimal]:
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
    if value < 0:
        return None
    return value.quantize(Decimal("0.01"))


def parse_positive_decimal(raw_text: str) -> Optional[Decimal]:
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


def build_manufacturers_keyboard(manufacturers: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for name in manufacturers:
        rows.append([KeyboardButton(text=name)])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_series_keyboard(series: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for name in series:
        rows.append([KeyboardButton(text=name)])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_power_values_keyboard(values: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for value in values:
        rows.append([KeyboardButton(text=value)])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_voltage_values_keyboard(values: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for value in values:
        rows.append([KeyboardButton(text=value)])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_lens_counts_keyboard(counts: list[int]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for value in counts:
        rows.append([KeyboardButton(text=str(value))])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_led_module_articles_keyboard(articles: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for article in articles:
        rows.append([KeyboardButton(text=article)])
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


def build_advanced_materials_keyboard(materials: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for name in materials:
        rows.append([KeyboardButton(text=name)])
    rows.append([KeyboardButton(text=ADVANCED_SEARCH_SKIP_MATERIAL_TEXT)])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_advanced_thickness_keyboard(thicknesses: list[Decimal]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for value in thicknesses:
        rows.append([KeyboardButton(text=format_thickness_value(value))])
    rows.append([KeyboardButton(text=ADVANCED_SEARCH_ALL_THICKNESSES_TEXT)])
    rows.append([KeyboardButton(text=CANCEL_TEXT)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def build_advanced_colors_keyboard(colors: list[str]) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for value in colors:
        rows.append([KeyboardButton(text=value)])
    rows.append([KeyboardButton(text=ADVANCED_SEARCH_ALL_COLORS_TEXT)])
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


async def send_film_settings_overview(message: Message) -> None:
    manufacturers = await fetch_film_manufacturers_with_series()
    if manufacturers:
        lines = []
        for manufacturer in manufacturers:
            name = manufacturer["name"]
            series = manufacturer.get("series") or []
            formatted_series = format_series_list(series)
            lines.append(
                "\n".join(
                    [
                        f"• {name}",
                        f"   Серии: {formatted_series}",
                    ]
                )
            )
        formatted = "\n".join(lines)
        intro = "Доступные производители и серии:"
    else:
        formatted = (
            "Производители ещё не добавлены. Добавьте производителей,"
            " а затем укажите для них серии."
        )
        intro = "Список производителей пуст."
    storage_locations = await fetch_film_storage_locations()
    storage_text = format_storage_locations_list(storage_locations)
    text = (
        "⚙️ Настройки склада → Пленки.\n\n"
        f"{intro}\n"
        f"{formatted}\n\n"
        "Используйте кнопки «🏭 Производитель» и «🎬 Серия», чтобы управлять списками."\
        "\n\n"
        "Места хранения:\n"
        f"{storage_text}\n\n"
        "Кнопка «🏬 Склад» поможет добавить или удалить место хранения."
    )
    await message.answer(text, reply_markup=WAREHOUSE_SETTINGS_FILM_KB)


async def send_film_manufacturers_menu(message: Message) -> None:
    manufacturers = await fetch_film_manufacturers()
    formatted = format_materials_list(manufacturers)
    await message.answer(
        "⚙️ Настройки склада → Пленки → Производитель.\n\n"
        "Доступные производители:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить производителя.",
        reply_markup=WAREHOUSE_SETTINGS_FILM_MANUFACTURERS_KB,
    )


async def send_film_storage_overview(message: Message) -> None:
    locations = await fetch_film_storage_locations()
    formatted = format_storage_locations_list(locations)
    await message.answer(
        "⚙️ Настройки склада → Пленки → Склад.\n\n"
        "Доступные места хранения:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить место.",
        reply_markup=WAREHOUSE_SETTINGS_FILM_STORAGE_KB,
    )


async def send_electrics_settings_overview(message: Message) -> None:
    await message.answer(
        "⚙️ Настройки склада → Электрика.\n\n"
        "Выберите категорию, которую хотите настроить.",
        reply_markup=WAREHOUSE_SETTINGS_ELECTRICS_KB,
    )

async def send_led_modules_settings_overview(message: Message) -> None:
    manufacturers = await fetch_led_module_manufacturers_with_series()
    storage_locations = await fetch_led_module_storage_locations()
    lens_counts = await fetch_led_module_lens_counts()
    colors = await fetch_led_module_colors()
    power_options = await fetch_led_module_power_options()
    voltage_options = await fetch_led_module_voltage_options()
    formatted_lens_counts = format_materials_list([str(value) for value in lens_counts])
    formatted_colors = format_materials_list(colors)
    formatted_power = format_materials_list(power_options)
    formatted_voltage = format_materials_list(voltage_options)
    formatted_storage = format_storage_locations_list(storage_locations)
    if manufacturers:
        lines: list[str] = []
        for manufacturer in manufacturers:
            name = manufacturer["name"]
            formatted_series = format_series_list(manufacturer.get("series") or [])
            lines.append(
                "\n".join(
                    [
                        f"• {name}",
                        f"   Серии: {formatted_series}",
                    ]
                )
            )
        formatted = "\n".join(lines)
        text = (
            "⚙️ Настройки склада → Электрика → Led модули.\n\n"
            "Доступные производители и серии:\n"
            f"{formatted}\n\n"
            "Используйте кнопки «🏭 Производитель Led модулей» и «🎬 Серия Led модулей», чтобы управлять списками."
        )
    else:
        text = (
            "⚙️ Настройки склада → Электрика → Led модули.\n\n"
            "Производители ещё не добавлены. Добавьте производителя, чтобы начать."\
            " Затем можно будет указать серии."
        )
    text += (
        "\n\nМеста хранения:\n"
        f"{formatted_storage}\n\n"
        "Используйте кнопку «🏬 Места хранения Led модулей», чтобы управлять списком."
    )
    text += (
        "\n\nДоступные количества линз:\n"
        f"{formatted_lens_counts}\n\n"
        "Используйте кнопку «🔢 Количество линз», чтобы управлять общим списком значений."
    )
    text += (
        "\n\nДоступные цвета модулей:\n"
        f"{formatted_colors}\n\n"
        "Используйте кнопку «🎨 Цвет модулей», чтобы управлять общим списком цветов."
    )
    text += (
        "\n\nДоступные мощности модулей:\n"
        f"{formatted_power}\n\n"
        "Используйте кнопку «⚡ Мощность модулей», чтобы управлять общим списком значений."
    )
    text += (
        "\n\nДоступные напряжения модулей:\n"
        f"{formatted_voltage}\n\n"
        "Используйте кнопку «🔌 Напряжение модулей», чтобы управлять общим списком значений."
    )
    await message.answer(text, reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB)


async def send_led_module_manufacturers_menu(message: Message) -> None:
    manufacturers = await fetch_led_module_manufacturers()
    formatted = format_materials_list(manufacturers)
    await message.answer(
        "⚙️ Настройки склада → Электрика → Led модули → Производитель.\n\n"
        "Доступные производители:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить производителя.",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_MANUFACTURERS_KB,
    )


async def send_led_module_storage_overview(message: Message) -> None:
    locations = await fetch_led_module_storage_locations()
    formatted = format_storage_locations_list(locations)
    await message.answer(
        "⚙️ Настройки склада → Электрика → Led модули → Места хранения.\n\n"
        "Доступные места хранения:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить место.",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_STORAGE_KB,
    )


async def send_led_module_base_menu(message: Message) -> None:
    modules = await fetch_generated_led_modules_with_details()
    if modules:
        lines = []
        for module in modules:
            article = module.get("article", "—")
            manufacturer = module.get("manufacturer", "—")
            series = module.get("series", "—")
            color = module.get("color", "—")
            lens_count = module.get("lens_count")
            power = module.get("power", "—")
            voltage = module.get("voltage", "—")
            lens_text = "—" if lens_count is None else str(lens_count)
            lines.append(
                " | ".join(
                    [
                        f"Артикул: {article}",
                        f"Производитель: {manufacturer}",
                        f"Серия: {series}",
                        f"Цвет: {color}",
                        f"Линз: {lens_text}",
                        f"Мощность: {power}",
                        f"Напряжение: {voltage}",
                    ]
                )
            )
        generated_text = "📋 Уже сгенерированные Led модули:\n" + "\n".join(lines)
    else:
        generated_text = (
            "ℹ️ Пока нет сгенерированных Led модулей. Используйте кнопку «Сгенерировать Led модуль»."
        )
    await message.answer(
        "⚙️ Настройки склада → Электрика → Led модули → Led модули baza.\n\n"
        f"{generated_text}\n\n"
        "Выберите действие, чтобы управлять базой Led модулей.",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_BASE_KB,
    )


async def send_led_module_colors_menu(message: Message) -> None:
    colors = await fetch_led_module_colors()
    formatted = format_materials_list(colors)
    await message.answer(
        "⚙️ Настройки склада → Электрика → Led модули → Цвет модулей.\n\n"
        "Доступные цвета:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить цвет.",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_COLORS_KB,
    )


async def send_led_module_power_menu(message: Message) -> None:
    power_options = await fetch_led_module_power_options()
    formatted = format_materials_list(power_options)
    await message.answer(
        "⚙️ Настройки склада → Электрика → Led модули → Мощность модулей.\n\n"
        "Доступные мощности:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить значение.",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_POWER_KB,
    )


async def send_led_module_voltage_menu(message: Message) -> None:
    voltage_options = await fetch_led_module_voltage_options()
    formatted = format_materials_list(voltage_options)
    await message.answer(
        "⚙️ Настройки склада → Электрика → Led модули → Напряжение модулей.\n\n"
        "Доступные напряжения:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить значение.",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_VOLTAGE_KB,
    )


async def send_led_module_lens_menu(message: Message) -> None:
    lens_counts = await fetch_led_module_lens_counts()
    formatted = format_materials_list([str(value) for value in lens_counts])
    await message.answer(
        "⚙️ Настройки склада → Электрика → Led модули → Количество линз.\n\n"
        "Доступные количества линз:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить значение.",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_LENS_KB,
    )


async def send_led_module_series_menu(message: Message) -> None:
    manufacturers = await fetch_led_module_manufacturers_with_series()
    if manufacturers:
        lines: list[str] = []
        for manufacturer in manufacturers:
            name = manufacturer["name"]
            formatted_series = format_series_list(manufacturer.get("series") or [])
            lines.append(
                "\n".join(
                    [
                        f"• {name}",
                        f"   Серии: {formatted_series}",
                    ]
                )
            )
        formatted = "\n".join(lines)
        text = (
            "⚙️ Настройки склада → Электрика → Led модули → Серия.\n\n"
            "Доступные серии по производителям:\n"
            f"{formatted}\n\n"
            "Используйте кнопки ниже, чтобы добавить или удалить серию."
        )
    else:
        text = (
            "⚙️ Настройки склада → Электрика → Led модули → Серия.\n\n"
            "Сначала добавьте производителей, чтобы создавать серии."
        )
    await message.answer(text, reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_SERIES_KB)


async def send_led_strips_settings_overview(message: Message) -> None:
    manufacturers = await fetch_led_strip_manufacturers()
    formatted = format_materials_list(manufacturers)
    await message.answer(
        "⚙️ Настройки склада → Электрика → Led лента.\n\n"
        "Доступные производители:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить производителя.",
        reply_markup=WAREHOUSE_SETTINGS_LED_STRIPS_MANUFACTURERS_KB,
    )


async def send_power_supplies_settings_overview(message: Message) -> None:
    manufacturers = await fetch_power_supply_manufacturers()
    formatted = format_materials_list(manufacturers)
    await message.answer(
        "⚙️ Настройки склада → Электрика → Блоки питания.\n\n"
        "Доступные производители:\n"
        f"{formatted}\n\n"
        "Используйте кнопки ниже, чтобы добавить или удалить производителя.",
        reply_markup=WAREHOUSE_SETTINGS_POWER_SUPPLIES_MANUFACTURERS_KB,
    )


# === Команды ===
@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer("👋 Привет! Выберите действие:", reply_markup=MAIN_MENU_KB)


@dp.message(Command("settings"))
@dp.message(F.text == "⚙️ Настройки")
async def handle_settings(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await message.answer("⚙️ Настройки. Выберите действие:", reply_markup=SETTINGS_MENU_KB)


@dp.message(F.text == "🔄 Перезагрузить")
async def handle_restart(message: Message) -> None:
    if not await ensure_admin_access(message):
        return

    if not UPDATE_SCRIPT_PATH.exists():
        await message.answer(
            "⚠️ Файл update.sh не найден на сервере.", reply_markup=SETTINGS_MENU_KB
        )
        return

    await message.answer(
        "♻️ Перезапуск системы начат... Подожди немного ⏳",
        reply_markup=SETTINGS_MENU_KB,
    )

    try:
        subprocess.Popen(
            ["bash", str(UPDATE_SCRIPT_PATH)],
            cwd=str(UPDATE_SCRIPT_PATH.parent),
        )
    except Exception as exc:
        await message.answer(
            f"⚠️ Ошибка при запуске обновления:\n`{exc}`",
            reply_markup=SETTINGS_MENU_KB,
        )
        return

    await message.answer(
        "✅ Скрипт обновления запущен!\nЯ пришлю уведомление, когда процесс завершится.",
        reply_markup=SETTINGS_MENU_KB,
    )


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


@dp.message(F.text == "📋 Посмотреть всех пользователей")
async def handle_list_all_users(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    users = await fetch_all_users_from_db()
    if not users:
        await message.answer(
            "ℹ️ В базе пока нет пользователей.", reply_markup=USERS_MENU_KB
        )
        return
    formatted_records = [
        format_user_record_for_message(record, index)
        for index, record in enumerate(users, start=1)
    ]
    header = f"📋 Всего пользователей: {len(users)}"
    full_text = f"{header}\n\n" + "\n\n".join(formatted_records)
    chunks = split_text_into_messages(full_text)
    for idx, chunk in enumerate(chunks):
        if idx == 0:
            await message.answer(chunk, reply_markup=USERS_MENU_KB)
        else:
            await message.answer(chunk)


@dp.message(F.text == "➕ Добавить пользователя")
async def handle_add_user_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await state.set_state(AddUserStates.waiting_for_tg_id)
    await message.answer(
        "Введите Telegram ID пользователя (только цифры).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddUserStates.waiting_for_tg_id)
async def process_add_user_tg_id(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_user_flow(message, state)
        return
    if not text.isdigit():
        await message.answer(
            "⚠️ Telegram ID должен содержать только цифры. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(tg_id=int(text))
    await state.set_state(AddUserStates.waiting_for_username)
    await message.answer(
        "Введите имя пользователя (как будет отображаться в списке).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddUserStates.waiting_for_username)
async def process_add_user_username(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_user_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Имя не может быть пустым. Введите имя пользователя.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(username=text)
    await state.set_state(AddUserStates.waiting_for_position)
    await message.answer(
        "Введите должность пользователя.",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddUserStates.waiting_for_position)
async def process_add_user_position(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_user_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Должность не может быть пустой. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(position=text)
    await state.set_state(AddUserStates.waiting_for_role)
    await message.answer(
        "Укажите роль пользователя (например, администратор или сотрудник).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddUserStates.waiting_for_role)
async def process_add_user_role(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_user_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Роль не может быть пустой. Введите описание роли.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(role=text)
    await state.set_state(AddUserStates.waiting_for_created_at)
    await message.answer(
        "Введите дату и время добавления пользователя (например, 2024-01-31 или"
        " 31.01.2024 09:30).\nЕсли хотите использовать текущее время, нажмите"
        " «Пропустить».",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


@dp.message(AddUserStates.waiting_for_created_at)
async def process_add_user_created_at(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_user_flow(message, state)
        return

    custom_created_at: Optional[datetime]
    if text == SKIP_TEXT:
        custom_created_at = None
    else:
        parsed = parse_user_created_at_input(text)
        if parsed is None:
            await message.answer(
                "⚠️ Не удалось распознать дату. Используйте формат ГГГГ-ММ-ДД или"
                " ГГГГ-ММ-ДД ЧЧ:ММ. Можно также ввести 31.01.2024 или 31.01.2024"
                " 09:30.",
                reply_markup=SKIP_OR_CANCEL_KB,
            )
            return
        custom_created_at = parsed

    data = await state.get_data()
    tg_id = data.get("tg_id")
    username = data.get("username")
    position = data.get("position")
    role = data.get("role")

    if tg_id is None or username is None or position is None or role is None:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось получить введённые данные. Попробуйте начать заново.",
            reply_markup=USERS_MENU_KB,
        )
        return

    try:
        await upsert_user_in_db(
            tg_id=int(tg_id),
            username=str(username),
            position=str(position),
            role=str(role),
            created_at=custom_created_at,
        )
    except Exception as exc:
        logging.exception("Failed to add or update user")
        await state.clear()
        await message.answer(
            "⚠️ Не удалось сохранить пользователя. Попробуйте позже.\n"
            f"Техническая информация: {exc}",
            reply_markup=USERS_MENU_KB,
        )
        return

    await state.clear()
    created_info = (
        _format_datetime(custom_created_at)
        if custom_created_at is not None
        else "текущее время (по умолчанию)"
    )
    await message.answer(
        "✅ Пользователь сохранён.\n"
        f"👤 Имя: {username}\n"
        f"🆔 TG ID: {tg_id}\n"
        f"🏢 Должность: {position}\n"
        f"🔐 Роль: {role}\n"
        f"🗓 Дата добавления: {created_info}",
        reply_markup=USERS_MENU_KB,
    )


@dp.message(F.text == "⬅️ Главное меню")
async def handle_back_to_main(message: Message) -> None:
    await message.answer("Главное меню.", reply_markup=MAIN_MENU_KB)


@dp.message(F.text == "Клиенты")
async def handle_clients_section(message: Message) -> None:
    await message.answer(
        "👥 Раздел «Клиенты». Выберите действие:", reply_markup=CLIENTS_MENU_KB
    )


@dp.message(F.text == CLIENTS_ADD_CLIENT_TEXT)
async def handle_clients_add(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddClientStates.waiting_for_name)
    await message.answer(
        "Введите название клиента:",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == CLIENTS_SEARCH_CLIENT_TEXT)
async def handle_clients_search(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SearchClientStates.waiting_for_query)
    await message.answer(
        "Введите название клиента или его часть:",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddClientStates.waiting_for_name)
async def process_add_client_name(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_client_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Название клиента не может быть пустым. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(name=text)
    await state.set_state(AddClientStates.waiting_for_phone)
    await message.answer(
        "Введите телефон клиента или нажмите «Пропустить»:",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


@dp.message(AddClientStates.waiting_for_phone)
async def process_add_client_phone(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_client_flow(message, state)
        return
    phone: Optional[str]
    if text == SKIP_TEXT or not text:
        phone = None
    else:
        phone = text
    await state.update_data(phone=phone)
    await state.set_state(AddClientStates.waiting_for_contact_person)
    await message.answer(
        "Введите контактное лицо или нажмите «Пропустить»:",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


@dp.message(AddClientStates.waiting_for_contact_person)
async def process_add_client_contact_person(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_client_flow(message, state)
        return
    contact_person: Optional[str]
    if text == SKIP_TEXT or not text:
        contact_person = None
    else:
        contact_person = text
    await state.update_data(contact_person=contact_person)
    await state.set_state(AddClientStates.waiting_for_address)
    await message.answer(
        "Введите адрес клиента или нажмите «Пропустить»:",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


@dp.message(AddClientStates.waiting_for_address)
async def process_add_client_address(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_client_flow(message, state)
        return
    address: Optional[str]
    if text == SKIP_TEXT or not text:
        address = None
    else:
        address = text
    await state.update_data(address=address)
    await state.set_state(AddClientStates.waiting_for_map_link)
    await message.answer(
        "Добавьте ссылку на Google Карты или нажмите «Пропустить»:",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


@dp.message(AddClientStates.waiting_for_map_link)
async def process_add_client_map_link(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_client_flow(message, state)
        return
    map_link: Optional[str]
    if text == SKIP_TEXT or not text:
        map_link = None
    else:
        map_link = text
    data = await state.get_data()
    name = data.get("name")
    phone = data.get("phone")
    contact_person = data.get("contact_person")
    address = data.get("address")
    if not name:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось получить данные клиента. Попробуйте начать заново.",
            reply_markup=CLIENTS_MENU_KB,
        )
        return
    try:
        client_id = await create_client_in_db(
            name=name,
            phone=phone,
            contact_person=contact_person,
        )
        if address is not None or map_link is not None:
            await add_client_address_in_db(
                client_id=client_id,
                address=address,
                google_maps_link=map_link,
            )
    except Exception as exc:
        logging.exception("Failed to add client")
        await state.clear()
        await message.answer(
            "⚠️ Не удалось сохранить клиента. Попробуйте позже.\n"
            f"Техническая информация: {exc}",
            reply_markup=CLIENTS_MENU_KB,
        )
        return

    await state.clear()
    phone_text = phone or "—"
    contact_text = contact_person or "—"
    address_text = address or "—"
    map_text = map_link or "—"
    await message.answer(
        "✅ Клиент добавлен.\n"
        f"🏢 Название: {name}\n"
        f"📞 Телефон: {phone_text}\n"
        f"👤 Контактное лицо: {contact_text}\n"
        f"📍 Адрес: {address_text}\n"
        f"🔗 Google Карты: {map_text}",
        reply_markup=CLIENTS_MENU_KB,
    )


@dp.message(SearchClientStates.waiting_for_query)
async def process_clients_search_query(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_client_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Название клиента не может быть пустым. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    try:
        matches, has_more = await search_clients_by_name(text)
    except Exception as exc:
        logging.exception("Failed to search clients")
        await state.clear()
        await message.answer(
            "⚠️ Не удалось выполнить поиск клиентов. Попробуйте позже.\n"
            f"Техническая информация: {exc}",
            reply_markup=CLIENTS_MENU_KB,
        )
        return

    await state.clear()
    if not matches:
        await message.answer(
            "ℹ️ Клиенты с таким названием не найдены.",
            reply_markup=CLIENTS_MENU_KB,
        )
        return

    cards: list[str] = []
    for client in matches:
        card_lines = [
            f"🏢 Название: {client['name']}",
            f"📞 Телефон: {client.get('phone') or '—'}",
            f"👤 Контактное лицо: {client.get('contact_person') or '—'}",
        ]
        addresses = client.get("addresses") or []
        if addresses:
            card_lines.append("📍 Адреса:")
            for entry in addresses:
                address_value = entry.get("address") or "—"
                map_link_value = entry.get("google_maps_link")
                card_lines.append(f"  • {address_value}")
                if map_link_value:
                    card_lines.append(f"    🔗 {map_link_value}")
        else:
            card_lines.append("📍 Адрес: —")
            card_lines.append("🔗 Google Карты: —")
        cards.append("\n".join(card_lines))

    response_text = "🔎 Результаты поиска:\n\n" + "\n\n".join(cards)
    if has_more:
        response_text += (
            "\n\nℹ️ Показаны первые 10 совпадений. Уточните запрос для более точного результата."
        )
    await message.answer(response_text, reply_markup=CLIENTS_MENU_KB)


@dp.message(F.text == "Заказы")
async def handle_orders_section(message: Message) -> None:
    await message.answer(
        "🧾 Раздел «Заказы». Выберите действие:", reply_markup=ORDERS_MENU_KB
    )


def _format_client_search_results_for_order(
    clients: list[Dict[str, Any]],
    has_more: bool,
) -> str:
    result_lines = ["🔎 Найденные заказчики:", ""]
    formatted_entries: list[str] = []
    for index, client in enumerate(clients, start=1):
        entry_lines = [f"{index}. {client['name']}"]
        contact_person = (client.get("contact_person") or "").strip()
        phone = (client.get("phone") or "").strip()
        if contact_person:
            entry_lines.append(f"   👤 Контакт: {contact_person}")
        if phone:
            entry_lines.append(f"   📞 Телефон: {phone}")
        addresses = client.get("addresses") or []
        if addresses:
            first_address = addresses[0]
            address_value = (first_address.get("address") or "").strip()
            if address_value:
                entry_lines.append(f"   📍 Адрес: {address_value}")
        formatted_entries.append("\n".join(entry_lines))
    result_lines.append("\n\n".join(formatted_entries))
    if has_more:
        result_lines.extend(
            [
                "",
                "ℹ️ Показаны первые совпадения. Уточните запрос для более точного результата.",
            ]
        )
    result_lines.extend(
        [
            "",
            "Отправьте номер подходящего заказчика или введите новый запрос для поиска.",
        ]
    )
    return "\n".join(result_lines)


def _build_order_summary_message(
    order_row: Dict[str, Any],
    client_details: Dict[str, Any],
) -> str:
    due_date_raw = order_row.get("due_date")
    if isinstance(due_date_raw, datetime):
        due_date_value: Optional[date] = due_date_raw.date()
    else:
        due_date_value = due_date_raw
    due_date_text = _format_date(due_date_value)
    deadline_line = "⏳ Дедлайн: —"
    if isinstance(due_date_value, date):
        today = datetime.now(WARSAW_TZ).date()
        days_diff = (due_date_value - today).days
        if days_diff > 0:
            deadline_line = f"⏳ Дедлайн: осталось {days_diff} дн."
        elif days_diff == 0:
            deadline_line = "⏳ Дедлайн: сегодня"
        else:
            deadline_line = f"⏳ Дедлайн: просрочка {abs(days_diff)} дн."
    created_text = _format_datetime(order_row.get("created_at"))
    is_urgent = bool(order_row.get("is_urgent"))
    creator_name = order_row.get("created_by_name")
    if not creator_name:
        creator_id = order_row.get("created_by_id")
        creator_name = f"ID: {creator_id}" if creator_id else "—"
    client_lines = [f"🏢 Заказчик: {client_details['name']}"]
    contact_person = (client_details.get("contact_person") or "").strip()
    phone = (client_details.get("phone") or "").strip()
    if contact_person:
        client_lines.append(f"👤 Контактное лицо: {contact_person}")
    if phone:
        client_lines.append(f"📞 Телефон: {phone}")
    summary_lines = [
        f"✅ Заказ №{order_row['order_number']} создан.",
        "",
        "\n".join(client_lines),
        "",
        f"📝 Название: {order_row['title']}",
        f"🗂️ Тип заказа: {order_row['order_type']}",
        f"📂 Папка: {order_row['folder_path']}",
        f"📅 Срок выполнения: {due_date_text}",
        deadline_line,
        f"🔥 Срочный: {'Да' if is_urgent else 'Нет'}",
        "",
        f"🕒 Создано: {created_text}",
        f"✍️ Создал: {creator_name}",
    ]
    return "\n".join(summary_lines)


def format_orders_overview(order_rows: list[Dict[str, Any]]) -> str:
    if not order_rows:
        return "ℹ️ В таблице заказов пока нет записей."

    overview_lines = ["📋 Заказы в работе:", ""]
    formatted_entries: list[str] = []

    for index, row in enumerate(order_rows, start=1):
        due_date_text = _format_date(row.get("due_date"))
        created_at_text = _format_datetime(row.get("created_at"))
        responsible = (row.get("created_by_name") or "").strip() or "—"
        urgency_text = "Да" if row.get("is_urgent") else "Нет"

        entry_lines = [
            f"{index}. №{row.get('order_number')} — {row.get('title')}",
            f"   🏢 Заказчик: {row.get('client_name')}",
            f"   🗂️ Тип: {row.get('order_type')}",
            f"   📁 Папка: {row.get('folder_path')}",
            f"   📅 Срок: {due_date_text}",
            f"   ⚡ Срочный: {urgency_text}",
            f"   🕒 Создан: {created_at_text}",
            f"   👤 Ответственный: {responsible}",
        ]
        formatted_entries.append("\n".join(entry_lines))

    overview_lines.append("\n\n".join(formatted_entries))
    return "\n".join(overview_lines)


async def _send_client_search_prompt(
    message: Message,
    state: FSMContext,
    query: str,
) -> None:
    matches, has_more = await search_clients_by_name(query)
    if not matches:
        await message.answer(
            "ℹ️ Заказчики с таким названием не найдены. Попробуйте другой запрос.",
            reply_markup=CANCEL_KB,
        )
        await state.set_state(CreateOrderStates.waiting_for_client_query)
        return
    await state.update_data(client_search_results=matches)
    await state.set_state(CreateOrderStates.waiting_for_client_selection)
    await message.answer(
        _format_client_search_results_for_order(matches, has_more),
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == ORDERS_IN_PROGRESS_TEXT)
async def handle_orders_in_progress(message: Message) -> None:
    try:
        orders = await fetch_all_orders()
    except Exception:
        logging.exception("Failed to fetch orders overview")
        await message.answer(
            "⚠️ Не удалось получить список заказов. Попробуйте позже.",
            reply_markup=ORDERS_MENU_KB,
        )
        return

    overview_text = format_orders_overview(orders)
    await message.answer(overview_text, reply_markup=ORDERS_MENU_KB)


@dp.message(F.text == ORDERS_NEW_ORDER_TEXT)
async def handle_orders_new_order(message: Message, state: FSMContext) -> None:
    await state.clear()
    order_types = await fetch_order_types()
    if not order_types:
        await message.answer(
            "ℹ️ Сначала добавьте хотя бы один тип заказа в разделе «⚙️ Настройки заказов».",
            reply_markup=ORDERS_MENU_KB,
        )
        return
    next_number = await fetch_next_order_number()
    await state.update_data(order_number_hint=next_number)
    await state.set_state(CreateOrderStates.waiting_for_client_query)
    await message.answer(
        (
            f"🆕 Создание заказа №{next_number}.\n"
            "Введите не менее двух символов названия заказчика, чтобы найти его в базе."
        ),
        reply_markup=CANCEL_KB,
    )


@dp.message(CreateOrderStates.waiting_for_client_query)
async def process_order_client_query(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_create_order_flow(message, state)
        return
    if len(text) < 2:
        await message.answer(
            "⚠️ Введите минимум два символа для поиска заказчика.",
            reply_markup=CANCEL_KB,
        )
        return
    await _send_client_search_prompt(message, state, text)


@dp.message(CreateOrderStates.waiting_for_client_selection)
async def process_order_client_selection(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_create_order_flow(message, state)
        return
    data = await state.get_data()
    candidates = data.get("client_search_results") or []
    if text.isdigit():
        selected_index = int(text)
        if 1 <= selected_index <= len(candidates):
            selected_client = candidates[selected_index - 1]
            await state.update_data(
                selected_client={
                    "id": selected_client["id"],
                    "name": selected_client["name"],
                    "phone": selected_client.get("phone"),
                    "contact_person": selected_client.get("contact_person"),
                },
                client_search_results=None,
            )
            await state.set_state(CreateOrderStates.waiting_for_order_name)
            await message.answer(
                (
                    f"✅ Заказчик «{selected_client['name']}» выбран.\n"
                    "Теперь введите название заказа."
                ),
                reply_markup=CANCEL_KB,
            )
            return
    if len(text) >= 2:
        await _send_client_search_prompt(message, state, text)
        return
    await message.answer(
        "ℹ️ Укажите номер из списка или введите новый запрос (минимум два символа).",
        reply_markup=CANCEL_KB,
    )


@dp.message(CreateOrderStates.waiting_for_order_name)
async def process_order_name(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_create_order_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Название заказа не может быть пустым. Укажите название.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(order_name=text)
    order_types = await fetch_order_types()
    if not order_types:
        await state.clear()
        await message.answer(
            "ℹ️ Список типов заказов пуст. Добавьте типы и начните заново.",
            reply_markup=ORDERS_MENU_KB,
        )
        return
    await state.update_data(order_type_options=order_types)
    options_lines = [f"{index}. {name}" for index, name in enumerate(order_types, start=1)]
    prompt_lines = [
        "Выберите тип заказа из списка:",
        "",
        "\n".join(options_lines),
        "",
        "Отправьте номер или название типа заказа.",
    ]
    await state.set_state(CreateOrderStates.waiting_for_order_type)
    await message.answer("\n".join(prompt_lines), reply_markup=CANCEL_KB)


@dp.message(CreateOrderStates.waiting_for_order_type)
async def process_order_type(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_create_order_flow(message, state)
        return
    data = await state.get_data()
    order_types = data.get("order_type_options") or []
    if not order_types:
        order_types = await fetch_order_types()
        if not order_types:
            await state.clear()
            await message.answer(
                "ℹ️ Список типов заказов пуст. Добавьте типы и начните заново.",
                reply_markup=ORDERS_MENU_KB,
            )
            return
        await state.update_data(order_type_options=order_types)
    selected_type: Optional[str] = None
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(order_types):
            selected_type = order_types[index - 1]
    else:
        lowered = text.lower()
        for option in order_types:
            if option.lower() == lowered:
                selected_type = option
                break
    if not selected_type:
        options_lines = "\n".join(
            f"{index}. {name}" for index, name in enumerate(order_types, start=1)
        )
        await message.answer(
            (
                "⚠️ Не удалось распознать тип заказа.\n"
                "Выберите номер из списка или укажите точное название.\n\n"
                f"Доступные варианты:\n{options_lines}"
            ),
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(order_type=selected_type)
    await state.set_state(CreateOrderStates.waiting_for_folder_path)
    await message.answer(
        "Укажите путь к папке заказа (например, сетевой путь или каталог на диске).",
        reply_markup=CANCEL_KB,
    )


@dp.message(CreateOrderStates.waiting_for_folder_path)
async def process_order_folder_path(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_create_order_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Путь к папке не может быть пустым. Укажите путь.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(order_folder_path=text)
    await state.set_state(CreateOrderStates.waiting_for_due_date)
    await message.answer(
        "Укажите дату, когда заказ должен быть выполнен (формат ДД.ММ.ГГГГ).",
        reply_markup=CANCEL_KB,
    )


@dp.message(CreateOrderStates.waiting_for_due_date)
async def process_order_due_date(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_create_order_flow(message, state)
        return
    due_date = _parse_due_date_input(text)
    if not due_date:
        await message.answer(
            "⚠️ Не удалось распознать дату. Укажите дату в формате ДД.ММ.ГГГГ.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(order_due_date=due_date)
    await state.set_state(CreateOrderStates.waiting_for_urgency)
    await message.answer(
        "Это срочный заказ? Ответьте «Да» или «Нет».",
        reply_markup=ORDER_URGENCY_KB,
    )


@dp.message(CreateOrderStates.waiting_for_urgency)
async def process_order_urgency(message: Message, state: FSMContext) -> None:
    text_raw = message.text or ""
    text = text_raw.strip().lower()
    if text_raw.strip() == CANCEL_TEXT:
        await _cancel_create_order_flow(message, state)
        return
    if text in {ORDER_URGENCY_YES_TEXT.lower(), "yes", "y"}:
        is_urgent = True
    elif text in {ORDER_URGENCY_NO_TEXT.lower(), "no", "n"}:
        is_urgent = False
    else:
        await message.answer(
            "⚠️ Ответ не распознан. Укажите «Да» или «Нет».",
            reply_markup=ORDER_URGENCY_KB,
        )
        return
    data = await state.get_data()
    selected_client = data.get("selected_client")
    if not selected_client:
        await state.clear()
        await message.answer(
            "ℹ️ Не удалось определить выбранного заказчика. Попробуйте создать заказ заново.",
            reply_markup=ORDERS_MENU_KB,
        )
        return
    order_name = data.get("order_name")
    order_type = data.get("order_type")
    folder_path = data.get("order_folder_path")
    due_date = data.get("order_due_date")
    if not all([order_name, order_type, folder_path, due_date]):
        await state.clear()
        await message.answer(
            "ℹ️ Данные заказа неполные. Попробуйте создать заказ заново.",
            reply_markup=ORDERS_MENU_KB,
        )
        return
    creator_id = message.from_user.id if message.from_user else None
    creator_name = message.from_user.full_name if message.from_user else None
    try:
        order_row = await create_order_in_db(
            client_id=int(selected_client["id"]),
            client_name=selected_client["name"],
            title=order_name,
            order_type=order_type,
            folder_path=folder_path,
            due_date=due_date,
            is_urgent=is_urgent,
            created_by_id=creator_id,
            created_by_name=creator_name,
        )
    except Exception:
        logging.exception("Failed to create order")
        await state.clear()
        await message.answer(
            "⚠️ Не удалось сохранить заказ. Попробуйте позже.",
            reply_markup=ORDERS_MENU_KB,
        )
        return
    await state.clear()
    summary = _build_order_summary_message(order_row, selected_client)
    await message.answer(summary, reply_markup=ORDERS_MENU_KB)


@dp.message(F.text == ORDERS_SETTINGS_TEXT)
async def handle_orders_settings(message: Message) -> None:
    await message.answer(
        "⚙️ Настройки заказов. Выберите категорию:",
        reply_markup=ORDERS_SETTINGS_KB,
    )


async def send_order_type_settings_overview(message: Message) -> None:
    order_types = await fetch_order_types()
    lines = ["📁 Настройки заказов → Тип заказа.", ""]
    if order_types:
        lines.extend(
            [
                "Сейчас доступны типы заказов:",
                format_order_types_list(order_types),
                "",
                "Используйте кнопки ниже, чтобы управлять типами.",
            ]
        )
    else:
        lines.extend(
            [
                "Типы заказов ещё не добавлены.",
                "Используйте кнопку «➕ Добавить тип заказа», чтобы создать первый тип.",
            ]
        )
    await message.answer("\n".join(lines), reply_markup=ORDERS_ORDER_TYPE_KB)


@dp.message(F.text == ORDERS_SETTINGS_ORDER_TYPE_TEXT)
async def handle_orders_settings_order_type(message: Message) -> None:
    await send_order_type_settings_overview(message)


@dp.message(F.text == ORDER_TYPE_ADD_TEXT)
async def handle_order_type_add(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(ManageOrderTypeStates.waiting_for_new_type_name)
    order_types = await fetch_order_types()
    prompt_lines = ["Введите название нового типа заказа."]
    if order_types:
        prompt_lines.extend(
            [
                "",
                "Уже добавлены:",
                format_order_types_list(order_types),
            ]
        )
    else:
        prompt_lines.extend(
            [
                "",
                "Пока ещё не добавлено ни одного типа заказа.",
            ]
        )
    await message.answer("\n".join(prompt_lines), reply_markup=CANCEL_KB)


@dp.message(F.text == ORDER_TYPE_DELETE_TEXT)
async def handle_order_type_delete(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await message.answer(
        "➖ Удаление типов заказов находится в разработке.",
        reply_markup=ORDERS_ORDER_TYPE_KB,
    )


@dp.message(ManageOrderTypeStates.waiting_for_new_type_name)
async def process_new_order_type(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_order_type(name):
        await message.answer(f"✅ Тип заказа «{name}» добавлен.")
    else:
        await message.answer(f"ℹ️ Тип заказа «{name}» уже есть в списке.")
    await state.clear()
    await send_order_type_settings_overview(message)


@dp.message(F.text == ORDER_TYPE_BACK_TEXT)
async def handle_back_to_orders_settings(message: Message) -> None:
    await message.answer(
        "⚙️ Настройки заказов. Выберите категорию:",
        reply_markup=ORDERS_SETTINGS_KB,
    )


@dp.message(F.text == ORDERS_SETTINGS_BACK_TEXT)
async def handle_back_to_orders(message: Message) -> None:
    await message.answer(
        "🧾 Раздел «Заказы». Выберите действие:",
        reply_markup=ORDERS_MENU_KB,
    )


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


@dp.message(F.text == "🎞️ Пленки")
async def handle_warehouse_films(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🎞️ Раздел «Пленки». Выберите действие:",
        reply_markup=WAREHOUSE_FILMS_KB,
    )


@dp.message(F.text == WAREHOUSE_ELECTRICS_TEXT)
async def handle_warehouse_electrics(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "⚡ Раздел «Электрика». Выберите категорию:",
        reply_markup=WAREHOUSE_ELECTRICS_KB,
    )


@dp.message(F.text == WAREHOUSE_ELECTRICS_LED_STRIPS_TEXT)
async def handle_warehouse_electrics_led_strips(
    message: Message, state: FSMContext
) -> None:
    await state.clear()
    await message.answer(
        "💡 Раздел «Led лента». Функционал находится в разработке.",
        reply_markup=WAREHOUSE_ELECTRICS_KB,
    )


@dp.message(F.text == WAREHOUSE_ELECTRICS_LED_MODULES_TEXT)
async def handle_warehouse_electrics_led_modules(
    message: Message, state: FSMContext
) -> None:
    await state.clear()
    await message.answer(
        "🧩 Раздел «Led модули». Выберите действие:",
        reply_markup=WAREHOUSE_LED_MODULES_KB,
    )


@dp.message(F.text == WAREHOUSE_LED_MODULES_STOCK_TEXT)
async def handle_led_module_stock(message: Message, state: FSMContext) -> None:
    await state.clear()
    stock = await fetch_led_module_stock_summary()
    if not stock:
        await message.answer(
            "ℹ️ На складе пока нет Led модулей. Добавьте позиции через кнопку "
            f"«{WAREHOUSE_LED_MODULES_ADD_TEXT}».",
            reply_markup=WAREHOUSE_LED_MODULES_KB,
        )
        return
    lines = []
    for item in stock:
        details = (
            f"{item['manufacturer']} / {item['series']} / {item['color']}, "
            f"{item['lens_count']} линз, {item['power']} / {item['voltage']}"
        )
        lines.append(
            f"• {item['article']} — {item['total_quantity']} шт. ({details})"
        )
    await message.answer(
        "📦 Остаток Led модулей на складе:\n\n" + "\n".join(lines),
        reply_markup=WAREHOUSE_LED_MODULES_KB,
    )


@dp.message(F.text == WAREHOUSE_LED_MODULES_ADD_TEXT)
async def handle_add_warehouse_led_modules(message: Message, state: FSMContext) -> None:
    await state.clear()
    modules = await fetch_generated_led_modules_with_details()
    if not modules:
        await message.answer(
            "ℹ️ В базе пока нет Led модулей. Добавьте их через «⚙️ Настройки склада → "
            "Электрика → Led модули → Led модули baza».",
            reply_markup=WAREHOUSE_LED_MODULES_KB,
        )
        return
    overview_lines = []
    for module in modules:
        line = (
            f"• {module['article']} — {module['manufacturer']} / {module['series']} / {module['color']}, "
            f"{module['lens_count']} линз, {module['power']} / {module['voltage']}"
        )
        overview_lines.append(line)
    await state.set_state(AddWarehouseLedModuleStates.waiting_for_module)
    await message.answer(
        "Выберите Led модуль, который нужно добавить на склад.\n\n"
        "Доступные позиции:\n"
        + "\n".join(overview_lines),
        reply_markup=build_led_module_articles_keyboard([module["article"] for module in modules]),
    )


@dp.message(AddWarehouseLedModuleStates.waiting_for_module)
async def process_add_led_module_selection(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_led_module_flow(message, state)
        return
    if not text:
        modules = await fetch_generated_led_modules_with_details()
        if not modules:
            await state.clear()
            await message.answer(
                "ℹ️ В базе пока нет Led модулей. Добавьте их через настройки.",
                reply_markup=WAREHOUSE_LED_MODULES_KB,
            )
            return
        await message.answer(
            "⚠️ Укажите артикул Led модуля, используя кнопки ниже.",
            reply_markup=build_led_module_articles_keyboard([module["article"] for module in modules]),
        )
        return
    module = await get_generated_led_module_by_article(text)
    if module is None:
        modules = await fetch_generated_led_modules_with_details()
        if not modules:
            await state.clear()
            await message.answer(
                "ℹ️ В базе пока нет Led модулей. Добавьте их через настройки.",
                reply_markup=WAREHOUSE_LED_MODULES_KB,
            )
            return
        await message.answer(
            "⚠️ Led модуль не найден. Выберите вариант из списка.",
            reply_markup=build_led_module_articles_keyboard([module["article"] for module in modules]),
        )
        return
    await state.update_data(
        selected_led_module_id=module["id"],
        selected_led_module_article=module["article"],
    )
    await state.set_state(AddWarehouseLedModuleStates.waiting_for_quantity)
    await message.answer(
        f"Укажите количество для артикула {module['article']} (положительное число).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehouseLedModuleStates.waiting_for_quantity)
async def process_add_led_module_quantity(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_led_module_flow(message, state)
        return
    quantity = parse_positive_integer(text)
    if quantity is None:
        await message.answer(
            "⚠️ Количество должно быть положительным числом. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    data = await state.get_data()
    module_id = data.get("selected_led_module_id")
    article = data.get("selected_led_module_article")
    if module_id is None or article is None:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось определить выбранный Led модуль. Начните заново.",
            reply_markup=WAREHOUSE_LED_MODULES_KB,
        )
        return
    added_by_id = message.from_user.id if message.from_user else None
    added_by_name = message.from_user.full_name if message.from_user else None
    try:
        record = await insert_warehouse_led_module_record(
            led_module_id=module_id,
            article=article,
            quantity=quantity,
            added_by_id=added_by_id,
            added_by_name=added_by_name,
        )
    except Exception:
        logging.exception("Failed to insert warehouse led module record")
        await state.clear()
        await message.answer(
            "⚠️ Не удалось сохранить запись. Попробуйте позже.",
            reply_markup=WAREHOUSE_LED_MODULES_KB,
        )
        return
    if not record:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось сохранить запись. Попробуйте позже.",
            reply_markup=WAREHOUSE_LED_MODULES_KB,
        )
        return
    details = await get_generated_led_module_details(module_id)
    await state.clear()
    details_lines: list[str] = []
    if details:
        details_lines.extend(
            [
                f"Артикул: {details['article']}",
                f"Производитель: {details['manufacturer']}",
                f"Серия: {details['series']}",
                f"Цвет: {details['color']}",
                f"Линз: {details['lens_count']}",
                f"Мощность: {details['power']}",
                f"Напряжение: {details['voltage']}",
            ]
        )
    else:
        details_lines.append(f"Артикул: {article}")
    quantity_value = record.get("quantity", quantity)
    summary_employee = record.get("added_by_name") or added_by_name or "—"
    added_at_text = _format_datetime(record.get("added_at"))
    details_lines.append(f"Количество: {quantity_value} шт")
    details_lines.append(f"Добавил: {summary_employee}")
    details_lines.append(f"Добавлено: {added_at_text}")
    await message.answer(
        "✅ Led модуль добавлен на склад.\n\n" + "\n".join(details_lines),
        reply_markup=WAREHOUSE_LED_MODULES_KB,
    )


@dp.message(F.text == WAREHOUSE_LED_MODULES_WRITE_OFF_TEXT)
async def handle_write_off_warehouse_led_modules(
    message: Message, state: FSMContext
) -> None:
    await state.clear()
    stock = await fetch_led_module_stock_summary()
    available_modules = [
        item for item in stock if int(item.get("total_quantity") or 0) > 0
    ]
    if not available_modules:
        await message.answer(
            "ℹ️ На складе нет Led модулей для списания.",
            reply_markup=WAREHOUSE_LED_MODULES_KB,
        )
        return
    overview_lines: list[str] = []
    for item in available_modules:
        details = (
            f"{item['manufacturer']} / {item['series']} / {item['color']}, "
            f"{item['lens_count']} линз, {item['power']} / {item['voltage']}"
        )
        overview_lines.append(
            f"• {item['article']} — {details}. Остаток: {item['total_quantity']} шт"
        )
    await state.set_state(WriteOffWarehouseLedModuleStates.waiting_for_module)
    await message.answer(
        "Выберите Led модуль, который нужно списать со склада.\n\n"
        "Доступные позиции:\n" + "\n".join(overview_lines),
        reply_markup=build_led_module_articles_keyboard(
            [item["article"] for item in available_modules]
        ),
    )


@dp.message(WriteOffWarehouseLedModuleStates.waiting_for_module)
async def process_write_off_led_module_selection(
    message: Message, state: FSMContext
) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_write_off_led_module_flow(message, state)
        return
    if not text:
        stock = await fetch_led_module_stock_summary()
        available_modules = [
            item for item in stock if int(item.get("total_quantity") or 0) > 0
        ]
        if not available_modules:
            await state.clear()
            await message.answer(
                "ℹ️ На складе нет Led модулей для списания.",
                reply_markup=WAREHOUSE_LED_MODULES_KB,
            )
            return
        await message.answer(
            "⚠️ Выберите артикул Led модуля из списка ниже.",
            reply_markup=build_led_module_articles_keyboard(
                [item["article"] for item in available_modules]
            ),
        )
        return
    module = await get_generated_led_module_by_article(text)
    if module is None:
        stock = await fetch_led_module_stock_summary()
        available_modules = [
            item for item in stock if int(item.get("total_quantity") or 0) > 0
        ]
        if not available_modules:
            await state.clear()
            await message.answer(
                "ℹ️ На складе нет Led модулей для списания.",
                reply_markup=WAREHOUSE_LED_MODULES_KB,
            )
            return
        await message.answer(
            "⚠️ Led модуль с таким артикулом не найден. Выберите вариант из списка.",
            reply_markup=build_led_module_articles_keyboard(
                [item["article"] for item in available_modules]
            ),
        )
        return
    available_quantity = await get_led_module_stock_quantity(module["id"])
    if available_quantity <= 0:
        await message.answer(
            "ℹ️ Указанный Led модуль отсутствует на складе. Выберите другой артикул.",
            reply_markup=WAREHOUSE_LED_MODULES_KB,
        )
        await state.clear()
        return
    await state.update_data(
        selected_led_module_id=module["id"],
        selected_led_module_article=module["article"],
        available_quantity=available_quantity,
    )
    await state.set_state(WriteOffWarehouseLedModuleStates.waiting_for_quantity)
    await message.answer(
        f"Укажите количество для списания (доступно {available_quantity} шт).",
        reply_markup=CANCEL_KB,
    )


@dp.message(WriteOffWarehouseLedModuleStates.waiting_for_quantity)
async def process_write_off_led_module_quantity(
    message: Message, state: FSMContext
) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_write_off_led_module_flow(message, state)
        return
    quantity = parse_positive_integer(text)
    if quantity is None:
        await message.answer(
            "⚠️ Количество должно быть положительным числом. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    data = await state.get_data()
    available_quantity = int(data.get("available_quantity") or 0)
    if available_quantity <= 0:
        await state.clear()
        await message.answer(
            "ℹ️ Указанный Led модуль отсутствует на складе.",
            reply_markup=WAREHOUSE_LED_MODULES_KB,
        )
        return
    if quantity > available_quantity:
        await message.answer(
            f"⚠️ Для списания доступно только {available_quantity} шт. Укажите другое количество.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(write_off_quantity=quantity)
    await state.set_state(WriteOffWarehouseLedModuleStates.waiting_for_project)
    await message.answer(
        "Укажите заказ, для которого списываются Led модули.",
        reply_markup=CANCEL_KB,
    )


@dp.message(WriteOffWarehouseLedModuleStates.waiting_for_project)
async def process_write_off_led_module_project(
    message: Message, state: FSMContext
) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_write_off_led_module_flow(message, state)
        return
    project = text
    if not project:
        await message.answer(
            "⚠️ Название заказа не может быть пустым. Укажите заказ.",
            reply_markup=CANCEL_KB,
        )
        return
    data = await state.get_data()
    module_id = data.get("selected_led_module_id")
    article = data.get("selected_led_module_article")
    quantity = data.get("write_off_quantity")
    if module_id is None or article is None or quantity is None:
        await _cancel_write_off_led_module_flow(message, state)
        return
    written_off_by_id = message.from_user.id if message.from_user else None
    written_off_by_name = message.from_user.full_name if message.from_user else None
    try:
        result = await write_off_led_module_stock(
            led_module_id=int(module_id),
            article=str(article),
            quantity=int(quantity),
            project=project,
            written_off_by_id=written_off_by_id,
            written_off_by_name=written_off_by_name,
        )
    except Exception:
        logging.exception("Failed to write off led modules")
        await state.clear()
        await message.answer(
            "⚠️ Не удалось списать Led модули. Попробуйте позже.",
            reply_markup=WAREHOUSE_LED_MODULES_KB,
        )
        return
    if result is None:
        current_available = await get_led_module_stock_quantity(int(module_id))
        if current_available <= 0:
            await state.clear()
            await message.answer(
                "ℹ️ Указанный Led модуль отсутствует на складе.",
                reply_markup=WAREHOUSE_LED_MODULES_KB,
            )
            return
        await state.update_data(available_quantity=current_available)
        await state.set_state(WriteOffWarehouseLedModuleStates.waiting_for_quantity)
        await message.answer(
            f"⚠️ Для списания доступно только {current_available} шт. Укажите другое количество.",
            reply_markup=CANCEL_KB,
        )
        return
    details = await get_generated_led_module_details(int(module_id))
    remaining_quantity = await get_led_module_stock_quantity(int(module_id))
    await state.clear()
    summary_lines: list[str] = []
    if details:
        summary_lines.extend(
            [
                f"Артикул: {details['article']}",
                f"Производитель: {details['manufacturer']}",
                f"Серия: {details['series']}",
                f"Цвет: {details['color']}",
                f"Линз: {details['lens_count']}",
                f"Мощность: {details['power']}",
                f"Напряжение: {details['voltage']}",
            ]
        )
    else:
        summary_lines.append(f"Артикул: {article}")
    summary_lines.append(f"Списано: {quantity} шт")
    summary_lines.append(f"Остаток на складе: {remaining_quantity} шт")
    summary_lines.append(f"Заказ: {project}")
    summary_lines.append(
        f"Списал: {result.get('written_off_by_name') or written_off_by_name or '—'}"
    )
    summary_lines.append(
        f"Дата списания: {_format_datetime(result.get('written_off_at'))}"
    )
    await message.answer(
        "✅ Led модули списаны со склада.\n\n" + "\n".join(summary_lines),
        reply_markup=WAREHOUSE_LED_MODULES_KB,
    )


@dp.message(F.text == WAREHOUSE_LED_MODULES_BACK_TO_ELECTRICS_TEXT)
async def handle_back_to_electrics_from_led_modules(
    message: Message, state: FSMContext
) -> None:
    await state.clear()
    await message.answer(
        "⚡ Раздел «Электрика». Выберите категорию:",
        reply_markup=WAREHOUSE_ELECTRICS_KB,
    )


@dp.message(F.text == WAREHOUSE_ELECTRICS_POWER_SUPPLIES_TEXT)
async def handle_warehouse_electrics_power_supplies(
    message: Message, state: FSMContext
) -> None:
    await state.clear()
    await message.answer(
        "🔌 Раздел «Блоки питания». Функционал находится в разработке.",
        reply_markup=WAREHOUSE_ELECTRICS_KB,
    )


async def _reply_films_feature_in_development(message: Message, feature: str) -> None:
    await message.answer(
        f"⚙️ Функция «{feature}» для раздела «Пленки» находится в разработке.",
        reply_markup=WAREHOUSE_FILMS_KB,
    )


@dp.message(F.text == WAREHOUSE_FILMS_ADD_TEXT)
async def handle_add_warehouse_film(message: Message, state: FSMContext) -> None:
    await state.clear()
    manufacturers = await fetch_film_manufacturers()
    if not manufacturers:
        await message.answer(
            "ℹ️ Справочник производителей пуст. Добавьте данные в настройках.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    await state.set_state(AddWarehouseFilmStates.waiting_for_article)
    suggested_article: Optional[str] = None
    last_article = await fetch_max_film_article()
    if last_article is not None:
        suggested_article = str(last_article + 1)
    await state.update_data(article_suggestion=suggested_article)
    prompt_lines = ["Введите артикул пленки (только цифры)."]
    if last_article is not None and suggested_article is not None:
        prompt_lines.append("")
        prompt_lines.append(
            "Последний добавленный артикул: "
            f"{last_article}. Нажмите кнопку ниже, чтобы использовать следующий номер."
        )
    await message.answer(
        "\n".join(prompt_lines),
        reply_markup=build_article_input_keyboard(suggested_article),
    )


@dp.message(F.text == WAREHOUSE_FILMS_WRITE_OFF_TEXT)
async def handle_write_off_warehouse_film(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(WriteOffWarehouseFilmStates.waiting_for_article)
    await message.answer(
        "Введите номер артикула, чтобы списать пленку со склада.",
        reply_markup=CANCEL_KB,
    )


@dp.message(WriteOffWarehouseFilmStates.waiting_for_article)
async def process_write_off_film_article(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_write_off_film_flow(message, state)
        return
    article = (message.text or "").strip()
    if not article.isdigit():
        await message.answer(
            "⚠️ Артикул должен содержать только цифры. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    record = await fetch_warehouse_film_by_article(article)
    if record is None:
        await message.answer(
            "ℹ️ Пленка с таким артикулом не найдена. Попробуйте другой артикул.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(film_id=record["id"], article=record.get("article"))
    formatted = format_film_record_for_message(record)
    await state.set_state(WriteOffWarehouseFilmStates.waiting_for_project)
    await message.answer(
        "Найдена запись:\n\n"
        f"{formatted}\n\n"
        "Укажите проект, на который выполняется списание.",
        reply_markup=CANCEL_KB,
    )


@dp.message(WriteOffWarehouseFilmStates.waiting_for_project)
async def process_write_off_film_project(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_write_off_film_flow(message, state)
        return
    project = (message.text or "").strip()
    if not project:
        await message.answer(
            "⚠️ Название проекта не может быть пустым. Укажите проект.",
            reply_markup=CANCEL_KB,
        )
        return
    data = await state.get_data()
    record_id = data.get("film_id")
    article = data.get("article")
    if record_id is None or article is None:
        await _cancel_write_off_film_flow(message, state)
        return
    written_off_by_id = message.from_user.id if message.from_user else None
    written_off_by_name = message.from_user.full_name if message.from_user else None
    try:
        result = await write_off_warehouse_film(
            record_id=record_id,
            project=project,
            written_off_by_id=written_off_by_id,
            written_off_by_name=written_off_by_name,
        )
    except Exception:
        logging.exception("Failed to write off film record")
        await state.clear()
        await message.answer(
            "⚠️ Не удалось списать пленку. Попробуйте позже.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    if result is None:
        await state.clear()
        await message.answer(
            "ℹ️ Не удалось найти запись для списания. Возможно, она уже была изменена.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    await state.clear()
    formatted = format_written_off_film_record(result)
    await message.answer(
        "✅ Пленка списана со склада.\n\n"
        f"Артикул: {article}\n"
        f"Проект: {project}\n\n"
        f"Данные списанной записи:\n{formatted}",
        reply_markup=WAREHOUSE_FILMS_KB,
    )


@dp.message(F.text == WAREHOUSE_FILMS_COMMENT_TEXT)
async def handle_comment_warehouse_film(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CommentWarehouseFilmStates.waiting_for_article)
    await message.answer(
        "Введите номер артикула, чтобы просмотреть и изменить комментарий к пленке.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == WAREHOUSE_FILMS_MOVE_TEXT)
async def handle_move_warehouse_film(message: Message, state: FSMContext) -> None:
    await state.clear()
    locations = await fetch_film_storage_locations()
    if not locations:
        await message.answer(
            "Справочник мест хранения пуст. Добавьте места в настройках склада.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    await state.set_state(MoveWarehouseFilmStates.waiting_for_article)
    await message.answer(
        "Введите номер артикула, чтобы выбрать новое место хранения.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == WAREHOUSE_FILMS_SEARCH_TEXT)
async def handle_search_warehouse_film(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SearchWarehouseFilmStates.choosing_mode)
    await message.answer(
        "Выберите параметр поиска:",
        reply_markup=WAREHOUSE_FILMS_SEARCH_KB,
    )


@dp.message(F.text == WAREHOUSE_FILMS_EXPORT_TEXT)
async def handle_export_warehouse_film(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("⏳ Формирую файл экспорта. Пожалуйста, подождите...")
    try:
        records = await fetch_all_warehouse_films()
    except Exception:
        logging.exception("Failed to fetch films for export")
        await message.answer(
            "⚠️ Не удалось получить данные склада. Попробуйте позже.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return

    if not records:
        await message.answer(
            "ℹ️ Нет данных для экспорта.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return

    try:
        export_file = build_films_export_file(records)
    except Exception:
        logging.exception("Failed to build films export file")
        await message.answer(
            "⚠️ Не удалось сформировать файл экспорта. Попробуйте позже.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return

    await message.answer_document(
        document=export_file,
        caption="📄 Экспорт пленок",
        reply_markup=WAREHOUSE_FILMS_KB,
    )


@dp.message(SearchWarehouseFilmStates.choosing_mode)
async def process_search_film_menu(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_film_flow(message, state)
        return
    if text == WAREHOUSE_FILMS_SEARCH_BACK_TEXT:
        await state.clear()
        await message.answer(
            "Вы вернулись в раздел «Пленки».",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    if text == WAREHOUSE_FILMS_SEARCH_BY_ARTICLE_TEXT:
        await state.set_state(SearchWarehouseFilmStates.waiting_for_article)
        await message.answer(
            "Введите артикул пленки для поиска.",
            reply_markup=CANCEL_KB,
        )
        return
    if text == WAREHOUSE_FILMS_SEARCH_BY_NUMBER_TEXT:
        await state.set_state(SearchWarehouseFilmStates.waiting_for_number)
        await message.answer(
            "Введите номер пленки (код цвета).",
            reply_markup=CANCEL_KB,
        )
        return
    if text == WAREHOUSE_FILMS_SEARCH_BY_COLOR_TEXT:
        await state.set_state(SearchWarehouseFilmStates.waiting_for_color)
        await message.answer(
            "Укажите цвет или его часть для поиска.",
            reply_markup=CANCEL_KB,
        )
        return
    await message.answer(
        "Пожалуйста, выберите один из вариантов меню ниже.",
        reply_markup=WAREHOUSE_FILMS_SEARCH_KB,
    )


@dp.message(SearchWarehouseFilmStates.waiting_for_article)
async def process_search_film_by_article(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_film_flow(message, state)
        return
    if not text.isdigit():
        await message.answer(
            "⚠️ Артикул должен содержать только цифры. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    record = await fetch_warehouse_film_by_article(text)
    if record is None:
        await message.answer(
            "ℹ️ Пленка с таким артикулом не найдена. Попробуйте другой артикул.",
            reply_markup=CANCEL_KB,
        )
        return
    formatted = format_film_record_for_message(record)
    await message.answer(
        "Найдена запись:\n\n" f"{formatted}",
        reply_markup=CANCEL_KB,
    )
    await message.answer(
        "Введите другой артикул для нового поиска или нажмите «❌ Отмена».",
        reply_markup=CANCEL_KB,
    )


@dp.message(SearchWarehouseFilmStates.waiting_for_number)
async def process_search_film_by_number(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_film_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Номер не может быть пустым. Укажите значение.",
            reply_markup=CANCEL_KB,
        )
        return
    matches = await search_warehouse_films_by_color_code(
        text, limit=FILM_SEARCH_RESULTS_LIMIT
    )
    if matches:
        if len(matches) == 1:
            formatted = format_film_record_for_message(matches[0])
            await message.answer(
                "Найдена запись:\n\n" f"{formatted}",
                reply_markup=CANCEL_KB,
            )
        else:
            formatted_list = format_film_records_list_for_message(matches)
            header = [f"Найдено записей: {len(matches)}."]
            if len(matches) == FILM_SEARCH_RESULTS_LIMIT:
                header.append(
                    f"Показаны первые {FILM_SEARCH_RESULTS_LIMIT} записей. Уточните запрос для более точного поиска."
                )
            await message.answer(
                " ".join(header) + "\n\n" + formatted_list,
                reply_markup=CANCEL_KB,
            )
        await message.answer(
            "Введите другой номер (код цвета) для поиска или нажмите «❌ Отмена».",
            reply_markup=CANCEL_KB,
        )
        return
    if text.isdigit():
        record_id = int(text)
        record = await fetch_warehouse_film_by_id(record_id)
        if record is not None:
            formatted = format_film_record_for_message(record)
            await message.answer(
                "Найдена запись:\n\n" f"{formatted}",
                reply_markup=CANCEL_KB,
            )
            await message.answer(
                "Введите другой номер (код цвета) для поиска или нажмите «❌ Отмена».",
                reply_markup=CANCEL_KB,
            )
            return
    await message.answer(
        "ℹ️ Пленки с таким номером (кодом цвета) не найдены. Попробуйте указать другой номер.",
        reply_markup=CANCEL_KB,
    )


@dp.message(SearchWarehouseFilmStates.waiting_for_color)
async def process_search_film_by_color(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_film_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Цвет не может быть пустым. Укажите значение для поиска.",
            reply_markup=CANCEL_KB,
        )
        return
    matches = await search_warehouse_films_by_color(
        text, limit=FILM_SEARCH_RESULTS_LIMIT
    )
    if not matches:
        await message.answer(
            "ℹ️ Пленки с таким цветом не найдены. Попробуйте другой запрос.",
            reply_markup=CANCEL_KB,
        )
        return
    formatted_list = format_film_records_list_for_message(matches)
    header = [f"Найдено записей: {len(matches)}."]
    if len(matches) == FILM_SEARCH_RESULTS_LIMIT:
        header.append(
            f"Показаны первые {FILM_SEARCH_RESULTS_LIMIT} записей. Уточните запрос для более точного поиска."
        )
    await message.answer(
        " ".join(header) + "\n\n" + formatted_list,
        reply_markup=CANCEL_KB,
    )
    await message.answer(
        "Введите другой цвет для поиска или нажмите «❌ Отмена».",
        reply_markup=CANCEL_KB,
    )


@dp.message(CommentWarehouseFilmStates.waiting_for_article)
async def process_film_comment_article(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_comment_film_flow(message, state)
        return
    article = (message.text or "").strip()
    if not article.isdigit():
        await message.answer(
            "⚠️ Артикул должен содержать только цифры. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    record = await fetch_warehouse_film_by_article(article)
    if record is None:
        await message.answer(
            "ℹ️ Пленка с таким артикулом не найдена. Попробуйте другой артикул.",
            reply_markup=CANCEL_KB,
        )
        return
    previous_comment = record.get("comment")
    formatted = format_film_record_for_message(record)
    await state.update_data(
        film_id=record["id"],
        article=record.get("article"),
        previous_comment=previous_comment,
    )
    await state.set_state(CommentWarehouseFilmStates.waiting_for_comment)
    await message.answer(
        "Найдена запись:\n\n"
        f"{formatted}\n\n"
        f"Текущий комментарий: {previous_comment or '—'}",
        reply_markup=CANCEL_KB,
    )
    await message.answer(
        "Введите новый комментарий. Пустое сообщение удалит существующий комментарий.",
        reply_markup=CANCEL_KB,
    )


@dp.message(CommentWarehouseFilmStates.waiting_for_comment)
async def process_film_comment_update(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_comment_film_flow(message, state)
        return
    data = await state.get_data()
    record_id = data.get("film_id")
    article = data.get("article")
    previous_comment = data.get("previous_comment")
    if record_id is None or article is None:
        await _cancel_comment_film_flow(message, state)
        return
    new_comment_raw = (message.text or "").strip()
    new_comment: Optional[str]
    if new_comment_raw:
        new_comment = new_comment_raw
    else:
        new_comment = None
    updated = await update_warehouse_film_comment(record_id, new_comment)
    if not updated:
        await message.answer(
            "⚠️ Не удалось обновить комментарий. Попробуйте позже.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        await state.clear()
        return
    await state.clear()
    await message.answer(
        "✅ Комментарий обновлён.\n"
        f"Артикул: {article}\n"
        f"Старый комментарий: {previous_comment or '—'}\n"
        f"Новый комментарий: {new_comment or '—'}",
        reply_markup=WAREHOUSE_FILMS_KB,
    )


@dp.message(MoveWarehouseFilmStates.waiting_for_article)
async def process_move_film_article(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_move_film_flow(message, state)
        return
    article = (message.text or "").strip()
    if not article.isdigit():
        await message.answer(
            "⚠️ Артикул должен содержать только цифры. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    record = await fetch_warehouse_film_by_article(article)
    if record is None:
        await message.answer(
            "ℹ️ Пленка с таким артикулом не найдена. Попробуйте другой артикул.",
            reply_markup=CANCEL_KB,
        )
        return
    locations = await fetch_film_storage_locations()
    if not locations:
        await state.clear()
        await message.answer(
            "Справочник мест хранения пуст. Добавьте места в настройках склада.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    await state.update_data(
        film_id=record["id"],
        article=record.get("article"),
        previous_location=record.get("warehouse"),
    )
    previous_location = record.get("warehouse") or "—"
    formatted_record = format_film_record_for_message(record)
    await state.set_state(MoveWarehouseFilmStates.waiting_for_new_location)
    await message.answer(
        "Найдена запись:\n\n"
        f"{formatted_record}\n\n"
        f"Текущее место хранения: {previous_location}\n\n"
        "Выберите новое место хранения из списка ниже.",
        reply_markup=build_storage_locations_keyboard(locations),
    )


@dp.message(MoveWarehouseFilmStates.waiting_for_new_location)
async def process_move_film_new_location(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_move_film_flow(message, state)
        return
    locations = await fetch_film_storage_locations()
    if not locations:
        await state.clear()
        await message.answer(
            "Справочник мест хранения пуст. Добавьте места в настройках склада.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    raw_location = (message.text or "").strip()
    match = next((item for item in locations if item.lower() == raw_location.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Место хранения не найдено. Выберите одно из списка.",
            reply_markup=build_storage_locations_keyboard(locations),
        )
        return
    data = await state.get_data()
    record_id = data.get("film_id")
    article = data.get("article")
    previous_location_raw = data.get("previous_location")
    previous_location_display = previous_location_raw or "—"
    if record_id is None or article is None:
        await _cancel_move_film_flow(message, state)
        return
    if previous_location_raw and previous_location_raw.lower() == match.lower():
        await message.answer(
            "ℹ️ Пленка уже находится в выбранном месте. Выберите другое место.",
            reply_markup=build_storage_locations_keyboard(locations),
        )
        return
    employee_id = message.from_user.id if message.from_user else None
    employee_nick: Optional[str] = None
    if message.from_user:
        employee_nick = message.from_user.username or message.from_user.full_name
    updated_record = await update_warehouse_film_location(
        record_id=record_id,
        new_location=match,
        employee_id=employee_id,
        employee_nick=employee_nick,
    )
    if updated_record is None:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось обновить место хранения. Попробуйте позже.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    await state.clear()
    formatted = format_film_record_for_message(updated_record)
    await message.answer(
        "✅ Место хранения обновлено.\n\n"
        f"Артикул: {article}\n"
        f"Предыдущее место: {previous_location_display}\n"
        f"Новое место: {match}\n\n"
        f"Актуальные данные:\n{formatted}",
        reply_markup=WAREHOUSE_FILMS_KB,
    )


@dp.message(AddWarehouseFilmStates.waiting_for_article)
async def process_film_article(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_film_flow(message, state)
        return
    article = (message.text or "").strip()
    if not article.isdigit():
        data = await state.get_data()
        suggestion = data.get("article_suggestion")
        await message.answer(
            "⚠️ Артикул должен содержать только цифры. Попробуйте снова.",
            reply_markup=build_article_input_keyboard(suggestion),
        )
        return
    manufacturers = await fetch_film_manufacturers()
    if not manufacturers:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник производителей пуст. Добавьте данные в настройках.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    await state.update_data(article=article, article_suggestion=None)
    await state.set_state(AddWarehouseFilmStates.waiting_for_manufacturer)
    await message.answer(
        "Выберите производителя:",
        reply_markup=build_manufacturers_keyboard(manufacturers),
    )


@dp.message(AddWarehouseFilmStates.waiting_for_manufacturer)
async def process_film_manufacturer(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_film_flow(message, state)
        return
    manufacturers = await fetch_film_manufacturers()
    raw = (message.text or "").strip()
    match = next((item for item in manufacturers if item.lower() == raw.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Производитель не найден. Выберите из списка.",
            reply_markup=build_manufacturers_keyboard(manufacturers),
        )
        return
    series_list = await fetch_film_series_by_manufacturer(match)
    if not series_list:
        await state.clear()
        await message.answer(
            "ℹ️ Для выбранного производителя не указаны серии. Добавьте их в настройках.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    await state.update_data(manufacturer=match)
    await state.set_state(AddWarehouseFilmStates.waiting_for_series)
    await message.answer(
        "Выберите серию:",
        reply_markup=build_series_keyboard(series_list),
    )


@dp.message(AddWarehouseFilmStates.waiting_for_series)
async def process_film_series(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_film_flow(message, state)
        return
    data = await state.get_data()
    manufacturer = data.get("manufacturer")
    if not manufacturer:
        await _cancel_add_film_flow(message, state)
        return
    series_list = await fetch_film_series_by_manufacturer(manufacturer)
    raw = (message.text or "").strip()
    match = next((item for item in series_list if item.lower() == raw.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Серия не найдена. Выберите из списка.",
            reply_markup=build_series_keyboard(series_list),
        )
        return
    await state.update_data(series=match)
    await state.set_state(AddWarehouseFilmStates.waiting_for_color_code)
    await message.answer(
        "Введите код цвета (например, 3-45).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehouseFilmStates.waiting_for_color_code)
async def process_film_color_code(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_film_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Код цвета не может быть пустым. Укажите значение.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(color_code=text)
    await state.set_state(AddWarehouseFilmStates.waiting_for_color)
    await message.answer(
        "Укажите цвет (например, Белый).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehouseFilmStates.waiting_for_color)
async def process_film_color(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_film_flow(message, state)
        return
    if not text:
        await message.answer(
            "⚠️ Цвет не может быть пустым. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(color=text)
    await state.set_state(AddWarehouseFilmStates.waiting_for_width)
    await message.answer(
        "Укажите ширину пленки в миллиметрах (можно через точку или запятую).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehouseFilmStates.waiting_for_width)
async def process_film_width(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_film_flow(message, state)
        return
    value = parse_positive_decimal(message.text or "")
    if value is None:
        await message.answer(
            "⚠️ Ширина должна быть положительным числом. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(width=value)
    await state.set_state(AddWarehouseFilmStates.waiting_for_length)
    await message.answer(
        "Укажите длину пленки в миллиметрах (можно через точку или запятую).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehouseFilmStates.waiting_for_length)
async def process_film_length(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_film_flow(message, state)
        return
    value = parse_positive_decimal(message.text or "")
    if value is None:
        await message.answer(
            "⚠️ Длина должна быть положительным числом. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    locations = await fetch_film_storage_locations()
    if not locations:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник мест хранения пуст. Добавьте места в настройках.",
            reply_markup=WAREHOUSE_FILMS_KB,
        )
        return
    await state.update_data(length=value)
    await state.set_state(AddWarehouseFilmStates.waiting_for_storage)
    await message.answer(
        "Выберите место хранения:",
        reply_markup=build_storage_locations_keyboard(locations),
    )


@dp.message(AddWarehouseFilmStates.waiting_for_storage)
async def process_film_storage(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_film_flow(message, state)
        return
    locations = await fetch_film_storage_locations()
    raw = (message.text or "").strip()
    match = next((item for item in locations if item.lower() == raw.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Место хранения не найдено. Выберите из списка.",
            reply_markup=build_storage_locations_keyboard(locations),
        )
        return
    await state.update_data(storage=match)
    await state.set_state(AddWarehouseFilmStates.waiting_for_comment)
    await message.answer(
        "Добавьте комментарий (необязательно) или нажмите «Пропустить».",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


@dp.message(AddWarehouseFilmStates.waiting_for_comment)
async def process_film_comment(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_film_flow(message, state)
        return
    comment: Optional[str]
    if text == SKIP_TEXT:
        comment = None
    else:
        comment = text or None
    data = await state.get_data()
    article = data.get("article")
    manufacturer = data.get("manufacturer")
    series = data.get("series")
    color_code = data.get("color_code")
    color = data.get("color")
    width: Optional[Decimal] = data.get("width")
    length: Optional[Decimal] = data.get("length")
    storage = data.get("storage")
    if not all([article, manufacturer, series, color_code, color, width, length, storage]):
        await _cancel_add_film_flow(message, state)
        return
    employee_id = message.from_user.id if message.from_user else None
    employee_nick: Optional[str] = None
    if message.from_user:
        employee_nick = message.from_user.username or message.from_user.full_name
    record = await insert_warehouse_film_record(
        article=article,
        manufacturer=manufacturer,
        series=series,
        color_code=color_code,
        color=color,
        width_mm=width,
        length_mm=length,
        warehouse=storage,
        comment=comment,
        employee_id=employee_id,
        employee_nick=employee_nick,
    )
    await state.clear()
    formatted = format_film_record_for_message(record)
    await message.answer(
        "✅ Пленка добавлена на склад.\n\n"
        f"{formatted}",
        reply_markup=WAREHOUSE_FILMS_KB,
    )


@dp.message(F.text == "📤 Экспорт")
async def handle_export_warehouse_plastics(message: Message) -> None:
    await message.answer("⏳ Формирую файл экспорта. Пожалуйста, подождите...")
    try:
        records = await fetch_all_warehouse_plastics()
    except Exception:
        logging.exception("Failed to fetch plastics for export")
        await message.answer(
            "⚠️ Не удалось получить данные склада. Попробуйте позже.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return

    if not records:
        await message.answer(
            "ℹ️ Нет данных для экспорта.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return

    try:
        export_file = build_plastics_export_file(records)
    except Exception:
        logging.exception("Failed to build plastics export file")
        await message.answer(
            "⚠️ Не удалось сформировать файл экспорта. Попробуйте позже.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return

    await message.answer_document(
        document=export_file,
        caption="📄 Экспорт пластиков",
        reply_markup=WAREHOUSE_PLASTICS_KB,
    )


@dp.message(F.text == "🔍 Найти")
async def handle_search_warehouse_plastic(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchWarehousePlasticStates.choosing_mode)
    await message.answer(
        "Выберите вариант поиска:",
        reply_markup=WAREHOUSE_PLASTICS_SEARCH_KB,
    )


@dp.message(SearchWarehousePlasticStates.choosing_mode)
async def process_search_menu_choice(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_plastic_flow(message, state)
        return
    if text == BACK_TO_PLASTICS_MENU_TEXT:
        await state.clear()
        await message.answer(
            "Вы вернулись в меню пластиков.", reply_markup=WAREHOUSE_PLASTICS_KB
        )
        return
    if text == SEARCH_BY_ARTICLE_TEXT:
        await state.set_state(SearchWarehousePlasticStates.waiting_for_article)
        await message.answer(
            "Введите номер артикула для поиска.",
            reply_markup=CANCEL_KB,
        )
        return
    if text == ADVANCED_SEARCH_TEXT:
        await _start_advanced_search_flow(message, state)
        return
    await message.answer(
        "Пожалуйста, выберите один из вариантов ниже.",
        reply_markup=WAREHOUSE_PLASTICS_SEARCH_KB,
    )


@dp.message(F.text == "💬 Комментировать")
async def handle_comment_warehouse_plastic(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CommentWarehousePlasticStates.waiting_for_article)
    await message.answer(
        "Введите номер артикула, чтобы просмотреть и изменить комментарий.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "🔁 Переместить")
async def handle_move_warehouse_plastic(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(MoveWarehousePlasticStates.waiting_for_article)
    await message.answer(
        "Введите номер артикула, чтобы выбрать новое место хранения.",
        reply_markup=CANCEL_KB,
    )


@dp.message(F.text == "➖ Списать")
async def handle_write_off_warehouse_plastic(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(WriteOffWarehousePlasticStates.waiting_for_article)
    await message.answer(
        "Введите номер артикула, чтобы списать пластик со склада.",
        reply_markup=CANCEL_KB,
    )


@dp.message(SearchWarehousePlasticStates.waiting_for_article)
async def process_search_plastic_by_article(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_plastic_flow(message, state)
        return
    if not text.isdigit():
        await message.answer(
            "⚠️ Артикул должен содержать только цифры. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    record = await fetch_warehouse_plastic_by_article(text)
    if record is None:
        await message.answer(
            "ℹ️ Пластик с таким артикулом не найден. Попробуйте другой номер.",
            reply_markup=CANCEL_KB,
        )
        return
    await message.answer(
        "Найдена запись:\n\n" f"{format_plastic_record_for_message(record)}",
        reply_markup=CANCEL_KB,
    )
    await message.answer(
        "Введите другой артикул для нового поиска или нажмите «❌ Отмена».",
        reply_markup=CANCEL_KB,
    )


async def _start_advanced_search_flow(message: Message, state: FSMContext) -> None:
    materials = await fetch_plastic_material_types()
    await state.update_data(
        advanced_material=None,
        advanced_thickness=None,
        advanced_color=None,
        advanced_min_length=None,
        advanced_min_width=None,
    )
    if not materials:
        await message.answer(
            "Справочник материалов пуст. Поиск будет выполнен по всем материалам."
        )
        await _prompt_advanced_thickness_choice(message, state, None)
        return
    await state.set_state(SearchWarehousePlasticStates.waiting_for_material)
    await message.answer(
        "Выберите материал или нажмите «➡️ Далее», чтобы искать по всем материалам.",
        reply_markup=build_advanced_materials_keyboard(materials),
    )


async def _prompt_advanced_thickness_choice(
    message: Message, state: FSMContext, material: Optional[str]
) -> None:
    if material:
        thicknesses = await fetch_material_thicknesses(material)
    else:
        thicknesses = await fetch_all_material_thicknesses()
    if not thicknesses:
        await state.update_data(advanced_thickness=None)
        await message.answer(
            "Для выбранного материала не указаны толщины. Поиск будет выполнен по всем толщинам."
        )
        await _prompt_advanced_color_choice(message, state, material)
        return
    await state.set_state(SearchWarehousePlasticStates.waiting_for_thickness)
    await message.answer(
        "Выберите толщину или нажмите «📏 Все толщины».",
        reply_markup=build_advanced_thickness_keyboard(thicknesses),
    )


async def _prompt_advanced_color_choice(
    message: Message, state: FSMContext, material: Optional[str]
) -> None:
    if material:
        colors = await fetch_material_colors(material)
    else:
        colors = await fetch_all_material_colors()
    if not colors:
        await state.update_data(advanced_color=None)
        await message.answer(
            "Для выбранного материала не указаны цвета. Поиск будет выполнен по всем цветам."
        )
        await _prompt_advanced_min_length(message, state)
        return
    await state.set_state(SearchWarehousePlasticStates.waiting_for_color)
    await message.answer(
        "Выберите цвет или нажмите «🎨 Все цвета».",
        reply_markup=build_advanced_colors_keyboard(colors),
    )


async def _prompt_advanced_min_length(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchWarehousePlasticStates.waiting_for_min_length)
    await message.answer(
        "Укажите минимальную длину листа в миллиметрах или нажмите «Пропустить».",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


async def _prompt_advanced_min_width(message: Message, state: FSMContext) -> None:
    await state.set_state(SearchWarehousePlasticStates.waiting_for_min_width)
    await message.answer(
        "Укажите минимальную ширину листа в миллиметрах или нажмите «Пропустить».",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


async def _perform_advanced_search(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    material = data.get("advanced_material")
    thickness = data.get("advanced_thickness")
    color = data.get("advanced_color")
    min_length = data.get("advanced_min_length")
    min_width = data.get("advanced_min_width")
    try:
        records = await search_warehouse_plastics_advanced(
            material=material,
            thickness=thickness,
            color=color,
            min_length=min_length,
            min_width=min_width,
        )
    except Exception:
        logging.exception("Failed to run advanced search for plastics")
        await state.clear()
        await message.answer(
            "⚠️ Не удалось выполнить расширенный поиск. Попробуйте позже.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    await state.clear()
    if not records:
        await message.answer(
            "По заданным параметрам ничего не найдено.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
    else:
        header_parts = ["Результаты расширенного поиска:"]
        if material:
            header_parts.append(f"Материал: {material}")
        if thickness is not None:
            header_parts.append(f"Толщина: {format_thickness_value(thickness)}")
        if color:
            header_parts.append(f"Цвет: {color}")
        if min_length is not None:
            header_parts.append(f"Мин. длина: {format_dimension_value(min_length)}")
        if min_width is not None:
            header_parts.append(f"Мин. ширина: {format_dimension_value(min_width)}")
        header_text = "\n".join(header_parts)
        records_text = []
        for index, record in enumerate(records, start=1):
            records_text.append(
                f"{index}.\n{format_plastic_record_for_message(record)}"
            )
        full_text = f"{header_text}\n\n" + "\n\n".join(records_text)
        chunks = split_text_into_messages(full_text)
        for idx, chunk in enumerate(chunks):
            if idx == 0:
                await message.answer(chunk, reply_markup=WAREHOUSE_PLASTICS_KB)
            else:
                await message.answer(chunk)
    await state.set_state(SearchWarehousePlasticStates.choosing_mode)
    await message.answer(
        "Выберите вариант поиска:",
        reply_markup=WAREHOUSE_PLASTICS_SEARCH_KB,
    )


@dp.message(SearchWarehousePlasticStates.waiting_for_material)
async def process_advanced_search_material(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_plastic_flow(message, state)
        return
    if text == ADVANCED_SEARCH_SKIP_MATERIAL_TEXT:
        await state.update_data(advanced_material=None)
        await _prompt_advanced_thickness_choice(message, state, None)
        return
    materials = await fetch_plastic_material_types()
    match = next((item for item in materials if item.lower() == text.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Материал не найден. Выберите один из списка или нажмите «➡️ Далее».",
            reply_markup=build_advanced_materials_keyboard(materials),
        )
        return
    await state.update_data(advanced_material=match)
    await _prompt_advanced_thickness_choice(message, state, match)


@dp.message(SearchWarehousePlasticStates.waiting_for_thickness)
async def process_advanced_search_thickness(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_plastic_flow(message, state)
        return
    if text == ADVANCED_SEARCH_ALL_THICKNESSES_TEXT:
        await state.update_data(advanced_thickness=None)
        data = await state.get_data()
        material = data.get("advanced_material")
        await _prompt_advanced_color_choice(message, state, material)
        return
    data = await state.get_data()
    material = data.get("advanced_material")
    if material:
        thicknesses = await fetch_material_thicknesses(material)
    else:
        thicknesses = await fetch_all_material_thicknesses()
    value = parse_thickness_input(text)
    if value is None or all(item != value for item in thicknesses):
        await message.answer(
            "ℹ️ Используйте кнопки для выбора толщины или нажмите «📏 Все толщины».",
            reply_markup=build_advanced_thickness_keyboard(thicknesses),
        )
        return
    await state.update_data(advanced_thickness=value)
    await _prompt_advanced_color_choice(message, state, material)


@dp.message(SearchWarehousePlasticStates.waiting_for_color)
async def process_advanced_search_color(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_plastic_flow(message, state)
        return
    if text == ADVANCED_SEARCH_ALL_COLORS_TEXT:
        await state.update_data(advanced_color=None)
        await _prompt_advanced_min_length(message, state)
        return
    data = await state.get_data()
    material = data.get("advanced_material")
    if material:
        colors = await fetch_material_colors(material)
    else:
        colors = await fetch_all_material_colors()
    match = next((item for item in colors if item.lower() == text.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Цвет не найден. Выберите из списка или нажмите «🎨 Все цвета».",
            reply_markup=build_advanced_colors_keyboard(colors),
        )
        return
    await state.update_data(advanced_color=match)
    await _prompt_advanced_min_length(message, state)


@dp.message(SearchWarehousePlasticStates.waiting_for_min_length)
async def process_advanced_search_min_length(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_plastic_flow(message, state)
        return
    if text == SKIP_TEXT:
        await state.update_data(advanced_min_length=None)
        await _prompt_advanced_min_width(message, state)
        return
    value = parse_dimension_filter_value(text)
    if value is None:
        await message.answer(
            "⚠️ Введите длину числом или нажмите «Пропустить».",
            reply_markup=SKIP_OR_CANCEL_KB,
        )
        return
    await state.update_data(advanced_min_length=value)
    await _prompt_advanced_min_width(message, state)


@dp.message(SearchWarehousePlasticStates.waiting_for_min_width)
async def process_advanced_search_min_width(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_search_plastic_flow(message, state)
        return
    if text == SKIP_TEXT:
        await state.update_data(advanced_min_width=None)
        await _perform_advanced_search(message, state)
        return
    value = parse_dimension_filter_value(text)
    if value is None:
        await message.answer(
            "⚠️ Введите ширину числом или нажмите «Пропустить».",
            reply_markup=SKIP_OR_CANCEL_KB,
        )
        return
    await state.update_data(advanced_min_width=value)
    await _perform_advanced_search(message, state)


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


@dp.message(MoveWarehousePlasticStates.waiting_for_article)
async def process_move_article(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_move_plastic_flow(message, state)
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
    locations = await fetch_plastic_storage_locations()
    if not locations:
        await state.clear()
        await message.answer(
            "Справочник мест хранения пуст. Добавьте места в настройках склада.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    await state.update_data(
        plastic_id=record["id"],
        article=record.get("article"),
        previous_location=record.get("warehouse"),
    )
    previous_location = record.get("warehouse") or "—"
    formatted_record = format_plastic_record_for_message(record)
    await state.set_state(MoveWarehousePlasticStates.waiting_for_new_location)
    await message.answer(
        "Найдена запись:\n\n"
        f"{formatted_record}\n\n"
        f"Текущее место хранения: {previous_location}\n\n"
        "Выберите новое место хранения из списка ниже.",
        reply_markup=build_storage_locations_keyboard(locations),
    )


@dp.message(MoveWarehousePlasticStates.waiting_for_new_location)
async def process_move_new_location(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_move_plastic_flow(message, state)
        return
    locations = await fetch_plastic_storage_locations()
    if not locations:
        await state.clear()
        await message.answer(
            "Справочник мест хранения пуст. Добавьте места в настройках склада.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    raw_location = (message.text or "").strip()
    match = next((item for item in locations if item.lower() == raw_location.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Место хранения не найдено. Выберите одно из списка.",
            reply_markup=build_storage_locations_keyboard(locations),
        )
        return
    data = await state.get_data()
    record_id = data.get("plastic_id")
    article = data.get("article")
    previous_location_raw = data.get("previous_location")
    previous_location_display = previous_location_raw or "—"
    if record_id is None or article is None:
        await _cancel_move_plastic_flow(message, state)
        return
    if previous_location_raw and previous_location_raw.lower() == match.lower():
        await message.answer(
            "ℹ️ Пластик уже находится в выбранном месте. Выберите другое место.",
            reply_markup=build_storage_locations_keyboard(locations),
        )
        return
    employee_id = message.from_user.id if message.from_user else None
    employee_name = message.from_user.full_name if message.from_user else None
    updated_record = await update_warehouse_plastic_location(
        record_id=record_id,
        new_location=match,
        employee_id=employee_id,
        employee_name=employee_name,
    )
    if updated_record is None:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось обновить место хранения. Попробуйте позже.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    await state.clear()
    formatted = format_plastic_record_for_message(updated_record)
    await message.answer(
        "✅ Место хранения обновлено.\n\n"
        f"Артикул: {article}\n"
        f"Предыдущее место: {previous_location_display}\n"
        f"Новое место: {match}\n\n"
        f"Актуальные данные:\n{formatted}",
        reply_markup=WAREHOUSE_PLASTICS_KB,
    )


@dp.message(WriteOffWarehousePlasticStates.waiting_for_article)
async def process_write_off_article(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_write_off_plastic_flow(message, state)
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
    await state.update_data(plastic_id=record["id"], article=record.get("article"))
    formatted = format_plastic_record_for_message(record)
    await state.set_state(WriteOffWarehousePlasticStates.waiting_for_project)
    await message.answer(
        "Найдена запись:\n\n"
        f"{formatted}\n\n"
        "Укажите проект, на который выполняется списание.",
        reply_markup=CANCEL_KB,
    )


@dp.message(WriteOffWarehousePlasticStates.waiting_for_project)
async def process_write_off_project(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_write_off_plastic_flow(message, state)
        return
    project = (message.text or "").strip()
    if not project:
        await message.answer(
            "⚠️ Название проекта не может быть пустым. Укажите проект.",
            reply_markup=CANCEL_KB,
        )
        return
    data = await state.get_data()
    record_id = data.get("plastic_id")
    article = data.get("article")
    if record_id is None or article is None:
        await _cancel_write_off_plastic_flow(message, state)
        return
    written_off_by_id = message.from_user.id if message.from_user else None
    written_off_by_name = message.from_user.full_name if message.from_user else None
    try:
        result = await write_off_warehouse_plastic(
            record_id=record_id,
            project=project,
            written_off_by_id=written_off_by_id,
            written_off_by_name=written_off_by_name,
        )
    except Exception:
        logging.exception("Failed to write off plastic record")
        await state.clear()
        await message.answer(
            "⚠️ Не удалось списать пластик. Попробуйте позже.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    if result is None:
        await state.clear()
        await message.answer(
            "ℹ️ Не удалось найти запись для списания. Возможно, она уже была изменена.",
            reply_markup=WAREHOUSE_PLASTICS_KB,
        )
        return
    await state.clear()
    formatted = format_written_off_plastic_record(result)
    await message.answer(
        "✅ Пластик списан со склада.\n\n"
        f"Артикул: {article}\n"
        f"Проект: {project}\n\n"
        f"Данные списанной записи:\n{formatted}",
        reply_markup=WAREHOUSE_PLASTICS_KB,
    )


@dp.message(F.text == "➕ Добавить")
async def handle_add_warehouse_plastic(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddWarehousePlasticStates.waiting_for_article)
    suggested_article: Optional[str] = None
    last_article = await fetch_max_plastic_article()
    if last_article is not None:
        suggested_article = str(last_article + 1)
    await state.update_data(article_suggestion=suggested_article)
    prompt_lines = ["Введите номер артикула (только цифры)."]
    if last_article is not None and suggested_article is not None:
        prompt_lines.append("")
        prompt_lines.append(
            "Последний добавленный артикул: "
            f"{last_article}. Нажмите кнопку ниже, чтобы использовать следующий номер."
        )
    await message.answer(
        "\n".join(prompt_lines),
        reply_markup=build_article_input_keyboard(suggested_article),
    )


@dp.message(AddWarehousePlasticStates.waiting_for_article)
async def process_plastic_article(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_flow(message, state)
        return
    article = (message.text or "").strip()
    if not article.isdigit():
        data = await state.get_data()
        suggestion = data.get("article_suggestion")
        await message.answer(
            "⚠️ Артикул должен содержать только цифры. Попробуйте снова.",
            reply_markup=build_article_input_keyboard(suggestion),
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
    await state.update_data(article=article, article_suggestion=None)
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


@dp.message(F.text == "++добавить пачку")
async def handle_add_warehouse_plastic_batch(
    message: Message, state: FSMContext
) -> None:
    await state.clear()
    last_article = await fetch_max_plastic_article()
    await state.update_data(batch_last_article=last_article)
    await state.set_state(AddWarehousePlasticBatchStates.waiting_for_quantity)
    prompt_lines = ["Сколько листов пластика нужно добавить? Введите число."]
    if last_article is None:
        prompt_lines.append("")
        prompt_lines.append("Сейчас нет добавленных артикулов. Нумерация начнётся с 1.")
    else:
        prompt_lines.append("")
        prompt_lines.append(
            "Последний добавленный артикул: "
            f"{last_article}. Новые листы получат номера начиная с {last_article + 1}."
        )
    await message.answer("\n".join(prompt_lines), reply_markup=CANCEL_KB)


@dp.message(AddWarehousePlasticBatchStates.waiting_for_quantity)
async def process_plastic_batch_quantity(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_batch_flow(message, state)
        return
    quantity = parse_positive_integer(message.text or "")
    if quantity is None:
        await message.answer(
            "⚠️ Количество должно быть положительным числом. Попробуйте снова.",
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
    await state.update_data(batch_quantity=quantity)
    await state.set_state(AddWarehousePlasticBatchStates.waiting_for_material)
    await message.answer(
        "Выберите тип материала:",
        reply_markup=build_materials_keyboard(materials),
    )


@dp.message(AddWarehousePlasticBatchStates.waiting_for_material)
async def process_plastic_batch_material(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_batch_flow(message, state)
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
    await state.update_data(batch_material=match)
    await state.set_state(AddWarehousePlasticBatchStates.waiting_for_thickness)
    await message.answer(
        "Выберите толщину из списка:",
        reply_markup=build_thickness_keyboard(thicknesses),
    )


@dp.message(AddWarehousePlasticBatchStates.waiting_for_thickness)
async def process_plastic_batch_thickness(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_batch_flow(message, state)
        return
    data = await state.get_data()
    material = data.get("batch_material")
    if not material:
        await _cancel_add_plastic_batch_flow(message, state)
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
    await state.update_data(batch_thickness=value)
    await state.set_state(AddWarehousePlasticBatchStates.waiting_for_color)
    await message.answer(
        "Выберите цвет:",
        reply_markup=build_colors_keyboard(colors),
    )


@dp.message(AddWarehousePlasticBatchStates.waiting_for_color)
async def process_plastic_batch_color(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_batch_flow(message, state)
        return
    data = await state.get_data()
    material = data.get("batch_material")
    if not material:
        await _cancel_add_plastic_batch_flow(message, state)
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
    await state.update_data(batch_color=match)
    await state.set_state(AddWarehousePlasticBatchStates.waiting_for_length)
    await message.answer(
        "Укажите длину листа в миллиметрах (только число).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehousePlasticBatchStates.waiting_for_length)
async def process_plastic_batch_length(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_batch_flow(message, state)
        return
    value = parse_positive_integer(message.text or "")
    if value is None:
        await message.answer(
            "⚠️ Длина должна быть положительным числом. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    await state.update_data(batch_length=value)
    await state.set_state(AddWarehousePlasticBatchStates.waiting_for_width)
    await message.answer(
        "Укажите ширину листа в миллиметрах (только число).",
        reply_markup=CANCEL_KB,
    )


@dp.message(AddWarehousePlasticBatchStates.waiting_for_width)
async def process_plastic_batch_width(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_batch_flow(message, state)
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
    await state.update_data(batch_width=value)
    await state.set_state(AddWarehousePlasticBatchStates.waiting_for_storage)
    await message.answer(
        "Выберите место хранения:",
        reply_markup=build_storage_locations_keyboard(locations),
    )


@dp.message(AddWarehousePlasticBatchStates.waiting_for_storage)
async def process_plastic_batch_storage(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip() == CANCEL_TEXT:
        await _cancel_add_plastic_batch_flow(message, state)
        return
    locations = await fetch_plastic_storage_locations()
    raw = (message.text or "").strip()
    match = next((item for item in locations if item.lower() == raw.lower()), None)
    if match is None:
        await message.answer(
            "ℹ️ Такое место хранения не найдено. Выберите одно из списка.",
            reply_markup=build_storage_locations_keyboard(locations),
        )
        return
    await state.update_data(batch_storage=match)
    await state.set_state(AddWarehousePlasticBatchStates.waiting_for_comment)
    await message.answer(
        "Добавьте комментарий (необязательно) или нажмите «Пропустить».",
        reply_markup=SKIP_OR_CANCEL_KB,
    )


@dp.message(AddWarehousePlasticBatchStates.waiting_for_comment)
async def process_plastic_batch_comment(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if text == CANCEL_TEXT:
        await _cancel_add_plastic_batch_flow(message, state)
        return
    if text == SKIP_TEXT:
        comment: Optional[str] = None
    else:
        comment = text or None
    data = await state.get_data()
    quantity = data.get("batch_quantity")
    material = data.get("batch_material")
    thickness: Optional[Decimal] = data.get("batch_thickness")
    color = data.get("batch_color")
    length = data.get("batch_length")
    width = data.get("batch_width")
    storage = data.get("batch_storage")
    last_article = data.get("batch_last_article")
    if not all([quantity, material, thickness, color, length, width, storage]):
        await _cancel_add_plastic_batch_flow(message, state)
        return
    if not isinstance(quantity, int):
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            await _cancel_add_plastic_batch_flow(message, state)
            return
    start_article = 1 if last_article is None else int(last_article) + 1
    articles = [str(start_article + idx) for idx in range(quantity)]
    employee_id = message.from_user.id if message.from_user else None
    employee_name = message.from_user.full_name if message.from_user else None
    records: list[Dict[str, Any]] = []
    for article in articles:
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
        if not record:
            await state.clear()
            await message.answer(
                "⚠️ Не удалось добавить пластик. Попробуйте позже.",
                reply_markup=WAREHOUSE_PLASTICS_KB,
            )
            return
        records.append(record)
    await state.clear()
    summary_comment = (records[0].get("comment") if records else comment) or "—"
    if records and records[0].get("employee_name"):
        summary_employee = records[0].get("employee_name") or "—"
    else:
        summary_employee = employee_name or "—"
    arrival_at = records[0].get("arrival_at") if records else None
    if arrival_at:
        try:
            arrival_local = arrival_at.astimezone(WARSAW_TZ)
        except Exception:
            arrival_local = arrival_at
        arrival_formatted = arrival_local.strftime("%Y-%m-%d %H:%M")
    else:
        arrival_formatted = datetime.now(WARSAW_TZ).strftime("%Y-%m-%d %H:%M")
    articles_text = ", ".join(articles)
    await message.answer(
        "✅ Пачка пластика добавлена на склад.\n\n"
        f"Количество: {quantity}\n"
        f"Артикулы: {articles_text}\n"
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


@dp.message(F.text == "🎞️ Пленки ⚙️")
async def handle_warehouse_settings_films(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_film_settings_overview(message)


@dp.message(F.text == "🏭 Производитель")
async def handle_film_manufacturers_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_film_manufacturers_menu(message)


@dp.message(F.text == "🏬 Склад")
async def handle_film_storage_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_film_storage_overview(message)


@dp.message(F.text == "🎬 Серия")
async def handle_film_series_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    manufacturers = await fetch_film_manufacturers_with_series()
    if manufacturers:
        lines = []
        for manufacturer in manufacturers:
            name = manufacturer["name"]
            formatted_series = format_series_list(manufacturer.get("series") or [])
            lines.append(
                "\n".join(
                    [
                        f"• {name}",
                        f"   Серии: {formatted_series}",
                    ]
                )
            )
        formatted = "\n".join(lines)
        text = (
            "⚙️ Настройки склада → Пленки → Серия.\n\n"
            "Доступные серии по производителям:\n"
            f"{formatted}\n\n"
            "Используйте кнопки ниже, чтобы добавить или удалить серию."
        )
    else:
        text = (
            "⚙️ Настройки склада → Пленки → Серия.\n\n"
            "Сначала добавьте производителей, чтобы создавать серии."
        )
    await message.answer(text, reply_markup=WAREHOUSE_SETTINGS_FILM_SERIES_KB)


@dp.message(F.text == "⬅️ Назад к пленкам")
async def handle_back_to_film_settings(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_film_settings_overview(message)


@dp.message(F.text == "➕ Добавить производителя")
async def handle_add_film_manufacturer_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(
        ManageFilmManufacturerStates.waiting_for_new_manufacturer_name
    )
    manufacturers = await fetch_film_manufacturers()
    existing_text = format_materials_list(manufacturers)
    await message.answer(
        "Введите название производителя.\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageFilmManufacturerStates.waiting_for_new_manufacturer_name)
async def process_new_film_manufacturer(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_film_manufacturer(name):
        await message.answer(f"✅ Производитель «{name}» добавлен.")
    else:
        await message.answer(f"ℹ️ Производитель «{name}» уже есть в списке.")
    await state.clear()
    await send_film_settings_overview(message)


@dp.message(F.text == "➖ Удалить производителя")
async def handle_remove_film_manufacturer_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    manufacturers = await fetch_film_manufacturers()
    if not manufacturers:
        await message.answer(
            "Список производителей пуст. Добавьте производителей перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_FILM_MANUFACTURERS_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManageFilmManufacturerStates.waiting_for_manufacturer_name_to_delete
    )
    await message.answer(
        "Выберите производителя, которого нужно удалить:",
        reply_markup=build_manufacturers_keyboard(manufacturers),
    )


@dp.message(ManageFilmManufacturerStates.waiting_for_manufacturer_name_to_delete)
async def process_remove_film_manufacturer(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await delete_film_manufacturer(name):
        await message.answer(f"🗑 Производитель «{name}» удалён.")
    else:
        await message.answer(f"ℹ️ Производитель «{name}» не найден в списке.")
    await state.clear()
    await send_film_settings_overview(message)


@dp.message(F.text == "➕ Добавить место хранения пленки")
async def handle_add_film_storage_location_button(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(
        ManageFilmStorageStates.waiting_for_new_storage_location_name
    )
    locations = await fetch_film_storage_locations()
    existing_text = format_storage_locations_list(locations)
    await message.answer(
        "Введите название места хранения для пленки.\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageFilmStorageStates.waiting_for_new_storage_location_name)
async def process_new_film_storage_location(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_film_storage_location(name):
        await message.answer(f"✅ Место хранения «{name}» добавлено.")
    else:
        await message.answer(f"ℹ️ Место хранения «{name}» уже есть в списке.")
    await state.clear()
    await send_film_storage_overview(message)


@dp.message(F.text == "➖ Удалить место хранения пленки")
async def handle_remove_film_storage_location_button(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    locations = await fetch_film_storage_locations()
    if not locations:
        await message.answer(
            "Список мест хранения пуст. Добавьте места перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_FILM_STORAGE_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManageFilmStorageStates.waiting_for_storage_location_to_delete
    )
    await message.answer(
        "Выберите место хранения, которое нужно удалить:",
        reply_markup=build_storage_locations_keyboard(locations),
    )


@dp.message(ManageFilmStorageStates.waiting_for_storage_location_to_delete)
async def process_remove_film_storage_location(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await delete_film_storage_location(name):
        await message.answer(f"🗑 Место хранения «{name}» удалено.")
    else:
        await message.answer(f"ℹ️ Место хранения «{name}» не найдено в списке.")
    await state.clear()
    await send_film_storage_overview(message)


@dp.message(F.text == "➕ Добавить серию")
async def handle_add_film_series_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    manufacturers = await fetch_film_manufacturers()
    if not manufacturers:
        await message.answer(
            "Сначала добавьте производителей, чтобы указать их серии.",
            reply_markup=WAREHOUSE_SETTINGS_FILM_SERIES_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManageFilmSeriesStates.waiting_for_manufacturer_for_new_series
    )
    await message.answer(
        "Выберите производителя, для которого нужно добавить серию:",
        reply_markup=build_manufacturers_keyboard(manufacturers),
    )


@dp.message(ManageFilmSeriesStates.waiting_for_manufacturer_for_new_series)
async def process_choose_manufacturer_for_new_series(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    manufacturer_name = (message.text or "").strip()
    manufacturer = await get_film_manufacturer_by_name(manufacturer_name)
    if manufacturer is None:
        manufacturers = await fetch_film_manufacturers()
        if not manufacturers:
            await state.clear()
            await message.answer(
                "Список производителей пуст. Добавьте производителей, чтобы продолжить.",
                reply_markup=WAREHOUSE_SETTINGS_FILM_SERIES_KB,
            )
            return
        await message.answer(
            "⚠️ Производитель не найден. Выберите вариант из списка.",
            reply_markup=build_manufacturers_keyboard(manufacturers),
        )
        return
    await state.update_data(selected_manufacturer=manufacturer["name"])
    await state.set_state(ManageFilmSeriesStates.waiting_for_new_series_name)
    existing_series = await fetch_film_series_by_manufacturer(manufacturer["name"])
    formatted_series = format_series_list(existing_series)
    await message.answer(
        "Введите название новой серии.\n\n"
        f"Текущие серии у «{manufacturer['name']}»: {formatted_series}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageFilmSeriesStates.waiting_for_new_series_name)
async def process_new_series_name(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    series_name = (message.text or "").strip()
    if not series_name:
        await message.answer("⚠️ Название серии не может быть пустым. Попробуйте снова.")
        return
    data = await state.get_data()
    manufacturer_name = data.get("selected_manufacturer")
    if not manufacturer_name:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось определить производителя. Попробуйте снова.",
            reply_markup=WAREHOUSE_SETTINGS_FILM_SERIES_KB,
        )
        await send_film_settings_overview(message)
        return
    status = await insert_film_series(manufacturer_name, series_name)
    if status == "manufacturer_not_found":
        await message.answer(
            "ℹ️ Производитель больше не существует. Обновите список и попробуйте снова."
        )
    elif status == "already_exists":
        await message.answer(
            f"ℹ️ Серия «{series_name}» уже указана для «{manufacturer_name}»."
        )
    elif status == "inserted":
        await message.answer(
            f"✅ Серия «{series_name}» добавлена для «{manufacturer_name}»."
        )
    else:
        await message.answer(
            "⚠️ Не удалось добавить серию. Попробуйте позже."
        )
    await state.clear()
    await send_film_settings_overview(message)


@dp.message(F.text == "➖ Удалить серию")
async def handle_remove_film_series_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    manufacturers = await fetch_film_manufacturers_with_series()
    manufacturers_with_series = [
        item["name"] for item in manufacturers if item.get("series")
    ]
    if not manufacturers_with_series:
        await message.answer(
            "Для удаления пока нет добавленных серий.",
            reply_markup=WAREHOUSE_SETTINGS_FILM_SERIES_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManageFilmSeriesStates.waiting_for_manufacturer_for_series_deletion
    )
    await message.answer(
        "Выберите производителя, у которого нужно удалить серию:",
        reply_markup=build_manufacturers_keyboard(manufacturers_with_series),
    )


@dp.message(ManageFilmSeriesStates.waiting_for_manufacturer_for_series_deletion)
async def process_choose_manufacturer_for_series_deletion(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    manufacturer_name = (message.text or "").strip()
    manufacturer = await get_film_manufacturer_by_name(manufacturer_name)
    if manufacturer is None:
        manufacturers = await fetch_film_manufacturers_with_series()
        manufacturers_with_series = [
            item["name"] for item in manufacturers if item.get("series")
        ]
        if not manufacturers_with_series:
            await state.clear()
            await message.answer(
                "Список серий пуст. Добавьте серии, чтобы их удалить.",
                reply_markup=WAREHOUSE_SETTINGS_FILM_SERIES_KB,
            )
            return
        await message.answer(
            "⚠️ Производитель не найден. Выберите из списка.",
            reply_markup=build_manufacturers_keyboard(manufacturers_with_series),
        )
        return
    series = await fetch_film_series_by_manufacturer(manufacturer["name"])
    if not series:
        manufacturers = await fetch_film_manufacturers_with_series()
        manufacturers_with_series = [
            item["name"] for item in manufacturers if item.get("series")
        ]
        if not manufacturers_with_series:
            await state.clear()
            await message.answer(
                "Список серий пуст. Добавьте серии, чтобы их удалить.",
                reply_markup=WAREHOUSE_SETTINGS_FILM_SERIES_KB,
            )
            return
        await message.answer(
            "ℹ️ У выбранного производителя нет серий. Выберите другого производителя.",
            reply_markup=build_manufacturers_keyboard(manufacturers_with_series),
        )
        return
    await state.update_data(selected_manufacturer=manufacturer["name"])
    await state.set_state(ManageFilmSeriesStates.waiting_for_series_name_to_delete)
    await message.answer(
        f"Выберите серию для удаления у «{manufacturer['name']}»:",
        reply_markup=build_series_keyboard(series),
    )


@dp.message(ManageFilmSeriesStates.waiting_for_series_name_to_delete)
async def process_remove_film_series(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    series_name = (message.text or "").strip()
    if not series_name:
        await message.answer("⚠️ Название серии не может быть пустым. Попробуйте снова.")
        return
    data = await state.get_data()
    manufacturer_name = data.get("selected_manufacturer")
    if not manufacturer_name:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось определить производителя. Попробуйте снова."
        )
        await send_film_settings_overview(message)
        return
    status = await delete_film_series(manufacturer_name, series_name)
    if status == "manufacturer_not_found":
        await message.answer(
            "ℹ️ Производитель больше не существует. Обновите список и попробуйте снова."
        )
    elif status == "deleted":
        await message.answer(
            f"🗑 Серия «{series_name}» удалена у «{manufacturer_name}»."
        )
    else:
        await message.answer(
            f"ℹ️ Серия «{series_name}» не найдена у «{manufacturer_name}»."
        )
    await state.clear()
    await send_film_settings_overview(message)


@dp.message(F.text == "🧱 Пластик")
async def handle_warehouse_settings_plastic(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await send_plastic_settings_overview(message)


@dp.message(F.text == WAREHOUSE_SETTINGS_ELECTRICS_TEXT)
async def handle_warehouse_settings_electrics(message: Message) -> None:
    if not await ensure_admin_access(message):
        return
    await send_electrics_settings_overview(message)


@dp.message(F.text == WAREHOUSE_SETTINGS_ELECTRICS_LED_STRIPS_TEXT)
async def handle_warehouse_settings_led_strips(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_strips_settings_overview(message)


@dp.message(F.text == WAREHOUSE_SETTINGS_ELECTRICS_LED_MODULES_TEXT)
async def handle_warehouse_settings_led_modules(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_modules_settings_overview(message)


@dp.message(F.text == LED_MODULES_MANUFACTURERS_MENU_TEXT)
async def handle_led_module_manufacturers_menu(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_module_manufacturers_menu(message)


@dp.message(F.text == LED_MODULES_COLORS_MENU_TEXT)
async def handle_led_module_colors_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_module_colors_menu(message)


@dp.message(F.text == LED_MODULES_POWER_MENU_TEXT)
async def handle_led_module_power_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_module_power_menu(message)


@dp.message(F.text == LED_MODULES_VOLTAGE_MENU_TEXT)
async def handle_led_module_voltage_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_module_voltage_menu(message)


@dp.message(F.text == LED_MODULES_LENS_MENU_TEXT)
async def handle_led_module_lens_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_module_lens_menu(message)


@dp.message(F.text == LED_MODULES_SERIES_MENU_TEXT)
async def handle_led_module_series_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_module_series_menu(message)


@dp.message(F.text == LED_MODULES_STORAGE_MENU_TEXT)
async def handle_led_module_storage_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_module_storage_overview(message)


@dp.message(F.text == LED_MODULES_BASE_MENU_TEXT)
async def handle_led_module_base_menu(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_module_base_menu(message)


@dp.message(F.text == LED_MODULES_GENERATE_TEXT)
async def handle_generate_led_module(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    manufacturers_with_series = [
        item
        for item in await fetch_led_module_manufacturers_with_series()
        if item.get("series")
    ]
    colors = await fetch_led_module_colors()
    lens_counts = await fetch_led_module_lens_counts()
    power_options = await fetch_led_module_power_options()
    voltage_options = await fetch_led_module_voltage_options()
    missing: list[str] = []
    if not manufacturers_with_series:
        missing.append("• производители и серии")
    if not colors:
        missing.append("• цвета модулей")
    if not lens_counts:
        missing.append("• значения количества линз")
    if not power_options:
        missing.append("• значения мощности")
    if not voltage_options:
        missing.append("• значения напряжения")
    if missing:
        details = "\n".join(missing)
        await message.answer(
            "⚠️ Невозможно начать генерацию Led модуля.\n\n"
            "Заполните следующие справочники в настройках:\n"
            f"{details}",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    await state.set_state(GenerateLedModuleStates.waiting_for_article)
    await message.answer(
        "Введите артикул для нового Led модуля.",
        reply_markup=build_article_input_keyboard(),
    )


@dp.message(GenerateLedModuleStates.waiting_for_article)
async def process_generate_led_module_article(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    article = (message.text or "").strip()
    if not article:
        await message.answer(
            "⚠️ Артикул не может быть пустым. Попробуйте снова.",
            reply_markup=build_article_input_keyboard(),
        )
        return
    existing = await get_generated_led_module_by_article(article)
    if existing:
        await message.answer(
            f"⚠️ Артикул «{article}» уже существует. Укажите другой.",
            reply_markup=build_article_input_keyboard(),
        )
        return
    manufacturers_with_series = [
        item
        for item in await fetch_led_module_manufacturers_with_series()
        if item.get("series")
    ]
    if not manufacturers_with_series:
        await state.clear()
        await message.answer(
            "ℹ️ Список производителей с сериями пуст. Добавьте данные в настройках Led модулей.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    manufacturer_names = [item["name"] for item in manufacturers_with_series]
    await state.update_data(generated_led_module_article=article)
    await state.set_state(GenerateLedModuleStates.waiting_for_manufacturer)
    await message.answer(
        "Выберите производителя Led модуля.\n\n"
        "Доступные варианты:\n"
        f"{format_materials_list(manufacturer_names)}",
        reply_markup=build_manufacturers_keyboard(manufacturer_names),
    )


@dp.message(GenerateLedModuleStates.waiting_for_manufacturer)
async def process_generate_led_module_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    manufacturers_with_series = [
        item
        for item in await fetch_led_module_manufacturers_with_series()
        if item.get("series")
    ]
    if not manufacturers_with_series:
        await state.clear()
        await message.answer(
            "ℹ️ Список производителей с сериями пуст. Добавьте данные в настройках Led модулей.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    manufacturer_names = [item["name"] for item in manufacturers_with_series]
    raw = (message.text or "").strip()
    match = next(
        (item for item in manufacturers_with_series if item["name"].lower() == raw.lower()),
        None,
    )
    if match is None:
        await message.answer(
            "⚠️ Производитель не найден. Выберите вариант из списка.",
            reply_markup=build_manufacturers_keyboard(manufacturer_names),
        )
        return
    series_names = await fetch_led_module_series_by_manufacturer(match["name"])
    if not series_names:
        await message.answer(
            "ℹ️ У выбранного производителя пока нет серий. Выберите другого производителя или добавьте серию в настройках.",
            reply_markup=build_manufacturers_keyboard(manufacturer_names),
        )
        return
    await state.update_data(
        generated_led_module_manufacturer={"id": match["id"], "name": match["name"]}
    )
    await state.set_state(GenerateLedModuleStates.waiting_for_series)
    await message.answer(
        f"Выберите серию для производителя «{match['name']}».\n\n"
        "Доступные серии:\n"
        f"{format_materials_list(series_names)}",
        reply_markup=build_series_keyboard(series_names),
    )


@dp.message(GenerateLedModuleStates.waiting_for_series)
async def process_generate_led_module_series(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    data = await state.get_data()
    manufacturer: Optional[dict[str, Any]] = data.get("generated_led_module_manufacturer")
    if not manufacturer:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось определить выбранного производителя. Начните генерацию заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_BASE_KB,
        )
        return
    series_names = await fetch_led_module_series_by_manufacturer(manufacturer["name"])
    if not series_names:
        await state.clear()
        await message.answer(
            "ℹ️ У производителя больше нет серий. Добавьте серию и начните снова.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    raw = (message.text or "").strip()
    series_name = next((item for item in series_names if item.lower() == raw.lower()), None)
    if series_name is None:
        await message.answer(
            "⚠️ Серия не найдена. Выберите значение из списка.",
            reply_markup=build_series_keyboard(series_names),
        )
        return
    series = await get_led_module_series_by_name(manufacturer["id"], series_name)
    if series is None:
        await message.answer(
            "⚠️ Не удалось подтвердить выбранную серию. Попробуйте снова.",
            reply_markup=build_series_keyboard(series_names),
        )
        return
    await state.update_data(generated_led_module_series=series)
    colors = await fetch_led_module_colors()
    if not colors:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник цветов пуст. Добавьте цвета и начните заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    await state.set_state(GenerateLedModuleStates.waiting_for_color)
    await message.answer(
        "Выберите цвет Led модуля.\n\n"
        "Доступные цвета:\n"
        f"{format_materials_list(colors)}",
        reply_markup=build_colors_keyboard(colors),
    )


@dp.message(GenerateLedModuleStates.waiting_for_color)
async def process_generate_led_module_color(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    colors = await fetch_led_module_colors()
    if not colors:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник цветов пуст. Добавьте цвета и начните заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    raw = (message.text or "").strip()
    color_name = next((item for item in colors if item.lower() == raw.lower()), None)
    if color_name is None:
        await message.answer(
            "⚠️ Цвет не найден. Выберите значение из списка.",
            reply_markup=build_colors_keyboard(colors),
        )
        return
    color = await get_led_module_color_by_name(color_name)
    if color is None:
        await message.answer(
            "⚠️ Не удалось подтвердить выбранный цвет. Попробуйте снова.",
            reply_markup=build_colors_keyboard(colors),
        )
        return
    await state.update_data(generated_led_module_color=color)
    lens_counts = await fetch_led_module_lens_counts()
    if not lens_counts:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник количества линз пуст. Добавьте значения и начните заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    await state.set_state(GenerateLedModuleStates.waiting_for_lens_count)
    await message.answer(
        "Выберите количество линз для Led модуля.\n\n"
        "Доступные варианты:\n"
        f"{format_materials_list([str(value) for value in lens_counts])}",
        reply_markup=build_lens_counts_keyboard(lens_counts),
    )


@dp.message(GenerateLedModuleStates.waiting_for_lens_count)
async def process_generate_led_module_lens_count(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    lens_counts = await fetch_led_module_lens_counts()
    if not lens_counts:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник количества линз пуст. Добавьте значения и начните заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    raw = (message.text or "").strip()
    parsed = parse_positive_integer(raw)
    if parsed is None or parsed not in lens_counts:
        await message.answer(
            "⚠️ Укажите количество линз, доступное в списке.",
            reply_markup=build_lens_counts_keyboard(lens_counts),
        )
        return
    lens = await get_led_module_lens_count_by_value(parsed)
    if lens is None:
        await message.answer(
            "⚠️ Не удалось подтвердить выбранное количество линз. Попробуйте снова.",
            reply_markup=build_lens_counts_keyboard(lens_counts),
        )
        return
    await state.update_data(generated_led_module_lens_count=lens)
    power_options = await fetch_led_module_power_options()
    if not power_options:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник мощностей пуст. Добавьте значения и начните заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    await state.set_state(GenerateLedModuleStates.waiting_for_power)
    await message.answer(
        "Выберите мощность Led модуля.\n\n"
        "Доступные варианты:\n"
        f"{format_materials_list(power_options)}",
        reply_markup=build_power_values_keyboard(power_options),
    )


@dp.message(GenerateLedModuleStates.waiting_for_power)
async def process_generate_led_module_power(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    power_options = await fetch_led_module_power_options()
    if not power_options:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник мощностей пуст. Добавьте значения и начните заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    raw = (message.text or "").strip()
    power_name = next((item for item in power_options if item.lower() == raw.lower()), None)
    if power_name is None:
        await message.answer(
            "⚠️ Мощность не найдена. Выберите значение из списка.",
            reply_markup=build_power_values_keyboard(power_options),
        )
        return
    power = await get_led_module_power_option_by_name(power_name)
    if power is None:
        await message.answer(
            "⚠️ Не удалось подтвердить выбранную мощность. Попробуйте снова.",
            reply_markup=build_power_values_keyboard(power_options),
        )
        return
    await state.update_data(generated_led_module_power=power)
    voltage_options = await fetch_led_module_voltage_options()
    if not voltage_options:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник напряжений пуст. Добавьте значения и начните заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    await state.set_state(GenerateLedModuleStates.waiting_for_voltage)
    await message.answer(
        "Выберите напряжение Led модуля.\n\n"
        "Доступные варианты:\n"
        f"{format_materials_list(voltage_options)}",
        reply_markup=build_voltage_values_keyboard(voltage_options),
    )


@dp.message(GenerateLedModuleStates.waiting_for_voltage)
async def process_generate_led_module_voltage(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    voltage_options = await fetch_led_module_voltage_options()
    if not voltage_options:
        await state.clear()
        await message.answer(
            "ℹ️ Справочник напряжений пуст. Добавьте значения и начните заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_KB,
        )
        return
    raw = (message.text or "").strip()
    voltage_name = next((item for item in voltage_options if item.lower() == raw.lower()), None)
    if voltage_name is None:
        await message.answer(
            "⚠️ Напряжение не найдено. Выберите значение из списка.",
            reply_markup=build_voltage_values_keyboard(voltage_options),
        )
        return
    voltage = await get_led_module_voltage_option_by_name(voltage_name)
    if voltage is None:
        await message.answer(
            "⚠️ Не удалось подтвердить выбранное напряжение. Попробуйте снова.",
            reply_markup=build_voltage_values_keyboard(voltage_options),
        )
        return
    data = await state.get_data()
    article = data.get("generated_led_module_article")
    manufacturer = data.get("generated_led_module_manufacturer")
    series = data.get("generated_led_module_series")
    color = data.get("generated_led_module_color")
    lens = data.get("generated_led_module_lens_count")
    power = data.get("generated_led_module_power")
    if not all([article, manufacturer, series, color, lens, power]):
        await state.clear()
        await message.answer(
            "⚠️ Недостаточно данных для сохранения Led модуля. Начните заново.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_BASE_KB,
        )
        return
    record = await insert_generated_led_module(
        article=article,
        manufacturer_id=manufacturer["id"],
        series_id=series["id"],
        color_id=color["id"],
        lens_count_id=lens["id"],
        power_option_id=power["id"],
        voltage_option_id=voltage["id"],
    )
    if record is None:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось сохранить Led модуль. Попробуйте позже.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_BASE_KB,
        )
        return
    await state.clear()
    created_at = record.get("created_at")
    created_text = _format_datetime(created_at)
    await message.answer(
        "✅ Led модуль добавлен в базу.\n\n"
        f"Артикул: {article}\n"
        f"Производитель: {manufacturer['name']}\n"
        f"Серия: {series['name']}\n"
        f"Цвет: {color['name']}\n"
        f"Количество линз: {lens['value']}\n"
        f"Мощность: {power['name']}\n"
        f"Напряжение: {voltage['name']}\n"
        f"Создано: {created_text}",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_BASE_KB,
    )


@dp.message(F.text == LED_MODULES_DELETE_TEXT)
async def handle_delete_led_module(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await message.answer(
        "🗑️ Удаление Led модуля находится в разработке.",
        reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_BASE_KB,
    )


@dp.message(F.text == LED_MODULES_BACK_TEXT)
async def handle_back_to_led_module_settings(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_led_modules_settings_overview(message)


@dp.message(F.text == WAREHOUSE_SETTINGS_ELECTRICS_POWER_SUPPLIES_TEXT)
async def handle_warehouse_settings_power_supplies(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_power_supplies_settings_overview(message)


@dp.message(F.text == LED_STRIPS_ADD_MANUFACTURER_TEXT)
async def handle_add_led_strip_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(
        ManageLedStripManufacturerStates.waiting_for_new_manufacturer_name
    )
    manufacturers = await fetch_led_strip_manufacturers()
    existing_text = format_materials_list(manufacturers)
    await message.answer(
        "Введите название производителя Led ленты.\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageLedStripManufacturerStates.waiting_for_new_manufacturer_name)
async def process_new_led_strip_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_led_strip_manufacturer(name):
        await message.answer(f"✅ Производитель «{name}» добавлен.")
    else:
        await message.answer(f"ℹ️ Производитель «{name}» уже есть в списке.")
    await state.clear()
    await send_led_strips_settings_overview(message)


@dp.message(F.text == LED_STRIPS_REMOVE_MANUFACTURER_TEXT)
async def handle_remove_led_strip_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    manufacturers = await fetch_led_strip_manufacturers()
    if not manufacturers:
        await message.answer(
            "Список производителей пуст. Добавьте производителей перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_LED_STRIPS_MANUFACTURERS_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManageLedStripManufacturerStates.waiting_for_manufacturer_name_to_delete
    )
    await message.answer(
        "Выберите производителя, которого нужно удалить:",
        reply_markup=build_manufacturers_keyboard(manufacturers),
    )


@dp.message(ManageLedStripManufacturerStates.waiting_for_manufacturer_name_to_delete)
async def process_remove_led_strip_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await delete_led_strip_manufacturer(name):
        await message.answer(f"🗑 Производитель «{name}» удалён.")
    else:
        await message.answer(f"ℹ️ Производитель «{name}» не найден в списке.")
    await state.clear()
    await send_led_strips_settings_overview(message)


@dp.message(F.text == LED_MODULES_ADD_MANUFACTURER_TEXT)
async def handle_add_led_module_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(
        ManageLedModuleManufacturerStates.waiting_for_new_manufacturer_name
    )
    manufacturers = await fetch_led_module_manufacturers()
    existing_text = format_materials_list(manufacturers)
    await message.answer(
        "Введите название производителя Led модулей.\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageLedModuleManufacturerStates.waiting_for_new_manufacturer_name)
async def process_new_led_module_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_led_module_manufacturer(name):
        await message.answer(f"✅ Производитель «{name}» добавлен.")
    else:
        await message.answer(f"ℹ️ Производитель «{name}» уже есть в списке.")
    await state.clear()
    await send_led_modules_settings_overview(message)


@dp.message(F.text == LED_MODULES_REMOVE_MANUFACTURER_TEXT)
async def handle_remove_led_module_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    manufacturers = await fetch_led_module_manufacturers()
    if not manufacturers:
        await message.answer(
            "Список производителей пуст. Добавьте производителей перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_MANUFACTURERS_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManageLedModuleManufacturerStates.waiting_for_manufacturer_name_to_delete
    )
    await message.answer(
        "Выберите производителя, которого нужно удалить:",
        reply_markup=build_manufacturers_keyboard(manufacturers),
    )


@dp.message(F.text == LED_MODULES_ADD_STORAGE_TEXT)
async def handle_add_led_module_storage_location(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(
        ManageLedModuleStorageStates.waiting_for_new_storage_location_name
    )
    locations = await fetch_led_module_storage_locations()
    existing_text = format_storage_locations_list(locations)
    await message.answer(
        "Введите название места хранения для Led модулей.\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageLedModuleStorageStates.waiting_for_new_storage_location_name)
async def process_new_led_module_storage_location(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_led_module_storage_location(name):
        await message.answer(f"✅ Место хранения «{name}» добавлено.")
    else:
        await message.answer(f"ℹ️ Место хранения «{name}» уже есть в списке.")
    await state.clear()
    await send_led_module_storage_overview(message)


@dp.message(F.text == LED_MODULES_REMOVE_STORAGE_TEXT)
async def handle_remove_led_module_storage_location(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    locations = await fetch_led_module_storage_locations()
    if not locations:
        await message.answer(
            "Список мест хранения пуст. Добавьте места перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_STORAGE_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManageLedModuleStorageStates.waiting_for_storage_location_to_delete
    )
    await message.answer(
        "Выберите место хранения, которое нужно удалить:",
        reply_markup=build_storage_locations_keyboard(locations),
    )


@dp.message(ManageLedModuleStorageStates.waiting_for_storage_location_to_delete)
async def process_remove_led_module_storage_location(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await delete_led_module_storage_location(name):
        await message.answer(f"🗑 Место хранения «{name}» удалено.")
    else:
        await message.answer(f"ℹ️ Место хранения «{name}» не найдено в списке.")
    await state.clear()
    await send_led_module_storage_overview(message)


@dp.message(ManageLedModuleManufacturerStates.waiting_for_manufacturer_name_to_delete)
async def process_remove_led_module_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await delete_led_module_manufacturer(name):
        await message.answer(f"🗑 Производитель «{name}» удалён.")
    else:
        await message.answer(f"ℹ️ Производитель «{name}» не найден в списке.")
    await state.clear()
    await send_led_modules_settings_overview(message)


@dp.message(F.text == LED_MODULES_ADD_COLOR_TEXT)
async def handle_add_led_module_color(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(ManageLedModuleColorStates.waiting_for_new_color_name)
    colors = await fetch_led_module_colors()
    existing_text = format_materials_list(colors)
    await message.answer(
        "Введите цвет Led модулей.\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageLedModuleColorStates.waiting_for_new_color_name)
async def process_new_led_module_color(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    color_name = (message.text or "").strip()
    if not color_name:
        await message.answer("⚠️ Название цвета не может быть пустым. Попробуйте снова.")
        return
    if await insert_led_module_color(color_name):
        await message.answer(f"✅ Цвет «{color_name}» добавлен.")
    else:
        await message.answer(f"ℹ️ Цвет «{color_name}» уже есть в списке.")
    await state.clear()
    await send_led_module_colors_menu(message)


@dp.message(F.text == LED_MODULES_ADD_POWER_TEXT)
async def handle_add_led_module_power_option(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(ManageLedModulePowerStates.waiting_for_new_power_value)
    existing = await fetch_led_module_power_options()
    existing_text = format_materials_list(existing)
    await message.answer(
        "Введите значение мощности для Led модулей.\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageLedModulePowerStates.waiting_for_new_power_value)
async def process_new_led_module_power_option(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer("⚠️ Значение не может быть пустым. Попробуйте снова.")
        return
    if await insert_led_module_power_option(value):
        await message.answer(f"✅ Мощность «{value}» добавлена.")
    else:
        await message.answer(f"ℹ️ Мощность «{value}» уже есть в списке.")
    await state.clear()
    await send_led_module_power_menu(message)


@dp.message(F.text == LED_MODULES_ADD_VOLTAGE_TEXT)
async def handle_add_led_module_voltage_option(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(ManageLedModuleVoltageStates.waiting_for_new_voltage_value)
    existing = await fetch_led_module_voltage_options()
    existing_text = format_materials_list(existing)
    await message.answer(
        "Введите значение напряжения для Led модулей.\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageLedModuleVoltageStates.waiting_for_new_voltage_value)
async def process_new_led_module_voltage_option(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer(
            "⚠️ Значение не может быть пустым. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    if await insert_led_module_voltage_option(value):
        await message.answer(f"✅ Напряжение «{value}» добавлено.")
    else:
        await message.answer(f"ℹ️ Напряжение «{value}» уже есть в списке.")
    await state.clear()
    await send_led_module_voltage_menu(message)


@dp.message(F.text == LED_MODULES_REMOVE_COLOR_TEXT)
async def handle_remove_led_module_color(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    colors = await fetch_led_module_colors()
    if not colors:
        await message.answer(
            "Список цветов пуст. Добавьте цвета перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_COLORS_KB,
        )
        await state.clear()
        return
    await state.set_state(ManageLedModuleColorStates.waiting_for_color_name_to_delete)
    await message.answer(
        "Выберите цвет, который нужно удалить:",
        reply_markup=build_colors_keyboard(colors),
    )


@dp.message(ManageLedModuleColorStates.waiting_for_color_name_to_delete)
async def process_remove_led_module_color(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    color_name = (message.text or "").strip()
    if not color_name:
        await message.answer("⚠️ Название цвета не может быть пустым. Попробуйте снова.")
        return
    if await delete_led_module_color(color_name):
        await message.answer(f"🗑 Цвет «{color_name}» удалён.")
    else:
        await message.answer(f"ℹ️ Цвет «{color_name}» не найден в списке.")
    await state.clear()
    await send_led_module_colors_menu(message)


@dp.message(F.text == LED_MODULES_REMOVE_POWER_TEXT)
async def handle_remove_led_module_power_option(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    power_options = await fetch_led_module_power_options()
    if not power_options:
        await message.answer(
            "Список мощностей пуст. Добавьте значения перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_POWER_KB,
        )
        await state.clear()
        return
    await state.set_state(ManageLedModulePowerStates.waiting_for_power_value_to_delete)
    await message.answer(
        "Выберите мощность, которую нужно удалить:",
        reply_markup=build_power_values_keyboard(power_options),
    )


@dp.message(ManageLedModulePowerStates.waiting_for_power_value_to_delete)
async def process_remove_led_module_power_option(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer(
            "⚠️ Значение не может быть пустым. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    if await delete_led_module_power_option(value):
        await message.answer(f"🗑 Мощность «{value}» удалена.")
        await state.clear()
        await send_led_module_power_menu(message)
    else:
        await message.answer(
            f"ℹ️ Мощность «{value}» не найдена в списке.",
            reply_markup=CANCEL_KB,
        )


@dp.message(F.text == LED_MODULES_REMOVE_VOLTAGE_TEXT)
async def handle_remove_led_module_voltage_option(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    voltage_options = await fetch_led_module_voltage_options()
    if not voltage_options:
        await message.answer(
            "Список напряжений пуст. Добавьте значения перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_VOLTAGE_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManageLedModuleVoltageStates.waiting_for_voltage_value_to_delete
    )
    await message.answer(
        "Выберите напряжение, которое нужно удалить:",
        reply_markup=build_voltage_values_keyboard(voltage_options),
    )


@dp.message(ManageLedModuleVoltageStates.waiting_for_voltage_value_to_delete)
async def process_remove_led_module_voltage_option(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    value = (message.text or "").strip()
    if not value:
        await message.answer(
            "⚠️ Значение не может быть пустым. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    if await delete_led_module_voltage_option(value):
        await message.answer(f"🗑 Напряжение «{value}» удалено.")
        await state.clear()
        await send_led_module_voltage_menu(message)
    else:
        await message.answer(
            f"ℹ️ Напряжение «{value}» не найдено в списке.",
            reply_markup=CANCEL_KB,
        )


@dp.message(F.text == LED_MODULES_ADD_LENS_COUNT_TEXT)
async def handle_add_led_module_lens_count(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(ManageLedModuleLensStates.waiting_for_new_lens_count)
    existing = await fetch_led_module_lens_counts()
    existing_text = format_materials_list([str(value) for value in existing])
    await message.answer(
        "Введите количество линз (целое положительное число).\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageLedModuleLensStates.waiting_for_new_lens_count)
async def process_new_led_module_lens_count(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    value = parse_positive_integer(message.text or "")
    if value is None:
        await message.answer(
            "⚠️ Введите положительное целое число. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    if await insert_led_module_lens_count(value):
        await message.answer(f"✅ Количество линз «{value}» добавлено.")
    else:
        await message.answer(f"ℹ️ Количество линз «{value}» уже есть в списке.")
    await state.clear()
    await send_led_module_lens_menu(message)


@dp.message(F.text == LED_MODULES_REMOVE_LENS_COUNT_TEXT)
async def handle_remove_led_module_lens_count(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    existing = await fetch_led_module_lens_counts()
    if not existing:
        await message.answer(
            "Список количеств линз пуст. Добавьте значения перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_LENS_KB,
        )
        await state.clear()
        return
    await state.set_state(ManageLedModuleLensStates.waiting_for_lens_count_to_delete)
    await message.answer(
        "Выберите количество линз, которое нужно удалить:",
        reply_markup=build_lens_counts_keyboard(existing),
    )


@dp.message(ManageLedModuleLensStates.waiting_for_lens_count_to_delete)
async def process_remove_led_module_lens_count(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    value = parse_positive_integer(message.text or "")
    if value is None:
        await message.answer(
            "⚠️ Введите положительное целое число. Попробуйте снова.",
            reply_markup=CANCEL_KB,
        )
        return
    if await delete_led_module_lens_count(value):
        await message.answer(f"🗑 Количество линз «{value}» удалено.")
        await state.clear()
        await send_led_module_lens_menu(message)
    else:
        await message.answer(
            f"ℹ️ Количество линз «{value}» не найдено в списке.",
            reply_markup=CANCEL_KB,
        )


@dp.message(F.text == LED_MODULES_ADD_SERIES_TEXT)
async def handle_add_led_module_series(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    manufacturers = await fetch_led_module_manufacturers()
    if not manufacturers:
        await state.clear()
        await message.answer(
            "Сначала добавьте производителей, чтобы указывать серии.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_SERIES_KB,
        )
        return
    await state.set_state(
        ManageLedModuleSeriesStates.waiting_for_manufacturer_for_new_series
    )
    await message.answer(
        "Выберите производителя, для которого нужно добавить серию:",
        reply_markup=build_manufacturers_keyboard(manufacturers),
    )


@dp.message(ManageLedModuleSeriesStates.waiting_for_manufacturer_for_new_series)
async def process_choose_led_module_manufacturer_for_series(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    manufacturer_name = (message.text or "").strip()
    manufacturer = await get_led_module_manufacturer_by_name(manufacturer_name)
    if manufacturer is None:
        manufacturers = await fetch_led_module_manufacturers()
        if not manufacturers:
            await state.clear()
            await message.answer(
                "Список производителей пуст. Добавьте производителя, чтобы указать серию.",
                reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_SERIES_KB,
            )
            return
        await message.answer(
            "⚠️ Производитель не найден. Выберите из списка.",
            reply_markup=build_manufacturers_keyboard(manufacturers),
        )
        return
    existing_series = await fetch_led_module_series_by_manufacturer(
        manufacturer["name"]
    )
    formatted_series = format_series_list(existing_series)
    await state.update_data(selected_manufacturer=manufacturer["name"])
    await state.set_state(ManageLedModuleSeriesStates.waiting_for_new_series_name)
    await message.answer(
        "Введите название новой серии.\n\n"
        f"Уже добавлены:\n{formatted_series}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManageLedModuleSeriesStates.waiting_for_new_series_name)
async def process_new_led_module_series(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    series_name = (message.text or "").strip()
    if not series_name:
        await message.answer("⚠️ Название серии не может быть пустым. Попробуйте снова.")
        return
    data = await state.get_data()
    manufacturer_name = data.get("selected_manufacturer")
    if not manufacturer_name:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось определить производителя. Попробуйте снова."
        )
        await send_led_modules_settings_overview(message)
        return
    status = await insert_led_module_series(manufacturer_name, series_name)
    if status == "manufacturer_not_found":
        await message.answer(
            "ℹ️ Производитель больше не существует. Обновите список и попробуйте снова."
        )
    elif status == "already_exists":
        await message.answer(
            f"ℹ️ Серия «{series_name}» уже указана для «{manufacturer_name}»."
        )
    elif status == "inserted":
        await message.answer(
            f"✅ Серия «{series_name}» добавлена для «{manufacturer_name}»."
        )
    else:
        await message.answer(
            "⚠️ Не удалось добавить серию. Попробуйте позже."
        )
    await state.clear()
    await send_led_modules_settings_overview(message)


@dp.message(F.text == LED_MODULES_REMOVE_SERIES_TEXT)
async def handle_remove_led_module_series(message: Message, state: FSMContext) -> None:
    if not await ensure_admin_access(message, state):
        return
    manufacturers = await fetch_led_module_manufacturers_with_series()
    manufacturers_with_series = [
        item["name"] for item in manufacturers if item.get("series")
    ]
    if not manufacturers_with_series:
        await state.clear()
        await message.answer(
            "Для удаления пока нет добавленных серий.",
            reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_SERIES_KB,
        )
        return
    await state.set_state(
        ManageLedModuleSeriesStates.waiting_for_manufacturer_for_series_deletion
    )
    await message.answer(
        "Выберите производителя, у которого нужно удалить серию:",
        reply_markup=build_manufacturers_keyboard(manufacturers_with_series),
    )


@dp.message(ManageLedModuleSeriesStates.waiting_for_manufacturer_for_series_deletion)
async def process_choose_led_module_manufacturer_for_series_deletion(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    manufacturer_name = (message.text or "").strip()
    manufacturer = await get_led_module_manufacturer_by_name(manufacturer_name)
    if manufacturer is None:
        manufacturers = await fetch_led_module_manufacturers_with_series()
        manufacturers_with_series = [
            item["name"] for item in manufacturers if item.get("series")
        ]
        if not manufacturers_with_series:
            await state.clear()
            await message.answer(
                "Список серий пуст. Добавьте серии, чтобы их удалить.",
                reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_SERIES_KB,
            )
            return
        await message.answer(
            "⚠️ Производитель не найден. Выберите из списка.",
            reply_markup=build_manufacturers_keyboard(manufacturers_with_series),
        )
        return
    series = await fetch_led_module_series_by_manufacturer(manufacturer["name"])
    if not series:
        manufacturers = await fetch_led_module_manufacturers_with_series()
        manufacturers_with_series = [
            item["name"] for item in manufacturers if item.get("series")
        ]
        if not manufacturers_with_series:
            await state.clear()
            await message.answer(
                "Список серий пуст. Добавьте серии, чтобы их удалить.",
                reply_markup=WAREHOUSE_SETTINGS_LED_MODULES_SERIES_KB,
            )
            return
        await message.answer(
            "ℹ️ У выбранного производителя нет серий. Выберите другого производителя.",
            reply_markup=build_manufacturers_keyboard(manufacturers_with_series),
        )
        return
    await state.update_data(selected_manufacturer=manufacturer["name"])
    await state.set_state(ManageLedModuleSeriesStates.waiting_for_series_name_to_delete)
    await message.answer(
        f"Выберите серию для удаления у «{manufacturer['name']}»:",
        reply_markup=build_series_keyboard(series),
    )


@dp.message(ManageLedModuleSeriesStates.waiting_for_series_name_to_delete)
async def process_remove_led_module_series(message: Message, state: FSMContext) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    series_name = (message.text or "").strip()
    if not series_name:
        await message.answer("⚠️ Название серии не может быть пустым. Попробуйте снова.")
        return
    data = await state.get_data()
    manufacturer_name = data.get("selected_manufacturer")
    if not manufacturer_name:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось определить производителя. Попробуйте снова."
        )
        await send_led_modules_settings_overview(message)
        return
    status = await delete_led_module_series(manufacturer_name, series_name)
    if status == "manufacturer_not_found":
        await message.answer(
            "ℹ️ Производитель больше не существует. Обновите список и попробуйте снова."
        )
    elif status == "deleted":
        await message.answer(
            f"🗑 Серия «{series_name}» удалена у «{manufacturer_name}»."
        )
    else:
        await message.answer(
            f"ℹ️ Серия «{series_name}» не найдена у «{manufacturer_name}»."
        )
    await state.clear()
    await send_led_modules_settings_overview(message)


@dp.message(F.text == POWER_SUPPLIES_ADD_MANUFACTURER_TEXT)
async def handle_add_power_supply_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.set_state(
        ManagePowerSupplyManufacturerStates.waiting_for_new_manufacturer_name
    )
    manufacturers = await fetch_power_supply_manufacturers()
    existing_text = format_materials_list(manufacturers)
    await message.answer(
        "Введите название производителя блоков питания.\n\n"
        f"Уже добавлены:\n{existing_text}",
        reply_markup=CANCEL_KB,
    )


@dp.message(ManagePowerSupplyManufacturerStates.waiting_for_new_manufacturer_name)
async def process_new_power_supply_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await insert_power_supply_manufacturer(name):
        await message.answer(f"✅ Производитель «{name}» добавлен.")
    else:
        await message.answer(f"ℹ️ Производитель «{name}» уже есть в списке.")
    await state.clear()
    await send_power_supplies_settings_overview(message)


@dp.message(F.text == POWER_SUPPLIES_REMOVE_MANUFACTURER_TEXT)
async def handle_remove_power_supply_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    manufacturers = await fetch_power_supply_manufacturers()
    if not manufacturers:
        await message.answer(
            "Список производителей пуст. Добавьте производителей перед удалением.",
            reply_markup=WAREHOUSE_SETTINGS_POWER_SUPPLIES_MANUFACTURERS_KB,
        )
        await state.clear()
        return
    await state.set_state(
        ManagePowerSupplyManufacturerStates.waiting_for_manufacturer_name_to_delete
    )
    await message.answer(
        "Выберите производителя, которого нужно удалить:",
        reply_markup=build_manufacturers_keyboard(manufacturers),
    )


@dp.message(
    ManagePowerSupplyManufacturerStates.waiting_for_manufacturer_name_to_delete
)
async def process_remove_power_supply_manufacturer(
    message: Message, state: FSMContext
) -> None:
    if await _process_cancel_if_requested(message, state):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return
    if await delete_power_supply_manufacturer(name):
        await message.answer(f"🗑 Производитель «{name}» удалён.")
    else:
        await message.answer(f"ℹ️ Производитель «{name}» не найден в списке.")
    await state.clear()
    await send_power_supplies_settings_overview(message)


@dp.message(F.text == WAREHOUSE_SETTINGS_BACK_TO_ELECTRICS_TEXT)
async def handle_back_to_electrics_settings(
    message: Message, state: FSMContext
) -> None:
    if not await ensure_admin_access(message, state):
        return
    await state.clear()
    await send_electrics_settings_overview(message)


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
    current_state = await state.get_state()
    if current_state and current_state.startswith(AddUserStates.__name__):
        await _cancel_add_user_flow(message, state)
        return
    if current_state and current_state.startswith(
        ManageFilmManufacturerStates.__name__
    ):
        await state.clear()
        await send_film_settings_overview(message)
        return
    if current_state and current_state.startswith(
        ManageOrderTypeStates.__name__
    ):
        await state.clear()
        await send_order_type_settings_overview(message)
        return
    if current_state and current_state.startswith(
        ManageFilmSeriesStates.__name__
    ):
        await state.clear()
        await send_film_settings_overview(message)
        return
    if current_state and current_state.startswith(
        ManageFilmStorageStates.__name__
    ):
        await state.clear()
        await send_film_storage_overview(message)
        return
    if current_state and current_state.startswith(
        ManageLedStripManufacturerStates.__name__
    ):
        await state.clear()
        await send_led_strips_settings_overview(message)
        return
    if current_state and current_state.startswith(
        ManageLedModuleManufacturerStates.__name__
    ):
        await state.clear()
        await send_led_modules_settings_overview(message)
        return
    if current_state and current_state.startswith(
        ManageLedModuleSeriesStates.__name__
    ):
        await state.clear()
        await send_led_modules_settings_overview(message)
        return
    if current_state and current_state.startswith(
        AddWarehouseLedModuleStates.__name__
    ):
        await _cancel_add_led_module_flow(message, state)
        return
    if current_state and current_state.startswith(
        GenerateLedModuleStates.__name__
    ):
        await _cancel_generate_led_module_flow(message, state)
        return
    if current_state and current_state.startswith(
        ManageLedModuleVoltageStates.__name__
    ):
        await state.clear()
        await send_led_module_voltage_menu(message)
        return
    if current_state and current_state.startswith(
        ManagePowerSupplyManufacturerStates.__name__
    ):
        await state.clear()
        await send_power_supplies_settings_overview(message)
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
