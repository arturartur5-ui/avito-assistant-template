#!/usr/bin/env python3
"""Метрики фильтра: сколько ответов он задерживает и на чём.

Считает по репликам специалиста из корпуса — это ближайшее к реальным ответам,
что у нас есть. Доля «ложных» задержаний оценивается по выборке: скрипт
выводит случайные задержанные реплики, чтобы их просмотрел человек.
"""
import json, random, sys, collections
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from guard import check_outgoing, HOLD

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
sample_n = int(sys.argv[2]) if len(sys.argv) > 2 else 10

replies = []
for name in ("dialogs.jsonl", "telegram_dialogs.jsonl"):
    p = root / "corpus" / name
    if not p.exists():
        continue
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            for t in json.loads(line).get("turns", []):
                if t.get("role") == "specialist" and t.get("text", "").strip():
                    replies.append(t["text"])

held = collections.Counter()
examples = collections.defaultdict(list)
for r in replies:
    v = check_outgoing(r)
    if v.action == HOLD:
        held[v.category] += 1
        examples[v.category].append(r)

# В корпусе плейсхолдеры обезличивания стоят у каждой четвёртой реплики — это
# след чистки, а не поведение модели. Считаем отдельно, чтобы не завышать долю.
artifact = held.pop("остаток обезличивания", 0)
total_held = sum(held.values())
print(f"реплик специалиста: {len(replies)}")
print(f"задержано фильтром: {total_held} ({total_held / max(len(replies), 1) * 100:.1f}%)")
print(f"  (плюс {artifact} с плейсхолдерами обезличивания — артефакт корпуса, не считаем)")
for cat, n in held.most_common():
    print(f"  {cat:26s} {n:5d}  ({n / max(len(replies), 1) * 100:.1f}%)")
print(f"\nвыборка для ручной проверки «ложное или нет» (по {sample_n} на категорию):")
random.seed(1)
for cat, items in examples.items():
    print(f"\n--- {cat}")
    for r in random.sample(items, min(sample_n, len(items))):
        print("   ", r.replace("\n", " ")[:150])
