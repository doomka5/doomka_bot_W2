"""Telegram bot with basic user management backed by PostgreSQL."""

import asyncio
import logging
import os
import shlex
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "botdb")
DB_USER = os.getenv("DB_USER", "botuser")
DB_PASS = os.getenv("DB_PASS", "botpass")

db_pool: Optional[asyncpg.Pool] = None


async def init_database() -> None:
    """Инициализация базы данных — создание таблицы users и администратора."""

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

            # Добавляем или обновляем запись администратора
            await conn.execute("""
                INSERT INTO users (tg_id, username, position, role)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (tg_id) DO UPDATE
                SET username = EXCLUDED.username,
                    position = EXCLUDED.position,
                    role = EXCLUDED.role
            """, 37352491, "DooMka", "Администратор", "administrator_full_access")


async def close_database() -> None:
    """Закрывает подключение к базе данных."""
    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None


async def on_startup(bot: Bot) -> None:
    """Обработка запуска — подключение к базе и создание таблиц."""
    await init_database()
    logging.info("Привет! Бот запущен и готов к работе.")
    print("Привет! Бот запущен и готов к работе.")


async def on_shutdown(bot: Bot) -> None:
    """Отключение от базы при остановке."""
    await close_database()


dp = Dispatcher()
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Ответ на /start."""
    await message.answer("Привет! Для добавления пользователей используйте /adduser.")


@dp.message(Command("adduser"))
async def handle_add_user(message: Message, command: CommandObject) -> None:
    """Добавление или обновление пользователя."""
    if not command.args:
        await message.answer(
            "Использование: /adduser <tg_id> <username> <position> <role>.\n"
            "Если значения содержат пробелы — заключайте их в кавычки."
        )
        return

    try:
        parts = shlex.split(command.args)
    except ValueError:
        await message.answer("Не удалось разобрать аргументы команды. Проверьте синтаксис.")
        return

    if len(parts) < 4:
        await message.answer(
            "Недостаточно аргументов.\n"
            "Использование: /adduser <tg_id> <username> <position> <role>."
        )
        return

    tg_id_str, username, position, *role_parts = parts
    try:
        tg_id = int(tg_id_str)
    except ValueError:
        await message.answer("tg_id должен быть числом.")
        return

    role = " ".join(role_parts)
    if db_pool is None:
        await message.answer("База данных временно недоступна. Попробуйте позже.")
        return

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (tg_id, username, position, role)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tg_id) DO UPDATE
            SET username = EXCLUDED.username,
                position = EXCLUDED.position,
                role = EXCLUDED.role
        """, tg_id, username, position, role)

    await message.answer(
        f"✅ Пользователь добавлен или обновлён:\n"
        f"🆔 ID: {tg_id}\n"
        f"👤 Ник: {username}\n"
        f"💼 Должность: {position}\n"
        f"🔑 Роль: {role}"
    )


async def main() -> None:
    """Главная функция запуска бота."""
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
