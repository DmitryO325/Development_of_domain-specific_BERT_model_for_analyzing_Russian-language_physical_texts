"""Проверки обхода Иоффе на локальных HTTP-снимках без сетевых соединений."""

from __future__ import annotations

import io
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import Mock, patch

import pymupdf

from src.collect.base import HttpResponseSnapshot
from src.collect.ioffe_archive import IoffeArchive
from src.collect.ioffe_collection import IoffeCollector
from src.collect.ioffe_http import IoffeClient, IoffeError

ORIGIN = "https://journals.ioffe.ru"


def _snapshot(url: str, body: bytes, content_type: str) -> HttpResponseSnapshot:
    """Создать завершённый ответ с точными заголовками и фиксированным временем."""

    return HttpResponseSnapshot(
        requested_url=url,
        final_url=url,
        status_code=200,
        headers=(("content-type", content_type), ("content-length", str(len(body)))),
        retrieved_at="2026-10-12T12:00:00+00:00",
        body=body,
    )


def _pdf_bytes() -> bytes:
    """Создать корректный одностраничный PDF в памяти."""

    with pymupdf.open() as document:
        document.new_page()
        return document.tobytes()


class _Clock:
    """Подменить часы и ожидание, сохраняя порядок действий без реальных пауз."""

    def __init__(self) -> None:
        """Начать отсчёт с фиксированного положительного времени."""

        self.now = 1000.0

    def time(self) -> float:
        """Вернуть одинаковый управляемый отсчёт для обоих видов часов."""

        return self.now

    def sleep(self, seconds: float) -> None:
        """Продвинуть часы вместо блокировки потока."""

        self.now += seconds


