"""Связка этапа 2: что уходит специалисту и что уходит клиенту.

Сети здесь нет — Авито и Telegram подставные. Проверяется то, из-за чего
этап 2 вообще существует: клиенту ничего не уходит без решения человека,
а в служебный канал не попадают персональные данные клиента.
"""
# personal-check: off — файл с тестами: все имена, номера и адреса здесь выдуманы
import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("OWNER_NAME_FORMS", "Ярополк,Ярополк,Примеров")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:1/v1")

import draft_loop  # noqa: E402


class FakeAvito:
    """Подставной клиент Авито: помнит, что у него просили отправить."""

    user_id = 42

    def __init__(self, messages=None):
        self._messages = messages or []
        self.sent: list[tuple[str, str]] = []

    def messages(self, chat_id, *, limit=50, offset=0):
        return {"messages": self._messages}

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return {"ok": True}


def memory_db() -> sqlite3.Connection:
    """Та же схема, что у draft_loop.db(), но в памяти."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE drafts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, message_id TEXT NOT NULL,
        incoming TEXT NOT NULL, draft TEXT NOT NULL, held INTEGER NOT NULL, reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'wait', created REAL NOT NULL, UNIQUE(chat_id, message_id))""")
    return conn


def add_draft(conn, draft: str, chat_id: str = "u2i-1", held: int = 0) -> int:
    cur = conn.execute("INSERT INTO drafts(chat_id, message_id, incoming, draft, held, reason, created)"
                       " VALUES(?,?,?,?,?,?,0)", (chat_id, "m1", "вопрос клиента", draft, held, ""))
    conn.commit()
    return cur.lastrowid


class ClientCode(unittest.TestCase):
    def test_stable_and_short(self):
        self.assertEqual(draft_loop.client_code("u2i-123"), draft_loop.client_code("u2i-123"))
        self.assertNotEqual(draft_loop.client_code("u2i-123"), draft_loop.client_code("u2i-124"))
        self.assertRegex(draft_loop.client_code("u2i-123"), r"^К-\d{3}$")

    def test_no_lookup_table_on_disk(self):
        # код считается из chat_id на лету: таблиц соответствий проект не заводит
        self.assertNotIn("mapping", draft_loop.__file__)
        source = Path(draft_loop.__file__).read_text(encoding="utf-8")
        self.assertNotIn("INSERT INTO codes", source)


class Sending(unittest.TestCase):
    def test_nothing_goes_out_by_itself(self):
        """Черновик лежит в базе и ждёт: пока кнопку не нажали, клиенту тихо."""
        conn = memory_db()
        avito = FakeAvito()
        add_draft(conn, "Здравствуйте! Расскажите, что у вас за задача?")
        self.assertEqual(avito.sent, [])

    def test_send_goes_through_guard(self):
        conn = memory_db()
        avito = FakeAvito()
        # черновик с контактом: фильтр обязан остановить, даже если кнопку нажали
        bad = add_draft(conn, "Напишите мне в вотсап, телефон 8 900 000 00 00")
        result = draft_loop.send_to_client(avito, conn, bad)
        self.assertEqual(avito.sent, [], "контакт ушёл клиенту")
        self.assertIn("фильтр против", result)
        self.assertEqual(conn.execute("SELECT status FROM drafts WHERE id=?", (bad,)).fetchone()[0], "wait")

    def test_specialist_own_text_also_checked(self):
        """Свой текст специалиста — тоже через фильтр: правила площадки одни для всех."""
        conn = memory_db()
        avito = FakeAvito()
        did = add_draft(conn, "заготовка")
        result = draft_loop.send_to_client(avito, conn, did, "Мой вотсап 8 900 000 00 00, пишите")
        self.assertEqual(avito.sent, [])
        self.assertIn("фильтр против", result)

    def test_clean_reply_is_sent_once(self):
        conn = memory_db()
        avito = FakeAvito()
        did = add_draft(conn, "Здравствуйте! Расскажите подробнее, что нужно сделать?")
        first = draft_loop.send_to_client(avito, conn, did)
        self.assertIn("отправлено", first)
        self.assertEqual(len(avito.sent), 1)
        # вторая кнопка по тому же черновику не должна слать клиенту дубль
        second = draft_loop.send_to_client(avito, conn, did)
        self.assertEqual(len(avito.sent), 1, "клиент получил ответ дважды")
        self.assertIn("уже sent", second)

    def test_unknown_draft_is_safe(self):
        conn = memory_db()
        avito = FakeAvito()
        self.assertIn("не найден", draft_loop.send_to_client(avito, conn, 999))
        self.assertEqual(avito.sent, [])


class HistoryFitsTheEngine(unittest.TestCase):
    """Связка отдаёт историю движку — форма должна совпадать, иначе черновиков нет.

    Из-за расхождения («text»/«client» вместо «content»/«user») ни один чат с
    предыдущими репликами не получал черновика: исключение гасилось в main().
    """

    def test_engine_accepts_history_from_loop(self):
        avito = FakeAvito([
            {"id": "m1", "author_id": 7, "created": 1, "content": {"text": "здравствуйте"}},
            {"id": "m2", "author_id": 42, "created": 2, "content": {"text": "добрый день"}},
            {"id": "m3", "author_id": 7, "created": 3, "content": {"text": "а сроки какие?"}},
        ])
        history, last_in = draft_loop.history_of(avito, "u2i-1", 42)
        self.assertEqual({h["role"] for h in history}, {"user", "assistant"})
        for h in history:
            self.assertIn("content", h, "движок читает content, а не text")
        # ровно то, что делает движок с историей на входе
        import engine
        said = engine._client_said(last_in["text"], history[:-1])
        self.assertIn("здравствуйте", " ".join(said))


class NoPersonalDataInNotifications(unittest.TestCase):
    def test_history_is_anonymized(self):
        """Всё, что прочитано из чата, обезличено сразу: сырого текста дальше нет."""
        avito = FakeAvito([
            {"id": "m1", "author_id": 7, "created": 1,
             "content": {"text": "Меня зовут Мария Петрова, мой телефон 8 900 000 00 00"}},
            {"id": "m2", "author_id": 42, "created": 2, "content": {"text": "Здравствуйте!"}},
        ])
        history, last_in = draft_loop.history_of(avito, "u2i-1", 42)
        everything = " ".join(h["content"] for h in history)
        for leak in ("Мария", "Петрова", "900 000 00 00"):
            self.assertNotIn(leak, everything, f"в историю попало: {leak}")
        self.assertEqual(history[-1]["role"], "assistant")
        self.assertIsNotNone(last_in)

    def test_attachments_are_not_read(self):
        """Вложения не трогаем: у них нет текста, и скачивать их проект не умеет."""
        avito = FakeAvito([{"id": "m1", "author_id": 7, "created": 1,
                            "content": {"image": {"sizes": {}}}}])
        history, last_in = draft_loop.history_of(avito, "u2i-1", 42)
        self.assertEqual(history, [])
        self.assertIsNone(last_in)

    def test_notification_shows_code_not_name(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            draft_loop.notify(1, "u2i-777", "нужна консультация", "Расскажите подробнее?",
                              False, "", dry_run=True)
        text = out.getvalue()
        self.assertIn(draft_loop.client_code("u2i-777"), text)
        self.assertNotIn("u2i-777", text)    # специалист видит код, а не идентификатор чата


if __name__ == "__main__":
    unittest.main()
