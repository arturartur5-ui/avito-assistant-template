#!/usr/bin/env python3
"""Импорт публичного канала Telegram в базу знаний.

Канал — не переписка: автор один, реплик собеседника нет. Поэтому результат
кладётся не в корпус диалогов, а в knowledge/ как справочный материал,
разложенный по датам.

Обезличивание то же, что и везде. Посты публичные, но имена клиентов из
отзывов боту не нужны, а в базе знаний они только мешают.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from import_telegram import message_text
from anonymize import anonymize, split_names


def _load_env() -> None:
    """Переменные из .env проекта, если они ещё не заданы в окружении."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def slug(name: str) -> str:
    table = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                          "abvgdeejziyklmnoprstufhccss'y'eua")
    s = name.lower().translate(table)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:50] or "kanal"


def main() -> int:
    ap = argparse.ArgumentParser(description="Импорт канала Telegram в базу знаний")
    ap.add_argument("source", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "knowledge")
    ap.add_argument("--force", action="store_true", help="перезаписать файл, если он уже есть")
    ap.add_argument("--owner", default="",
                    help="имя владельца канала — чтобы вырезать его из текста")
    ap.add_argument("--min-len", type=int, default=40,
                    help="короче этого пост в базу знаний не идёт")
    args = ap.parse_args()

    data = json.loads(args.source.read_text(encoding="utf-8"))
    title = data.get("name") or "Канал"
    names = split_names(title) | split_names(args.owner or "")

    args.out.mkdir(parents=True, exist_ok=True)
    args.out.chmod(0o700)

    lines = [f"# {anonymize(title, set())}", "",
             f"Посты канала, выгружено {dt.date.today():%d.%m.%Y}. Это публикации самой",
             "специалиста, а не переписка с клиентами. Обезличено тем же набором правил,",
             "что и корпус диалогов.", ""]
    kept = skipped = 0
    last_day = None
    for m in sorted((m for m in data.get("messages", []) if m.get("type") == "message"),
                    key=lambda x: int(x.get("date_unixtime", 0))):
        raw = message_text(m)
        if not raw or raw.startswith("[ВЛОЖЕНИЕ"):
            skipped += 1
            continue
        clean = anonymize(raw, names)
        if len(clean) < args.min_len:
            skipped += 1
            continue
        day = dt.datetime.fromtimestamp(int(m.get("date_unixtime", 0))).strftime("%Y-%m-%d")
        if day != last_day:
            lines += ["", f"## {day}", ""]
            last_day = day
        lines += [clean, ""]
        kept += 1

    # Название канала может содержать имя или телефон: в имя файла и на экран
    # идёт обезличенный вариант, а хвост-хеш отличает похожие названия.
    safe_title = anonymize(title, names)
    path = args.out / f"kanal-{slug(safe_title)[:40]}-{hashlib.sha256(title.encode()).hexdigest()[:8]}.md"
    if path.exists() and not args.force:
        print(f"{path.name} уже есть — перезапись только с --force", file=sys.stderr)
        return 1
    if path.exists():
        os.chmod(path, 0o600)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"«{safe_title}»: сохранено {kept} постов, пропущено {skipped}")
    print(f"  -> {path.name}  ({path.stat().st_size/1024:.0f} КБ)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
