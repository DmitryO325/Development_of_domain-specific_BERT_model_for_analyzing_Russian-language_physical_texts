"""Проверки безопасного выбора опубликованных ссылок на препринты ОИЯИ."""

from __future__ import annotations

import copy
import unittest

from typing import Any

from src.collect.jinr_preprints import extract_preprint_urls, validate_preprint_url

FIRST_PREPRINT_URL = "http://www1.jinr.ru/Preprints/2024/05(P11-2024-5).pdf"
SECOND_PREPRINT_URL = "https://www1.jinr.ru/Preprints/2024/16(D17-2024-16)_rus.pdf"


class PreprintUrlTests(unittest.TestCase):
    """Проверки сервера, пути и отсутствия скрытых параметров в адресе PDF."""

    def test_published_preprint_urls_and_standard_ports_are_allowed(self) -> None:
        """Обе схемы и соответствующие им стандартные порты допустимы."""

        urls = (
            FIRST_PREPRINT_URL,
            SECOND_PREPRINT_URL,
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru:80"),
            SECOND_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru:443"),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "WWW1.JINR.RU"),
            FIRST_PREPRINT_URL.replace("(", "%28").replace(")", "%29"),
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(validate_preprint_url(url))

    def test_other_servers_schemes_and_credentials_are_rejected(self) -> None:
        """Внешние серверы, сведения для авторизации и неподходящие порты запрещены."""

        urls = (
            FIRST_PREPRINT_URL.replace("http:", "ftp:"),
            FIRST_PREPRINT_URL.replace("http:", "file:"),
            FIRST_PREPRINT_URL.replace("http:", ""),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "other.example"),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru.evil.example"),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru."),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "user@www1.jinr.ru"),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "user:secret@www1.jinr.ru"),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru:443"),
            SECOND_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru:80"),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru:8080"),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru:bad"),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru:"),
            FIRST_PREPRINT_URL.replace("www1.jinr.ru", "www1.jinr.ru:080"),
            "https://pubrepo-api.jinr.ru/server/api",
        )

        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_preprint_url(url)

    def test_other_paths_queries_and_fragments_are_rejected(self) -> None:
        """Разрешён только файл PDF без параметров и фрагментов, включая пустые."""

        urls = (
            FIRST_PREPRINT_URL + "?",
            FIRST_PREPRINT_URL + "?download=1",
            FIRST_PREPRINT_URL + "#",
            FIRST_PREPRINT_URL + "#page=1",
            FIRST_PREPRINT_URL + "/",
            FIRST_PREPRINT_URL.replace("/Preprints/", "/Other/"),
            FIRST_PREPRINT_URL.replace("/Preprints/", "/preprints/"),
            FIRST_PREPRINT_URL.replace("/2024/", "/24/"),
            FIRST_PREPRINT_URL.replace("/2024/", "/２０２４/"),
            FIRST_PREPRINT_URL.replace(".pdf", ".html"),
            FIRST_PREPRINT_URL.replace(".pdf", ".pdf.exe"),
            "http://www1.jinr.ru/Preprints/2024/",
            "http://www1.jinr.ru/Preprints/2024/.pdf",
        )

        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_preprint_url(url)

    def test_traversal_encoding_and_controls_are_rejected(self) -> None:
        """Переходы, повторное кодирование и скрытые разделители не проходят проверку."""

        unsafe_paths = (
            "/Preprints/2024/../private.pdf",
            "/Preprints/2024/./paper.pdf",
            "/Preprints/2024/%2e%2e/private.pdf",
            "/Preprints/2024/%2e%2e%2fprivate.pdf",
            "/Preprints/2024/%252e%252e%252fprivate.pdf",
            "/Preprints/2024/subdir%2fpaper.pdf",
            "/Preprints/2024/subdir%2Fpaper.pdf",
            "/Preprints/2024/subdir\\paper.pdf",
            "/Preprints/2024/subdir%5cpaper.pdf",
            "/Preprints/2024/subdir%255cpaper.pdf",
            "/Preprints/2024/paper%00.pdf",
            "/Preprints/2024/paper%0a.pdf",
            "/Preprints/2024/paper%7f.pdf",
            "/Preprints/2024/paper%20name.pdf",
            "/Preprints/2024/paper%3fname.pdf",
            "/Preprints/2024/paper%23name.pdf",
            "/Preprints/2024/paper%2528name%2529.pdf",
            "/Preprints/2024/paper%ZZ.pdf",
            "/Preprints/2024/paper%.pdf",
        )

        for path in unsafe_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_preprint_url("http://www1.jinr.ru" + path)

        for character in (" ", "\t", "\n", "\r", "\x00", "\x7f"):
            for url in (character + FIRST_PREPRINT_URL, FIRST_PREPRINT_URL + character):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    validate_preprint_url(url)

    def test_non_string_values_are_rejected(self) -> None:
        """Значения неподходящего типа не передаются разборщику URL."""

        invalid_values: list[Any] = [None, 1, False, [], {}, b"http://www1.jinr.ru/"]

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_preprint_url(value)


