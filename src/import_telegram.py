#!/usr/bin/env python3
"""Импорт переписок Telegram в обезличенный корпус.

Берёт result.json из экспорта Telegram Desktop и кладёт рядом с корпусом Авито
диалоги в том же формате. Сырьё на диск не пишется — обезличивание идёт в памяти.

Отличие от Авито: Telegram сам размечает сущности (phone, email, mention, link)
в text_entities — используем эту разметку как первый слой, регулярки как второй.
Вложения не скачиваются, от них остаётся только тип.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
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

# Чат считаем профильным, если слова ниши встречаются достаточно часто.
# Слова зависят от того, чем занимается специалист, поэтому живут в настройках,
# а не в коде: TOPIC_KEYWORDS в .env, через запятую (пример — в .env.example).
# Без них импорт не запускается: иначе в корпус попадут личные переписки.
_TOPIC_WORDS = [w.strip() for w in os.environ.get("TOPIC_KEYWORDS", "").split(",") if w.strip()]
TOPIC = re.compile("|".join(re.escape(w) for w in _TOPIC_WORDS) or r"(?!x)x", re.I)
MIN_TOPIC_HITS = int(os.environ.get("TOPIC_MIN_HITS", "3"))

# Что не берём никогда.
# saved_messages — «Избранное», личные заметки владельца, не переписка с клиентом.
# групповые чаты — там пишут посторонние, которые к специалисту не обращались.
EXCLUDE_TYPES = {"saved_messages", "private_supergroup", "private_group"}
# Личные чаты, не относящиеся к работе: семья, близкие, друзья.
# Заполняется под конкретного специалиста — именами и подписями чатов,
# как они выглядят в его Telegram (регистр и эмодзи не важны).
# Значения — персональные данные, в репозиторий не коммитятся:
# держите их в EXCLUDE_CHATS в .env через запятую.
EXCLUDE_NAMES = {
    n.strip().lower()
    for n in os.environ.get("EXCLUDE_CHATS", "").split(",")
    if n.strip()
}


def is_excluded(chat: dict) -> tuple[bool, str]:
    if chat.get("type") in EXCLUDE_TYPES:
        return True, chat.get("type", "")
    name = (chat.get("name") or "").strip().lower()
    bare = "".join(ch for ch in name if ch.isalpha() or ch.isspace()).strip()
    if name in EXCLUDE_NAMES or bare in EXCLUDE_NAMES:
        return True, "личный чат"
    return False, ""

# Сущности Telegram, которые вырезаем сразу по его же разметке.
ENTITY_MAP = {
    "phone": "[ТЕЛЕФОН]", "email": "[EMAIL]", "mention": "[НИК]",
    "mention_name": "[ИМЯ]", "link": "[ССЫЛКА]", "text_link": "[ССЫЛКА]",
    "bank_card": "[КАРТА]",
}


def entity_text(part) -> str:
    """Одна часть сообщения: строка или сущность с типом."""
    if isinstance(part, str):
        return part
    kind = part.get("type", "")
    if kind in ENTITY_MAP:
        return ENTITY_MAP[kind]
    return part.get("text", "")


def message_text(m: dict) -> str:
    parts = m.get("text_entities") or m.get("text") or ""
    if isinstance(parts, list):
        body = "".join(entity_text(p) for p in parts)
    else:
        body = parts or ""
    body = body.strip()
    if not body and (m.get("file") or m.get("photo") or m.get("media_type")):
        kind = m.get("media_type") or ("photo" if m.get("photo") else "file")
        return f"[ВЛОЖЕНИЕ: {kind}]"
    return body


def chat_names(chat: dict, owner_names: set[str]) -> set[str]:
    names = set(owner_names)
    names |= split_names(chat.get("name") or "")
    for m in chat.get("messages", [])[:200]:
        names |= split_names(m.get("from") or "")
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description="Импорт Telegram в обезличенный корпус")
    ap.add_argument("source", type=Path, help="путь к result.json")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "corpus")
    ap.add_argument("--owner", default="",
                    help="имя владельца аккаунта, как оно написано в экспорте (поле from); "
                         "без флага владельцем считается тот, кто написал больше всех")
    ap.add_argument("--all-chats", action="store_true",
                    help="брать все чаты, а не только профильные")
    args = ap.parse_args()
    if not _TOPIC_WORDS:
        print("TOPIC_KEYWORDS в .env пуст: без слов ниши рабочие чаты не отличить от личных, "
              "и в корпус уйдёт лишнее. Заполните и запустите снова.", file=sys.stderr)
        return 2

    data = json.loads(args.source.read_text(encoding="utf-8"))
    chats = data.get("chats", {}).get("list", [])

    # Владелец аккаунта — тот, кто пишет больше всех.
    senders = Counter()
    for c in chats:
        for m in c.get("messages", []):
            if m.get("from_id"):
                senders[m["from_id"]] += 1
    if args.owner:
        owner_id = next((m.get("from_id") for c in chats for m in c.get("messages", [])
                         if (m.get("from") or "").strip().lower() == args.owner.strip().lower()), None)
        if owner_id is None:
            print(f"в экспорте нет сообщений от «{args.owner}» — проверьте, как имя написано в result.json",
                  file=sys.stderr)
            return 2
    else:
        top = senders.most_common(2)
        if len(top) > 1 and top[0][1] == top[1][1]:
            print("двое написали поровну — кто из них владелец, не понять. Укажите --owner \"Имя как в экспорте\"",
                  file=sys.stderr)
            return 2
        owner_id = top[0][0]
    owner_name = next((m.get("from") for c in chats for m in c.get("messages", [])
                       if m.get("from_id") == owner_id and m.get("from")), "")
    owner_names = split_names(owner_name)
    print(f"владельцем считаю: {owner_name or '?'} — сообщений {senders[owner_id]} из "
          f"{sum(senders.values())}. Не он — перезапустите с --owner \"Имя как в экспорте\"")

    args.out.mkdir(parents=True, exist_ok=True)
    args.out.chmod(0o700)
    def private(name: str):
        # переписки, пусть и обезличенные, — только владельцу: права 0600 с момента создания
        if (args.out / name).exists():
            os.chmod(args.out / name, 0o600)
        return os.fdopen(os.open(args.out / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                         "w", encoding="utf-8")
    fd = private("telegram_dialogs.jsonl")
    fq = private("telegram_qa_pairs.jsonl")

    n_dialogs = n_turns = n_pairs = skipped = 0
    dropped: dict[str, int] = {}
    for chat in chats:
        msgs = [m for m in chat.get("messages", []) if m.get("type") == "message"]
        if not msgs:
            continue
        excluded, why = is_excluded(chat)
        if excluded:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        if not args.all_chats:
            body = " ".join(message_text(m) for m in msgs[:400])
            if len(TOPIC.findall(body)) < MIN_TOPIC_HITS:
                skipped += 1
                continue

        names = chat_names(chat, owner_names)
        dialog, pending = [], []
        for m in sorted(msgs, key=lambda x: int(x.get("date_unixtime", 0))):
            raw = message_text(m)
            if not raw:
                continue
            clean = anonymize(raw, names)
            if not clean:
                continue
            role = "specialist" if m.get("from_id") == owner_id else "client"
            dialog.append({"role": role, "text": clean,
                           "ts": int(m.get("date_unixtime", 0))})
            if role == "client":
                pending.append(clean)
            elif pending:
                fq.write(json.dumps({"source": "telegram",
                                     "question": " ".join(pending)[:2000],
                                     "answer": clean[:2000]}, ensure_ascii=False) + "\n")
                n_pairs += 1
                pending = []

        if not dialog:
            continue
        n_dialogs += 1
        n_turns += len(dialog)
        fd.write(json.dumps({"dialog_id": f"tg{n_dialogs:05d}", "source": "telegram",
                             "turns": dialog}, ensure_ascii=False) + "\n")

    fd.close(); fq.close()
    print(f"диалогов {n_dialogs}, реплик {n_turns}, пар {n_pairs}")
    print(f"пропущено непрофильных чатов: {skipped}")
    for why, n in sorted(dropped.items()):
        print(f"  исключено ({why}): {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
