#!/usr/bin/env python3
"""Проверка настройки ниши: заполнено ли, компилируется ли, не подвесит ли бота.

Запускать после интервью и после каждой правки config/niche.py:

    python3 scripts/check-niche.py

Проверяет:
  - все регулярные выражения компилируются;
  - ни одно не «взрывается» на длинном сообщении (катастрофический перебор:
    такой шаблон подвешивает бота на сообщении клиента);
  - имена в ASK_STEPS совпадают с названиями пунктов в INTAKE_FIELDS;
  - каждый пункт сбора попадает хотя бы в один шаг разговора;
  - заполнены черновики, без которых фильтр ответит пустой строкой.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Несколько враждебных строк: длинные повторы букв, цифр, пробелов и «почти
# совпадение» без конца — на одной фиксированной строке катастрофический
# перебор легко не заметить.
PROBES = [
    "Здравствуйте! " + "нужна консультация по моему вопросу, " * 40 + "перезвоните мне пожалуйста",
    "a" * 3000 + "!", "я" * 3000 + "?", "1" * 3000 + "x", " " * 2000 + "конец",
    "ab " * 1500 + "!", "8 " * 1500 + "9",
]
SLOW = 0.25   # секунд на одно сообщение — больше уже опасно
# --file путь.py — проверить другой файл настройки (например, образец из config/examples/)
NICHE_FILE = sys.argv[sys.argv.index("--file") + 1] if "--file" in sys.argv else ""


def check() -> tuple[list[str], list[str]]:
    """Возвращает (ошибки, незаполненное).

    Ошибка — то, что сломает помощника: выражение не компилируется или
    подвешивает бота, опечатка в имени шага, пустой черновик при заполненных
    правилах. Незаполненное — нормальное состояние до интервью, это не сбой.
    """
    problems: list[str] = []
    todo: list[str] = []
    try:
        if NICHE_FILE:
            import importlib.util
            spec = importlib.util.spec_from_file_location("niche_under_check", NICHE_FILE)
            niche = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(niche)
        else:
            from config import niche
    except Exception as e:                                   # noqa: BLE001
        return [f"{NICHE_FILE or 'config/niche.py'} не читается: {e}"], []

    def patterns():
        """Все регулярки настройки: и готовые, и строками."""
        for name in dir(niche):
            if name.startswith("_"):
                continue
            value = getattr(niche, name)
            if isinstance(value, re.Pattern):
                yield name, value.pattern, value
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, tuple) and item and isinstance(item[0], (str, re.Pattern)):
                        raw = item[0]
                        try:
                            rx = raw if isinstance(raw, re.Pattern) else re.compile(raw, re.I)
                        except re.error as e:
                            problems.append(f"{name}: выражение не компилируется — {e}")
                            continue
                        yield name, rx.pattern, rx

    for name, pattern, rx in patterns():
        started = time.perf_counter()
        try:
            for probe in PROBES:
                rx.search(probe)
        except Exception as e:                               # noqa: BLE001
            problems.append(f"{name}: ошибка при проверке — {e}")
            continue
        spent = time.perf_counter() - started
        if spent > SLOW:
            problems.append(f"{name}: выражение слишком медленное ({spent:.2f} с на одно сообщение) — "
                            f"перепишите проще, без вложенных повторов: {pattern[:60]}")

    fields = [name for name, _ in getattr(niche, "INTAKE_FIELDS", [])]
    steps = getattr(niche, "ASK_STEPS", [])
    if not fields:
        todo.append("INTAKE_FIELDS пуст: помощник не знает, что выяснять у клиента")
    if not steps:
        todo.append("ASK_STEPS пуст: помощник не знает, в каком порядке спрашивать")
    for group, question in steps:
        for f in group:
            if f not in fields:
                problems.append(f"ASK_STEPS: «{f}» нет среди пунктов INTAKE_FIELDS — опечатка?")
        if not question.strip():
            problems.append(f"ASK_STEPS: у шага {group} пустой вопрос")
    covered = {f for group, _ in steps for f in group}
    for f in fields:
        if f not in covered:
            problems.append(f"пункт «{f}» не попал ни в один шаг разговора — его никогда не спросят")

    for draft, why in [("DRAFT_PRICE", "ответ на «сколько стоит»"),
                       ("DRAFT_HANDOFF", "фраза при передаче специалисту")]:
        if not (getattr(niche, draft, "") or "").strip():
            todo.append(f"{draft} не заполнен: {why}")
    for rules, draft in [("ILLEGAL_RULES", "DRAFT_ILLEGAL"),
                         ("FORBIDDEN_NAMES", "DRAFT_FORBIDDEN"),
                         ("NOT_OUR_WORK", "DRAFT_NOT_OUR_WORK")]:
        if getattr(niche, rules, []) and not (getattr(niche, draft, "") or "").strip():
            problems.append(f"{rules} заполнены, а {draft} пуст — клиенту уйдёт пустой ответ")
    return problems, todo


def main() -> int:
    problems, todo = check()
    if todo:
        print("Ниша ещё не заполнена — это нормально до интервью:")
        for t in todo:
            print(f"  {t}")
    if not problems:
        print("Настройка ниши в порядке." if not todo else "Ошибок нет.")
        return 0
    print("НАСТРОЙКА НИШИ: есть ошибки\n", file=sys.stderr)
    for p in problems:
        print(f"  {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
