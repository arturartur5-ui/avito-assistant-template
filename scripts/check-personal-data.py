#!/usr/bin/env python3
"""Проверка перед коммитом: нет ли в изменениях личных данных и секретов.

Ставится как pre-commit hook и не пускает коммит, если в проиндексированных
файлах нашлись имя клиента, правдоподобный телефон, почта, паспорт, карта,
ключ API или токен.

Зачем это вообще. Шаблон проекта собирался из рабочей копии, и данные клиента
трижды просачивались в него через комментарии, примеры в коде и названия
исключаемых чатов. Ручная проверка каждый раз что-то пропускала: искали имя
с заглавной буквы, а в коде оно было строчными. Машина надёжнее памяти.

Имена клиента живут в `.personal-terms` (по одному в строке) — файл в
.gitignore и сам в репозиторий не попадает. Остальное ловится по форме.

Что важно знать про поведение:
  - проверяется содержимое ИЗ ИНДЕКСА (`git show :файл`), а не рабочая копия:
    иначе можно подготовить к коммиту секрет, а рабочий файл затереть чистым;
  - имена файлов проверяются тоже — «Ярослава_Примерова_паспорт.txt» это находка;
  - файлы ключей (.pem, .key, .p12 …) под версионным контролем — находка
    сами по себе, что бы в них ни лежало;
  - секреты ищутся везде, маркеры `personal-check: off/on` их не выключают,
    а незакрытый маркер — ошибка, а не молчаливое отключение проверки.

Запуск вручную:  python3 scripts/check-personal-data.py
Проверить всё:   python3 scripts/check-personal-data.py --all
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / ".personal-terms"

# Бинарное и служебное не проверяем.
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".woff",
               ".woff2", ".ttf", ".ico", ".mp4", ".pyc", ".lock"}
# Документы, изображения, архивы и записи: текстовым аудитом их не проверить
# (внутри может быть скан паспорта или переписка), поэтому в репозиторий они
# не попадают вовсе. Шрифты и иконки — не документы, их это не касается.
DOCUMENT_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic",
                   ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
                   ".zip", ".gz", ".rar", ".7z", ".tar", ".mp4", ".mov", ".mp3", ".ogg", ".wav"}
# Файлы ключей: под версионным контролем им не место, содержимое не важно.
KEY_SUFFIX = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".ppk", ".asc"}
SKIP_PARTS = {".git", "node_modules", "__pycache__"}

_SEP = r"[\s.\-/()‑‒–—−]"

# Проверки по форме — работают без всякого списка имён.
SHAPE_RULES: list[tuple[str, str]] = [
    (rf"(?:\+7|\b[78]){_SEP}*\d{{3}}{_SEP}*\d{{3}}{_SEP}*\d{{2}}{_SEP}*\d{{2}}", "телефон"),
    (r"\b[78]\d{10}\b", "телефон слитно"),
    (r"\b9\d{9}\b", "телефон без кода"),
    (rf"\b9\d{{2}}{_SEP}+\d{{3}}{_SEP}*\d{{2}}{_SEP}*\d{{2}}\b", "телефон с разделителями"),
    (r"[\w.+-]+@[\w-]+\.[\w.-]+", "почта"),
    (r"\b\d{4}[\s.\-№:]{0,3}\d{6}\b", "паспорт"),
    (r"\b\d{3}[\s.\-]\d{3}[\s.\-]\d{3}[\s.\-]\d{2}\b", "СНИЛС"),
    (r"\b(?:\d{4}[\s.\-]?){3}\d{4}\b", "карта"),
]
COMPILED = [(re.compile(p), name) for p, name in SHAPE_RULES]

# Формальные секреты. Ключ, попавший в git, живёт там навсегда, а боты
# находят его в открытом репозитории за минуты — ловим на коммите.
# Имя переменной проверяется по окончанию: AVITO_CLIENT_SECRET, LLM_API_KEY,
# TELEGRAM_BOT_TOKEN — граница слова после подчёркивания не работает, поэтому
# никакого \b перед словом.
SECRETS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY"), "приватный ключ"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "токен телеграм-бота"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "ключ Anthropic"),
    (re.compile(r"\bsk-(?:proj-|or-|live-|test-)?[A-Za-z0-9_-]{20,}"), "ключ вида sk-…"),
    (re.compile(r"\b(?:pk|sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"), "ключ Stripe"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"), "токен GitHub"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "токен GitLab"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "ключ AWS"),
    (re.compile(r"AIza[0-9A-Za-z_-]{30,}"), "ключ Google"),
    (re.compile(r"\bya29\.[A-Za-z0-9_-]{30,}|\by[0-3]_[A-Za-z0-9_-]{40,}|\bt1\.[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}"),
     "токен Яндекса"),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), "токен Hugging Face"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "токен Slack"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT"),
    (re.compile(r"(?i)(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s'\"]+:[^\s'\"]+@"),
     "строка подключения с паролем"),
    (re.compile(r"(?i)(?<![a-z0-9])[a-z0-9_]*(?:api[_-]?key|client[_-]?secret|access[_-]?token|secret[_-]?key|"
                r"auth[_-]?token|bot[_-]?token|private[_-]?key|password|passwd|secret|token|key|credential\w*)"
                r"[ \t]*[\"']?[ \t]*[:=][ \t]*[\"']?([A-Za-z0-9_\-./+=:@!%$#*&]{16,})"), "ключ в паре имя=значение"),
]
# Значение вида self.client_secret или os.environ — это код, а не секрет.
_CODE_VALUE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z_]")

# Что не считается находкой: образцы из нулей и зарезервированные тестовые домены.
SAFE_EMAIL = re.compile(r"@(?:example\.(?:com|net|org|ru)|test\.(?:com|ru)|localhost)$|noreply@anthropic\.com$")


class MarkerError(Exception):
    pass


def is_placeholder(value: str) -> bool:
    """Образец из нулей или из одной повторяющейся цифры — это не данные.
    Номер из двух разных цифр — уже может быть настоящим."""
    digits = re.sub(r"\D", "", value)
    if not digits:
        return False
    return (digits.count("0") >= len(digits) - 2 or len(set(digits)) == 1
            or digits in ("1234567890", "0123456789"))


def load_terms() -> list[str]:
    if not TERMS_FILE.exists():
        return []
    return [line.strip().lower() for line in
            TERMS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]


def _git(*args: str, binary: bool = False):
    # core.quotePath=false: иначе кириллические имена печатаются октальными
    # escape-последовательностями, и такой файл молча выпадает из проверки
    r = subprocess.run(["git", "-c", "core.quotePath=false", *args], capture_output=True, cwd=ROOT)
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def staged() -> list[tuple[str, bytes]]:
    """(путь, содержимое из индекса) — то, что реально уйдёт в коммит."""
    out = []
    for rel in _git("diff", "--cached", "--name-only", "--diff-filter=ACM", "-z").split("\0"):
        if rel:
            out.append((rel, _git("show", f":{rel}", binary=True)))
    return out


def all_tracked() -> list[tuple[str, bytes]]:
    out = []
    for rel in _git("ls-files", "-z").split("\0"):
        p = ROOT / rel
        if rel and p.is_file():
            out.append((rel, p.read_bytes()))
    return out


def decode_any(data: bytes) -> str:
    """UTF-8, UTF-16 с BOM, иначе latin-1: двоичное тоже читаем — ради ключей."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def interesting(rel: str) -> bool:
    p = Path(rel)
    if p.suffix.lower() in SKIP_SUFFIX:
        return False
    return not any(part in SKIP_PARTS for part in p.parts)


