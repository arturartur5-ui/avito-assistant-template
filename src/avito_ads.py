#!/usr/bin/env python3
"""Клиент публичного API Авито Рекламы (Avito Ads).

Авторизация: OAuth2 client_credentials -> Bearer-токен.
База API:    https://api.avito.ru/ads/v1/
Ключи:       кабинет Авито Рекламы -> Настройки аккаунта -> API (нужна роль Администратор).

Зависимостей нет, только стандартная библиотека Python 3.9+.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import ssl
import hashlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TOKEN_URL = "https://api.avito.ru/token"
API_BASE = "https://api.avito.ru/ads/v1"
USER_AGENT = "avito-ads-client/1.0"
TOKEN_CACHE = Path(__file__).with_name(".token_cache_ads.json")   # у каждого клиента свой


def _write_private(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Файл с секретом создаётся сразу с правами 0600.

    write_text() + chmod() оставляют окно, когда файл уже лежит с правами по
    umask (обычно 0644), а для уже существующего файла старые права
    сохраняются до chmod. Здесь права задаются в момент создания.
    """
    if path.exists():
        os.chmod(path, 0o600)             # старый файл с другими правами — закрыть до записи
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding=encoding) as fh:
        fh.write(text)


def _fingerprint(token: str) -> str:
    """Необратимый отпечаток токена для вывода на экран: сам токен не показываем."""
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()[:12]


# Переменные, которые .env задавать не должен: через них можно увести трафик
# на прокси или подменить доверенные сертификаты, не трогая код.
_ENV_FORBIDDEN = {"PATH", "HOME", "SHELL"}
_ENV_FORBIDDEN_PREFIX = ("PYTHON", "LD_", "DYLD_", "SSL_")


def _env_forbidden(key: str) -> bool:
    up = key.upper()
    return key in _ENV_FORBIDDEN or up.startswith(_ENV_FORBIDDEN_PREFIX) or up.endswith("PROXY")

# Заголовки, в которых Авито отдаёт остаток баллов / лимиты. Точные имена
# в докам не проверены, поэтому логируем всё, что похоже.
QUOTA_HEADER_HINTS = ("ratelimit", "rate-limit", "x-limit", "points", "quota", "retry-after")


class NetworkError(RuntimeError):
    pass


# Python с python.org носит свой набор корней и не видит сертификаты из
# системной связки — из-за этого TLS падает за корпоративным прокси, хотя curl
# работает. Пробуем системный bundle как запасной вариант.
SYSTEM_CA_FALLBACK = "/etc/ssl/cert.pem"
_ca_fallback_active = False


def _ssl_context() -> ssl.SSLContext:
    """Системные корневые сертификаты; свой CA — через AVITO_ADS_CA_BUNDLE."""
    ca = os.environ.get("AVITO_ADS_CA_BUNDLE")
    if not ca and _ca_fallback_active:
        ca = SYSTEM_CA_FALLBACK
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


class AvitoError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} {url}\n{body[:2000]}")


def load_env(path: str | None = None) -> None:
    """Читает .env: по умолчанию из корня проекта (рядом с .env.example),
    для старых установок — ещё и из src/. Уже заданные переменные окружения
    не перебивает."""
    if path is None:
        candidates = [Path(__file__).resolve().parent.parent / ".env", Path(__file__).with_name(".env")]
    else:
        p = Path(path)
        candidates = [p if p.is_absolute() else Path(__file__).with_name(path)]
    for p in candidates:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            if _env_forbidden(key):
                print(f"[.env] {key} пропущена: такие переменные из файла не берутся", file=sys.stderr)
                continue
            # Вставка в терминал часто тащит с собой escape-последовательности от
            # стрелок и невидимые управляющие символы — вычищаем их.
            val = "".join(ch for ch in val if ch.isprintable())
            os.environ.setdefault(key, val.strip().strip('"').strip("'").strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Перенаправления не выполняем: urllib повторил бы запрос вместе с
    заголовком Authorization уже на чужой адрес. API Авито не перенаправляет."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NetworkError(f"Сервер перенаправляет на {newurl} — не следуем, чтобы не отдать токен")


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                       urllib.request.HTTPSHandler(context=_ssl_context()))


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            global _ca_fallback_active
            if (
                not _ca_fallback_active
                and not os.environ.get("AVITO_ADS_CA_BUNDLE")
                and Path(SYSTEM_CA_FALLBACK).exists()
            ):
                _ca_fallback_active = True
                return _request(method, url, headers=headers, body=body, timeout=timeout)
            raise NetworkError(
                f"Не удалось проверить TLS-сертификат {urllib.parse.urlparse(url).netloc}: {reason}.\n"
                "Обычно это MITM-прокси или корпоративный антивирус. Варианты:\n"
                "  - указать корневой сертификат: AVITO_ADS_CA_BUNDLE=/путь/до/ca.pem\n"
                "  - на macOS с Python c python.org выполнить один раз:\n"
                "    '/Applications/Python 3.13/Install Certificates.command'"
            ) from None
        raise NetworkError(f"Сеть недоступна ({urllib.parse.urlparse(url).netloc}): {reason}") from None


