"""Simple Telegram bot that greets users and announces startup."""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")


async def on_startup(bot: Bot) -> None:
    """Handle dispatcher startup event."""
    greeting = "Привет! Бот запущен и готов к работе."
    logging.info(greeting)
    print(greeting)


dp = Dispatcher()
dp.startup.register(on_startup)


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Reply to /start commands with a greeting."""
    await message.answer("Привет!")


async def main() -> None:
    """Entrypoint for running the bot."""
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
