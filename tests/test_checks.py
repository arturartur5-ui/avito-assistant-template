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


class BeforePublish(unittest.TestCase):
    """Проверка перед публикацией: роботов GitHub не считает людьми, людей — считает.

    Dependabot подписывает коммиты служебными адресами GitHub, и его ветки
    появятся у каждого, кто включит его. Останавливать публикацию из-за этого
    нельзя; а вот настоящая почта человека в коммите — по-прежнему находка.
    """

    def _repo(self, tmp: Path, name: str, email: str, message: str) -> None:
        import subprocess
        def g(*a): subprocess.run(["git", "-C", str(tmp), *a], check=True, capture_output=True)
        if not (tmp / ".git").exists():
            g("init", "-q")
        (tmp / "a.txt").write_text("x\n", encoding="utf-8")
        g("add", "a.txt")
        g("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "--allow-empty", "-m", message)

    def _run(self, tmp: Path) -> tuple[int, list[str]]:
        import io, sys, contextlib
        mod = load("check-before-publish.py")
        mod.ROOT = tmp
        mod._BOT_COMMITS = None
        mod.WARNINGS.clear()
        old = sys.argv; sys.argv = ["check-before-publish.py", "--ci"]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = mod.main()
        finally:
            sys.argv = old
        return code, list(mod.WARNINGS)

    def test_bot_commit_is_not_a_finding(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._repo(tmp, "dependabot[bot]", "49699333+dependabot[bot]@users.noreply.github.com",
                       "Bump x\n\nSigned-off-by: dependabot[bot] <support" + "@github.com>")
            code, warnings = self._run(tmp)
            self.assertEqual(code, 0, "коммит робота остановил публикацию")
            self.assertEqual(warnings, [])

    def test_human_email_is_still_flagged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._repo(tmp, "Ярополк Примеров", "yaropolk" + "@mail.example", "пробный")
            code, warnings = self._run(tmp)
            self.assertTrue(any("настоящая почта" in w for w in warnings),
                            "настоящая почта автора не замечена")


    def test_no_terms_file_explains_instead_of_crashing(self):
        """Человек скачал шаблон: своего .personal-terms у него ещё нет, лежит
        только .example. Проверка обязана сказать, чего не хватает, а не
        свалиться трейсбеком — иначе первое же знакомство с ней выглядит
        как сломанный проект."""
        import io, sys, contextlib, tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self._repo(tmp, "Ярополк Примеров", "yaropolk" + "@mail.example", "пробный")
            mod = load("check-before-publish.py")
            mod.ROOT = tmp
            mod._BOT_COMMITS = None
            mod.WARNINGS.clear()
            mod._mod.TERMS_FILE = tmp / ".personal-terms"      # его нет, как у нового человека
            old = sys.argv
            sys.argv = ["check-before-publish.py"]             # без --ci: обычный запуск руками
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    code = mod.main()
            finally:
                sys.argv = old
            self.assertEqual(code, 2, "без списка терминов публикация обязана остановиться")
            self.assertIn(".personal-terms", err.getvalue(),
                          "человеку не сказали, какого файла не хватает")

    def test_every_helper_used_exists(self):
        """Два скрипта — одна пара: check-before-publish зовёт функции соседа
        через _mod. Переименовали или убрали функцию у соседа — падать должно
        здесь, а не у человека в момент публикации."""
        import re
        src = (SCRIPTS / "check-before-publish.py").read_text(encoding="utf-8")
        used = sorted(set(re.findall(r"_mod\.([A-Za-z_][A-Za-z0-9_]*)", src)))
        self.assertTrue(used, "вызовов соседнего модуля не нашлось — проверка потеряла смысл")
        mod = load("check-before-publish.py")
        missing = [name for name in used if not hasattr(mod._mod, name)]
        self.assertEqual(missing, [], f"в check-personal-data.py нет: {missing}")


class KnowledgeFormats(unittest.TestCase):
    """Прайс в .txt и таблица в .csv должны попадать в индекс наравне со статьями.

    Индексатор их разрешал, а сборка читала только .md — файл молча пропадал.
    """

    def test_plain_files_are_indexed(self):
        import sys, tempfile
        sys.path.insert(0, str(SCRIPTS.parent / "src"))
        import chunking
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "knowledge" / "articles").mkdir(parents=True)
            (root / "corpus").mkdir()
            (root / "knowledge" / "price.txt").write_text(
                "Прайс на работы. Штукатурка стен по маякам — 600 за квадрат, "
                "при объёме больше ста метров скидка десять процентов. "
                "Демонтаж перегородок считается отдельно по факту.", encoding="utf-8")
            (root / "knowledge" / "articles" / "table.csv").write_text(
                "услуга;цена;единица\nукладка плитки;1200;за квадратный метр\n"
                "затирка швов;150;за квадратный метр\nустановка двери;3500;за штуку", encoding="utf-8")
            chunks = chunking.build_all(root / "knowledge", root / "corpus")
            origins = {c.origin for c in chunks}
            self.assertIn("price.txt", origins, "прайс в .txt не попал в индекс")
            self.assertIn("table.csv", origins, "таблица в .csv не попала в индекс")


if __name__ == "__main__":
    unittest.main()
