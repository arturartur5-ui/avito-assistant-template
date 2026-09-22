#!/usr/bin/env python3
"""Проверка перед тем, как открыть репозиторий на общий доступ.

Отличие от `check-personal-data.py`: тот стоит на коммите и смотрит
изменения. Этот смотрит **всё сразу и всю историю** — потому что при
открытии репозитория наружу уходит каждый коммит, который когда-либо был
сделан, включая удалённые файлы. `.gitignore` их уже не спасёт.

Что проверяется:
  1. все файлы под версионным контролем и неотслеживаемые (то, что внесёт
     случайный `git add .`);
  2. каждый блоб во всей истории, включая давно удалённые файлы и блобы без
     имени — следы `git add` + `reset`;
  3. сообщения коммитов и имена веток и тегов;
  4. формальные секреты: токены ботов, ключи API, приватные ключи, JWT,
     строки подключения к БД с паролем; файлы ключей под версионным контролем.

Файл `.personal-terms` обязателен: без него проверка не имеет смысла и
скрипт останавливается. Пишите туда имя и фамилию **и латиницей тоже** —
транслитерация это та щель, через которую имя проекта клиента дважды
прошло мимо проверок. Исключение — `--ci`: в облачной проверке списка нет,
там работают только правила по форме и секреты.

Запуск:  python3 scripts/check-before-publish.py [--ci]
В хуке:  … --pre-push — проверяет только то, что уходит этим push
Код выхода: 0 — можно публиковать, 1 — есть находки, 2 — нет списка терминов.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WARNINGS: list[str] = []   # не блокируют публикацию, но печатаются

# Правила формы и секретов берём из соседнего скрипта, чтобы они жили в одном месте.
_spec = importlib.util.spec_from_file_location(
    "check_personal_data", Path(__file__).with_name("check-personal-data.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def git(*args: str, binary: bool = False):
    r = subprocess.run(["git", "-C", str(ROOT), "-c", "core.quotePath=false", *args], capture_output=True)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def files_now(terms: list[str]) -> list[str]:
    """Рабочее дерево: отслеживаемое и неотслеживаемое, кроме игнорируемого."""
    out: list[str] = []
    listing = git("ls-files", "-z") + git("ls-files", "--others", "--exclude-standard", "-z")
    for rel in dict.fromkeys(r for r in listing.split("\0") if r):
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            out += _mod.scan_file(rel, p.read_bytes(), terms)
        except OSError:
            continue
    return out


def history(terms: list[str], refs: list[str] | None = None) -> list[str]:
    """Каждый блоб в репозитории, включая давно удалённые файлы и безымянные.

    refs — только то, что достижимо из этих коммитов: так проверяется push
    чистой ветки из репозитория с грязной историей. Безымянные блобы при
    этом не смотрим: недостижимое при push никуда не уходит.
    """
    out: list[str] = []
    names: dict[str, str] = {}
    for line in git("rev-list", "--objects", *(refs or ["--all"])).split("\n"):
        sha, _, name = line.partition(" ")
        if sha.strip():
            names.setdefault(sha.strip(), name.strip())
    if refs:
        rows = [f"{sha} blob" for sha in names]
    else:
        rows = git("cat-file", "--batch-check", "--batch-all-objects").split("\n")
    for row in rows:
        parts = row.split()
        if len(parts) < 2 or parts[1] != "blob":
            continue
        sha = parts[0]
        if git("cat-file", "-t", sha).strip() != "blob":
            continue
        name = names.get(sha) or None
        data = git("cat-file", "-p", sha, binary=True)
        if name is not None:
            out += [f"история → {x}" for x in _mod.scan_file(name, data, terms)]
        else:
            out += _mod.scan_text(_mod.decode_any(data), terms, f"история → безымянный блоб {sha[:8]}")
    # сообщения коммитов и имена ссылок
    for entry in git("log", "--format=%h%x00%B%x01", *(refs or ["--all"])).split("\x01"):
        sha, _, body = entry.strip().partition("\x00")
        if sha and not any(full.startswith(sha) for full in _bot_commits()):
            out += _mod.scan_text(body, terms, f"коммит {sha}")
    out += _mod.scan_text(git("for-each-ref", "--format=%(refname)"), terms, "имена веток и тегов")
    # Автор и коммитер каждого коммита видны любому, кто клонирует, — а по
    # содержимому файлов их не найти. Имя из списка терминов — находка;
    # настоящая почта вместо адреса-заглушки GitHub — предупреждение.
    seen: set[tuple[str, str]] = set()
    for entry in git("log", "--format=%an%x00%ae%x00%cn%x00%ce", *(refs or ["--all"])).split("\n"):
        if not entry.strip():
            continue
        an, ae, cn, ce = (entry.split("\x00") + ["", "", "", ""])[:4]
        for who, name, email in (("автор", an, ae), ("коммитер", cn, ce)):
            if (name, email) in seen or _is_bot(name, email):
                continue
            seen.add((name, email))
            low = f"{name} {email}".lower()
            for term in terms:
                if re.search(r"(?<![0-9A-Za-zА-Яа-яЁё])" + re.escape(term), low):
                    out.append(f"{who} коммита  личное имя или метка «{term}» в имени или почте")
                    break
            if email and not email.endswith(("@users.noreply.github.com", "noreply@anthropic.com")):
                WARNINGS.append(f"{who} коммита: настоящая почта на домене «{email.split('@')[-1]}» видна всем, "
                                "кто клонирует. Для публичного репозитория — адрес-заглушка: GitHub → "
                                "Settings → Emails → Keep my email private, и коммиты от этого адреса.")
    return out


# Коммиты роботов GitHub: dependabot и подобные подписываются служебными адресами
# самого GitHub. Это не личные данные, а ветки таких коммитов появятся у любого,
# кто включит dependabot, — проверка не должна на них останавливаться.
BOT_EMAILS = {"noreply" + "@github.com", "support" + "@github.com"}   # склейка: не находка по форме


def _is_bot(name: str, email: str) -> bool:
    return name.endswith("[bot]") or email.lower() in BOT_EMAILS or (name == "GitHub" and email.endswith("@github.com"))


_BOT_COMMITS: set[str] | None = None


def _bot_commits() -> set[str]:
    global _BOT_COMMITS
    if _BOT_COMMITS is None:
        _BOT_COMMITS = set()
        for line in git("log", "--all", "--format=%H%x00%an%x00%ae").split("\n"):
            parts = line.split("\x00")
            if len(parts) == 3 and _is_bot(parts[1], parts[2]):
                _BOT_COMMITS.add(parts[0])
    return _BOT_COMMITS


def pushed_refs() -> list[str]:
    """Хук pre-push получает на stdin строки «локальная ссылка sha удалённая ссылка sha»."""
    refs = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) >= 2 and set(parts[1]) != {"0"}:
            refs.append(parts[1])
    return refs


def main() -> int:
    ci = "--ci" in sys.argv
    terms = _mod.load_terms()
    if not terms and not ci:
        state = _mod.terms_state()
        print(f"Проверка по именам невозможна: {state}. Публиковать вслепую нельзя.\n"
              "  cp .personal-terms.example .personal-terms   # если файла нет\n"
              "и вписать имя и фамилию во всех падежах, домен, id аккаунтов —\n"
              "кириллицей И латиницей, по одному в строке.", file=sys.stderr)
        return 2

    if "--pre-push" in sys.argv:
        refs = pushed_refs()
        if not refs:
            print("push без новых ссылок — проверять нечего.")
            return 0
        problems = dict.fromkeys(history(terms, refs))
    else:
        problems = dict.fromkeys(files_now(terms) + history(terms))
    for w in dict.fromkeys(WARNINGS):
        print(f"ПРЕДУПРЕЖДЕНИЕ: {w}", file=sys.stderr)
    if not problems:
        what = f"по {len(terms)} терминам, " if terms else "без списка терминов (--ci), "
        print(f"Чисто: рабочее дерево, вся история и сообщения коммитов проверены {what}"
              "формальным признакам и секретам.\nРепозиторий можно открывать.")
        return 0

    print("ПУБЛИКОВАТЬ НЕЛЬЗЯ: найдено похожее на личные данные или секреты\n", file=sys.stderr)
    for line in problems:
        print(f"  {line}", file=sys.stderr)
    print("\nЕсли находка в истории — одним файлом её не убрать: публикуйте\n"
          "новым репозиторием с чистой историей (см. SECURITY.md).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
