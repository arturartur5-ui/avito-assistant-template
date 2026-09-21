"""Обезличивание: к модели не должно уходить ничего личного.

Каждый образец — форма, которую когда-то пропускали или могли пропустить.
Все номера и документы — из нулей (правило проекта), имена выдуманы. Тесты
не знают ниши: они про формы данных, а не про предметную область.
"""
# personal-check: off — файл с тестами: все имена, номера и адреса здесь выдуманы
import os
import time
import unittest

os.environ.setdefault("OWNER_NAME_FORMS", "Ярополк,Ярополк,Примеров")

from anonymize import anonymize, leak_scan, outbound  # noqa: E402

PHONES = [
    "мой номер +7 900 000 00 00, звоните", "тел 8-900-000-00-00", "79000000000 это я", "89000000000",
    "9000000000 мой", "+7 (900) 000-00-00", "900 000 00 00", "+44 20 0000 0000",
    # разделители, которые когда-то не считались разделителями
    "+7.900.000.00.00", "8/900/000/00/00", "8‑900‑000‑00‑00", "8—900—000—00—00", "900.000.00.00",
    "9 0 0 0 0 0 0 0 0 0", "Записывайте +7 [900] 000-00-00", "тел +7(900)000_00_00",
]

# что должно уцелеть: суммы, диапазоны, география, обычные слова
UNTOUCHED = [
    "Питер, 7 млн, нужно до конца года", "доход 50 000 - 100 000", "бюджет 10 000 000 - 12 000 000",
    "взнос 100 000 - 150 000", "8 000 000 рублей", "Московская область, дом 3 этажа",
    "Причина отказа неизвестна", "справки, выписки и пр. документы", "с 10.00 до 18.00 по будням",
    "2024-2025 годы, ставка 6%", "света нет в подъезде", "слава богу, всё хорошо", "в Новой Москве",
    "ИНН [ИНН] уже есть", "Общая пл. 45 кв.м, цена 5 500 000", "Сельская программа подходит?",
    # эти формы ломались дважды: любое слово на «ул/наб/мкр/пр/пер/ш» и «площадь»
    "Здравствуйте! Хочу улучшить жилищные условия, смотрю двушку примерно 12 млн",
    "Первый взнос примерно 2 500 000, доход 180 000 в месяц", "Просрочка 3 месяца была в 2022 году",
    "Общая площадь 62 кв м, дом сдан", "При 20% взноса какая будет ставка?", "Наберите мне попозже",
    "Нужен полный набор документов? Какой?", "Какая площадь у этой квартиры?", "Есть проезд к дому",
    "в Московской области", "примерно 5 млн", "Шкаф встроенный, перегородка гипсокартон",
]


