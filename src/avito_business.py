#!/usr/bin/env python3
"""Клиент Avito Business API — обычный аккаунт Авито.

Мессенджер, объявления, профиль. Это НЕ рекламный кабинет: у него свои ключи,
которые берутся в кабинете разработчика developers.avito.ru.

Транспорт такой же, как у клиента Авито Рекламы: OAuth2 client_credentials,
кеш токена, ретраи на 429/5xx, запасной набор корневых сертификатов.
"""
from __future__ import annotations

import argparse
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
API_BASE = "https://api.avito.ru"
USER_AGENT = "avito-business-client/1.0"
TOKEN_CACHE = Path(__file__).with_name(".token_cache_business.json")   # у каждого клиента свой


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
SYSTEM_CA_FALLBACK = "/etc/ssl/cert.pem"
_ca_fallback_active = False


class NetworkError(RuntimeError):
    pass


class AvitoError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        self.status, self.body, self.url = status, body, url
        super().__init__(f"HTTP {status} {url}\n{body[:2000]}")


def _ssl_context() -> ssl.SSLContext:
    ca = os.environ.get("AVITO_CA_BUNDLE")
    if not ca and _ca_fallback_active:
        ca = SYSTEM_CA_FALLBACK
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


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


def _request(method, url, *, headers=None, body=None, timeout=30):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with _opener().open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            global _ca_fallback_active
            if not _ca_fallback_active and not os.environ.get("AVITO_CA_BUNDLE") \
                    and Path(SYSTEM_CA_FALLBACK).exists():
                _ca_fallback_active = True
                return _request(method, url, headers=headers, body=body, timeout=timeout)
            raise NetworkError(f"TLS не проверяется: {reason}. Укажите AVITO_CA_BUNDLE.") from None
        raise NetworkError(f"Сеть недоступна: {reason}") from None


