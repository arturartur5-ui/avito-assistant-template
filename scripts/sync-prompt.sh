#!/usr/bin/env bash
# Системный промпт: сверка и выкладка на шлюз.
#   ./scripts/sync-prompt.sh check  — совпадает ли локальный файл с тем, что на шлюзе
#   ./scripts/sync-prompt.sh push   — выложить локальный файл на шлюз и перезапустить его
#
# Нужен только если промпт живёт на шлюзе. Подаёте его модели из кода — скрипт не нужен.
# Всё про ваш шлюз — в .env проекта или в окружении, в скрипте ничего не зашито:
#   GATEWAY_HOST        — ssh-адрес шлюза: алиас из ~/.ssh/config или user@host
#   GATEWAY_PROMPT_PATH — абсолютный путь к файлу промпта на шлюзе, оканчивается на SOUL.md
#   GATEWAY_RESTART     — команда на той стороне, которая перезапускает шлюз после выкладки
#   GATEWAY_OWNER       — необязательно: владелец файла на шлюзе, например 1000:1000
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Из .env берём только эти четыре переменные, и только те, что не заданы в окружении
if [ -f "$ROOT/.env" ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      GATEWAY_HOST|GATEWAY_PROMPT_PATH|GATEWAY_RESTART|GATEWAY_OWNER)
        v="${v%\"}"; v="${v#\"}"
        [ -n "${!k:-}" ] || export "$k=$v" ;;
    esac
  done < <(grep -E '^GATEWAY_[A-Z_]+=' "$ROOT/.env" || true)
fi
HOST="${GATEWAY_HOST:?задайте GATEWAY_HOST — ssh-адрес шлюза (в .env или в окружении)}"
REMOTE="${GATEWAY_PROMPT_PATH:?задайте GATEWAY_PROMPT_PATH — путь к SOUL.md на шлюзе}"
RESTART="${GATEWAY_RESTART:?задайте GATEWAY_RESTART — команда перезапуска шлюза}"
OWNER="${GATEWAY_OWNER:-}"
LOCAL="$ROOT/prompt/SOUL.md"
# Путь подставляется в удалённую команду — только абсолютный и только к SOUL.md.
case "$REMOTE" in
  /*/SOUL.md) ;;
  *) echo "GATEWAY_PROMPT_PATH: ожидается абсолютный путь к SOUL.md, получено: $REMOTE" >&2; exit 2 ;;
esac
[ -f "$LOCAL" ] || { echo "нет $LOCAL — сначала скопируйте prompt/SOUL.template.md в prompt/SOUL.md" >&2; exit 2; }

local_sum=$(shasum -a 256 "$LOCAL" | cut -d' ' -f1)
remote_sum=$(ssh "$HOST" "sha256sum '$REMOTE' 2>/dev/null | cut -d' ' -f1" || true)

case "${1:-check}" in
  check)
    if [ "$local_sum" = "$remote_sum" ]; then echo "совпадает: $local_sum"
    else echo "РАСХОЖДЕНИЕ"; echo "  локально: $local_sum"; echo "  на шлюзе: ${remote_sum:-нет файла}"; exit 1; fi ;;
  push)
    stamp=$(date +%Y%m%d-%H%M%S)
    ssh "$HOST" "[ -f '$REMOTE' ] && cp '$REMOTE' '$REMOTE'.before-$stamp || true"
    scp -q "$LOCAL" "$HOST:/tmp/SOUL.new"
    if [ -n "$OWNER" ]; then
      ssh "$HOST" "install -o '${OWNER%%:*}' -g '${OWNER##*:}' -m 640 /tmp/SOUL.new '$REMOTE'; rm -f /tmp/SOUL.new"
    else
      ssh "$HOST" "install -m 640 /tmp/SOUL.new '$REMOTE'; rm -f /tmp/SOUL.new"
    fi
    ssh "$HOST" "$RESTART"
    echo "выложено, шлюз перезапущен" ;;
  *) echo "использование: $0 check|push" >&2; exit 2 ;;
esac
