FROM python:3.11-slim

# Устанавливаем git и системные зависимости
RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем только нужные файлы
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота
COPY bot/ .

# Команда запуска
CMD ["python", "bot.py"]
