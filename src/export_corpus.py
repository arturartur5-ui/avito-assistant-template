#!/usr/bin/env python3
"""Выгрузка переписок Авито сразу в обезличенный корпус.

Сырые переписки на диск НЕ попадают: текст обезличивается в памяти, на Mac
ложится только очищенный результат. Так на машине не возникает новой базы
персональных данных — а значит, нет ни обязанностей по её защите, ни риска
утечки, ни вопроса о трансграничной передаче при дальнейшей работе с корпусом.

Результат (по умолчанию в corpus/ проекта, права 700):
  dialogs.jsonl   — диалоги целиком, реплики размечены client/specialist
  qa_pairs.jsonl  — пары «вопрос клиента → ответ специалиста», основа для RAG
  stats.json      — сводка: сколько диалогов, реплик, частотные темы
  .checkpoint     — прогресс, чтобы продолжить после обрыва (отпечатки чатов,
                    не сами идентификаторы: иначе файл связывал бы диалог
                    d00042 с конкретным человеком)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import avito_business as ab
from anonymize import anonymize, split_names

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "corpus"   # движок читает отсюда
PAUSE = 0.35            # пауза между запросами, чтобы не ловить 429
CHATS_PER_PAGE = 100
MSGS_PER_PAGE = 100


def chat_mark(chat_id: str) -> str:
    """Отпечаток чата для чекпоинта — вместо самого идентификатора.

    Чекпоинту нужно только «этот чат уже выгружен», а сам chat_id — зацепка к
    человеку: рядом с последовательной нумерацией диалогов он превращал файл в
    таблицу соответствий, которых в проекте быть не должно (см. CLAUDE.md).
    """
    return hashlib.sha256(chat_id.encode()).hexdigest()[:16]


def load_checkpoint(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}


def collect_chats(client: ab.AvitoBusinessClient, limit: int | None) -> list[dict]:
    chats, offset = [], 0
    while offset <= client.MAX_OFFSET:
        page = client.chats(limit=CHATS_PER_PAGE, offset=offset).get("chats", [])
        if not page:
            break
        chats.extend(page)
        if limit and len(chats) >= limit:
            return chats[:limit]
        offset += CHATS_PER_PAGE
        time.sleep(PAUSE)
    return chats


def is_my_item(chat: dict, my_user_id: int) -> bool:
    """Чат по МОЕМУ объявлению — значит пишет клиент, а не я покупателю."""
    ctx = chat.get("context") or {}
    return (ctx.get("value") or {}).get("user_id") == my_user_id


def item_title(chat: dict) -> str:
    return ((chat.get("context") or {}).get("value") or {}).get("title", "")


def chat_names(chat: dict) -> set[str]:
    names: set[str] = set()
    for u in chat.get("users") or []:
        names |= split_names(u.get("name", ""))
    return names


def fetch_messages(client: ab.AvitoBusinessClient, chat_id: str) -> list[dict]:
    out, offset = [], 0
    while True:
        page = client.messages(chat_id, limit=MSGS_PER_PAGE, offset=offset).get("messages", [])
        if not page:
            break
        out.extend(page)
        if len(page) < MSGS_PER_PAGE:
            break
        offset += MSGS_PER_PAGE
        time.sleep(PAUSE)
    return out


def message_text(m: dict) -> str:
    content = m.get("content") or {}
    if m.get("type") == "text" and content.get("text"):
        return content["text"]
    if content.get("text"):
        return content["text"]
    return f"[ВЛОЖЕНИЕ: {m.get('type', 'неизвестно')}]"


SYSTEM_MARK = re.compile(r"^\[Системное сообщение\]")


def build_dialog(msgs: list[dict], names: set[str]) -> list[dict]:
    """Хронологический диалог с обезличенным текстом."""
    dialog = []
    for m in sorted(msgs, key=lambda x: x.get("created", 0)):
        text = message_text(m)
        if SYSTEM_MARK.match(text):
            role = "system"
        else:
            role = "specialist" if m.get("direction") == "out" else "client"
        clean = anonymize(text, names)
        if not clean:
            continue
        dialog.append({"role": role, "text": clean, "ts": m.get("created")})
    return dialog


def qa_pairs(dialog: list[dict]) -> list[dict]:
    """Пары «последняя реплика клиента → следующий ответ специалиста»."""
    pairs, pending = [], []
    for turn in dialog:
        if turn["role"] == "client":
            pending.append(turn["text"])
        elif turn["role"] == "specialist" and pending:
            pairs.append({"question": " ".join(pending)[:2000], "answer": turn["text"][:2000]})
            pending = []
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description="Обезличенная выгрузка переписок Авито")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, help="взять только первые N чатов (для пробы)")
    ap.add_argument("--resume", action="store_true", help="продолжить прерванную выгрузку")
    ap.add_argument("--all-chats", action="store_true",
                    help="брать и чаты, где специалист сам покупатель (по умолчанию только свои объявления)")
    args = ap.parse_args()

    ab.load_env()
    cid, sec = os.environ.get("AVITO_CLIENT_ID"), os.environ.get("AVITO_CLIENT_SECRET")
    if not cid or not sec:
        print("Нет ключей в .env", file=sys.stderr)
        return 2

    client = ab.AvitoBusinessClient(cid, sec)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)

    for entry in [out, *out.iterdir()]:
        if entry.is_symlink():
            print(f"{entry} — символическая ссылка, в такой каталог не пишем", file=sys.stderr)
            return 2
    ckpt_path = out / ".checkpoint"
    done = load_checkpoint(ckpt_path) if args.resume else set()
    mode = "a" if args.resume else "w"

    print("Собираю список чатов…")
    chats = collect_chats(client, args.limit)
    print(f"чатов: {len(chats)}" + (f", уже выгружено: {len(done)}" if done else ""))

    me = client.whoami()
    my_id = int(me["id"])
    broker_names = split_names(me.get("name", ""))

    if not args.all_chats:
        before = len(chats)
        chats = [c for c in chats if is_my_item(c, my_id)]
        print(f"оставлено {len(chats)} из {before}: только чаты по объявлениям специалиста")
    counter: Counter[str] = Counter()
    n_dialogs = n_turns = n_pairs = 0

    with (out / "dialogs.jsonl").open(mode, encoding="utf-8") as fd, \
         (out / "qa_pairs.jsonl").open(mode, encoding="utf-8") as fq, \
         ckpt_path.open("a", encoding="utf-8") as fc:
        for i, chat in enumerate(chats, 1):
            cid_ = chat.get("id", "")
            if chat_mark(cid_) in done:
                continue
            try:
                msgs = fetch_messages(client, cid_)
            except ab.AvitoError as e:
                print(f"  [{i}/{len(chats)}] пропуск {cid_}: HTTP {e.status}", file=sys.stderr)
                continue

            names = broker_names | chat_names(chat)
            dialog = build_dialog(msgs, names)
            if not dialog:
                fc.write(chat_mark(cid_) + "\n")
                continue

            # Номер вместо chat_id: идентификатор чата — тоже зацепка к человеку.
            n_dialogs += 1
            n_turns += len(dialog)
            fd.write(json.dumps({"dialog_id": f"d{n_dialogs:05d}",
                                 "item": anonymize(item_title(chat), names),
                                 "turns": dialog}, ensure_ascii=False) + "\n")
            for p in qa_pairs(dialog):
                n_pairs += 1
                fq.write(json.dumps(p, ensure_ascii=False) + "\n")
            for turn in dialog:
                if turn["role"] == "client":
                    counter.update(w for w in re.findall(r"[а-яё]{4,}", turn["text"].lower()))
            fc.write(chat_mark(cid_) + "\n")
            fc.flush()

            if i % 25 == 0:
                print(f"  {i}/{len(chats)} чатов, {n_pairs} пар")
            time.sleep(PAUSE)

    stats = {
        "диалогов": n_dialogs,
        "реплик": n_turns,
        "пар вопрос-ответ": n_pairs,
        "частые слова клиентов": dict(counter.most_common(40)),
    }
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    print(f"\nГотово. Диалогов {n_dialogs}, реплик {n_turns}, пар {n_pairs}")
    print(f"Корпус: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