# Осознанные исключения. Между маркерами проверка по именам и формам не
# работает — так помечают места, где совпадение по форме неизбежно и
# безопасно: например словарь распространённых имён. Секреты маркеры не
# выключают. Незакрытый или слишком длинный блок — ошибка.
# Маркер — только целой строкой-комментарием: упоминание в тексте или в
# строковом литерале маркером не считается.
MARKER = re.compile(r"^\s*(?:#|//|<!--|;|--)\s*personal-check:\s*(off|on)\b")
MAX_IGNORED_LINES = 120
# Маркер в первых пяти строках файла с тестами выключает проверку по именам до
# конца файла: тестовые данные выдуманы по определению, а без распространённых
# имён тесты обезличивания ничего не проверяют. На секреты это не влияет —
# они ищутся по сырому тексту всегда.
WHOLE_FILE_MARKER_LINES = 20


def strip_ignored(text: str, rel: str = "") -> str:
    """Вырезает блоки между маркерами, сохраняя нумерацию строк."""
    lines = text.splitlines()
    if rel.startswith("tests/") or "/tests/" in rel:
        head = lines[:WHOLE_FILE_MARKER_LINES]
        if any(MARKER.match(l) and MARKER.match(l).group(1) == "off" for l in head):
            return ""
    out, skipping, opened, length = [], False, 0, 0
    for i, line in enumerate(lines, 1):
        marker = MARKER.match(line)
        if marker and marker.group(1) == "off":
            skipping, opened, length = True, i, 0
        elif marker:
            skipping = False
        elif skipping:
            length += 1
            if length > MAX_IGNORED_LINES:
                raise MarkerError(f"блок «personal-check: off» со строки {opened} длиннее {MAX_IGNORED_LINES} строк")
        out.append("" if skipping else line)
    if skipping:
        raise MarkerError(f"маркер «personal-check: off» на строке {opened} не закрыт")
    return "\n".join(out)


def scan_secrets(text: str, where: str) -> list[str]:
    """Секреты — по сырому тексту, исключения-маркеры на них не действуют."""
    found: list[str] = []
    for rx, name in SECRETS:
        for m in rx.finditer(text):
            value = m.group(1) if m.groups() else ""
            if _CODE_VALUE.match(value or "") or (value.isdigit() and is_placeholder(value)):
                continue
            found.append(f"{where}:{text[:m.start()].count(chr(10)) + 1}  {name}")
    return found