class IoffeCollectorTests(unittest.TestCase):
    """Проверять robots.txt, отдельные статьи, границы обхода и возобновление."""

    def setUp(self) -> None:
        """Подготовить временный архив и клиент, возвращающий только фикстуры."""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.responses: dict[str, HttpResponseSnapshot] = {}
        self.client = Mock(spec=IoffeClient)
        self.client.delay_seconds = 60.0
        self.client.get_response.side_effect = self.responses.__getitem__
        self.archive = IoffeArchive(
            self.root / "raw", self.root / "state", self.client, allow_network=True,
        )
        self.collector = IoffeCollector(self.archive, self.root / "state", ORIGIN)
        stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)
        self._response("/robots.txt", "User-agent: *\nDisallow:\n", "text/plain")

    def _response(self, path: str, body: str | bytes, content_type: str = "text/html") -> None:
        """Добавить точный ответ, доступный только через подменённый клиент."""

        encoded_body = body.encode("utf-8") if isinstance(body, str) else body
        self.responses[f"{ORIGIN}{path}"] = _snapshot(f"{ORIGIN}{path}", encoded_body, content_type)

    def _card(self, article_id: int = 10, *, with_pdf: bool = False) -> None:
        """Подготовить карточку с аннотацией, а не с полным текстом статьи."""

        html = (
            '<meta name="citation_title" content="Исследование полупроводников">'
            '<meta name="citation_author" content="Иванов И. И.">'
            '<meta name="citation_abstract" content="Аннотация о свойствах кристаллов.">'
            '<meta name="citation_doi" content="10.1234/example">'
        )

        if with_pdf:
            html += f'<a href="/articles/viewPDF/{article_id}">PDF</a>'
            self._response(f"/articles/viewPDF/{article_id}", _pdf_bytes(), "application/pdf")

        self._response(f"/articles/{article_id}", html)

    def _journal(self) -> None:
        """Подготовить два выпуска с общей статьёй и повторяющимися ссылками."""

        self._response("/journals/1", '<a href="/issues/100">1</a><a href="/issues/200">2</a>')
        self._response("/issues/100", '<a href="/articles/10">A</a><a href="/articles/20">B</a>')
        self._response("/issues/200", '<a href="/articles/20">B</a><a href="/articles/30">C</a>')

    def test_prepare_is_required_before_article_access(self) -> None:
        """Отсутствие robots.txt должно останавливать даже чтение карточки."""

        with self.assertRaisesRegex(IoffeError, "robots.txt"):
            self.collector.collect_article(f"{ORIGIN}/articles/10")

        self.client.get_response.assert_not_called()

    def test_robots_denial_prevents_request(self) -> None:
        """Запрещённая карточка не должна запрашиваться или становиться готовой."""

        self._response("/robots.txt", "User-agent: *\nDisallow: /articles/\n", "text/plain")
        self.collector.prepare()

        with self.assertRaisesRegex(IoffeError, "запрещён"):
            self.collector.collect_article(f"{ORIGIN}/articles/10")

        self.assertEqual(self.client.get_response.call_count, 1)
        self.assertEqual(self.collector.completed, 0)

    def test_html_and_unsupported_robots_rules_fail_closed(self) -> None:
        """HTML и неподдерживаемые маски нельзя трактовать как разрешение обхода."""

        cases = (
            ("User-agent: *\nDisallow:\n", "text/html"),
            ("<!DOCTYPE html><HTML>Ошибка</HTML>", "text/plain"),
            ("User-agent: *\nDisallow: /articles/*\n", "text/plain"),
            ("User-agent: *\nAllow: /articles/10$\n", "text/plain"),
            (b"\xff\xfe", "text/plain"),
        )

        for body, content_type in cases:
            with self.subTest(body=body, content_type=content_type):
                self._response("/robots.txt", body, content_type)

                with self.assertRaises(IoffeError):
                    self.collector.prepare()

                self.assertIsNone(self.collector.robots)

    def test_server_delay_can_only_increase_minute_interval(self) -> None:
        """Crawl-delay и Request-rate могут усилить, но не ослабить минутную паузу."""

        for rule, expected in (("Crawl-delay: 120", 120), ("Crawl-delay: 2", 60),
                               ("Request-rate: 1/180", 180)):
            with self.subTest(rule=rule):
                self.client.delay_seconds = 60.0
                self.client.set_minimum_delay.reset_mock()
                self._response("/robots.txt", f"User-agent: *\n{rule}\nDisallow:\n", "text/plain")
                self.collector.prepare()

                if expected > 60:
                    self.client.set_minimum_delay.assert_called_once_with(expected)

                else:
                    self.client.set_minimum_delay.assert_not_called()

    def test_article_cannot_change_origin(self) -> None:
        """Даже допустимый узел нельзя автоматически переключать с HTTPS на HTTP."""

        self.collector.prepare()

        for origin in ("http://journals.ioffe.ru", "https://example.org"):
            with self.subTest(origin=origin), self.assertRaises((IoffeError, ValueError)):
                self.collector.collect_article(f"{origin}/articles/10")

        self.assertEqual(self.client.get_response.call_count, 1)

    def test_crawl_delay_applies_to_first_request_after_robots(self) -> None:
        """Повышенная пауза должна действовать сразу и переживать создание нового клиента."""

        self._card()
        self._card(20)
        self._response("/robots.txt", "User-agent: *\nCrawl-delay: 120\nDisallow:\n", "text/plain")
        clock = _Clock()
        client = IoffeClient(self.root / "rate")
        archive = IoffeArchive(self.root / "raw", self.root / "state", client, allow_network=True)
        collector = IoffeCollector(archive, self.root / "state", ORIGIN)

        with (
            patch("src.collect.ioffe_http.time.time", side_effect=clock.time),
            patch("src.collect.ioffe_http.time.monotonic", side_effect=clock.time),
            patch("src.collect.ioffe_http.time.sleep", side_effect=clock.sleep),
            patch.object(IoffeClient, "_request_once", side_effect=self.responses.__getitem__) as request,
        ):
            collector.prepare()
            self.assertEqual(clock.now, 1000)
            collector.collect_article(f"{ORIGIN}/articles/10")
            self.assertEqual(clock.now, 1120)
            restarted = IoffeClient(self.root / "rate")
            restarted.get_response(f"{ORIGIN}/articles/20")

        self.assertEqual(clock.now, 1240)
        self.assertEqual(restarted.delay_seconds, 120)
        self.assertEqual(request.call_count, 3)

    def test_metadata_only_card_is_saved_without_fake_pdf(self) -> None:
        """Аннотация остаётся метаданными, а отсутствие PDF не становится ошибкой."""

        self._card()
        self.collector.prepare()
        record = self.collector.collect_article(f"{ORIGIN}/articles/10")

        self.assertEqual(record["status"], "metadata_only")
        self.assertEqual(record["document"]["text"], "Аннотация о свойствах кристаллов.")
        self.assertEqual(record["document"]["extra"]["content_role"], "metadata_only")
        self.assertEqual(record["document"]["extra"]["doi"], "10.1234/example")
        self.assertEqual(record["training_eligibility"], "not_assessed")
        self.assertFalse(record["registered"])
        self.assertNotIn("pdf_response", record)
        self.assertEqual((self.collector.completed, self.collector.metadata_only_count), (1, 1))
        self.assertEqual(json.loads((self.root / "state/articles/10.json").read_text()), record)

    def test_valid_pdf_is_saved_separately_without_text_extraction(self) -> None:
        """PDF получает свидетельство и число страниц, но аннотация не заменяется OCR."""

        self._card(with_pdf=True)
        self.collector.prepare()
        record = self.collector.collect_article(f"{ORIGIN}/articles/10")

        self.assertEqual(record["status"], "pdf_downloaded")
        self.assertEqual(record["page_count"], 1)
        self.assertEqual(record["document"]["extra"]["content_role"], "metadata_only")
        self.assertEqual(record["training_eligibility"], "not_assessed")
        self.assertFalse(record["registered"])
        self.assertEqual((self.collector.completed, self.collector.pdf_count), (1, 1))
        pdf_path = self.root / "raw" / record["pdf_response"]["relative_path"]
        self.assertEqual(pdf_path.read_bytes(), self.responses[f"{ORIGIN}/articles/viewPDF/10"].body)

    def test_invalid_pdf_never_marks_article_completed(self) -> None:
        """Неверный MIME, HTML и обломок PDF не должны попадать в готовые статьи."""

        self._card(with_pdf=True)
        self.collector.prepare()
        cases = ((b"<html>Error</html>", "text/html"),
                 (b"not PDF", "application/pdf"), (b"%PDF-1.7\nbroken", "application/pdf"))

        for body, content_type in cases:
            with self.subTest(content_type=content_type, body=body):
                snapshot = _snapshot(f"{ORIGIN}/articles/viewPDF/10", body, content_type)
                self.archive.import_response(snapshot)

                with self.assertRaises(IoffeError):
                    self.collector.collect_article(f"{ORIGIN}/articles/10")

                self.assertFalse((self.root / "state/articles/10.json").exists())
                self.assertEqual(self.collector.completed, 0)

    def test_non_html_card_is_rejected(self) -> None:
        """JSON по адресу карточки не должен разбираться как HTML."""

        self._response("/articles/10", "{}", "application/json")
        self.collector.prepare()

        with self.assertRaisesRegex(IoffeError, "Вместо HTML"):
            self.collector.collect_article(f"{ORIGIN}/articles/10")

        self.assertEqual(self.collector.completed, 0)

    def test_journal_traversal_deduplicates_across_issues(self) -> None:
        """Нулевые лимиты обходят все явные выпуски без повтора общих карточек."""

        self._journal()
        self.collector.prepare()

        urls = list(self.collector.article_urls(journal_id=1, max_issues=0, max_articles=0))

        self.assertEqual(urls, [f"{ORIGIN}/articles/{article_id}" for article_id in (10, 20, 30)])
        self.assertEqual(self.client.get_response.call_count, 4)

    def test_article_limit_is_global_and_avoids_next_issue(self) -> None:
        """Общий лимит статей должен останавливать обход до запроса лишнего выпуска."""

        self._journal()
        self.collector.prepare()
        urls = list(self.collector.article_urls(journal_id=1, max_issues=0, max_articles=2))

        self.assertEqual(len(urls), 2)
        self.assertNotIn(f"{ORIGIN}/issues/200", [
            call.args[0] for call in self.client.get_response.call_args_list
        ])
        self.assertEqual(self.client.get_response.call_count, 3)

    def test_issue_limit_does_not_limit_articles_inside_selected_issue(self) -> None:
        """Один выбранный выпуск может содержать больше одной статьи."""

        self._journal()
        self.collector.prepare()
        urls = list(self.collector.article_urls(journal_id=1, max_issues=1, max_articles=0))

        self.assertEqual(urls, [f"{ORIGIN}/articles/10", f"{ORIGIN}/articles/20"])

    def test_invalid_identifiers_and_limits_fail_before_requests(self) -> None:
        """Булевы, отрицательные, чужие журналы и неоднозначный выбор запрещены."""

        cases = ({}, {"article_id": 0}, {"article_id": True}, {"issue_id": -1},
                 {"journal_id": 6}, {"article_id": 1, "issue_id": 2},
                 {"article_id": 1, "max_articles": -1},
                 {"article_id": 1, "max_issues": True})

        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                list(self.collector.article_urls(**arguments))

        self.client.get_response.assert_not_called()

    def test_empty_archive_and_issue_fail_explicitly(self) -> None:
        """Отсутствие ожидаемых ссылок нельзя выдавать за успешный пустой сбор."""

        self._response("/journals/1", "<html>Нет выпусков</html>")
        self._response("/issues/100", "<html>Нет статей</html>")
        self.collector.prepare()

        for arguments in ({"journal_id": 1}, {"issue_id": 100}):
            with self.subTest(arguments=arguments), self.assertRaises(IoffeError):
                list(self.collector.article_urls(**arguments))

    def test_saved_article_can_be_rebuilt_offline_without_new_files(self) -> None:
        """Повторный запуск использует проверенный кеш и сохраняет готовую запись."""

        self._card(with_pdf=True)
        self.collector.prepare()
        first = self.collector.collect_article(f"{ORIGIN}/articles/10")
        initial_files = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.client.get_response.reset_mock()
        self.archive.allow_network = False
        restarted = IoffeCollector(self.archive, self.root / "state", ORIGIN)
        restarted.prepare()
        second = restarted.collect_article(f"{ORIGIN}/articles/10")

        self.assertEqual(first, second)
        self.client.get_response.assert_not_called()
        self.assertEqual(initial_files, {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        })


if __name__ == "__main__":
    unittest.main()