class Outbound(unittest.TestCase):
    def assertMasked(self, text: str, placeholder: str) -> str:
        out = outbound(text)
        self.assertIn(placeholder, out, f"{text!r} -> {out!r}")
        self.assertEqual(leak_scan(out), [], f"на выходе осталось: {out!r}")
        return out

    def test_phones_in_every_format(self):
        for text in PHONES:
            with self.subTest(text=text):
                out = self.assertMasked(text, "[ТЕЛЕФОН]")
                self.assertFalse(any(ch.isdigit() for ch in out.replace("[ТЕЛЕФОН]", "")),
                                 f"цифры номера остались: {out!r}")

    def test_email(self):
        self.assertMasked("пишите на ivan.petrov@example.com", "[EMAIL]")

    def test_nicks(self):
        self.assertMasked("мой тг @ivan_petrov", "[НИК]")
        self.assertMasked("телеграм ivan_petrov напишите", "[НИК]")
        self.assertMasked("Мой телеграм @иван_ремонт", "[НИК]")
        self.assertMasked("мой ник johnsmith00", "[НИК]")
        self.assertMasked("ник: johnsmith", "[НИК]")
        self.assertMasked("в вотсапе я Ivan", "[НИК]")

    def test_links(self):
        self.assertMasked("профиль https://avito.ru/user/abc", "[ССЫЛКА]")
        self.assertMasked("сайт ivanov.ru посмотрите", "[ССЫЛКА]")
        self.assertMasked("Все материалы тут: t.me/moy_kanal", "[ССЫЛКА]")
        self.assertMasked("Открою вам vk.cc/abc", "[ССЫЛКА]")

    def test_documents(self):
        self.assertMasked("паспорт 0000 000000", "[ПАСПОРТ]")
        self.assertMasked("паспорт 0000-000000", "[ПАСПОРТ]")
        self.assertMasked("снилс 000-000-000 00", "[СНИЛС]")
        self.assertMasked("снилс 000.000.000.00", "[СНИЛС]")
        self.assertMasked("карта 0000 0000 0000 0000", "[КАРТА]")
        self.assertMasked("инн 000000000000", "[ИНН]")
        self.assertMasked("ИНН 000 000 000 000", "[ИНН]")

    def test_addresses(self):
        self.assertMasked("живу ул. Ленина, д. 15, кв. 3", "[АДРЕС]")
        self.assertMasked("ул Садовая, д. 12", "[АДРЕС]")
        self.assertMasked("пр. Мира 10", "[АДРЕС]")
        out = self.assertMasked("Живу: Невский проспект 10, квартира 7", "[АДРЕС]")
        self.assertNotIn("Невский", out)

    def test_client_names(self):
        self.assertMasked("Меня зовут Сергей, хочу квартиру", "[ИМЯ]")
        out = self.assertMasked("Клиент Александра Данилова хочет двушку", "[ИМЯ]")
        self.assertNotIn("Данилова", out)
        self.assertMasked("меня зовут зульфия, хочу кухню", "[ИМЯ]")
        self.assertMasked("Смирнов, подскажите по ремонту", "[ИМЯ]")
        self.assertMasked("Кузнецова хочет узнать сроки", "[ИМЯ]")
        out = self.assertMasked("МАРИЯ ИВАНОВА, добрый день", "[ИМЯ]")
        self.assertNotIn("ИВАНОВА", out)
        out = self.assertMasked("Свяжитесь с Марией Ивановой", "[ИМЯ]")
        self.assertNotIn("Ивановой", out)
        out = self.assertMasked("договор со мной, Ильёй Петровым", "[ИМЯ]")
        self.assertNotIn("Петровым", out)

    def test_owner_name_becomes_role(self):
        out = outbound("Здравствуйте, это Ярополк Примеров, мастер по ремонту")
        self.assertNotIn("Ярополк", out)
        self.assertNotIn("Примеров", out)
        self.assertIn("специалист", out)
        self.assertEqual(leak_scan(out), [])

    def test_ordinary_text_survives(self):
        for text in UNTOUCHED:
            with self.subTest(text=text):
                self.assertEqual(outbound(text), text)

    def test_long_message_is_fast(self):
        text = ("Здравствуйте! Хочу уточнить по объекту, " * 300) + "+7 900 000 00 00"
        started = time.perf_counter()
        outbound(text)
        self.assertLess(time.perf_counter() - started, 1.0, "обезличивание подвисает на длинном тексте")
        started = time.perf_counter()
        outbound("тест Иванов " * 5000)
        self.assertLess(time.perf_counter() - started, 1.0, "квадратичный рост на заглавных словах")


class FormsFoundByOutsideAudit(unittest.TestCase):
    """Формы, которые пропускала первая версия (найдено сторонним аудитом 21.09.2026)."""

    def test_masked_in_live_mode(self):
        for s, leaks in [("My name is John Smith, call me later", ("John", "Smith")),
                         ("Я Ким, живу на Невского 15, квартира 7", ("Ким", "Невского", "15")),
                         ("Координаты 55.7558, 37.6173", ("55.7558", "37.6173")),
                         ("живу по адресу Садовая 10", ("Садовая",)),
                         ("I am Ivan Petrov, my address is Lenina 5", ("Ivan", "Petrov", "Lenina")),
                         ("Я Ким, работаю в такси", ("Ким",))]:
            out = outbound(s)
            for leak in leaks:
                self.assertNotIn(leak, out, f"{s!r} -> {out!r}")
            self.assertEqual(leak_scan(out), [], out)

    def test_numbers_that_are_not_coordinates(self):
        for s in ["рейтинг 4.9876 5.0000", "ставка 12.5000 8.2500", "цена 1.5 2.5 миллиона",
                  "площадь 55.5 кв м", "живу в Казани 5 лет", "я думаю, что дорого"]:
            self.assertEqual(outbound(s), s)