def _log_quota(headers: dict[str, str], verbose: bool) -> None:
    if not verbose:
        return
    hits = {k: v for k, v in headers.items() if any(h in k for h in QUOTA_HEADER_HINTS)}
    if hits:
        print(f"  [лимиты] {hits}", file=sys.stderr)


class AvitoAdsClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        account_id: str | None = None,
        verbose: bool = False,
        use_cache: bool = True,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_id = account_id
        self.verbose = verbose
        self.use_cache = use_cache
        self._token: str | None = None
        self._token_exp: float = 0.0

    # ---------- авторизация ----------

    def _load_cached_token(self) -> None:
        if not (self.use_cache and TOKEN_CACHE.exists()):
            return
        try:
            data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("client_id") == self.client_id and data.get("expires_at", 0) > time.time() + 60:
            self._token = data.get("access_token")
            self._token_exp = data["expires_at"]

    def _save_cached_token(self) -> None:
        if not self.use_cache:
            return
        try:
            _write_private(TOKEN_CACHE, 
                json.dumps(
                    {
                        "client_id": self.client_id,
                        "access_token": self._token,
                        "expires_at": self._token_exp,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def token(self, force: bool = False) -> str:
        if not force:
            if self._token and self._token_exp > time.time() + 60:
                return self._token
            self._load_cached_token()
            if self._token and self._token_exp > time.time() + 60:
                return self._token

        payload = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode()
        status, headers, raw = _request(
            "POST",
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=payload,
        )
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except ValueError:
            raise AvitoError(status, text, TOKEN_URL) from None

        # Авито отдаёт ошибку авторизации с кодом 200 и полем "error".
        if "access_token" not in data:
            raise AvitoError(status, text, TOKEN_URL)

        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600))
        self._save_cached_token()
        if self.verbose:
            ttl = int(self._token_exp - time.time())
            print(f"  [auth] токен получен, живёт ~{ttl} c", file=sys.stderr)
        return self._token

    # ---------- транспорт ----------

    def call(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: Any = None,
        max_retries: int = 4,
    ) -> Any:
        if path.startswith("http"):
            # Полный адрес — только сам API: иначе Bearer-токен уедет на чужой хост.
            if not path.startswith("https://api.avito.ru/"):
                raise ValueError("call(): полный адрес принимается только для https://api.avito.ru/")
            url = path
        else:
            url = f"{API_BASE}/{path.lstrip('/')}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean)

        body = json.dumps(json_body).encode() if json_body is not None else None

        for attempt in range(max_retries + 1):
            headers = {
                "Authorization": f"Bearer {self.token()}",
                "Accept": "application/json",
            }
            if body is not None:
                headers["Content-Type"] = "application/json"

            if self.verbose:
                print(f"  [http] {method} {url}", file=sys.stderr)
            status, resp_headers, raw = _request(method, url, headers=headers, body=body)
            _log_quota(resp_headers, self.verbose)

            # Токен протух — обновляем и повторяем один раз.
            if status == 401 and attempt < max_retries:
                self.token(force=True)
                continue

            # 429 — кончились баллы или упёрлись в частоту. 5xx — временное.
            if (status == 429 or status >= 500) and attempt < max_retries:
                wait = float(resp_headers.get("retry-after") or 0) or min(2**attempt, 30)
                if self.verbose:
                    print(f"  [retry] HTTP {status}, пауза {wait:.0f} c", file=sys.stderr)
                time.sleep(wait)
                continue

            text = raw.decode("utf-8", "replace")
            if status >= 400:
                raise AvitoError(status, text, url)
            if not text.strip():
                return None
            try:
                return json.loads(text)
            except ValueError:
                return text

        raise AvitoError(status, raw.decode("utf-8", "replace"), url)

    def get(self, path: str, **query: Any) -> Any:
        return self.call("GET", path, query=query)

    def post(self, path: str, json_body: Any = None, **query: Any) -> Any:
        return self.call("POST", path, query=query, json_body=json_body)

    # ---------- прикладные методы ----------
    # Пути собраны по публично известному образцу
    #   GET https://api.avito.ru/ads/v1/account/{account_id}/campaigns
    # Точные схемы — в техдоке developers.avito.ru/api-catalog/ads/documentation.
    # Если какой-то путь не совпал, используйте `raw` / `discover`.

    def _acc(self, account_id: str | None = None) -> str:
        acc = account_id or self.account_id
        if not acc:
            raise SystemExit(
                "Не задан account_id. Укажите --account-id или AVITO_ADS_ACCOUNT_ID в .env"
            )
        return str(acc)

    def account(self, account_id: str | None = None) -> Any:
        return self.get(f"account/{self._acc(account_id)}")

    # Ниже — только те методы, которые проверены живыми запросами.
    # Списки идут POST-ом: GET на них отвечает 405.

    def balance(self, account_id: str | None = None) -> Any:
        return self.get(f"account/{self._acc(account_id)}/balance")

    def users(self, account_id: str | None = None) -> Any:
        return self.get(f"account/{self._acc(account_id)}/users")

    def children(self, account_id: str | None = None) -> Any:
        """Дочерние аккаунты на том же договоре (агентский сценарий)."""
        return self.get(f"account/{self._acc(account_id)}/children")

    def campaigns(self, account_id: str | None = None, **filters: Any) -> Any:
        return self.call(
            "POST", f"account/{self._acc(account_id)}/campaigns", json_body=filters or {}
        )

    def groups(self, account_id: str | None = None, **filters: Any) -> Any:
        """Группы объявлений: ставка, бюджет, расписание, таргетинг."""
        return self.call(
            "POST", f"account/{self._acc(account_id)}/groups", json_body=filters or {}
        )

    def creatives(self, account_id: str | None = None, **filters: Any) -> Any:
        return self.call(
            "POST", f"account/{self._acc(account_id)}/creatives", json_body=filters or {}
        )


CANDIDATE_PATHS = [
    "account/{acc}",
    "account/{acc}/balance",
    "account/{acc}/campaigns",
    "account/{acc}/adgroups",
    "account/{acc}/ad_groups",
    "account/{acc}/creatives",
    "account/{acc}/statistics",
    "account/{acc}/stats",
    "account/{acc}/users",
    "account/{acc}/subaccounts",
    "account/{acc}/sub_accounts",
    "accounts",
]


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"   # общий .env проекта


def _update_env(path: Path, values: dict[str, str]) -> None:
    """Обновляет только свои ключи, остальное содержимое .env сохраняет."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in values:
            lines[i] = f"{key}={values[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    _write_private(path, "\n".join(lines) + "\n")


def cmd_setup() -> int:
    """Интерактивно спрашивает ключи и сам записывает их в .env."""
    print("\nНастройка доступа к API Авито Рекламы")
    print("=" * 42)
    print("Где взять ключи: кабинет Авито Рекламы -> Настройки аккаунта ->")
    print("раздел API -> создать ключ (нужна роль Администратор).\n")
    print(f"Значения будут записаны в файл:\n  {ENV_PATH}\n")

    if ENV_PATH.exists():
        ans = input("Файл .env уже есть. Обновить в нём ключи Авито Рекламы, остальное не трогая? [y/N]: ").strip().lower()
        if ans not in ("y", "yes", "д", "да"):
            print("Отменено, ничего не тронул.")
            return 0

    client_id = input("1) ID (он же Client ID / Client Key) — вставьте и Enter: ").strip()
    if not client_id:
        print("Пусто. Отменено.")
        return 1

    print("\n2) Секрет (Client Secret). При вставке на экране НИЧЕГО не появится — это")
    print("   нормально, так и задумано. Просто вставьте (Cmd+V) и нажмите Enter.")
    client_secret = getpass.getpass("Секрет: ").strip()
    if not client_secret:
        print("Пусто. Отменено.")
        return 1

    account_id = input("\n3) ID рекламного аккаунта — можно пропустить, просто Enter: ").strip()

    _update_env(ENV_PATH, {"AVITO_ADS_CLIENT_ID": client_id,
                           "AVITO_ADS_CLIENT_SECRET": client_secret,
                           "AVITO_ADS_ACCOUNT_ID": account_id})
    print(f"\nЗаписал в {ENV_PATH.name} (доступ только вам).")

    print("Проверяю ключи...")
    client = AvitoAdsClient(client_id, client_secret, account_id=account_id or None)
    try:
        client.token(force=True)
    except AvitoError as e:
        print("\nКлючи не подошли. Ответ Авито:")
        print(f"  {e.body[:300]}")
        print("\nПроверьте, что скопировали Client Key и Client Secret целиком,")
        print("без пробелов по краям, и запустите setup ещё раз.")
        return 1
    except NetworkError as e:
        print(f"\nНе достучался до Авито: {e}")
        return 1

    ttl = int(client._token_exp - time.time())
    print(f"Готово. Токен получен, живёт ~{ttl // 3600} ч.")
    print("\nДальше можно:")
    print("  python3 avito_ads.py account     # данные аккаунта и баланс")
    print("  python3 avito_ads.py campaigns   # список кампаний")
    print("  python3 avito_ads.py discover    # что вообще доступно")
    return 0


def cmd_discover(client: AvitoAdsClient, args: argparse.Namespace) -> int:
    acc = client._acc(args.account_id)
    print(f"Прощупываю пути под account_id={acc}\n")
    for tpl in CANDIDATE_PATHS:
        path = tpl.format(acc=acc)
        try:
            data = client.call("GET", path, max_retries=1)
            preview = json.dumps(data, ensure_ascii=False)[:160]
            print(f"  OK    /{path}\n        {preview}")
        except AvitoError as e:
            print(f"  {e.status:<5} /{path}  {e.body[:120]}")
        time.sleep(0.4)
    return 0


def cmd_summary(client: AvitoAdsClient) -> int:
    acc = client.account()["account"]
    bal = client.balance()
    camps = client.campaigns().get("campaigns", [])
    groups = client.groups().get("groups", [])
    creatives = client.creatives().get("creatives", [])
    kids = client.children().get("children", [])
    users = client.users()

    print(f"Аккаунт : {acc.get('shortName', '—')}  (ИНН {acc.get('inn', '—')})")
    print(f"Контакт : {acc.get('contact', {}).get('name', '—')}")
    print(f"Баланс  : {bal.get('balance', 0):,} ₽, бонусы {bal.get('bonusBalance', 0):,}".replace(",", " "))
    print(f"Юзеров  : {users.get('total', 0)}   дочерних аккаунтов: {len(kids)}")
    print(f"Объекты : {len(camps)} кампаний, {len(groups)} групп, {len(creatives)} креативов")

    by_status: dict[str, int] = {}
    for c in camps:
        by_status[c.get("status", "?")] = by_status.get(c.get("status", "?"), 0) + 1
    print("Статусы : " + ", ".join(f"{k} — {v}" for k, v in sorted(by_status.items())))

    print(f"\n{'ID':<12} {'статус':<10} {'модель':<5} {'бюджет':>9}  название")
    for c in sorted(camps, key=lambda x: x.get("createdAt", ""), reverse=True):
        budget = f"{c.get('budget', 0):,}".replace(",", " ")
        print(f"{c['id']:<12} {c.get('status', ''):<10} {c.get('paymentModel', ''):<5} "
              f"{budget:>9}  {c.get('name', '')[:38]}")
    return 0


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    load_env()

    parser = argparse.ArgumentParser(
        prog="avito_ads.py", description="Клиент публичного API Авито Рекламы"
    )
    parser.add_argument("--account-id", default=os.environ.get("AVITO_ADS_ACCOUNT_ID"))
    parser.add_argument("-v", "--verbose", action="store_true", help="логировать запросы и лимиты")
    parser.add_argument("--no-cache", action="store_true", help="не кешировать токен на диск")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="интерактивно ввести ключи и записать .env")
    sub.add_parser("token", help="проверить ключи и получить токен")
    p_acc = sub.add_parser("account", help="информация об аккаунте")
    p_acc.add_argument("--account-id", dest="account_id_override")
    for name, helptext in [
        ("campaigns", "список кампаний"),
        ("groups", "группы объявлений (ставки, бюджеты, расписание)"),
        ("creatives", "креативы"),
        ("balance", "баланс: рубли и бонусы"),
        ("users", "пользователи аккаунта и их роли"),
        ("children", "дочерние аккаунты"),
        ("summary", "сводка по аккаунту одной командой"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--account-id", dest="account_id_override")

    p_get = sub.add_parser("get", help="произвольный GET по пути из документации")
    p_get.add_argument("path", help="например account/123/campaigns")
    p_get.add_argument("--query", action="append", default=[], metavar="K=V")

    p_post = sub.add_parser("post", help="произвольный POST")
    p_post.add_argument("path")
    p_post.add_argument("--data", default=None, help="JSON-тело запроса")
    p_post.add_argument("--query", action="append", default=[], metavar="K=V")

    p_disc = sub.add_parser("discover", help="прощупать вероятные эндпоинты")
    p_disc.add_argument("--account-id", dest="account_id_override")

    args = parser.parse_args(argv)

    if args.cmd == "setup":
        try:
            return cmd_setup()
        except (KeyboardInterrupt, EOFError):
            print("\nОтменено.")
            return 130

    client_id = os.environ.get("AVITO_ADS_CLIENT_ID")
    client_secret = os.environ.get("AVITO_ADS_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "Ключи ещё не настроены. Запустите:\n\n"
            "    python3 avito_ads.py setup\n\n"
            "Он сам спросит Client Key и Client Secret и всё запишет.",
            file=sys.stderr,
        )
        return 2

    account_id = getattr(args, "account_id_override", None) or args.account_id
    client = AvitoAdsClient(
        client_id,
        client_secret,
        account_id=account_id,
        verbose=args.verbose,
        use_cache=not args.no_cache,
    )

    try:
        if args.cmd == "token":
            tok = client.token(force=True)
            ttl = int(client._token_exp - time.time())
            print(f"OK. Токен получен, отпечаток {_fingerprint(tok)} (живёт ~{ttl} c)")
            return 0
        if args.cmd == "account":
            _print(client.account())
            return 0
        if args.cmd in ("campaigns", "groups", "creatives", "balance", "users", "children"):
            _print(getattr(client, args.cmd)())
            return 0
        if args.cmd == "summary":
            return cmd_summary(client)
        if args.cmd == "get":
            q = dict(kv.split("=", 1) for kv in args.query)
            _print(client.get(args.path, **q))
            return 0
        if args.cmd == "post":
            q = dict(kv.split("=", 1) for kv in args.query)
            payload = json.loads(args.data) if args.data else None
            _print(client.post(args.path, json_body=payload, **q))
            return 0
        if args.cmd == "discover":
            return cmd_discover(client, args)
    except AvitoError as e:
        print(f"Ошибка API: {e}", file=sys.stderr)
        return 1
    except NetworkError as e:
        print(f"Ошибка сети: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
