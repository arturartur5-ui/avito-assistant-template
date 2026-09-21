#!/usr/bin/env python3
"""Тренировочный бот в Telegram: специалист допрашивает помощника и ставит оценки.

Это этап «экзамен» из docs/zapusk.md. К площадке не подключён, клиентов не
видит, отвечает только тем, кто вошёл по паролю. Каждый ответ движка получает
три кнопки: верно / неточно / неверно. На «неточно» и «неверно» бот просит
написать, как правильно, и сохраняет правку — из них потом собирается
дообучение базы знаний.

Только стандартная библиотека: long polling через getUpdates.
Хранилище оценок — sqlite в data/exam.db: для экзамена этого достаточно.
"""
from __future__ import annotations

import json
import hashlib
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from anonymize import outbound
from engine import ROOT, Engine, _opener, load_env

load_env(ROOT / ".env")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PASSWORD = os.environ.get("EXAM_PASSWORD", "")      # пустой → бот открыт всем по ссылке
SAMPLE_PASSWORD = "смените-меня"                    # образец из .env.example, запускаться с ним нельзя
DAILY_LIMIT = int(os.environ.get("EXAM_DAILY_LIMIT", "40"))  # сообщений в день на человека, в любом режиме
FAILED_TRIES: dict[int, list[float]] = {}   # chat_id -> когда вводили неверный пароль
MAX_TRIES, TRIES_WINDOW = 5, 3600           # пять неверных за час — молчим час
GLOBAL_TRIES: list[float] = []              # неверные пароли со всех чатов: перебор с многих аккаунтов
GLOBAL_MAX = 30
SEEN: dict[int, list[float]] = {}           # все сообщения за сутки, включая отбитые: защита от потока
API = f"https://api.telegram.org/bot{TOKEN}"
DB = ROOT / "data" / "exam.db"
HISTORY: dict[int, list[dict]] = {}          # chat_id -> последние реплики
AWAITING_FIX: dict[int, int] = {}            # chat_id -> id ответа, к которому ждём правку

RATINGS = {"ok": "✅ верно", "meh": "➖ неточно", "bad": "❌ неверно"}
# алиасы маршрутов шлюза → человеческие имена (для /who и /stat)
# Понятные названия моделей для статистики: алиас на шлюзе → как показывать.
MODEL_NAMES: dict[str, str] = {}


def tg(method: str, **params) -> dict:
    data = urllib.parse.urlencode({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                                   for k, v in params.items()}).encode()
    # без прокси из окружения и без редиректов — как у клиентов шлюза и Авито;
    # ответ Telegram не бывает больше пары мегабайт, остальное не читаем
    with _opener().open(urllib.request.Request(f"{API}/{method}", data=data), timeout=40) as r:
        return json.loads(r.read(4_000_000).decode("utf-8"))


