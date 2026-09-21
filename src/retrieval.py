#!/usr/bin/env python3
"""Поиск по базе знаний.

Интерфейс один, реализаций может быть несколько. Сейчас работает лексический
поиск BM25 на чистом Python: ставится везде, не требует ни видеокарты, ни
внешних служб, и на терминологичной нише (свои термины, цифры, названия услуг) даёт
приличный результат. Векторный поиск подключается той же функцией `search`,
когда на сервере появятся эмбеддинги.

Две поправки к чистому BM25, обе по делу:
- вес источника: актуальные условия важнее архивной статьи при равном совпадении;
- свежесть: из двух одинаково релевантных диалогов берём тот, что новее.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from chunking import Chunk, SOURCE_WEIGHT

K1 = 1.5      # насыщение по частоте термина
B = 0.75      # поправка на длину куска
FRESH_HALF_LIFE_DAYS = 540    # за полтора года вес свежести падает вдвое

_WORD = re.compile(r"[а-яёa-z0-9]+", re.I)

# Окончания, которые сносим, чтобы «заявку», «заявки», «заявка» совпадали.
_SUFFIXES = ("ами", "ями", "ого", "ему", "ыми", "ими", "ов", "ев", "ам", "ям",
             "ах", "ях", "ой", "ей", "ые", "ие", "ым", "им", "ую", "юю", "ая",
             "яя", "ее", "ой", "а", "я", "у", "ю", "ы", "и", "е", "о", "ь")


def normalize(word: str) -> str:
    w = word.lower().replace("ё", "е")
    if len(w) > 5:
        for suf in _SUFFIXES:
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                return w[: -len(suf)]
    return w


def tokenize(text: str) -> list[str]:
    return [normalize(m.group()) for m in _WORD.finditer(text)]


@dataclass
class Hit:
    chunk: Chunk
    score: float


class BM25Index:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.tokens: list[list[str]] = [tokenize(c.text) for c in chunks]
        self.lengths = [len(t) for t in self.tokens]
        self.avg_len = sum(self.lengths) / len(self.lengths) if self.lengths else 1.0
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, toks in enumerate(self.tokens):
            for term, tf in Counter(toks).items():
                self.postings[term].append((i, tf))
        self.n = len(chunks)

    def _idf(self, term: str) -> float:
        df = len(self.postings.get(term, ()))
        if not df:
            return 0.0
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def _freshness(self, c: Chunk) -> float:
        if not c.date:
            return 1.0
        try:
            y, m, d = (int(x) for x in c.date.split("-"))
            age = (date.today() - date(y, m, d)).days
        except ValueError:
            return 1.0
        return 0.5 ** (max(0, age) / FRESH_HALF_LIFE_DAYS) * 0.4 + 0.8

    def search(self, query: str, top_k: int = 5,
               sources: set[str] | None = None) -> list[Hit]:
        q_terms = [t for t in tokenize(query) if len(t) > 2]
        scores: dict[int, float] = defaultdict(float)
        for term in q_terms:
            idf = self._idf(term)
            if not idf:
                continue
            for i, tf in self.postings[term]:
                dl = self.lengths[i] or 1
                denom = tf + K1 * (1 - B + B * dl / self.avg_len)
                scores[i] += idf * tf * (K1 + 1) / denom

        hits: list[Hit] = []
        for i, base in scores.items():
            c = self.chunks[i]
            if sources and c.source not in sources:
                continue
            weight = SOURCE_WEIGHT.get(c.source, 1.0) * self._freshness(c)
            hits.append(Hit(c, base * weight))
        hits.sort(key=lambda h: -h.score)
        return hits[:top_k]

    def search_mixed(self, query: str, quotas: dict[str, int] | None = None) -> list[Hit]:
        """Выдача по квотам на каждый тип источника.

        Зачем не просто top-k: диалогов в базе в шесть раз больше, чем статей,
        и по чистой релевантности они вытесняют всё остальное. На вопрос
        «я самозанятый» подробная статья есть, а в выдачу попадали три обрывка
        переписки. Диалоги дают манеру, статьи дают матчасть — нужно и то и
        другое, поэтому каждому типу выделяется своя доля.

        Условия идут первыми и всегда: это единственный источник актуальных
        цифр, и без них бот начнёт повторять архивные ставки.
        """
        quotas = quotas or {"usloviya": 2, "article": 2, "dialog": 3,
                            "channel": 1, "note": 1}
        out: list[Hit] = []
        for source, n in quotas.items():
            if n > 0:
                out.extend(self.search(query, top_k=n, sources={source}))
        out.sort(key=lambda h: -h.score)
        return out

    def save(self, path: Path) -> None:
        path.write_text("\n".join(c.to_json() for c in self.chunks), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        chunks = [Chunk(**json.loads(l)) for l in
                  path.read_text(encoding="utf-8").splitlines() if l.strip()]
        return cls(chunks)


def build_index(root: Path) -> BM25Index:
    from chunking import build_all
    return BM25Index(build_all(root / "knowledge", root / "corpus"))
