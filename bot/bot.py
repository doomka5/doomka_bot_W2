"""Telegram bot with basic user management backed by PostgreSQL."""

from __future__ import annotations

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
    """Create the users table and ensure the default administrator exists."""

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
            await _ensure_user_table(conn)
            await _seed_default_admin(conn)


async def _ensure_user_table(conn: asyncpg.Connection) -> None:
    """Create the users table and add any missing legacy columns."""

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

    column_rows = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'users'
        """
    )
    existing_columns = {row["column_name"] for row in column_rows}

    if "username" not in existing_columns:
        await conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
    if "position" not in existing_columns:
        await conn.execute("ALTER TABLE users ADD COLUMN position TEXT")
    if "role" not in existing_columns:
        await conn.execute("ALTER TABLE users ADD COLUMN role TEXT")
    if "created_at" not in existing_columns:
        await conn.execute(
            """
            ALTER TABLE users
                ADD COLUMN created_at TIMESTAMPTZ DEFAULT timezone('utc', now())
            """
        )

    await conn.execute("UPDATE users SET username = '' WHERE username IS NULL")
    await conn.execute("UPDATE users SET position = '' WHERE position IS NULL")
    await conn.execute("UPDATE users SET role = '' WHERE role IS NULL")
    await conn.execute(
        """
        UPDATE users
        SET created_at = timezone('utc', now())
        WHERE created_at IS NULL
        """
    )

    await conn.execute(
        """
        ALTER TABLE users
            ALTER COLUMN username SET NOT NULL,
            ALTER COLUMN position SET NOT NULL,
            ALTER COLUMN role SET NOT NULL,
            ALTER COLUMN created_at SET NOT NULL
        """
    )


async def _seed_default_admin(conn: asyncpg.Connection) -> None:
    """Insert or update the requested administrator account."""

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
    """Close the database connection pool if it was created."""

    global db_pool
    if db_pool is not None:
        await db_pool.close()
        db_pool = None


async def on_startup(bot: Bot) -> None:
    """Handle dispatcher startup event by preparing the database."""

    await init_database()
    greeting = "Привет! Бот запущен и готов к работе."
    logging.info(greeting)
    print(greeting)


async def on_shutdown(bot: Bot) -> None:
    """Release database resources on shutdown."""

    await close_database()


dp = Dispatcher()
dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Reply to /start commands with a greeting."""

    await message.answer("Привет! Для добавления пользователей используйте /adduser.")


@dp.message(Command("adduser"))
async def handle_add_user(message: Message, command: CommandObject) -> None:
    """Add or update a user in the access database via /adduser command."""

    if not command.args:
        await message.answer(
            "Использование: /adduser <tg_id> <username> <position> <role>. "
            "Если значения содержат пробелы, заключайте их в кавычки."
        )
        return

    try:
        parts = shlex.split(command.args)
    except ValueError:
        await message.answer("Не удалось разобрать аргументы команды. Проверьте синтаксис.")
        return

    if len(parts) < 4:
        await message.answer(
            "Недостаточно аргументов. Использование: /adduser <tg_id> <username> <position> <role>."
        )
        return

    tg_id_str, username, position, *role_parts = parts

    try:
        tg_id = int(tg_id_str)
    except ValueError:
        await message.answer("tg_id должен быть числом.")
        return

    role = " ".join(role_parts) if role_parts else ""
    if not role:
        await message.answer("Роль не может быть пустой.")
        return

    if db_pool is None:
        await message.answer("База данных временно недоступна. Попробуйте позже.")
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
            tg_id,
            username,
            position,
            role,
        )

    await message.answer(
        "Пользователь успешно добавлен или обновлён:\n"
        f"• ID: {tg_id}\n"
        f"• Ник: {username}\n"
        f"• Должность: {position}\n"
        f"• Роль: {role}"
    )


async def main() -> None:
    """Entrypoint for running the bot."""

    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
