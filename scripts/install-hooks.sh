#!/bin/sh
# Ставит два хука git:
#   pre-commit — не пускает в коммит личные данные (проверяет изменения);
#   pre-push   — не пускает наружу репозиторий, в котором личные данные или
#                секреты есть где угодно: во всех файлах или в истории.
# Запускать один раз после клонирования: sh scripts/install-hooks.sh
set -e
ROOT=$(git rev-parse --show-toplevel)
mkdir -p "$ROOT/.git/hooks"
cat > "$ROOT/.git/hooks/pre-commit" <<'HOOK'
#!/bin/sh
exec python3 "$(git rev-parse --show-toplevel)/scripts/check-personal-data.py"
HOOK
cat > "$ROOT/.git/hooks/pre-push" <<'HOOK'
#!/bin/sh
exec python3 "$(git rev-parse --show-toplevel)/scripts/check-before-publish.py" --pre-push
HOOK
chmod +x "$ROOT/.git/hooks/pre-commit" "$ROOT/.git/hooks/pre-push"
echo "хуки установлены: pre-commit (изменения) и pre-push (всё и история)"
