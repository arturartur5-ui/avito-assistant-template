#!/usr/bin/env python3
"""Этап 2 из docs/zapusk.md: помощник готовит ответ, отправляет специалист.

Цикл: новое сообщение в чате Авито → движок готовит черновик → специалисту в
Telegram уходит обезличенный текст и кнопки. Клиенту ничего не уходит, пока
специалист не нажал «отправить».

Что уходит в Telegram: код клиента (К-417), обезличенный текст его сообщения,
черновик ответа. Имя, телефон и почта клиента в уведомление не попадают —
иначе мы аккуратно вычистили корпус и слили те же данные через служебный
канал. Нужен оригинал — кнопка «открыть в Авито».

    python3 src/draft_loop.py            # рабочий режим
    python3 src/draft_loop.py --dry-run  # ничего не отправляет, печатает в терминал
    python3 src/draft_loop.py --once     # один проход вместо бесконечного цикла

ЭТО ЭТАЛОН, А НЕ КОРОБКА. Связка зависит от того, где специалисту удобно
подтверждать: здесь Telegram, у вас может быть веб-панель или почта. Читайте
как образец и проверьте на своём аккаунте, прежде чем пускать на клиентов:
у Авито бывают свои особенности по типам чатов и правам тарифа.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from anonymize import leak_scan, outbound
from avito_business import AvitoBusinessClient, load_env as load_avito_env
from engine import ROOT, Engine, _opener, load_env
from guard import HOLD, check_outgoing, explain

load_env(ROOT / ".env")
load_avito_env()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# кому слать черновики: id чата специалиста с ботом. Узнать: написать боту и
# посмотреть chat_id в его выводе, либо взять из тренировочного бота.
OWNER_CHAT = os.environ.get("DRAFT_OWNER_CHAT", "")
POLL_SECONDS = int(os.environ.get("DRAFT_POLL_SECONDS", "60"))
MAX_HISTORY = 20                       # сколько реплик чата подаём движку
API = f"https://api.telegram.org/bot{TOKEN}"
DB = ROOT / "data" / "draft.db"
AWAITING_EDIT: dict[int, int] = {}     # chat_id специалиста -> id черновика, к которому ждём правку


# --- служебное ---------------------------------------------------------------

def tg(method: str, **params) -> dict:
    data = urllib.parse.urlencode({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                                   for k, v in params.items()}).encode()
    # без прокси из окружения и без редиректов — как у клиентов шлюза и Авито;
    # ответ Telegram не бывает больше пары мегабайт, остальное не читаем
    with _opener().open(urllib.request.Request(f"{API}/{method}", data=data), timeout=40) as r:
        return json.loads(r.read(4_000_000).decode("utf-8"))


def client_code(chat_id: str) -> str:
    """Короткая метка чата для разговора со специалистом: «К-417».

    Считается из chat_id хешем, нигде не хранится: таблицы соответствий
    в этом проекте не заводятся (docs/pdn-i-zakony.md). Нужен настоящий
    клиент — специалист открывает чат в Авито по кнопке.
    """
    return "К-" + str(int(hashlib.sha256(chat_id.encode()).hexdigest()[:6], 16) % 1000).zfill(3)


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(DB.parent, 0o700)
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS drafts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        incoming TEXT NOT NULL,          -- обезличенное сообщение клиента
        draft TEXT NOT NULL,             -- что предлагает помощник
        held INTEGER NOT NULL,           -- 1 = фильтр остановил, нужен свой ответ
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'wait',   -- wait | sent | skipped
        created REAL NOT NULL,
        UNIQUE(chat_id, message_id))""")
    conn.commit()
    if DB.exists():
        os.chmod(DB, 0o600)
    return conn


def seen(conn: sqlite3.Connection, chat_id: str, message_id: str) -> bool:
    return conn.execute("SELECT 1 FROM drafts WHERE chat_id=? AND message_id=?",
                        (chat_id, message_id)).fetchone() is not None


# --- сбор черновиков ---------------------------------------------------------

def history_of(client: AvitoBusinessClient, chat_id: str, owner_id: int) -> tuple[list[dict], dict | None]:
    """История чата для движка и последнее сообщение клиента.

    Всё, что читается из чата, сразу проходит через outbound(): дальше по коду
    сырого текста клиента не существует.
    """
    raw = client.messages(chat_id, limit=MAX_HISTORY).get("messages", [])
    raw = sorted(raw, key=lambda m: m.get("created", 0))
    history, last_in = [], None
    for m in raw:
        text = (m.get("content") or {}).get("text") or ""
        if not text:
            continue                     # вложения не трогаем: в корпусе от них остаётся только тип
        mine = str(m.get("author_id")) == str(owner_id)
        # роли и поле — как ждёт движок и OpenAI-совместимый API: assistant/user + content
        history.append({"role": "assistant" if mine else "user", "content": outbound(text)})
        if not mine:
            last_in = {"id": str(m.get("id")), "text": history[-1]["content"]}
    return history, last_in


