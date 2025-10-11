# 🧠 Doomka Bot W2

**Приватный Telegram-бот** с базой данных PostgreSQL и веб-интерфейсом на FastAPI.  
Развёрнут на **QNAP NAS** с помощью Docker Compose.  
Код редактируется через **Codex**, а репозиторий синхронизируется с GitHub.

---

## ⚙️ СТРУКТУРА ПРОЕКТА

```
doomka_bot_W2/
├── bot/                  # Код Telegram-бота (Aiogram)
│   └── bot.py
├── web/                  # FastAPI веб-сервис, работающий с той же базой
│   └── main.py
├── pgdata/               # Данные PostgreSQL
├── docker-compose.yml    # Конфигурация контейнеров
└── README.md             # Документация проекта
```

---

## 🧩 АРХИТЕКТУРА

Проект состоит из трёх контейнеров:

| Контейнер | Назначение | Порт | Описание |
|------------|-------------|-------|-----------|
| **postgres_bot** | База данных PostgreSQL | 5432 | Хранит пользователей, товары, склад, заказы и т.д. |
| **telegram_bot** | Telegram-бот (на Aiogram 3.x) | — | Принимает команды, записывает данные в базу |
| **fastapi_web** | Веб-интерфейс (FastAPI) | 8181 | Доступ к данным и API для n8n / CRM |

Все контейнеры соединены внутренней сетью `botnet`.

---

## 🔐 ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ

Эти переменные задаются в файле `docker-compose.yml` и автоматически доступны в коде бота и веб-приложения.

| Имя | Описание | Пример |
|------|-----------|--------|
| `BOT_TOKEN` | Токен Telegram-бота (из [@BotFather](https://t.me/BotFather))  |
| `DB_HOST` | Имя контейнера с PostgreSQL | postgres_bot |
| `DB_PORT` | Порт PostgreSQL | 5432 |
| `DB_NAME` | Имя базы данных | botdb |
| `DB_USER` | Имя пользователя PostgreSQL | botuser |
| `DB_PASS` | Пароль PostgreSQL | botpass |

> ⚠️ **Важно:** не вставляй токен и пароли в код — они хранятся в Docker окружении.  
> В Python коде получай их так:
> ```python
> import os
> BOT_TOKEN = os.getenv("BOT_TOKEN")
> DB_USER = os.getenv("DB_USER")
> ```

---

## 🚀 ЗАПУСК НА QNAP

1. Скопируй проект в папку:
   ```bash
   /share/3D/doomka_bot_W2
   ```

2. Собери и запусти контейнеры:
   ```bash
   cd /share/3D/doomka_bot_W2
   docker compose build --no-cache
   docker compose up -d
   ```

3. Проверить работу контейнеров:
   ```bash
   docker ps
   ```

4. Проверить логи:
   ```bash
   docker logs telegram_bot -f
   docker logs fastapi_web -f
   ```

5. FastAPI доступен по адресу:  
   👉 http://192.168.0.105:8181

---

## 🤖 TELEGRAM-БОТ

Бот написан на **Aiogram 3.x** и поддерживает приватный доступ.  
Пример базового кода (`bot/bot.py`):

```python
import os
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import asyncio

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USERS = {37352491}  # Белый список Telegram ID

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    if message.from_user.id not in ALLOWED_USERS:
        await message.answer("⛔ У тебя нет доступа к этому боту.")
        return
    await message.answer("✅ Привет! Доступ разрешён.")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 🌐 ВЕБ-СЕРВИС (FASTAPI)

Файл: `web/main.py`

При первом запуске создаёт таблицу `users` и отдаёт список пользователей.

Доступ:  
👉 [http://192.168.0.105:8181](http://192.168.0.105:8181)

---

## 🧠 РАБОТА ЧЕРЕЗ CODEX

1. Открой репозиторий **doomka_bot_W2** в Codex.  
2. Вноси изменения в код (`bot/` или `web/`).  
3. Сохрани и закоммить изменения в GitHub.  
4. На QNAP обнови проект:
   ```bash
   cd /share/3D/doomka_bot_W2
   git pull origin main
   docker compose build bot
   docker compose up -d bot
   ```

---

## 🔒 ПРИВАТНОСТЬ

- Бот отвечает **только авторизованным пользователям**.  
- Для доступа можно использовать белый список ID или таблицу `employees` в базе.  
- Доступ к веб-интерфейсу ограничивается локальной сетью (192.168.x.x).

---

## 🧩 ИНТЕГРАЦИИ

Бот и веб могут использоваться как база для:
- Google Sheets синхронизации (через gspread)
- n8n workflow automation
- CRM-панели
- складского учёта и логирования

---

## 💡 ПОЛЕЗНЫЕ КОМАНДЫ

```bash
# Проверить логи бота
docker logs telegram_bot --tail 50

# Подключиться к базе
docker exec -it postgres_bot psql -U botuser -d botdb

# Проверить таблицы
\dt
```

---

## 🛠 АВТОР

👨‍💻 **Doomka / Jarosław Iwanow**  
Telegram: [@doomka5pl](https://t.me/doomka5pl)  
GitHub: [github.com/doomka5](https://github.com/doomka5)
