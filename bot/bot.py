"""Telegram bot with basic user management backed by PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from typing import Any, Awaitable, Callable, Dict, Optional

import asyncpg
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
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


# === Мидлварь доступа (ОБЪЯВЛЯЕМ ДО регистрации в Dispatcher) ===
class AccessControlMiddleware(BaseMiddleware):
    """Ограничивает доступ к боту только добавленным пользователям."""
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user_id: Optional[int] = None

        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id

        # Позволяем проходить сервисным апдейтам
        if user_id is None:
            return await handler(event, data)

        if await user_has_access(user_id):
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer("🚫 У вас нет доступа к этому боту. Обратитесь к администратору.")
        return None


# === Инициализация базы данных ===
async def init_database() -> None:
    """Создаёт таблицу пользователей и добавляет администратора."""
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME,
    )

    async with db_pool.acquire() as conn:
        async with conn.transaction():
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
            # Добавляем администратора, если его нет
            await conn.execute(
                """
                INSERT INTO users (tg_id, username, position, role)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tg_id) DO UPDATE
                SET username = EXCLUDED.username,
                    position = EXCLUDED.position,
                    role = EXCLUDED.role
                """,
                37352491,           # Telegram ID администратора
                "DooMka",           # Имя
                "Администратор",    # Должность
                "администратор с полными правами и доступом",  # Роль
            )


async def close_database() -> None:
    """Закрывает пул соединений с БД."""
    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None


# === События запуска и остановки ===
async def on_startup(bot: Bot) -> None:
    await init_database()
    logging.info("Привет! Бот запущен и готов к работе.")
    print("Привет! Бот запущен и готов к работе.")


async def on_shutdown(bot: Bot) -> None:
    await close_database()


# === Dispatcher ===
dp = Dispatcher()
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)

# Регистрируем мидлварь ПОСЛЕ объявления класса
dp.message.outer_middleware(AccessControlMiddleware())


class AddUserStates(StatesGroup):
    """Состояния машины для пошагового добавления пользователя."""
    waiting_for_tg_id = State()
    waiting_for_username = State()
    waiting_for_position = State()
    waiting_for_role = State()


MAIN_MENU_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="Тест")]],
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


async def upsert_user_in_db(tg_id: int, username: str, position: str, role: str) -> None:
    """Добавляет или обновляет пользователя в базе данных."""
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


async def user_has_access(tg_id: int) -> bool:
    """Проверяет, добавлен ли пользователь в базу данных."""
    if db_pool is None:
        logging.warning("Database pool is not initialised when checking access")
        return False

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM users WHERE tg_id = $1", tg_id)
    return row is not None


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Приветствие при запуске бота."""
    await message.answer(
        "👋 Привет! Нажмите «⚙️ Настройки», чтобы управлять пользователями.",
        reply_markup=MAIN_MENU_KB,
    )


@dp.message(F.text == "Тест")
async def handle_test(message: Message) -> None:
    await message.answer("тест ок")


@dp.message(Command("settings"))
@dp.message(F.text == "⚙️ Настройки")
async def handle_settings(message: Message) -> None:
    await message.answer("⚙️ Настройки. Выберите действие:", reply_markup=SETTINGS_MENU_KB)


@dp.message(F.text == "👥 Пользователи")
async def handle_users_menu(message: Message) -> None:
    await message.answer("👥 Пользователи. Выберите действие:", reply_markup=USERS_MENU_KB)


@dp.message(F.text == "⬅️ Главное меню")
async def handle_back_to_main(message: Message) -> None:
    await message.answer("Главное меню.", reply_markup=MAIN_MENU_KB)


@dp.message(F.text == "⬅️ Назад в настройки")
async def handle_back_to_settings(message: Message) -> None:
    await handle_settings(message)