class PreprintExtractionTests(unittest.TestCase):
    """Проверки структуры метаданных и выбора точных исходных ссылок."""

    def test_only_publication_uri_is_used_without_guessing(self) -> None:
        """Ссылки из иных полей и номер препринта не используются для загрузки."""

        item = {
            "metadata": {
                "dc.identifier.uri": [{"value": FIRST_PREPRINT_URL}],
                "dc.description.abstract": [{"value": SECOND_PREPRINT_URL}],
                "local.identifier.JINR-preprint": [{"value": "P11-2024-5"}],
                "local.publication.uri": [{"value": SECOND_PREPRINT_URL}],
            },
            "_links": {"pdf": {"href": FIRST_PREPRINT_URL}},
        }

        self.assertEqual(extract_preprint_urls(item), [SECOND_PREPRINT_URL])

    def test_deduplication_preserves_exact_urls_order_and_metadata(self) -> None:
        """Точные дубли удаляются без изменения схемы, кодирования и исходной карточки."""

        encoded_url = FIRST_PREPRINT_URL.replace("(", "%28").replace(")", "%29")
        urls = [SECOND_PREPRINT_URL, FIRST_PREPRINT_URL, SECOND_PREPRINT_URL, encoded_url]
        item = {"metadata": {"local.publication.uri": [{"value": url} for url in urls]}}
        original_item = copy.deepcopy(item)

        self.assertEqual(
            extract_preprint_urls(item),
            [SECOND_PREPRINT_URL, FIRST_PREPRINT_URL, encoded_url],
        )
        self.assertEqual(item, original_item)

    def test_unrelated_and_unsafe_urls_are_skipped(self) -> None:
        """Ссылка на eLIBRARY, HTML или запрещённый путь не становится кандидатом PDF."""

        values = (
            "https://elibrary.ru/item.asp?id=123",
            "http://www1.jinr.ru/Preprints/2024/index.html",
            FIRST_PREPRINT_URL + "?download=1",
            FIRST_PREPRINT_URL.replace("/2024/", "/2024/../"),
            "",
            "не ссылка",
            FIRST_PREPRINT_URL,
        )
        item = {"metadata": {"local.publication.uri": [{"value": value} for value in values]}}

        self.assertEqual(extract_preprint_urls(item), [FIRST_PREPRINT_URL])

    def test_absent_and_empty_fields_return_empty_list(self) -> None:
        """Отсутствие ссылок является допустимым состоянием карточки."""

        items = ({}, {"metadata": {}}, {"metadata": {"local.publication.uri": []}})

        for item in items:
            with self.subTest(item=item):
                self.assertEqual(extract_preprint_urls(item), [])

    def test_malformed_metadata_is_not_silently_skipped(self) -> None:
        """Повреждённая структура карточки останавливает выбор ссылок."""

        items: list[Any] = [None, [], {"metadata": None}, {"metadata": []}]
        malformed_values = [None, "url", {}, [None], ["url"], [{}], [{"value": None}], [{"value": 1}]]
        items.extend({"metadata": {"local.publication.uri": value}} for value in malformed_values)

        for item in items:
            with self.subTest(item=item), self.assertRaises(ValueError):
                extract_preprint_urls(item)


if __name__ == "__main__":
    unittest.main()