def make_draft(eng: Engine, conn: sqlite3.Connection, chat_id: str, history: list[dict], last_in: dict) -> int | None:
    """Готовит черновик и кладёт в базу. Возвращает id или None, если уже был."""
    if seen(conn, chat_id, last_in["id"]):
        return None
    answer = eng.answer(last_in["text"], history[:-1])
    cur = conn.execute(
        "INSERT INTO drafts(chat_id, message_id, incoming, draft, held, reason, created) "
        "VALUES(?,?,?,?,?,?,?)",
        (chat_id, last_in["id"], last_in["text"], answer.reply,
         int(answer.escalate or answer.failed), answer.reason or "", time.time()))
    conn.commit()
    return cur.lastrowid


def notify(draft_id: int, chat_id: str, incoming: str, draft: str, held: bool, reason: str,
           *, dry_run: bool = False) -> None:
    """Черновик специалисту. Текст уже обезличен — здесь только оформление."""
    code = client_code(chat_id)
    # Та же проверка, что на двери к модели: если в тексте осталось что-то
    # похожее на личные данные, в Telegram он не уходит — специалист откроет
    # чат в Авито сам. Иначе всё, что маскировка не увидела, уезжало бы на
    # серверы Telegram.
    if leak_scan(incoming) or leak_scan(draft):
        incoming = "(в сообщении есть данные, похожие на личные — откройте чат в Авито)"
        draft = "(черновик скрыт по той же причине; кнопка «поправить» — свой ответ)"
        held, reason = True, reason or "в тексте остались личные данные"
    head = f"{code} пишет:\n{incoming}\n\n"
    if held:
        body = (f"Помощник отвечать не стал: {reason or 'разговор передан вам'}\n\n"
                f"Заготовка:\n{draft}\n\nСвой ответ — кнопка «поправить».")
    else:
        body = f"Хочу ответить:\n{draft}" + (f"\n\n(почему так: {reason})" if reason else "")
    text = head + body
    link = f"https://www.avito.ru/profile/messenger/channel/{chat_id}"
    kb = {"inline_keyboard": [
        [{"text": "✅ отправить", "callback_data": f"send:{draft_id}"},
         {"text": "✏️ поправить", "callback_data": f"edit:{draft_id}"}],
        [{"text": "⏭ пропустить", "callback_data": f"skip:{draft_id}"},
         {"text": "🔗 открыть в Авито", "url": link}],
    ]}
    if dry_run:
        print(f"\n--- черновик #{draft_id} ({code}) ---\n{text}\n[отправка отключена: --dry-run]")
        return
    tg("sendMessage", chat_id=OWNER_CHAT, text=text[:3900], reply_markup=kb)


def collect(eng: Engine, client: AvitoBusinessClient, conn: sqlite3.Connection, *, dry_run: bool) -> int:
    """Один проход по непрочитанным чатам. Возвращает число новых черновиков."""
    owner_id = client.user_id
    made = 0
    for chat in client.chats(unread_only=True, limit=50).get("chats", []):
        chat_id = str(chat.get("id"))
        try:
            history, last_in = history_of(client, chat_id, owner_id)
        except Exception as e:                       # noqa: BLE001
            print(f"чат {client_code(chat_id)}: не прочитать — {e}", file=sys.stderr)
            continue
        if not last_in:
            continue
        draft_id = make_draft(eng, conn, chat_id, history, last_in)
        if draft_id is None:
            continue
        row = conn.execute("SELECT incoming, draft, held, reason FROM drafts WHERE id=?",
                           (draft_id,)).fetchone()
        notify(draft_id, chat_id, row[0], row[1], bool(row[2]), row[3], dry_run=dry_run)
        made += 1
    return made


# --- решения специалиста -----------------------------------------------------

