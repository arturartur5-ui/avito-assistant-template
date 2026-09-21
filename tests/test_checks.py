"""Проверки перед коммитом и публикацией: ловят то, что должны, и не ловят образцы."""
import importlib.util
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PersonalDataCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load("check-personal-data.py")

    def found(self, text: str, terms=()) -> list[str]:
        return self.m.scan_text(text, list(terms), "образец")

    def test_project_secret_names_are_caught(self):
        value = "AbCdEf0123456789AbCdEf0123456789"
        for line in [f"AVITO_CLIENT_SECRET={value}", f"AVITO_ADS_CLIENT_SECRET={value}", f"MASTER_KEY={value}",
                     f"LLM_API_KEY={value}", f"EXAM_PASSWORD={value}", f"TELEGRAM_BOT_TOKEN={value}",
                     f'"access_token": "{value}"', f"client_secret={value}",
                     'password="Correct.Horse' + '/Battery:Staple!"']:
            with self.subTest(line=line):
                self.assertTrue(self.found(line), f"пропущено: {line[:30]}")

    def test_provider_keys_are_caught(self):
        # образцы склеены из частей: иначе этот файл сам был бы находкой для pre-commit
        for line in ["OPENAI_API_KEY=sk-proj-" + "AbCdEfGhIjKlMnOpQrStUvWxYz123456",
                     "sk-ant-" + "a" * 30, "AKIA" + "IOSFODNN7EXAMPLE", "ghp_" + "a" * 36,
                     "1234567890:" + "A" * 35, "-----BEGIN " + "PRIVATE KEY-----",
                     "postgres:" + "//user:pass@host/db"]:
            with self.subTest(line=line):
                self.assertTrue(self.found(line), f"пропущено: {line[:30]}")

    def test_code_lines_are_not_secrets(self):
        for line in ['TOKEN_CACHE = Path(__file__).with_name(".token_cache.json")',
                     'PASSWORD = os.environ.get("EXAM_PASSWORD", "")', "AVITO_CLIENT_SECRET=",
                     "EXAM_PASSWORD=смените-меня", "self.token(force=True)"]:
            with self.subTest(line=line):
                self.assertEqual(self.found(line), [], f"ложная тревога: {line[:40]}")

    def test_placeholders_pass_and_real_shapes_do_not(self):
        self.assertEqual(self.found("Телефон: +7 900 000 00 00"), [])
        self.assertEqual(self.found("Паспорт 0000 000000, карта 0000 0000 0000 0000"), [])
        self.assertEqual(self.found("почта user@example.com"), [])
        # personal-check: off — образец из двух цифр нужен, чтобы проверить, что
        # такие номера НЕ считаются заглушкой
        self.assertTrue(self.found("Телефон: +7 979 797 97 97"))
        self.assertTrue(self.found("Перезвоните +7.912.345.67.89"))
        self.assertTrue(self.found("Паспорт 4012-345678"))
        self.assertTrue(self.found("почта user@example.company.ru"))
        # personal-check: on

    def test_secrets_ignore_markers_and_unclosed_marker_is_an_error(self):
        text = "# personal-check: off\n" + "sk-ant-" + "a" * 30 + "\n"
        found = self.found(text)
        self.assertTrue(any("Anthropic" in f for f in found))
        self.assertTrue(any("не закрыт" in f for f in found))

    def test_binary_and_utf16_files_are_scanned_for_secrets(self):
        secret = ("AKIA" + "IOSFODNN7EXAMPLE").encode()
        self.assertTrue(self.m.scan_file("logo.png", b"\x89PNG\r\n" + secret, []))
        self.assertTrue(self.m.scan_file("notes.txt", ("LLM_API_KEY=" + "A" * 32).encode("utf-16"), []))
        png = self.m.scan_file("logo.png", b"\x89PNG 8 900 000 00 00", [])
        self.assertEqual(len(png), 1, png)              # телефон по форме в картинке не ищем…
        self.assertIn("в репозиторий не кладём", png[0])   # …но картинке в репозитории не место

    def test_terms_and_filenames(self):
        self.assertTrue(self.found("клиент Примеров сказал", terms=["примеров"]))
        self.assertEqual(self.found("в квартире тепло", terms=["ира"]), [])
        self.assertTrue(self.m.scan_path("docs/Примеров_паспорт.txt", ["примеров"]))
        self.assertTrue(self.m.scan_path("deploy/id_rsa.pem", []))
        self.assertEqual(self.m.scan_path("src/engine.py", ["примеров"]), [])


class AgentRules(unittest.TestCase):
    """CLAUDE.md и AGENTS.md — одни и те же правила для разных агентов.

    Проект настраивают и Claude Code, и Codex. Если файлы разъедутся, два
    агента будут работать по разным правилам — и разойдутся они незаметно.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def _body(self, name: str) -> str:
        text = (self.ROOT / name).read_text(encoding="utf-8")
        # шапка у файлов своя (она объясняет, зачем копия), правила — общие
        return text.split("## Язык", 1)[1].strip()

    def test_rules_match(self):
        self.assertTrue((self.ROOT / "AGENTS.md").exists(),
                        "нет AGENTS.md — агенты, читающие его, останутся без правил проекта")
        self.assertEqual(self._body("CLAUDE.md"), self._body("AGENTS.md"),
                         "правила разъехались: скопируйте изменения во второй файл")


if __name__ == "__main__":
    unittest.main()
