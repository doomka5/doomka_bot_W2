#!/bin/sh
# =====================================================
# 🚀 Doomka Bot W2 — автообновление и перезапуск (QNAP)
# =====================================================

# === Настройки ===
BOT_TOKEN="8429030887:AAH2xxAGtNsuejvzjyzzj3LXED2BdiK2V4k"
CHAT_ID="37352491"
PROJECT_DIR="/share/3D/doomka_bot_W2"

cd "$PROJECT_DIR" || exit 1

send_message() {
    local TEXT="$1"
    /opt/bin/curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "parse_mode=Markdown" \
        -d "text=${TEXT}" >/dev/null 2>&1
}

send_message "🔄 *Обновление Doomka Bot W2 началось...*%0A🕓 $(date '+%d.%m.%Y %H:%M')"

# === 1. Обновление кода с GitHub ===
echo "[1/4] Обновляем проект с GitHub..."
/opt/bin/git fetch origin main >/dev/null 2>&1
if ! /opt/bin/git pull origin main >/dev/null 2>&1; then
    send_message "⚠️ *Ошибка при обновлении кода с GitHub!*%0AПроверь подключение или права доступа."
    exit 1
fi

# === 2. Остановка контейнеров ===
echo "[2/4] Останавливаем старые контейнеры..."
if ! /opt/bin/docker compose down >/dev/null 2>&1; then
    send_message "⚠️ *Ошибка при остановке контейнеров!*"
    exit 1
fi

# === 3. Пересборка и запуск ===
echo "[3/4] Пересобираем и запускаем проект..."
if ! /opt/bin/docker compose up -d --build >/dev/null 2>&1; then
    send_message "⚠️ *Ошибка при сборке или запуске контейнеров!*"
    exit 1
fi

# === 4. Проверка статуса ===
echo "[4/4] Проверяем состояние..."
STATUS=$(/opt/bin/docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}")

send_message "✅ *Doomka Bot W2 успешно обновлён!*%0A🕓 $(date '+%d.%m.%Y %H:%M')%0A%0A\`\`\`${STATUS}\`\`\`"

echo "[✔] Обновление завершено."