def send_to_client(client: AvitoBusinessClient, conn: sqlite3.Connection, draft_id: int,
                   text: str | None = None) -> str:
    """Отправляет ответ клиенту. Мимо фильтра не проходит ничего, даже текст специалиста."""
    row = conn.execute("SELECT chat_id, draft, status FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if not row:
        return "черновик не найден"
    chat_id, draft, status = row
    if status != "wait":
        return f"уже {status}"
    reply = text if text is not None else draft
    verdict = check_outgoing(reply)
    if verdict.action == HOLD:
        # свой текст специалиста тоже проверяем: контакт или обещание в нём —
        # это нарушение правил площадки независимо от того, кто его написал
        return f"фильтр против: {explain(verdict)}. Отправка отменена."
    client.send(chat_id, reply)
    conn.execute("UPDATE drafts SET status='sent', draft=? WHERE id=?", (reply, draft_id))
    conn.commit()
    return f"отправлено {client_code(chat_id)}"


def handle_callback(client: AvitoBusinessClient, conn: sqlite3.Connection, cb: dict) -> None:
    data = cb.get("data", "")
    chat = cb.get("message", {}).get("chat", {}).get("id")
    # кнопку под черновиком может нажать только владелец: если DRAFT_OWNER_CHAT —
    # групповой чат, отправить клиенту смог бы любой его участник
    who = str((cb.get("from") or {}).get("id", ""))
    # Нажать может только владелец лично. Проверять нужно И человека, И чат:
    # если DRAFT_OWNER_CHAT оказался групповым, совпадения одного чата хватало,
    # чтобы любой участник группы отправил сообщение живому клиенту.
    if OWNER_CHAT and (who != str(OWNER_CHAT) or str(chat) != str(OWNER_CHAT)):
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Это может только владелец аккаунта")
        return
    action, _, raw_id = data.partition(":")
    if not raw_id.isdigit() or len(raw_id) > 12:
        return
    draft_id = int(raw_id)
    if action == "send":
        result = send_to_client(client, conn, draft_id)
    elif action == "skip":
        conn.execute("UPDATE drafts SET status='skipped' WHERE id=? AND status='wait'", (draft_id,))
        conn.commit()
        result = "пропущено"
    elif action == "edit":
        AWAITING_EDIT[int(who) if who.isdigit() else int(chat)] = draft_id
        result = "жду ваш текст следующим сообщением"
    else:
        return
    tg("answerCallbackQuery", callback_query_id=cb["id"], text=result[:200])
    tg("sendMessage", chat_id=chat, text=result)


def handle_message(client: AvitoBusinessClient, conn: sqlite3.Connection, msg: dict) -> None:
    """Сообщения от специалиста: сюда приходит его правка к черновику."""
    chat = msg.get("chat", {}).get("id")
    who = (msg.get("from") or {}).get("id")
    # правку принимаем только от владельца лично: DRAFT_OWNER_CHAT — личный чат,
    # и даже если туда попала группа, чужое сообщение ответом клиенту не станет
    if str(chat) != str(OWNER_CHAT) or str(who) != str(OWNER_CHAT):
        return
    draft_id = AWAITING_EDIT.pop(int(who), None)
    text = (msg.get("text") or "").strip()
    if draft_id is None or not text or text.startswith("/"):
        return
    tg("sendMessage", chat_id=chat, text=send_to_client(client, conn, draft_id, text))


# --- запуск ------------------------------------------------------------------

def main() -> int:
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv or dry_run
    if not dry_run and (not TOKEN or not OWNER_CHAT):
        print("Нужны TELEGRAM_BOT_TOKEN и DRAFT_OWNER_CHAT в .env.\n"
              "DRAFT_OWNER_CHAT — id чата специалиста с ботом: туда уходят черновики.\n"
              "Посмотреть связку без отправки: python3 src/draft_loop.py --dry-run", file=sys.stderr)
        return 2
    cid, secret = os.environ.get("AVITO_CLIENT_ID", ""), os.environ.get("AVITO_CLIENT_SECRET", "")
    if not (cid and secret):
        print("Нет ключей Авито. Что заполнено: python3 scripts/check-env.py", file=sys.stderr)
        return 2

    eng = Engine()
    client = AvitoBusinessClient(cid, secret)
    conn = db()
    print(f"движок готов, кусков {eng.index.n}. "
          f"{'проба без отправки' if dry_run else f'слежу за чатами, опрос раз в {POLL_SECONDS} с'}",
          flush=True)

    offset = 0
    while True:
        try:
            made = collect(eng, client, conn, dry_run=dry_run)
            if made:
                print(f"новых черновиков: {made}", flush=True)
        except Exception as e:                       # noqa: BLE001
            print("обход чатов:", e, file=sys.stderr)
        if once:
            return 0
        # решения специалиста забираем тем же опросом, что и тренировочный бот
        try:
            upd = tg("getUpdates", offset=offset, timeout=POLL_SECONDS)
            for u in upd.get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    handle_callback(client, conn, u["callback_query"])
                elif "message" in u:
                    handle_message(client, conn, u["message"])
        except Exception as e:                       # noqa: BLE001
            print("getUpdates:", e, file=sys.stderr)
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
