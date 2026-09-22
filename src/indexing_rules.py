#!/usr/bin/env python3
"""Что можно и чего нельзя брать в базу знаний ассистента.

Решение по проекту: фотографии, видео и PDF в модель не попадают.
Причина — их нельзя проверить текстовым аудитом: имя клиента на скриншоте
переписки или фамилия в углу фото документа регуляркой не видны, а OCR мы
не делаем. Поэтому двоичные файлы исключены целиком, а не выборочно.

В рабочей папке таких файлов сотни.

Правило действует на этапе индексации: индексатор ходит только через
collect_sources(), других путей к файлам у него нет.
"""
from __future__ import annotations

from pathlib import Path

# Только текст. Всё остальное не индексируется ни при каких условиях.
ALLOWED_EXT = {".md", ".txt", ".jsonl", ".json", ".csv", ".tsv", ".yaml", ".yml"}

# Никогда не индексируем: двоичное, служебное, зависимости.
DENIED_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".svg", ".bmp", ".tiff",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav", ".ogg", ".m4a",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".psd", ".ai", ".sketch", ".fig",
    ".css", ".js", ".mjs", ".map", ".ttf", ".woff", ".woff2", ".ico",
}
DENIED_DIRS = {"node_modules", ".git", "__pycache__", "pii-guard-data",
               ".next", "dist", "venv"}
DENIED_NAMES = {"package-lock.json", "package.json", "yarn.lock", ".env",
                ".token_cache.json", "mapping.json"}


class RefusedSource(RuntimeError):
    pass


def is_allowed(path: Path) -> tuple[bool, str]:
    if path.is_symlink():
        return False, "символическая ссылка — может вести куда угодно, хоть в .env"
    if any(part in DENIED_DIRS for part in path.parts):
        return False, "каталог исключён"
    if path.name in DENIED_NAMES:
        return False, "служебный файл"
    ext = path.suffix.lower()
    if ext in DENIED_EXT:
        return False, "двоичный файл — в модель не идёт"
    if ext not in ALLOWED_EXT:
        return False, f"неизвестный тип {ext or 'без расширения'}"
    return True, ""


def collect_sources(root: Path) -> list[Path]:
    """Единственный разрешённый способ набрать файлы для индексации."""
    root = root.resolve()
    return sorted(p for p in root.rglob("*")
                  if not p.is_symlink() and p.is_file() and is_allowed(p)[0]
                  and p.resolve().is_relative_to(root))


def report(root: Path) -> str:
    allowed, denied = [], {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        ok, why = is_allowed(p)
        if ok:
            allowed.append(p)
        else:
            denied[why] = denied.get(why, 0) + 1
    lines = [f"разрешено к индексации: {len(allowed)} файлов",
             f"отклонено: {sum(denied.values())}"]
    lines += [f"  {why}: {n}" for why, n in sorted(denied.items(), key=lambda kv: -kv[1])]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Укажите папку: python3 src/indexing_rules.py ПУТЬ", file=sys.stderr)
        raise SystemExit(2)
    root = Path(sys.argv[1])
    print(f"=== {root}")
    print(report(root))
