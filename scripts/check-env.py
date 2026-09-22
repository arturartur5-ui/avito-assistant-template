#!/usr/bin/env python3
"""Что уже настроено, а чего не хватает — одним списком.

Движок падает на первой же незаполненной переменной, и человек чинит их по
одной: запустил, увидел ошибку, вписал, снова запустил. Этот скрипт показывает
сразу всё: переменные по этапам, версию Python, проверку перед коммитом,
промпт, файл условий и его срок, настройку ниши, права на файлы с ключами.

    python3 scripts/check-env.py
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SAMPLE_PASSWORD = "смените-меня"

# (переменная, для чего, обязательна ли на этом этапе)
STAGES: list[tuple[str, list[tuple[str, str, bool]]]] = [
    ("Настройка помощника", [
        ("OWNER_NAME_FORMS", "основы имени специалиста через запятую: по ним его имя "
                             "вычищается из всего, что уходит к модели", True),
        ("OWNER_GENDER", "род, в котором помощник говорит о себе: f или m", True),
    ]),
    ("Модель", [
        ("LLM_BASE_URL", "адрес OpenAI-совместимого шлюза или провайдера", True),
        ("LLM_API_KEY", "ключ доступа к шлюзу", True),
        ("LLM_MODEL", "какая модель или маршрут на шлюзе", True),
    ]),
    ("Экзамен: тренировочный бот", [
        ("TELEGRAM_BOT_TOKEN", "токен бота от @BotFather — заводите отдельного, не рабочего", True),
        ("EXAM_PASSWORD", "свой пароль на вход в бота, от 16 знаков", True),
    ]),
    ("Черновики на подтверждение (этап 2)", [
        ("DRAFT_OWNER_CHAT", "id чата специалиста с ботом: туда уходят черновики", False),
    ]),
    ("Выгрузка переписок", [
        ("AVITO_CLIENT_ID", "ключ Авито: кабинет → «Для профессионалов» → «API»", True),
        ("AVITO_CLIENT_SECRET", "второй ключ оттуда же", True),
        ("TOPIC_KEYWORDS", "слова вашей ниши через запятую: по ним из Telegram "
                           "отбираются рабочие чаты", False),
    ]),
]


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def check_files() -> list[str]:
    """Что кроме .env должно быть на месте. Возвращает список проблем."""
    problems: list[str] = []
    print("\nМашина и файлы")
    if sys.version_info < (3, 10):
        print(f"  ✗ Python {sys.version_info.major}.{sys.version_info.minor} — нужен 3.10 или новее")
        problems.append("python")
    else:
        print(f"  ✓ Python {sys.version_info.major}.{sys.version_info.minor}")

    hook = ROOT / ".git" / "hooks" / "pre-commit"
    if hook.exists() and "check-personal-data" in hook.read_text(encoding="utf-8", errors="replace"):
        print("  ✓ проверка перед коммитом установлена")
    else:
        print("  ✗ проверка перед коммитом не установлена: sh scripts/install-hooks.sh")
        problems.append("hooks")

    soul = ROOT / "prompt" / "SOUL.md"
    if not soul.exists():
        print("  ✗ prompt/SOUL.md — нет промпта специалиста (cp prompt/SOUL.template.md prompt/SOUL.md)")
        problems.append("soul")
    elif re.search(r"\{[^}\n]{3,}\}", soul.read_text(encoding="utf-8")):
        print("  · prompt/SOUL.md — остались незаполненные места в фигурных скобках")
    else:
        print("  ✓ prompt/SOUL.md")

    cond = ROOT / "knowledge" / "usloviya.md"
    if not cond.exists():
        print("  ✗ knowledge/usloviya.md — нет файла условий: цифры заморожены")
        problems.append("usloviya")
    else:
        m = re.search(r"Годен до:?\**\s*\**\s*(\d{2})\.(\d{2})\.(\d{4})", cond.read_text(encoding="utf-8"))
        try:
            until = date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
        except ValueError:
            until = None
        if until is None:
            print("  ✗ knowledge/usloviya.md — нет строки «Годен до: ДД.ММ.ГГГГ»: цифры заморожены")
            problems.append("usloviya-date")
        elif until < date.today():
            print(f"  ✗ knowledge/usloviya.md — срок годности истёк {until:%d.%m.%Y}: цифры заморожены")
            problems.append("usloviya-expired")
        else:
            print(f"  ✓ knowledge/usloviya.md, годен до {until:%d.%m.%Y}")

    niche = ROOT / "config" / "niche.py"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("niche_check", niche)
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        if getattr(mod, "INTAKE_FIELDS", None):
            print(f"  ✓ config/niche.py — пунктов сбора: {len(mod.INTAKE_FIELDS)}")
        else:
            print("  · config/niche.py — пункты сбора не заполнены; помощник не ведёт карточку клиента")
    except Exception as e:                       # noqa: BLE001
        print(f"  ✗ config/niche.py не читается: {e}")
        problems.append("niche")

    # всё, что содержит ключи и оценки, — только владельцу
    for p in list(ROOT.glob(".token_cache*.json")) + [ROOT / "data"]:
        if p.exists() and (p.stat().st_mode & 0o077):
            print(f"  ✗ {p.relative_to(ROOT)} открыт другим пользователям: chmod {'700' if p.is_dir() else '600'} {p.relative_to(ROOT)}")
            problems.append("perms")
    return problems


def main() -> int:
    env_path = ROOT / ".env"
    if not env_path.exists():
        print("Файла .env нет. Создайте рабочие файлы одной командой:\n"
              "    python3 scripts/init.py\n"
              "Она создаст .env с правильными правами, спросит имя специалиста и\n"
              "придумает пароль для бота. Уже существующее не перезаписывает.", file=sys.stderr)
        return 1
    # значения из окружения важнее: так же их читает движок
    env = read_env(env_path)
    sample = read_env(ROOT / ".env.example")
    env.update({k: v for k, v in os.environ.items()
                if k in env or k.startswith(("LLM_", "AVITO_", "OWNER_", "EXAM_"))})

    missing: list[str] = []
    for stage, variables in STAGES:
        lines = []
        for name, why, required in variables:
            value = env.get(name, "").strip()
            if value and value == sample.get(name, "").strip() and name not in ("TOPIC_MIN_HITS", "EXAM_DAILY_LIMIT", "DRAFT_POLL_SECONDS"):
                lines.append(f"  · {name} — оставлено значение из образца, впишите своё")
                if required:
                    missing.append(f"{stage}: {name}")
                continue
            if value and not (name == "EXAM_PASSWORD" and value == SAMPLE_PASSWORD):
                lines.append(f"  ✓ {name}")
                continue
            mark = "✗" if required else "·"
            tail = "" if required else " (необязательно)"
            if name == "EXAM_PASSWORD" and value == SAMPLE_PASSWORD:
                why = "стоит образец из .env.example — по нему в бота зайдёт любой, кто его найдёт"
            lines.append(f"  {mark} {name} — {why}{tail}")
            if required:
                missing.append(f"{stage}: {name}")
        print(f"\n{stage}")
        print("\n".join(lines))

    # права на файл: ключи должен читать только владелец
    mode = env_path.stat().st_mode & 0o777
    if mode & 0o077:
        print(f"\nВНИМАНИЕ: .env открыт на чтение другим пользователям (права {mode:o}). "
              "Закройте: chmod 600 .env")
        missing.append("права .env")
    missing += check_files()

    if not missing:
        print("\nВсё, что нужно, заполнено.")
        return 0
    print(f"\nНе заполнено обязательного: {len(missing)}. Это не значит «сломано»: каждый\n"
          "этап требует своего, и до выгрузки переписок ключи Авито не нужны.\n"
          "Заполняйте по мере того, как доходите до этапа — порядок в SETUP.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