def _auth_tag() -> str:
    """Отпечаток текущего пароля: сменили пароль — старые входы недействительны."""
    return hashlib.sha256(PASSWORD.encode()).hexdigest()[:16] if PASSWORD else "open"


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    c = sqlite3.connect(DB)
    try:
        os.chmod(DB.parent, 0o700)
        os.chmod(DB, 0o600)               # правки специалиста — его методика, не для всех на сервере
    except OSError:
        pass
    c.executescript("""
    CREATE TABLE IF NOT EXISTS allowed(chat_id INTEGER PRIMARY KEY, name TEXT, since TEXT);
    CREATE TABLE IF NOT EXISTS resets(chat_id INTEGER PRIMARY KEY, ts TEXT);
    CREATE TABLE IF NOT EXISTS answers(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ts TEXT,
        question TEXT, reply TEXT, escalate INTEGER, reason TEXT, sources TEXT,
        prompt_tokens INTEGER, completion_tokens INTEGER, seconds REAL,
        rating TEXT, correction TEXT);
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(answers)")]
    for col in ("model", "recommended", "notes", "topic"):  # старые базы без колонок
        if col not in cols:
            c.execute(f"ALTER TABLE answers ADD COLUMN {col} TEXT")
    if "auth" not in [r[1] for r in c.execute("PRAGMA table_info(allowed)")]:
        c.execute("ALTER TABLE allowed ADD COLUMN auth TEXT")
    return c


def allowed(chat_id: int) -> bool:
    if not PASSWORD:
        return True                      # открытый режим: любой, у кого есть ссылка
    with db() as c:
        return c.execute("SELECT 1 FROM allowed WHERE chat_id=? AND auth=?",
                         (chat_id, _auth_tag())).fetchone() is not None


def locked_out(chat_id: int) -> bool:
    """Пять неверных паролей за час — и этот чат час не получает ответа вообще.
    Без этого пароль перебирается ботом за минуты."""
    now = time.time()
    tries = [t for t in FAILED_TRIES.get(chat_id, []) if now - t < TRIES_WINDOW]
    FAILED_TRIES[chat_id] = tries
    GLOBAL_TRIES[:] = [t for t in GLOBAL_TRIES if now - t < TRIES_WINDOW]
    return len(tries) >= MAX_TRIES or len(GLOBAL_TRIES) >= GLOBAL_MAX


def flooding(chat_id: int) -> bool:
    """Поток сообщений, включая отбитые фильтром: дневной лимит считает только
    ответы, а нагрузить бота можно и без них."""
    now = time.time()
    seen = [t for t in SEEN.get(chat_id, []) if now - t < 86400]
    seen.append(now)
    SEEN[chat_id] = seen
    return len(seen) > DAILY_LIMIT * 3


def over_limit(chat_id: int) -> bool:
    """Дневной лимит на человека — в любом режиме. С паролем тоже: пароль могут
    переслать дальше, и чужая квота модели не должна сгорать за ночь."""
    with db() as c:
        n = c.execute("SELECT count(*) FROM answers WHERE chat_id=? AND ts >= datetime('now','-1 day')", (chat_id,)).fetchone()[0]
    return n >= DAILY_LIMIT


def send(chat_id: int, text: str, answer_id: int | None = None) -> None:
    kb = None
    if answer_id is not None:
        kb = {"inline_keyboard": [
            [{"text": t, "callback_data": f"{k}:{answer_id}"} for k, t in RATINGS.items()],
            [{"text": "📊 статистика", "callback_data": f"stat:{answer_id}"},
             {"text": "🔄 сначала", "callback_data": f"reset:{answer_id}"}],
        ]}
    for i in range(0, len(text), 3900):
        tg("sendMessage", chat_id=chat_id, text=text[i:i + 3900],
           **({"reply_markup": kb} if kb and i + 3900 >= len(text) else {}))


# Правка специалиста: рекомендуемый ответ, а в квадратных скобках — что нельзя
# говорить или что критично: «Расскажите о ситуации [не называть банк] [сумму не считать]».
NOTE_RX = re.compile(r"\[([^\[\]]+)\]")


def save_feedback(chat_id: int, aid: int, text: str) -> list[str]:
    """Сохраняет правку к ответу: обезличенный текст, чистый рекомендуемый ответ и замечания из скобок."""
    text = outbound(text)                 # правка — тоже на диск, значит без персональных данных
    notes = [n.strip() for n in NOTE_RX.findall(text) if n.strip()]
    recommended = re.sub(r"\s+", " ", NOTE_RX.sub(" ", text)).strip()
    with db() as c:
        old = c.execute("SELECT correction, notes FROM answers WHERE id=? AND chat_id=?",
                        (aid, chat_id)).fetchone() or (None, None)
        correction = "\n".join(x for x in (old[0], text) if x)
        all_notes = " | ".join(x for x in (old[1], " | ".join(notes)) if x)
        c.execute("UPDATE answers SET correction=?, recommended=?, notes=? WHERE id=? AND chat_id=?",
                  (correction, recommended or None, all_notes or None, aid, chat_id))
    return notes


def feedback_reply(aid: int, notes: list[str]) -> str:
    msg = f"Записал правку к ответу №{aid}"
    if notes:
        msg += "\nЗамечания в скобках (" + str(len(notes)) + "): " + "; ".join(notes)
    return msg


def load_history(chat_id: int) -> list[dict]:
    """История диалога из базы ответов (после перезапуска бота), с момента последнего «сначала»."""
    with db() as c:
        row = c.execute("SELECT ts FROM resets WHERE chat_id=?", (chat_id,)).fetchone()
        rows = c.execute("SELECT question, reply FROM answers WHERE chat_id=? AND ts > ? "
                         "ORDER BY id DESC LIMIT 25",
                         (chat_id, row[0] if row else "")).fetchall()
    hist: list[dict] = []
    for q, r in reversed(rows):
        hist += [{"role": "user", "content": q}, {"role": "assistant", "content": r}]
    return hist


def forget(chat_id: int) -> None:
    """Удаляет всё, что хранится об участнике: строки со его chat_id из всех таблиц.

    Это и есть «удалить мои данные» для этапа экзамена: бот хранит только
    chat_id, обезличенные вопросы, ответы и правки. По таблицам идём по списку
    из базы, чтобы новая таблица не осталась незамеченной.
    """
    HISTORY.pop(chat_id, None)
    AWAITING_FIX.pop(chat_id, None)
    with db() as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({t})")]
            if "chat_id" in cols:
                c.execute(f"DELETE FROM {t} WHERE chat_id=?", (chat_id,))


def mark_reset(chat_id: int) -> None:
    """«Начать сначала»: бот забывает историю, и это переживает перезапуск."""
    HISTORY.pop(chat_id, None); AWAITING_FIX.pop(chat_id, None)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO resets VALUES(?, datetime('now'))", (chat_id,))


# Темы экзамена — из настройки ниши (config/niche.py → TOPICS): по ним
# считается покрытие и решается, какую тему отдать помощнику без подтверждения.
try:
    from config import niche as _niche
    TOPICS: list[tuple[str, "re.Pattern[str]"]] = list(getattr(_niche, "TOPICS", []) or [])
except Exception:
    TOPICS = []


def topic_of(question: str, reply: str = "") -> str:
    """Тема вопроса — по ключевым словам. Нужна для покрытия тем и автономии."""
    for name, rx in TOPICS:
        if rx.search(question or ""):
            return name
    return "прочее"


def stats_text() -> str:
    """Оценки по моделям по всем участникам экзамена."""
    with db() as c:
        rows = c.execute("SELECT coalesce(model, '?'), rating, count(*) FROM answers "
                         "GROUP BY 1, 2 ORDER BY 1").fetchall()
        topics = c.execute("SELECT coalesce(topic, 'прочее'), sum(rating='ok'), "
                           "sum(rating='meh'), sum(rating='bad'), count(*) "
                           "FROM answers GROUP BY 1 ORDER BY 5 DESC").fetchall()
    by_model: dict[str, list[str]] = {}
    for m, r, n in rows:
        by_model.setdefault(m, []).append(f"{RATINGS.get(r, 'без оценки')} — {n}")
    lines = [f"{MODEL_NAMES.get(m, m)}: " + ", ".join(v) for m, v in by_model.items()]
    if topics:
        lines.append("\nПо темам (верно / неточно / неверно / всего):")
        lines += [f"  {t}: {ok or 0} / {meh or 0} / {bad or 0} / {n}" for t, ok, meh, bad, n in topics]
    return "\n".join(lines) or "Оценок пока нет"


def who_text(chat_id: int, aid: int | None) -> str:
    """aid задан — один ответ подробно; иначе последние 10 ответов этого чата."""
    with db() as c:
        if aid is not None:
            row = c.execute("SELECT id, model, rating, seconds, question, reply, correction FROM answers "
                            "WHERE id=? AND chat_id=?", (aid, chat_id)).fetchone()
            if not row:
                return "Нет такого ответа в вашем диалоге."
            aid, m, r, sec, q, rp, corr = row
            return (f"№{aid} — {MODEL_NAMES.get(m, m or '?')}, {RATINGS.get(r, 'без оценки')}, {sec or 0:.1f} с\n\n"
                    f"Вопрос: {q[:300]}\n\nОтвет: {rp[:600]}" + (f"\n\nПравка: {corr[:400]}" if corr else ""))
        rows = c.execute("SELECT id, model, rating, seconds, question FROM answers WHERE chat_id=? "
                         "ORDER BY id DESC LIMIT 10", (chat_id,)).fetchall()
    return "\n".join(f"№{aid} · {MODEL_NAMES.get(m, m or '?')} · {RATINGS.get(r, 'без оценки')} · "
                     f"{sec or 0:.0f} с · «{q[:50]}»" for aid, m, r, sec, q in rows) or "Ответов пока нет"


def handle_message(eng: Engine, msg: dict) -> None:
    chat_id = msg["chat"]["id"]
    if msg["chat"].get("type", "private") != "private":
        return                            # в группе chat_id общий: один ввёл пароль — вошли бы все
    text = (msg.get("text") or "").strip()
    if flooding(chat_id):
        return

    if text.startswith("/start"):
        if locked_out(chat_id):
            return                        # перебор пароля: молчим, пока не пройдёт час
        pw = text.split(maxsplit=1)[1] if " " in text else ""
        if not PASSWORD or pw == PASSWORD:
            FAILED_TRIES.pop(chat_id, None)
            with db() as c:
                # имя из Telegram на диск не пишем: chat_id достаточно
                c.execute("INSERT OR REPLACE INTO allowed(chat_id, name, since, auth) "
                          "VALUES(?,?,datetime('now'),?)", (chat_id, "", _auth_tag()))
            # сообщение с паролем стираем из чата: иначе он лежит в истории Telegram
            # у каждого, кому переслали, — и у того, кто заглянет через плечо
            try:
                tg("deleteMessage", chat_id=chat_id, message_id=msg.get("message_id"))
            except Exception:                 # noqa: BLE001 — не вышло удалить, значит просто остался
                pass
            # id чата нужен для этапа 2 (DRAFT_OWNER_CHAT) — показываем сразу,
            # иначе его негде взять: в базу экзамена человек не полезет
            print(f"вошёл чат {chat_id} — это значение для DRAFT_OWNER_CHAT в .env", flush=True)
            send(chat_id, "Доступ открыт. Пишите как клиент с Авито — я отвечу как помощник.\n"
                          "Под каждым ответом кнопки: верно / неточно / неверно. На «неточно» и "
                          "«неверно» напишите, как правильно; что нельзя говорить или что критично — в квадратных "
                          "скобках: [подрядчика не называть].\n\n/reset — начать диалог заново\n/stat — оценки\n"
                          "/id — id этого чата, понадобится для этапа 2\n"
                          "/забыть — удалить всё, что бот хранит о вас")
        else:
            FAILED_TRIES.setdefault(chat_id, []).append(time.time())
            GLOBAL_TRIES.append(time.time())
            send(chat_id, "Это тренировочный бот. Введите: /start ПАРОЛЬ")
        return
    if not allowed(chat_id):
        send(chat_id, "Введите: /start ПАРОЛЬ")
        return
    if over_limit(chat_id):
        send(chat_id, "На сегодня лимит вопросов исчерпан, продолжим завтра")
        return
    if text == "/reset":
        mark_reset(chat_id)
        send(chat_id, "Диалог сброшен. Новый клиент, новая история.")
        return
    if text == "/stat":
        send(chat_id, stats_text()); return
    if text in ("/id", "/айди"):
        send(chat_id, f"id этого чата: {chat_id}\n\nЭто значение для DRAFT_OWNER_CHAT в .env — "
                      "туда будут приходить черновики ответов на этапе 2.")
        return
    if text in ("/забыть", "/forget"):
        forget(chat_id)
        send(chat_id, "Стёрто: доступ, история, оценки и правки этого чата. Вернуться — снова /start ПАРОЛЬ.")
        return

    # /who — снять маску: какая модель отвечала. /who — последние 10, /who 42 — конкретный ответ
    if text.startswith("/who"):
        arg = text.split(maxsplit=1)[1].strip("№# ") if " " in text else ""
        send(chat_id, who_text(chat_id, int(arg) if arg.isdigit() else None)); return

    # правка к предыдущему ответу
    if chat_id in AWAITING_FIX and text:
        aid = AWAITING_FIX.pop(chat_id)
        send(chat_id, feedback_reply(aid, save_feedback(chat_id, aid, text)))
        return

    # сообщение со скобками [...] вне режима правки — тоже замечание, к последнему ответу
    if NOTE_RX.search(text) and not text.startswith("/"):
        with db() as c:
            row = c.execute("SELECT id FROM answers WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        if row:
            send(chat_id, feedback_reply(row[0], save_feedback(chat_id, row[0], text)))
            return

    # вложения: по правилам это передача специалисту
    if any(k in msg for k in ("document", "photo", "voice", "video")):
        kind = {"document": "документ", "photo": "фото", "voice": "голосовое сообщение",
                "video": "видео"}[next(k for k in ("document", "photo", "voice", "video") if k in msg)]
        # Файл в историю не попадает, только факт: модель должна знать, что он
        # уже у специалиста, и не просить прислать заново.
        hist = HISTORY.get(chat_id)
        if hist is None:
            hist = HISTORY[chat_id] = load_history(chat_id)
        hist += [{"role": "user", "content": f"[клиент прислал вложение: {kind}]"},
                 {"role": "assistant", "content": "Вижу вложение, посмотрю и вернусь к вам чуть позже."}]
        send(chat_id, "⚠️ Клиент прислал файл — здесь бот замолкает и передаёт разговор специалисту. "
                      "Сам файл бот не открывает.")
        return
    if not text:
        return

    hist = HISTORY.get(chat_id)
    if hist is None:
        hist = HISTORY[chat_id] = load_history(chat_id)
    try:
        model = random.choice(eng.models)   # слепое сравнение: кто ответил, рейтер не видит
        a = eng.answer(text, hist, model)
    except Exception as e:                            # noqa: BLE001
        # Текст исключения — в журнал на сервере, в чат только факт сбоя.
        print(f"сбой движка: {e!r}", file=sys.stderr)
        send(chat_id, "⚠️ Не получилось ответить — ошибка в движке, подробности в журнале на сервере.")
        return
    if a.failed:
        # Шлюз не ответил: клиенту бы не ушло ничего, разговор — специалисту.
        send(chat_id, "⚠️ Шлюз не ответил, помощник промолчал.\n" + a.reason +
                      "\n\nВ бою клиент ничего бы не получил, а разговор ушёл бы специалисту.")
        return
    hist += [{"role": "user", "content": text}, {"role": "assistant", "content": a.reply}]
    del hist[:-50]   # диалог помнится целиком; в модель уходит только окно

    with db() as c:
        cur = c.execute(
            "INSERT INTO answers(chat_id,ts,question,reply,escalate,reason,sources,prompt_tokens,"
            "completion_tokens,seconds,model,topic) VALUES(?,datetime('now'),?,?,?,?,?,?,?,?,?,?)",
            # На диск кладём обезличенный текст: правило проекта — сырых
            # персональных данных на диске не держим.
            (chat_id, outbound(text), outbound(a.reply), int(a.escalate), a.reason,
             ", ".join(a.sources[:5]), a.usage.get("prompt_tokens"),
             a.usage.get("completion_tokens"), a.seconds, a.model, topic_of(text, a.reply)))
        aid = cur.lastrowid

    out = a.reply
    if a.escalate:
        out += f"\n\n⚠️ здесь бот передал бы разговор специалисту ({a.reason})"
    send(chat_id, out + f"\n\n№{aid}", answer_id=aid)   # номер — чтобы спросить /who 42


def handle_callback(cb: dict) -> None:
    chat_id = cb["message"]["chat"]["id"]
    try:
        key, aid = cb["data"].split(":", 1)
        aid = int(aid)
    except (ValueError, KeyError):
        return
    # Кнопки проверяются так же, как сообщения: чужой чат, группа или отозванный
    # доступ — молча выходим.
    if cb["message"]["chat"].get("type", "private") != "private" or not allowed(chat_id) \
            or key not in ("who", "stat", "reset", *RATINGS):
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        return
    if key == "who":
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        send(chat_id, who_text(chat_id, aid)); return
    if key == "stat":
        tg("answerCallbackQuery", callback_query_id=cb["id"])
        send(chat_id, "По всем участникам:\n" + stats_text()); return
    if key == "reset":   # то же, что /reset: бот забывает историю, дальше — как новый клиент
        mark_reset(chat_id)
        tg("answerCallbackQuery", callback_query_id=cb["id"], text="Диалог начат заново")
        send(chat_id, "Начинаем заново — пишите как новый клиент."); return
    with db() as c:
        # оценку можно поставить только своему ответу
        c.execute("UPDATE answers SET rating=? WHERE id=? AND chat_id=?", (key, aid, chat_id))
    tg("answerCallbackQuery", callback_query_id=cb["id"], text=RATINGS[key])
    if key in ("meh", "bad"):
        AWAITING_FIX[chat_id] = aid
        send(chat_id, "Напишите одним сообщением, как правильно ответить. Что нельзя говорить или что "
                      "критично — в квадратных скобках, например: [подрядчика не называть].")


def main() -> int:
    if not TOKEN:
        print("Нужен TELEGRAM_BOT_TOKEN в .env", file=sys.stderr)
        return 2
    if PASSWORD == SAMPLE_PASSWORD:
        print("EXAM_PASSWORD остался образцовым. Это значение лежит в открытом\n"
              "репозитории: по нему в вашего бота зайдёт любой, кто его найдёт —\n"
              "увидит ответы специалиста и израсходует вашу квоту модели.\n"
              "Поставьте свой пароль в .env и запустите заново.", file=sys.stderr)
        return 2
    if PASSWORD:
        print(f"режим: по паролю, лимит {DAILY_LIMIT} сообщений в день на человека", flush=True)
    elif os.environ.get("EXAM_OPEN") != "1":
        print("EXAM_PASSWORD пуст. Открытый режим — только осознанно: поставьте EXAM_OPEN=1\n"
              "в .env, если действительно хотите пускать всех по ссылке.", file=sys.stderr)
        return 2
    else:
        print(f"ВНИМАНИЕ: бот открыт всем, у кого есть ссылка (лимит {DAILY_LIMIT} сообщений в день\n"
              "на человека). Посторонние увидят ответы специалиста и израсходуют квоту модели.\n"
              "Закрыть: EXAM_PASSWORD в .env", flush=True)
    eng = Engine()
    print(f"движок готов, кусков {eng.index.n}; жду сообщения", flush=True)
    offset = 0
    while True:
        try:
            upd = tg("getUpdates", offset=offset, timeout=30)
        except Exception as e:
            print("getUpdates:", e, file=sys.stderr); time.sleep(5); continue
        for u in upd.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "message" in u:
                    handle_message(eng, u["message"])
                elif "callback_query" in u:
                    handle_callback(u["callback_query"])
            except Exception as e:
                print("ошибка обработки:", e, file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