class BypassAttempts(unittest.TestCase):
    """Способы обойти обезличивание, найденные сторонним аудитом 21.09.2026.

    Два разных требования. Часть форм проект обязан вычистить — на них проверяем
    результат. Остальные он может не уметь чистить, но обязан УВИДЕТЬ: тогда
    движок не отправит запрос наружу (Engine._egress_check), и разговор уйдёт
    специалисту. Пропуск — это когда форма и не вычищена, и не замечена.
    """

    BYPASSES = [
        # телефон, записанный в обход канонических форм
        "мой номер восемь девятьсот двенадцать триста сорок пять шестьдесят семь восемьдесят девять",
        "8 9OO 000 00 00",                       # латинские O вместо нулей
        "8 9ОО 000 00 00",                       # кириллические О
        "тел 8*900*000*00*00",
        "8,900,000,00,00",
        "8\u200b900\u200b000\u200b00\u200b00",   # нулевая ширина между цифрами
        "городской 495 000 00 00",
        # имена, которых нет в словарях
        "Ivan Petrov звонил вчера по объекту",
        "клиент Ли спрашивает",
        "это Черных, мы говорили",
        "передайте Игорю, что готово",
        # документы и номера
        "паспорт: серия 0000 номер 000000",
        "кадастровый номер 16:50:000000:000",
        "машина А000ВС777",
        # контакты в обфусцированном виде
        "ivan.petrov собака mail точка ru",
        "мой инст ivan.petrov.spb",
        # место и дата
        "координаты 55.7558,37.6173",
        "55.7558N, 37.6173E",
        "родился 5 марта 1990 года",
        "дата рождения 1991-05-12",
    ]

    def test_nothing_slips_through_unseen(self):
        for text in self.BYPASSES:
            out = outbound(text)
            masked = out != text
            noticed = bool(leak_scan(out))
            self.assertTrue(masked or noticed,
                            f"прошло незамеченным: {text!r} -> {out!r}")

    def test_dates_do_not_silence_the_assistant(self):
        """Дата — не номер документа.

        Правило «цепочка цифр» специально широкое, и оно ловило «Годен до:
        22.10.2026» и тег куска «[диалог, 2025-06-15]». Вместе с fail-closed
        это значило: заполнил условия по инструкции — помощник замолчал на
        каждое сообщение.
        """
        for text in ["- **Годен до:** 22.10.2026", "Годен до: 22.10.2026",
                     "[диалог, 2025-06-15]", "Обновлено: 01.09.2026",
                     "договор от 15.03.2024", "работаем с 01.10.2026 по 31.12.2026",
                     "5/3/24", "10.2026"]:
            self.assertEqual(leak_scan(outbound(text)), [], f"дата глушит помощника: {text!r}")

    def test_long_numbers_are_still_caught(self):
        for text in ["тел 8*900*000*00*00", "р/с 40817 810 0 9991 0004312", "8,900,000,00,00"]:
            out = outbound(text)
            self.assertTrue(out != text or leak_scan(out), f"номер прошёл: {text!r}")

    def test_ordinary_phrases_survive(self):
        # Ложная тревога здесь стоит дорого: помощник замолчит на обычном вопросе
        for text in ["бюджет 3 000 000, взнос 600 000",
                     "Apple Pay не работает", "оплата через Sber Bank",
                     "клиент доволен работой", "это очень дорого", "для дома нужен ремонт",
                     "это Москва, центр", "работаем с 9 до 18", "квартира 45 кв м, 3 комнаты",
                     "цена 1.5 2.5 миллиона", "площадь 55.5 кв м", "ставка 12.5 процента",
                     "Первый взнос примерно 2 500 000, доход 180 000 в месяц",
                     "с уважением, специалист", "живу в Казани 5 лет"]:
            out = outbound(text)
            self.assertEqual(out, text, f"зря замаскировано: {text!r} -> {out!r}")
            self.assertEqual(leak_scan(out), [], f"зря поднята тревога: {text!r}")


class CorpusMode(unittest.TestCase):
    """Режим корпуса: имена ловятся не только перед запятой, география остаётся."""

    def test_names_inside_text(self):
        # раньше корпус видел только известные имена и слово перед запятой —
        # «Пишет Мария Петрова» уходил в корпус как есть
        for s in ["Пишет Мария Петрова", "Меня зовут Иван Ковалевский, звоните",
                  "Здравствуйте, я Иван", "Позвонил Сергей Иванович", "Спасибо Владимиру за помощь"]:
            out = anonymize(s, set())
            self.assertIn("[ИМЯ]", out, s)
            for w in ("Мария", "Петрова", "Иван", "Ковалевский", "Сергей", "Иванович", "Владимиру"):
                self.assertNotIn(w, out, s)

    def test_geo_and_terms_survive(self):
        for s in ["Живу в Казани, район Азино", "Мы в Москве, приезжайте", "Сельская ипотека, ставка ниже",
                  "Косметический ремонт от 3 тыс за метр", "Причина отказа, как я понял, просрочки",
                  "Проверено 21.09.2026, прайс актуален"]:
            self.assertEqual(anonymize(s, set()), s)
            self.assertEqual(anonymize(s, set(), aggressive=False), s)

    def test_birth_dates_only(self):
        self.assertEqual(anonymize("родился 01.02.1990", set()), "родился [ДАТА]")
        self.assertEqual(anonymize("д.р. 05.03.2001", set()), "д.р. [ДАТА]")
        self.assertIn("[ДАТА]", anonymize("паспорт выдан 05.03.2010", set()))
        self.assertNotIn("[ДАТА]", anonymize("встреча 15.09.2026 в 14:00", set()))


if __name__ == "__main__":
    unittest.main()