class AvitoBusinessClient:
    def __init__(self, client_id, client_secret, *, verbose=False, use_cache=True):
        self.client_id, self.client_secret = client_id, client_secret
        self.verbose, self.use_cache = verbose, use_cache
        self._token: str | None = None
        self._exp = 0.0
        self._user_id: int | None = None

    def token(self, force: bool = False) -> str:
        if not force and self._token and self._exp > time.time() + 60:
            return self._token
        if not force and self.use_cache and TOKEN_CACHE.exists():
            try:
                d = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
                if d.get("client_id") == self.client_id and d.get("expires_at", 0) > time.time() + 60:
                    self._token, self._exp = d["access_token"], d["expires_at"]
                    return self._token
            except (OSError, ValueError, KeyError):
                pass

        payload = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode()
        status, _, raw = _request("POST", TOKEN_URL, body=payload,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except ValueError:
            raise AvitoError(status, text, TOKEN_URL) from None
        if "access_token" not in data:                # Авито отдаёт ошибку с HTTP 200
            if data.get("error") in ("unauthorized_client", "invalid_client", "invalid_grant"):
                raise NetworkError("Авито не принял ключи. Проверьте, что тариф с доступом к API "
                                   "оплачен и что значения скопированы кнопкой, а не набраны руками "
                                   "(в шрифте Авито 1, l и I неразличимы).")
            raise AvitoError(status, text, TOKEN_URL)

        self._token = data["access_token"]
        self._exp = time.time() + int(data.get("expires_in", 3600))
        if self.use_cache:
            try:
                _write_private(TOKEN_CACHE, json.dumps(
                    {"client_id": self.client_id, "access_token": self._token,
                     "expires_at": self._exp}), encoding="utf-8")
            except OSError:
                pass
        return self._token

    def call(self, method, path, *, query=None, json_body=None, max_retries=4):
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
            headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if self.verbose:
                print(f"  [http] {method} {url}", file=sys.stderr)
            status, resp_headers, raw = _request(method, url, headers=headers, body=body)

            if status == 401 and attempt < max_retries:
                self.token(force=True)
                continue
            if (status == 429 or status >= 500) and attempt < max_retries:
                wait = float(resp_headers.get("retry-after") or 0) or min(2 ** attempt, 30)
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

    # ---------- прикладное ----------

    def whoami(self) -> Any:
        return self.call("GET", "core/v1/accounts/self")

    @property
    def user_id(self) -> int:
        if self._user_id is None:
            env = os.environ.get("AVITO_USER_ID")
            self._user_id = int(env) if env else int(self.whoami()["id"])
        return self._user_id

    MAX_OFFSET = 1000  # дальше Авито отвечает 400

    def chats(self, *, unread_only=False, limit=50, offset=0, chat_types="u2i,u2u") -> Any:
        if offset > self.MAX_OFFSET:
            raise ValueError(
                f"Авито не отдаёт чаты с offset > {self.MAX_OFFSET}. "
                "Архив глубже тысячи чатов так не выгрузить — нужен обход по объявлениям "
                "или вебхуки на новые сообщения."
            )
        return self.call("GET", f"messenger/v2/accounts/{self.user_id}/chats", query={
            "unread_only": str(bool(unread_only)).lower(),
            "limit": limit, "offset": offset, "chat_types": chat_types,
        })

    def messages(self, chat_id: str, *, limit=50, offset=0) -> Any:
        return self.call("GET", f"messenger/v3/accounts/{self.user_id}/chats/{chat_id}/messages/",
                         query={"limit": limit, "offset": offset})

    def send(self, chat_id: str, text: str) -> Any:
        """Отправка сообщения живому человеку. Вызывать только по явной команде."""
        return self.call("POST", f"messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages",
                         json_body={"message": {"text": text}, "type": "text"})

    def mark_read(self, chat_id: str) -> Any:
        return self.call("POST", f"messenger/v1/accounts/{self.user_id}/chats/{chat_id}/read")

    def items(self, *, per_page=25, page=1, status="active") -> Any:
        return self.call("GET", "core/v1/items", query={
            "per_page": per_page, "page": page, "status": status})


def _print(d: Any) -> None:
    print(json.dumps(d, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    load_env()
    ap = argparse.ArgumentParser(prog="avito_business.py",
                                 description="Клиент обычного Авито (Business API)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("token", help="проверить ключи")
    sub.add_parser("whoami", help="профиль: id, имя, телефон, ссылка")
    sub.add_parser("items", help="объявления")
    p_ch = sub.add_parser("chats", help="список чатов")
    p_ch.add_argument("--unread", action="store_true", help="только непрочитанные")
    p_ch.add_argument("--limit", type=int, default=50)
    p_ms = sub.add_parser("messages", help="сообщения чата")
    p_ms.add_argument("chat_id")
    p_ms.add_argument("--limit", type=int, default=50)
    p_sd = sub.add_parser("send", help="отправить сообщение (живому человеку!)")
    p_sd.add_argument("chat_id")
    p_sd.add_argument("text")
    p_sd.add_argument("--yes", action="store_true", help="подтверждение отправки")
    p_gt = sub.add_parser("get", help="произвольный GET")
    p_gt.add_argument("path")
    p_gt.add_argument("--query", action="append", default=[], metavar="K=V")

    args = ap.parse_args(argv)
    cid, sec = os.environ.get("AVITO_CLIENT_ID"), os.environ.get("AVITO_CLIENT_SECRET")
    if not cid or not sec:
        print("Не заданы ключи. Впишите AVITO_CLIENT_ID и AVITO_CLIENT_SECRET в .env\n"
              "Берутся в кабинете Авито: «Для профессионалов» → «API» (нужен платный тариф).",
              file=sys.stderr)
        return 2

    c = AvitoBusinessClient(cid, sec, verbose=args.verbose)
    try:
        if args.cmd == "token":
            t = c.token(force=True)
            print(f"OK. Токен получен, отпечаток {_fingerprint(t)} (живёт ~{int(c._exp - time.time())} c)")
        elif args.cmd == "whoami":
            _print(c.whoami())
        elif args.cmd == "items":
            _print(c.items())
        elif args.cmd == "chats":
            _print(c.chats(unread_only=args.unread, limit=args.limit))
        elif args.cmd == "messages":
            _print(c.messages(args.chat_id, limit=args.limit))
        elif args.cmd == "send":
            if not args.yes:
                print("Это отправит сообщение реальному человеку в Авито.\n"
                      "Повторите команду с флагом --yes, если действительно надо.", file=sys.stderr)
                return 2
            _print(c.send(args.chat_id, args.text))
        elif args.cmd == "get":
            _print(c.call("GET", args.path, query=dict(kv.split("=", 1) for kv in args.query)))
    except AvitoError as e:
        print(f"Ошибка API: {e}", file=sys.stderr)
        return 1
    except NetworkError as e:
        print(f"Ошибка сети: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