@dp.message(F.text == "➕ Добавить пользователя")
async def handle_add_user_button(message: Message, state: FSMContext) -> None:
    await state.set_state(AddUserStates.waiting_for_tg_id)
    await message.answer(
        "Введите Telegram ID пользователя (только цифры).",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(AddUserStates.waiting_for_tg_id)
async def process_tg_id(message: Message, state: FSMContext) -> None:
    try:
        tg_id = int(message.text)
    except (TypeError, ValueError):
        await message.answer("ID должен состоять только из цифр. Повторите ввод.")
        return

    await state.update_data(tg_id=tg_id)
    await state.set_state(AddUserStates.waiting_for_username)
    await message.answer("Введите имя пользователя (username).")


@dp.message(AddUserStates.waiting_for_username)
async def process_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("Имя не может быть пустым. Введите имя пользователя.")
        return

    await state.update_data(username=username)
    await state.set_state(AddUserStates.waiting_for_position)
    await message.answer("Введите должность пользователя.")


@dp.message(AddUserStates.waiting_for_position)
async def process_position(message: Message, state: FSMContext) -> None:
    position = (message.text or "").strip()
    if not position:
        await message.answer("Должность не может быть пустой. Введите должность пользователя.")
        return

    await state.update_data(position=position)
    await state.set_state(AddUserStates.waiting_for_role)
    await message.answer("Введите роль пользователя.")


@dp.message(AddUserStates.waiting_for_role)
async def process_role(message: Message, state: FSMContext) -> None:
    role = (message.text or "").strip()
    if not role:
        await message.answer("Роль не может быть пустой. Введите роль пользователя.")
        return

    data = await state.get_data()
    await state.clear()

    try:
        await upsert_user_in_db(
            tg_id=data["tg_id"],
            username=data["username"],
            position=data["position"],
            role=role,
        )
    except RuntimeError:
        await message.answer("База данных недоступна. Попробуйте позже.", reply_markup=SETTINGS_MENU_KB)
        return

    await message.answer(
        "✅ Пользователь добавлен или обновлён:\n"
        f"• ID: {data['tg_id']}\n"
        f"• Имя: {data['username']}\n"
        f"• Должность: {data['position']}\n"
        f"• Роль: {role}",
        reply_markup=USERS_MENU_KB,
    )


@dp.message(Command("adduser"))
async def handle_add_user(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer(
            "Использование: /adduser <tg_id> <username> <position> <role>\n"
            "Если значения содержат пробелы — заключайте их в кавычки."
        )
        return

    try:
        parts = shlex.split(command.args)
    except ValueError:
        await message.answer("Ошибка разбора аргументов. Проверьте синтаксис.")
        return

    if len(parts) < 4:
        await message.answer("Недостаточно аргументов.")
        return

    tg_id_str, username, position, *role_parts = parts
    try:
        tg_id = int(tg_id_str)
    except ValueError:
        await message.answer("tg_id должен быть числом.")
        return

    role = " ".join(role_parts).strip()
    if not role:
        await message.answer("Роль не может быть пустой.")
        return

    try:
        await upsert_user_in_db(tg_id, username, position, role)
    except RuntimeError:
        await message.answer("База данных недоступна. Попробуйте позже.")
        return

    await message.answer(
        f"✅ Пользователь добавлен или обновлён:\n"
        f"• ID: {tg_id}\n"
        f"• Имя: {username}\n"
        f"• Должность: {position}\n"
        f"• Роль: {role}"
    )


async def fetch_all_users_from_db() -> list[asyncpg.Record]:
    if db_pool is None:
        raise RuntimeError("Database pool is not initialised")

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tg_id, username, position, role
            FROM users
            ORDER BY id DESC
            """
        )
    return rows


@dp.message(F.text == "📋 Посмотреть всех пользователей")
async def handle_list_users(message: Message) -> None:
    try:
        rows = await fetch_all_users_from_db()
    except RuntimeError:
        await message.answer("База данных недоступна. Попробуйте позже.", reply_markup=USERS_MENU_KB)
        return

    if not rows:
        await message.answer("Пока нет ни одного пользователя.", reply_markup=USERS_MENU_KB)
        return

    lines = [
        "• ID: {tg_id}\n  Имя: {username}\n  Должность: {position}\n  Роль: {role}".format(
            tg_id=row["tg_id"], username=row["username"], position=row["position"], role=row["role"]
        )
        for row in rows
    ]
    await message.answer("👥 Список пользователей:\n\n" + "\n\n".join(lines), reply_markup=USERS_MENU_KB)


# === Запуск бота ===
async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
