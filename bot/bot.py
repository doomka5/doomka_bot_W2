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
    logging.info("Бот запущен и готов к работе.")


async def on_shutdown(bot: Bot) -> None:
    await close_database()


# === Обработчики ===
dp = Dispatcher()
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Приветствие при запуске бота."""
    await message.answer("👋 Привет! Для добавления пользователей используйте /adduser.")


@dp.message(Command("adduser"))
async def handle_add_user(message: Message, command: CommandObject) -> None:
    """Добавление или обновление пользователя в БД."""
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

    role = " ".join(role_parts)
    if not role:
        await message.answer("Роль не может быть пустой.")
        return

    if db_pool is None:
        await message.answer("База данных недоступна. Попробуйте позже.")
        return

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

    await message.answer(
        f"✅ Пользователь добавлен или обновлён:\n"
        f"• ID: {tg_id}\n"
        f"• Имя: {username}\n"
        f"• Должность: {position}\n"
        f"• Роль: {role}"
    )


# === Запуск бота ===
async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
