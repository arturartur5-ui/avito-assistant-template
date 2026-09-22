#!/usr/bin/env python3
"""Первичная настройка: создаёт рабочие файлы из образцов.

Нужен тем, кто настраивает без Claude Code. Скрипт не заменяет интервью
(START-HERE.md) — он снимает механическую часть: скопировать пять файлов,
не перепутать права и придумать пароль. Содержание всё равно заполняет
человек своими словами.

    python3 scripts/init.py

Ничего существующего не перезаписывает: файл на месте — пропускает и говорит
об этом.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (образец, куда копировать, права, что это)
FILES: list[tuple[str, str, int, str]] = [
    (".env.example", ".env", 0o600, "ключи и настройки"),
    (".personal-terms.example", ".personal-terms", 0o600, "личные термины для проверки перед коммитом"),
    ("prompt/SOUL.template.md", "prompt/SOUL.md", 0o644, "системный промпт"),
    ("knowledge/usloviya.template.md", "knowledge/usloviya.md", 0o644, "условия и цены — единственный источник цифр"),
    ("TZ.template.md", "TZ.md", 0o644, "техзадание: точность, тональность, стоп-темы"),
]


def ask(question: str, *, allow_empty: bool = False) -> str:
    """Вопрос человеку. Пустой ответ — пропустить, если это разрешено."""
    while True:
        try:
            answer = input(f"{question}\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПрервано. Уже созданные файлы остались на месте.")
            raise SystemExit(1)
        if answer or allow_empty:
            return answer
        print("Нужен ответ. Пропустить нельзя — без этого помощник работать не будет.")


def copy_samples() -> list[str]:
    """Копирует образцы. Возвращает список того, что создал."""
    created = []
    for sample, target, mode, what in FILES:
        src, dst = ROOT / sample, ROOT / target
        if dst.exists():
            print(f"  · {target} уже есть — не трогаю")
            continue
        if not src.exists():
            print(f"  ! нет образца {sample} — пропускаю", file=sys.stderr)
            continue
        shutil.copy2(src, dst)
        _strip_template_header(dst)
        os.chmod(dst, mode)
        print(f"  ✓ {target} — {what}")
        created.append(target)
    return created


def _strip_template_header(path: Path) -> None:
    """Инструкция «Скопируйте этот файл…» нужна в образце, а не в рабочем файле:
    иначе она уезжает модели как часть промпта или условий."""
    if path.suffix != ".md":
        return
    text = path.read_text(encoding="utf-8")
    if path.name == "SOUL.md" and "\n---\n" in text:
        text = text.split("\n---\n", 1)[1].lstrip("\n")
    else:
        # первая цитата «> Шаблон. …» / «> Скопируйте …» — целиком
        text = re.sub(r"\A(?:# [^\n]*\n\n)?> (?:Шаблон|Скопируйте)[^\n]*\n(?:>[^\n]*\n)*\n?", lambda m: (m.group(0).split("\n")[0] + "\n\n") if m.group(0).startswith("# ") else "", text)
    path.write_text(text, encoding="utf-8")


def set_env(keys: dict[str, str]) -> None:
    """Вписывает значения в .env, не трогая остальные строки и комментарии."""
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines()
    for key, value in keys.items():
        for i, line in enumerate(lines):
            if re.match(rf"^{re.escape(key)}=", line):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> int:
    print("Первичная настройка. Создаю рабочие файлы из образцов.\n")
    created = copy_samples()

    if ".env" not in created:
        print("\n.env уже существовал — вопросы про имя и пароль пропускаю, чтобы\n"
              "не затереть то, что вы уже вписали. Что заполнено: python3 scripts/check-env.py")
        return 0

    print("\nДва вопроса, без которых помощник не запустится.\n")

    names = ask("Основы имени и фамилии специалиста через запятую.\n"
                "По ним его имя вычищается из всего, что уходит к модели.\n"
                "Пишите основы без окончаний: «Ярополк,Ярик,Примеров» покроет\n"
                "«Ярополку», «Ярику», «Примерова».")

    gender = ""
    while gender not in ("f", "m"):
        gender = ask("Род, в котором помощник говорит о себе: f — женский, m — мужской.").lower()
        if gender not in ("f", "m"):
            print("Только f или m.")

    # пароль придумывает машина: человек ставит слабый и один на всё
    alphabet = string.ascii_letters + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(24))

    set_env({"OWNER_NAME_FORMS": names, "OWNER_GENDER": gender, "EXAM_PASSWORD": password})

    print(f"\nГотово. Пароль для тренировочного бота сгенерирован и записан в .env:\n\n    {password}\n")
    print("Передайте его специалисту лично — не в переписке с клиентами.\n"
          "Он входит в бота командой: /start " + password + "\n")
    print("Что дальше:\n"
          "  1. Заполнить config/niche.py — образец в config/examples/\n"
          "  2. Заполнить prompt/SOUL.md и knowledge/usloviya.md своими словами\n"
          "  3. Заполнить TZ.md — по нему потом принимать работу\n"
          "  4. Вписать ключи в .env: python3 scripts/check-env.py покажет, чего не хватает\n"
          "  5. Поставить проверки перед коммитом: sh scripts/install-hooks.sh\n\n"
          "Разговором это делается быстрее: откройте проект в Claude Code и скажите\n"
          "«проведи интервью по START-HERE.md».")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
