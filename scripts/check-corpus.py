#!/usr/bin/env python3
"""Аудит корпуса и базы знаний: не осталось ли в них личных данных.

Обязательный шаг перед тем, как отдавать корпус куда-либо (SETUP.md, шаг 8).
Остальные проверки смотрят файлы под контролем версий, а `corpus/` и
`knowledge/` в `.gitignore` — то есть именно то, ради чего всё затевалось,
не проверял никто.

    python3 scripts/check-corpus.py            # corpus/ и knowledge/
    python3 scripts/check-corpus.py путь       # конкретный файл или папка

Что делает: прогоняет каждую запись через ту же проверку, что стоит на двери
наружу (`anonymize.leak_scan`), и печатает, сколько чего нашлось. Сами
значения не печатаются и никуда не пишутся: цель — узнать, что правило
пропустило, а не собрать вторую копию персональных данных.

Ноль находок — обязательное условие. Нашлось — правило дописывается в
`src/anonymize.py`, и выгрузка повторяется.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from anonymize import PLACEHOLDER, leak_scan, outbound  # noqa: E402

# Что читаем. Двоичное не трогаем: его нельзя проверить текстом, и в корпусе
# ему не место (см. src/indexing_rules.py).
TEXT_SUFFIX = {".jsonl", ".json", ".md", ".txt", ".csv"}
DEFAULT_DIRS = ["corpus", "knowledge"]
MAX_SHOW = 12                      # сколько мест показать, чтобы вывод не разрастался


def texts_of(path: Path):
    """Содержимое файла кусками: (номер строки, текст). JSONL — по записям."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"{path}: не прочитать — {e}", file=sys.stderr)
        return
    if path.suffix == ".jsonl":
        for n, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                yield n, line
                continue
            yield n, " ".join(_strings(item))
    else:
        for n, line in enumerate(raw.splitlines(), 1):
            if line.strip():
                yield n, line


def _strings(item) -> list[str]:
    """Все строковые значения записи, на любой глубине вложенности."""
    if isinstance(item, str):
        return [item]
    if isinstance(item, dict):
        return [s for v in item.values() for s in _strings(v)]
    if isinstance(item, list):
        return [s for v in item for s in _strings(v)]
    return []


def scan(paths: list[Path]) -> tuple[Counter, list[str], int]:
    kinds: Counter = Counter()
    places: list[str] = []
    files = 0
    for path in paths:
        targets = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for f in targets:
            if f.suffix.lower() not in TEXT_SUFFIX or f.name.startswith("."):
                continue
            files += 1
            for line_no, text in texts_of(f):
                found = list(leak_scan(text))
                # Корпус уже должен быть обезличен. Если обезличивание находит в
                # нём что-то ещё — например имя, которое leak_scan не ловит, —
                # значит файл собран старым кодом или правкой мимо скриптов.
                cleaned = outbound(text)
                if cleaned != text:
                    extra = {p for p in PLACEHOLDER.findall(cleaned)} - {p for p in PLACEHOLDER.findall(text)}
                    for kind in (extra or {"[НЕОБЕЗЛИЧЕНО]"}):
                        found.append((f"осталось необезличенным: {kind}", ""))
                for kind, _value in found:
                    kinds[kind] += 1
                    if len(places) < MAX_SHOW:
                        places.append(f"{f.relative_to(ROOT) if ROOT in f.parents else f}:{line_no}  {kind}")
    return kinds, places, files


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    paths = [Path(a) for a in args] or [ROOT / d for d in DEFAULT_DIRS]
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("Нечего проверять: нет ни corpus/, ни knowledge/. Соберите корпус "
              "(SETUP.md, шаг 7) и запустите снова.", file=sys.stderr)
        return 1

    kinds, places, files = scan(paths)
    print(f"проверено файлов: {files}")
    if not files:
        print("Проверять нечего: ни одного текстового файла не нашлось. "
              "Это не «корпус чист» — соберите корпус и запустите снова.", file=sys.stderr)
        return 1
    if not kinds:
        print("Личных данных не найдено. Корпус можно использовать.")
        return 0

    print(f"\nНАЙДЕНО: {sum(kinds.values())} мест в {files} файлах\n")
    for kind, n in kinds.most_common():
        print(f"  {kind}: {n}")
    print("\nГде именно (первые несколько; сами значения не печатаются):")
    for place in places:
        print(f"  {place}")
    print("\nЧто делать:\n"
          "  1. Откройте одно из мест и посмотрите, в каком виде там данные.\n"
          "  2. Допишите правило в src/anonymize.py и тест на эту форму.\n"
          "  3. Соберите корпус заново — правка правил задним числом не чинит\n"
          "     уже выгруженные файлы.\n"
          "Отдавать корпус куда-либо можно только при нуле находок.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
