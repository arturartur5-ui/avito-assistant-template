"""Мок-тест клиента: токен, ретрай 401/429, парсинг ошибок авторизации."""
import json, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import avito_ads

STATE = {"tokens_issued": 0, "campaign_calls": 0, "throttle_once": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.endswith("/campaigns"):
            self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            return self._campaigns()
        if self.path == "/token":
            raw = self.rfile.read(int(self.headers["Content-Length"])).decode()
            if "client_secret=good" not in raw:
                # Авито отдаёт ошибку авторизации с HTTP 200
                return self._send(200, {"error": "unauthorized_client"})
            STATE["tokens_issued"] += 1
            return self._send(200, {"access_token": f"tok{STATE['tokens_issued']}", "expires_in": 86400})
        self._send(404, {"error": "not found"})

    def do_GET(self):
        return self._send(405, {"error": "method not allowed"})

    def _campaigns(self):
        STATE["campaign_calls"] += 1
        token = self.headers.get("Authorization", "")
        if STATE["campaign_calls"] == 1:
            return self._send(401, {"error": "expired"})           # заставляем обновить токен
        if STATE["campaign_calls"] == 2 and STATE["throttle_once"]:
            return self._send(429, {"error": "no points"}, {"Retry-After": "0", "X-RateLimit-Points": "0"})
        self._send(200, {"campaigns": [{"id": 1, "name": "test"}], "seen_token": token})


class TestClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        avito_ads.TOKEN_URL = base + "/token"
        avito_ads.API_BASE = base + "/ads/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_bad_credentials_raise(self):
        c = avito_ads.AvitoAdsClient("id", "bad", use_cache=False)
        with self.assertRaises(avito_ads.AvitoError):
            c.token()

    def test_token_cached_in_memory(self):
        c = avito_ads.AvitoAdsClient("id", "good", use_cache=False)
        before = STATE["tokens_issued"]
        self.assertEqual(c.token(), c.token())
        self.assertEqual(STATE["tokens_issued"], before + 1)

    def test_retries_401_then_429_then_succeeds(self):
        c = avito_ads.AvitoAdsClient("id", "good", account_id="123", use_cache=False)
        data = c.campaigns()
        self.assertEqual(data["campaigns"][0]["name"], "test")
        self.assertEqual(STATE["campaign_calls"], 3)          # 401 -> 429 -> 200
        self.assertTrue(data["seen_token"].startswith("Bearer tok"))

    def test_missing_account_id(self):
        c = avito_ads.AvitoAdsClient("id", "good", use_cache=False)
        with self.assertRaises(SystemExit):
            c.campaigns()


if __name__ == "__main__":
    unittest.main(verbosity=2)
