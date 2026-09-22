#!/usr/bin/env python3
"""Нарезка базы знаний на куски для поиска.

Куски разного происхождения ведут себя по-разному, поэтому у каждого есть
метка источника и дата. Это нужно не для красоты: цифры в статьях и диалогах
архивные, а актуальны только те, что в usloviya.md. Поиск обязан различать.

Размер куска 200-500 слов с перехлёстом — практика для русского RAG: меньше
теряется контекст, больше начинает шуметь.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from anonymize import outbound
from guard import check_outgoing

# Категории, которым нельзя учить модель. Остаток обезличивания («[ИМЯ], …»)
# и неуверенность сюда не входят: в корпусе плейсхолдеры стоят у каждой
# четвёртой реплики, это след чистки, а не нарушение правил площадки.
DROPPED: dict[str, int] = {}     # окна диалогов, не попавшие в индекс, по причинам
TEACHES_VIOLATION = {"нелегальное", "внутренняя кухня", "контакты / увод с Авито",
                     "обещание результата", "цена услуг"}
from typing import Iterator

WORDS_PER_CHUNK = 320
OVERLAP_WORDS = 50

# Приоритет источника при равной релевантности: чем больше, тем важнее.
SOURCE_WEIGHT = {
    "usloviya": 3.0,   # единственный источник актуальных цифр
    "dialog": 1.4,     # как специалист разговаривает
    "channel": 1.1,    # его собственные посты
    "note": 1.0,       # личные заметки
    "article": 0.9,    # статьи: полно, но длинно и местами устарело
}


@dataclass
class Chunk:
    id: str
    text: str
    source: str          # usloviya | dialog | channel | note | article
    origin: str          # имя файла или id диалога
    title: str = ""
    date: str = ""       # YYYY-MM-DD, если известна

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def _split_words(text: str, size: int, overlap: int) -> Iterator[str]:
    words = text.split()
    if not words:
        return
    step = max(1, size - overlap)
    for start in range(0, len(words), step):
        piece = words[start:start + size]
        if len(piece) < 30 and start:      # хвост короче 30 слов приклеен к предыдущему
            break
        yield " ".join(piece)


# Мусор, который не должен попасть в контекст модели: служебные комментарии,
# MDX-компоненты из статей, ссылки-картинки, разметка фронтматтера.
NOISE_PATTERNS = [
    re.compile(r"<!--.*?-->", re.S),                 # <!-- источник: … -->
    re.compile(r"</?[A-Z][A-Za-z]*[^>]*>"),          # <AnyComponent>, <Note …>
    re.compile(r"\A\s*---\s*\n.*?\n---\s*\n", re.S),  # фронтматтер целиком
    re.compile(r"!\[[^\]]*\]\([^)]*\)"),            # картинки
    # Разделитель таблицы: только внутри строки. \s захватывал перевод строки
    # и склеивал следующий заголовок с таблицей — из-за этого разделы после
    # таблиц («Программы», «Стоимость услуг») переставали быть
    # разделами и попадали в соседний кусок.
    re.compile(r"\|[ \t:-]+\|[ \t:|-]*"),              # разделители таблиц
]


def strip_noise(text: str) -> str:
    # Порядок важен: комментарий в начале файла сдвигает фронтматтер,
    # поэтому сначала убираем комментарии, потом фронтматтер с любым отступом.
    text = NOISE_PATTERNS[0].sub(" ", text)
    text = re.sub(r"\A\s*---\s*\n.*?\n---\s*\n", " ", text, flags=re.S)
    for rx in NOISE_PATTERNS[1:]:
        text = rx.sub(" ", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def chunk_markdown(path: Path, source: str, skip_heads: tuple[str, ...] = ()) -> list[Chunk]:
    """Режет .md по заголовкам второго уровня, длинные разделы — дополнительно.

    skip_heads — разделы, которые в индекс не попадают вовсе.
    """
    text = strip_noise(path.read_text(encoding="utf-8", errors="replace"))
    raw = path.read_text(encoding="utf-8", errors="replace")
    title_m = re.search(r'^title:\s*"?(.+?)"?\s*$', raw, re.M) or re.search(r"^#\s+(.+)$", raw, re.M)
    title = title_m.group(1).strip() if title_m else path.stem

    # Дата из шапки-комментария или из заголовка раздела вида ## 2026-09-11
    date_m = re.search(r"обновлён (\d{4}-\d{2}-\d{2})", raw)
    file_date = date_m.group(1) if date_m else ""

    out: list[Chunk] = []
    sections = re.split(r"^##\s+", text, flags=re.M)
    for si, sec in enumerate(sections):
        sec = sec.strip()
        if len(sec) < 60:
            continue
        head = sec.splitlines()[0].strip()
        if any(head.startswith(h) for h in skip_heads):
            continue
        sec_date = head if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head) else file_date
        for ci, piece in enumerate(_split_words(sec, WORDS_PER_CHUNK, OVERLAP_WORDS)):
            out.append(Chunk(
                id=f"{path.stem}:{si}:{ci}",
                text=piece,
                source=source,
                origin=path.name,
                title=title,
                date=sec_date,
            ))
    return out


def chunk_dialogs(path: Path, max_turns: int = 6) -> list[Chunk]:
    """Диалог режем окнами реплик: пара «вопрос - ответ» без контекста бесполезна."""
    out: list[Chunk] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        turns = [t for t in d["turns"] if t["role"] in ("client", "specialist")]
        for i in range(0, len(turns), max_turns - 2 or 1):
            window = turns[i:i + max_turns]
            if len(window) < 2:
                continue
            if not any(t["role"] == "specialist" for t in window):
                continue
            # Корпус — образец манеры, но часть реплик специалиста нарушает
            # правила площадки (названия банков, контакты, уводы). Учить на них
            # нельзя: модель повторит, а фильтр будет задерживать ответ за
            # ответом. Такие окна в индекс не попадают.
            bad = [check_outgoing(t["text"]).category for t in window if t["role"] == "specialist"]
            bad = [c for c in bad if c in TEACHES_VIOLATION]
            if bad:
                DROPPED[bad[0]] = DROPPED.get(bad[0], 0) + 1
                continue
            body = "\n".join(
                ("Клиент: " if t["role"] == "client" else "Специалист: ") + t["text"]
                for t in window
            )
            ts = next((t.get("ts") for t in window if t.get("ts")), None)
            date = ""
            if ts:
                import datetime as dt
                try:
                    date = dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
                except (TypeError, ValueError, OSError):
                    # «ts» — число секунд от начала эпохи, а не строка «2025-06-15».
                    # Дата не критична: кусок годится и без неё.
                    date = ""
            out.append(Chunk(
                id=f"{d['dialog_id']}:{i}",
                text=body,
                source="dialog",
                origin=d["dialog_id"],
                title=d.get("item", ""),
                date=date,
            ))
    return out


def _source_ok(knowledge: Path, corpus: Path, chunk: Chunk) -> bool:
    from indexing_rules import is_allowed
    for base in (knowledge, knowledge / "articles", corpus):
        p = base / chunk.origin
        if p.exists():
            return is_allowed(p)[0]
    return True                       # origin — не путь (например, id диалога)


# Текстовые форматы, которые кладут в базу знаний чаще всего. Прайс,
# выгруженный из таблицы, и регламент из блокнота — такой же материал, как
# статья: индексатор их разрешает (indexing_rules.ALLOWED_EXT), а читались
# раньше только .md, и файл молча не попадал в индекс.
PLAIN_EXT = (".txt", ".csv", ".tsv")


def chunk_plain(path: Path, source: str) -> list[Chunk]:
    """Простой текст или таблица: режем окнами, заголовков тут нет.

    CSV не разбираем по колонкам — для поиска важны сами слова и цифры
    строки, а не структура таблицы.
    """
    text = strip_noise(path.read_text(encoding="utf-8", errors="replace"))
    out: list[Chunk] = []
    for i, part in enumerate(_split_words(text, WORDS_PER_CHUNK, OVERLAP_WORDS)):
        part = part.strip()
        if len(part) >= 60:
            out.append(Chunk(id=f"{path.stem}:{i}", text=part, source=source,
                             origin=path.name, title=path.stem, date=""))
    return out


def _plain_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.glob("*") if p.suffix.lower() in PLAIN_EXT and not p.name.startswith("_"))


def build_all(knowledge: Path, corpus: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    usl = knowledge / "usloviya.md"
    if usl.exists():
        # Раздел цен в индекс не идёт: иначе поиск достанет его на вопрос
        # «сколько стоят услуги» раньше, чем собрана картина по клиенту.
        # Цены движок берёт отдельно и только на нужной стадии.
        chunks += chunk_markdown(usl, "usloviya", skip_heads=("Стоимость услуг",))
    articles = knowledge / "articles"
    if articles.exists():
        for p in sorted(articles.glob("*.md")):
            if p.name.startswith("_"):
                continue
            chunks += chunk_markdown(p, "article")
        for p in _plain_files(articles):
            chunks += chunk_plain(p, "article")
    for p in sorted(knowledge.glob("kanal-*.md")):
        chunks += chunk_markdown(p, "channel")
    # knowledge/internal/ — кухня специалиста: поставщики, стратегии, что не
    # раскрывать клиенту. В индекс не попадает никогда — glob("*.md") без
    # рекурсии этот каталог и так не видит, но пусть будет сказано явно.
    for p in sorted(knowledge.glob("*.md")):
        if p.name in ("usloviya.md",) or p.name.startswith("kanal-") or p.name.endswith(".template.md"):
            continue
        chunks += chunk_markdown(p, "note")
    for p in _plain_files(knowledge):
        chunks += chunk_plain(p, "note")
    for name in ("dialogs.jsonl", "telegram_dialogs.jsonl"):
        p = corpus / name
        if p.exists() and not p.is_symlink():
            chunks += chunk_dialogs(p)
    # Одна дверь для файлов: символические ссылки и служебное — мимо, даже если
    # попали в knowledge/ (правила в indexing_rules.py).
    chunks = [c for c in chunks if _source_ok(knowledge, corpus, c)]
    # Обезличивание на сборке индекса: в самом индексе персональных данных
    # уже нет, поэтому их неоткуда взять ни одному потребителю — ни движку,
    # ни отладочной выдаче. Движок чистит куски ещё раз перед отправкой.
    for c in chunks:
        c.text = outbound(c.text)
        c.title = outbound(c.title)
    return chunks


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    cs = build_all(root / "knowledge", root / "corpus")
    from collections import Counter
    print(f"кусков: {len(cs)}")
    by_source = Counter(c.source for c in cs)
    for s in ("usloviya", "article", "channel", "note", "dialog"):
        if s in by_source or s == "dialog":
            print(f"  {s}: {by_source.get(s, 0)}")
    if DROPPED:
        total_dropped = sum(DROPPED.values())
        print(f"  выброшено окон диалогов: {total_dropped} — "
              + ", ".join(f"{k} {v}" for k, v in sorted(DROPPED.items(), key=lambda kv: -kv[1])))
        if total_dropped > by_source.get("dialog", 0):
            print("  ВНИМАНИЕ: выброшено больше половины диалогов. Так и задумано, если в переписке "
                  "специалист часто давал контакты или обещал результат: на этом модель не учат.")
    if cs:
        avg = sum(len(c.text.split()) for c in cs) / len(cs)
        print(f"  средняя длина: {avg:.0f} слов")