def scan_text(text: str, terms: list[str], where: str) -> list[str]:
    """Находки в одном тексте. Само значение не печатаем — только что и где."""
    found = scan_secrets(text, where)
    try:
        text = strip_ignored(text, where)
    except MarkerError as e:
        return found + [f"{where}  {e}"]
    lower = text.lower()
    for term in terms:
        # термин с границы слова: иначе «ира» ловится в «квартире»
        for m in re.finditer(r"(?<![0-9A-Za-zА-Яа-яЁё])" + re.escape(term), lower):
            found.append(f"{where}:{text[:m.start()].count(chr(10)) + 1}  личное имя или метка «{term}»")
            break
    for rx, name in COMPILED:
        for m in rx.finditer(text):
            value = m.group()
            if is_placeholder(value):
                continue
            if name == "почта" and SAFE_EMAIL.search(value):
                continue
            found.append(f"{where}:{text[:m.start()].count(chr(10)) + 1}  {name}")
    return found


def scan_file(rel: str, data: bytes, terms: list[str]) -> list[str]:
    """Файл целиком: имя и содержимое. Картинки, архивы и служебное — только на
    секреты: телефон по форме в них не ищем, а ключ ищем всё равно."""
    found = scan_path(rel, terms)
    if any(part in SKIP_PARTS for part in Path(rel).parts):
        return found
    text = decode_any(data)
    if Path(rel).suffix.lower() in SKIP_SUFFIX:
        return found + scan_secrets(text, rel)
    return found + scan_text(text, terms, rel)


def scan_path(rel: str, terms: list[str]) -> list[str]:
    """Имя файла — тоже текст: имя клиента или телефон в названии."""
    found = []
    if Path(rel).suffix.lower() in KEY_SUFFIX:
        found.append(f"{rel}  файл ключа под версионным контролем")
    if Path(rel).suffix.lower() in DOCUMENT_SUFFIX:
        found.append(f"{rel}  документ, изображение или архив: проверить содержимое нельзя — "
                     "в репозиторий не кладём")
    lower = rel.lower()
    for term in terms:
        if re.search(r"(?<![0-9A-Za-zА-Яа-яЁё])" + re.escape(term), lower):
            found.append(f"{rel}  в имени файла личное имя или метка «{term}»")
    for rx, name in COMPILED:
        m = rx.search(rel)
        if m and not is_placeholder(m.group()):
            found.append(f"{rel}  в имени файла {name}")
    return found


def check(items: list[tuple[str, bytes]], terms: list[str]) -> list[str]:
    problems: list[str] = []
    for rel, data in items:
        problems += scan_file(rel, data, terms)
    return problems


def main() -> int:
    terms = load_terms()
    items = all_tracked() if "--all" in sys.argv else staged()
    if "--all" in sys.argv and not items:
        print("Проверять пока нечего: ни один файл не добавлен в git (git add …). "
              "Зелёный свет на пустом репозитории ничего не значит.", file=sys.stderr)
        return 1
    problems = list(dict.fromkeys(check(items, terms)))

    if not problems:
        scope = "во всех файлах" if "--all" in sys.argv else "в изменениях"
        if terms:
            extra = ""
        else:
            why = ("файла .personal-terms нет" if not TERMS_FILE.exists()
                   else "файл .personal-terms есть, но в нём одни комментарии")
            extra = (f"\n  ВНИМАНИЕ: {why} — имя и фамилия специалиста\n"
                     "  НЕ проверяются, только телефоны, почты, документы и ключи.\n"
                     "  Включить полную проверку: вписать в .personal-terms имя во всех падежах,\n"
                     "  домен и id аккаунтов — кириллицей и латиницей, по одному в строке.\n"
                     "  Образец полей — в .personal-terms.example.")
        print(f"Личных данных и секретов {scope} не найдено.{extra}")
        return 0

    print("КОММИТ ОСТАНОВЛЕН: похоже на личные данные или секреты\n", file=sys.stderr)
    for line in problems:
        print(f"  {line}", file=sys.stderr)
    print("\nЧто делать:", file=sys.stderr)
    print("  - имя специалиста в коде и настройках не пишется вообще: оно живёт только в .env", file=sys.stderr)
    print("    (OWNER_NAME_FORMS) и в .personal-terms — уберите его из файла;", file=sys.stderr)
    print("  - настоящие данные заменить образцами из нулей;", file=sys.stderr)
    print("  - клиентское вынести в .env или .personal-terms;", file=sys.stderr)
    print("  - настоящий ключ, даже удалённый из файла, считать утёкшим и перевыпустить;", file=sys.stderr)
    print("  - если это ложная тревога — git commit --no-verify", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
