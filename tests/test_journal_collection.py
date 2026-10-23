"""Проверки общего обхода журналов без сети и настоящего ожидания."""

from __future__ import annotations

import io
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pymupdf

from src.collect.base import Document, HttpResponseSnapshot
from src.collect.ioffe_archive import IoffeArchive
from src.collect.ioffe_http import IoffeClient, IoffeError
from src.collect.journal_collection import JournalCollector, decode_html
from src.collect.journal_sources import JournalSource, get_source
from tests.test_jetp import ARTICLE_HTML as JETP_ARTICLE_HTML
from tests.test_quantum_electronics import (
    ARTICLE_HTML as QUANTUM_ARTICLE_HTML,
    ARTICLE_URL as QUANTUM_ARTICLE_URL,
    NESTED_ARTICLE_URL as QUANTUM_NESTED_ARTICLE_URL,
    OBITUARY_HTML,
    OBITUARY_URL,
    RELATIVE_ISSUE_URL as QUANTUM_RELATIVE_ISSUE_URL,
)

ORIGIN = "https://journal.example"
ARCHIVE = ORIGIN + "/archive"
ISSUE = ORIGIN + "/issue/1"
ARTICLE = ORIGIN + "/article/1"
PDF = ORIGIN + "/pdf/1.pdf"
ROBOTS = ORIGIN + "/robots.txt"


def _classify(url: str) -> str | None:
    """Узнавать только четыре адреса искусственного журнала."""

    return {ARCHIVE: "archive", ISSUE: "issue", ARTICLE: "article", PDF: "pdf"}.get(url)


def _links(html: str, url: str) -> list[str]:
    """Создать небольшой граф архива с обратной навигационной ссылкой."""

    return {ARCHIVE: [ISSUE], ISSUE: [ARCHIVE, ARTICLE]}.get(url, [])


def _article(html: str, url: str) -> Document:
    """Вернуть русскую карточку без подмены аннотации полным текстом."""

    return Document(source="journal.example", url=url, title="Физическая статья",
                    text="Краткая аннотация", pdf_url=PDF, extra={"content_role": "metadata_only"})


def _source() -> JournalSource:
    """Подготовить адаптер без импорта реальных сайтов."""

    module = ModuleType("fake_journal")

    for name, value in {
        "SOURCE_ID": "fake", "START_URL": ARCHIVE,
        "ALLOWED_ORIGINS": (ORIGIN,), "DEFAULT_ENCODING": "utf-8",
        "classify_url": _classify, "extract_links": _links, "parse_article": _article,
    }.items():
        setattr(module, name, value)

    return JournalSource(module)


def _snapshot(url: str, body: bytes, content_type: str, status: int = 200) -> HttpResponseSnapshot:
    """Создать полный ответ с фиксированными временем и заголовками."""

    return HttpResponseSnapshot(url, url, status, (("content-type", content_type),),
                                "2026-10-13T12:00:00+00:00", body)


class JournalCollectorTests(unittest.TestCase):
    """Проверять возобновление, robots.txt, отказ доступа и сохранение исходников."""

    def setUp(self) -> None:
        """Создать независимое хранилище и ответы маленького журнала."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = _source()
        self.client = MagicMock()
        self.client.delay_seconds = 60.0

        with pymupdf.open() as document:
            document.new_page()
            pdf = document.tobytes()

        self.responses = {
            ROBOTS: _snapshot(ROBOTS, b"User-agent: *\nDisallow: /private\n", "text/plain"),
            ARCHIVE: _snapshot(ARCHIVE, b"archive", "text/html; charset=utf-8"),
            ISSUE: _snapshot(ISSUE, b"issue", "text/html; charset=utf-8"),
            ARTICLE: _snapshot(ARTICLE, b"article", "text/html; charset=utf-8"),
            PDF: _snapshot(PDF, pdf, "application/pdf"),
        }
        self.client.get_response.side_effect = self.responses.__getitem__
        self.archive = self._archive(allow_network=True)
        stdout = patch("sys.stdout", new_callable=io.StringIO)
        stdout.start()
        self.addCleanup(stdout.stop)

    def _archive(self, *, allow_network: bool = False) -> IoffeArchive:
        """Собрать новое представление того же кеша с явным сетевым режимом."""

        return IoffeArchive(self.root / "raw", self.root / "state", self.client,
                             allow_network=allow_network, validate_url=self.source.validate_url,
                             accepted_statuses=self.source.accepted_statuses)

    def test_crawl_and_offline_resume(self) -> None:
        """Обратные ссылки не зацикливают обход, повтор не обращается к сети."""

        collector = JournalCollector(self.source, self.archive)
        collector.crawl()
        self.assertEqual(collector.completed, 1)
        self.assertEqual(collector.new_records, 1)
        self.assertEqual(collector.pdf_count, 1)
        self.assertEqual(self.client.get_response.call_count, 5)
        self.client.get_response.reset_mock()
        repeated = JournalCollector(self.source, self._archive())
        repeated.crawl()
        self.client.get_response.assert_not_called()
        self.assertEqual(repeated.new_records, 0)
        self.assertEqual(repeated.completed, 1)

    def test_no_network_without_flag(self) -> None:
        """Отсутствие исходников не включает сеть само по себе."""

        with self.assertRaises(IoffeError):
            JournalCollector(self.source, self._archive()).crawl()

        self.client.get_response.assert_not_called()

    def test_only_robots_may_return_404(self) -> None:
        """Явный 404 robots сохраняется и читается автономно; 404 статьи останавливает обход."""

        self.responses[ROBOTS] = _snapshot(ROBOTS, b"missing", "text/html", 404)
        self.responses[ARTICLE] = _snapshot(ARTICLE, b"missing", "text/html", 404)

        with self.assertRaisesRegex(IoffeError, "HTTP 404"):
            JournalCollector(self.source, self.archive).crawl()

        self.assertEqual(self._archive().get_response(ROBOTS).status_code, 404)
        self.assertNotIn(PDF, [call.args[0] for call in self.client.get_response.call_args_list])
        self.assertFalse((self.archive.state_dir / "articles").exists())

    def test_robots_forbidden_stops_before_archive(self) -> None:
        """HTTP 403 robots.txt не принимается за отсутствие правил."""

        self.responses[ROBOTS] = _snapshot(ROBOTS, b"forbidden", "text/html", 403)

        with self.assertRaisesRegex(IoffeError, "HTTP 403"):
            JournalCollector(self.source, self.archive).crawl()

        self.assertEqual(self.client.get_response.call_count, 1)

    def test_disallow_stops_before_forbidden_request(self) -> None:
        """Запрещённый PDF не запрашивается и не создаёт завершённую карточку."""

        self.responses[ROBOTS] = _snapshot(ROBOTS, b"User-agent: *\nDisallow: /pdf/\n", "text/plain")

        with self.assertRaisesRegex(IoffeError, "запрещён"):
            JournalCollector(self.source, self.archive).crawl()

        self.assertNotIn(PDF, [call.args[0] for call in self.client.get_response.call_args_list])

    def test_larger_crawl_delay_is_applied(self) -> None:
        """Более строгая пауза сайта передаётся устойчивому ограничителю."""

        self.responses[ROBOTS] = _snapshot(ROBOTS, b"User-agent: *\nCrawl-delay: 120\n", "text/plain")
        JournalCollector(self.source, self.archive).prepare()
        self.client.set_minimum_delay.assert_called_once_with(120.0)

    def test_robots_masks_fail_closed(self) -> None:
        """Неподдержанные маски не обходятся упрощённым RobotFileParser."""

        self.responses[ROBOTS] = _snapshot(ROBOTS, b"User-agent: *\nDisallow: /*.pdf$\n", "text/plain")

        with self.assertRaisesRegex(IoffeError, "Маски"):
            JournalCollector(self.source, self.archive).crawl()

        self.assertEqual(self.client.get_response.call_count, 1)

    def test_invalid_pdf_keeps_raw_but_no_completed_record(self) -> None:
        """Страница ошибки не выдаётся за сохранённый PDF."""

        self.responses[PDF] = _snapshot(PDF, b"login", "text/html")

        with self.assertRaisesRegex(IoffeError, "Вместо PDF"):
            JournalCollector(self.source, self.archive).crawl()

        self.assertEqual(self.archive.get_response(PDF).body, b"login")
        self.assertFalse((self.archive.state_dir / "articles").exists())

    def test_pdf_failure_resumes_from_cached_card(self) -> None:
        """После сбоя PDF карточка остаётся в кеше, новый проход получает только PDF и robots."""

        valid_pdf = self.responses[PDF]
        self.responses[PDF] = replace(valid_pdf, status_code=503)

        with self.assertRaises(IoffeError):
            JournalCollector(self.source, self.archive).crawl()

        self.responses[PDF] = valid_pdf
        self.client.get_response.reset_mock()
        collector = JournalCollector(self.source, self.archive)
        collector.crawl()
        self.assertEqual([call.args[0] for call in self.client.get_response.call_args_list], [ROBOTS, PDF])
        self.assertEqual(collector.pdf_count, 1)

    def test_working_rights_are_not_changed(self) -> None:
        """Скачанная статья ещё не зарегистрирована и не допущена к обучению."""

        record = JournalCollector(self.source, self.archive).collect_article(ARTICLE)
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")
        self.assertEqual(record["document"]["extra"]["content_role"], "metadata_only")

    def test_non_russian_card_is_saved_without_pdf_request(self) -> None:
        """Англоязычный материал в русском выпуске не скачивается как русский PDF."""

        document = _article("", ARTICLE)
        document.language = "en"

        with patch.object(self.source.module, "parse_article", return_value=document):
            record = JournalCollector(self.source, self.archive).collect_article(ARTICLE)

        self.assertEqual(record["status"], "skipped_non_russian")
        self.assertNotIn("pdf_response", record)
        self.assertNotIn(PDF, [call.args[0] for call in self.client.get_response.call_args_list])

    def test_metadata_only_article_does_not_invent_pdf(self) -> None:
        """Кнопка без подтверждённой ссылки PDF остаётся только метаданными."""

        document = _article("", ARTICLE)
        document.pdf_url = None

        with patch.object(self.source.module, "parse_article", return_value=document):
            record = JournalCollector(self.source, self.archive).collect_article(ARTICLE)

        self.assertEqual(record["status"], "metadata_only")
        self.assertNotIn(PDF, [call.args[0] for call in self.client.get_response.call_args_list])

    def test_quantum_obituary_keeps_metadata_crawl_running_without_pdf(self) -> None:
        """Подтверждённый некролог сохраняется отдельно от научной карточки без запроса PDF."""

        self.source = get_source("quantum")
        robots_url = "https://quantum-electronics.ru/robots.txt"
        issue_url = "https://quantum-electronics.ru/2025_55_10/"
        archive_html = f'<div class="entry-content"><a href="{issue_url}">10</a></div>'
        issue_html = (
            '<h1 class="entry-title">2025, том 55, номер 10</h1><div class="entry-content">'
            f'<p><a href="{OBITUARY_URL}">Памяти учёного</a></p>'
            f'<p><a href="{QUANTUM_ARTICLE_URL}">Исследование лазеров</a></p></div>'
        )
        responses = {
            robots_url: _snapshot(robots_url, b"User-agent: *\nDisallow:\n", "text/plain"),
            self.source.start_url: _snapshot(self.source.start_url, archive_html.encode(), "text/html"),
            issue_url: _snapshot(issue_url, issue_html.encode(), "text/html"),
            OBITUARY_URL: _snapshot(OBITUARY_URL, OBITUARY_HTML.encode(), "text/html"),
            QUANTUM_ARTICLE_URL: _snapshot(QUANTUM_ARTICLE_URL, QUANTUM_ARTICLE_HTML.encode(), "text/html"),
        }
        self.client.get_response.side_effect = responses.__getitem__
        archive = self._archive(allow_network=True)
        collector = JournalCollector(self.source, archive, download_pdfs=False)
        collector.crawl()

        self.assertEqual(collector.completed, 2)
        self.assertEqual(collector.pdf_count, 0)
        self.assertEqual(
            [call.args[0] for call in self.client.get_response.call_args_list],
            [robots_url, self.source.start_url, issue_url, OBITUARY_URL, QUANTUM_ARTICLE_URL],
        )
        records = [json.loads(path.read_text()) for path in (archive.state_dir / "metadata").glob("*.json")]
        obituary = next(record for record in records if record["document"]["url"] == OBITUARY_URL)
        self.assertEqual(obituary["status"], "metadata_only")
        self.assertEqual(obituary["document"]["section"], "Некролог")
        self.assertEqual(obituary["document"]["extra"]["publication_type"], "obituary")
        self.assertEqual(obituary["document"]["authors"], [])
        self.assertNotIn("pdf_response", obituary)
        self.assertFalse(obituary["registered"])
        self.assertEqual(obituary["training_eligibility"], "not_assessed")

        self.client.get_response.reset_mock()
        repeated = JournalCollector(self.source, self._archive(), download_pdfs=False)
        repeated.crawl()
        self.assertEqual(repeated.new_records, 0)
        self.client.get_response.assert_not_called()

    def test_quantum_obituary_does_not_request_pdf_in_full_collection_mode(self) -> None:
        """Новый разбор не разрешает PDF некролога даже без флага metadata-only."""

        self.source = get_source("quantum")
        robots_url = "https://quantum-electronics.ru/robots.txt"
        responses = {
            robots_url: _snapshot(robots_url, b"User-agent: *\nDisallow:\n", "text/plain"),
            OBITUARY_URL: _snapshot(OBITUARY_URL, OBITUARY_HTML.encode(), "text/html"),
        }
        self.client.get_response.side_effect = responses.__getitem__
        record = JournalCollector(self.source, self._archive(allow_network=True)).collect_article(OBITUARY_URL)

        self.assertEqual(record["status"], "metadata_only")
        self.assertIsNone(record["document"]["pdf_url"])
        self.assertNotIn("pdf_response", record)
        self.assertEqual([call.args[0] for call in self.client.get_response.call_args_list],
                         [robots_url, OBITUARY_URL])

    def test_quantum_relative_link_redirect_keeps_raw_evidence_and_offline_resume(self) -> None:
        """Вложенный href даёт одну каноническую карточку без PDF и повторной загрузки."""

        self.source = get_source("quantum")
        robots_url = "https://quantum-electronics.ru/robots.txt"
        archive_html = (
            '<div class="entry-content">'
            f'<a href="{QUANTUM_RELATIVE_ISSUE_URL}">1</a></div>'
        )
        issue_html = (
            '<h1 class="entry-title">2022, том 52, номер 1</h1><div class="entry-content">'
            '<p><a href="issledovanie-lazernyh-impulsov/">Исследование лазеров</a></p>'
            f'<p><a href="{QUANTUM_ARTICLE_URL}">Повтор карточки</a></p></div>'
        )
        redirect = replace(
            _snapshot(QUANTUM_NESTED_ARTICLE_URL, b"published redirect", "text/html", 301),
            headers=(("content-type", "text/html"), ("location", QUANTUM_ARTICLE_URL)),
        )
        responses = {
            robots_url: _snapshot(robots_url, b"User-agent: *\nDisallow:\n", "text/plain"),
            self.source.start_url: _snapshot(self.source.start_url, archive_html.encode(), "text/html"),
            QUANTUM_RELATIVE_ISSUE_URL: _snapshot(
                QUANTUM_RELATIVE_ISSUE_URL, issue_html.encode(), "text/html",
            ),
            QUANTUM_NESTED_ARTICLE_URL: redirect,
            QUANTUM_ARTICLE_URL: _snapshot(QUANTUM_ARTICLE_URL, QUANTUM_ARTICLE_HTML.encode(), "text/html"),
        }
        self.client.get_response.side_effect = responses.__getitem__
        archive = self._archive(allow_network=True)
        collector = JournalCollector(self.source, archive, download_pdfs=False)
        collector.crawl()

        self.assertEqual(collector.completed, 1)
        self.assertEqual(collector.new_records, 1)
        self.assertEqual(collector.pdf_count, 0)
        self.assertEqual(
            [call.args[0] for call in self.client.get_response.call_args_list],
            [robots_url, self.source.start_url, QUANTUM_RELATIVE_ISSUE_URL,
             QUANTUM_NESTED_ARTICLE_URL, QUANTUM_ARTICLE_URL],
        )
        paths = list((archive.state_dir / "metadata").glob("*.json"))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text())
        self.assertEqual(record["document"]["url"], QUANTUM_ARTICLE_URL)
        self.assertEqual(record["metadata_page_url"], QUANTUM_ARTICLE_URL)
        self.assertEqual(record["status"], "metadata_only")
        self.assertEqual(record["pdf_download_status"], "not_requested")
        self.assertNotIn("pdf_response", record)
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")

        saved_redirect = self._archive().get_response(QUANTUM_NESTED_ARTICLE_URL)
        self.assertEqual(saved_redirect.status_code, 301)
        self.assertEqual(saved_redirect.body, redirect.body)
        self.assertEqual(saved_redirect.headers, redirect.headers)
        saved_record = paths[0].read_bytes()
        self.client.get_response.reset_mock()
        repeated = JournalCollector(self.source, self._archive(), download_pdfs=False)
        repeated.crawl()

        self.assertEqual(repeated.completed, 1)
        self.assertEqual(repeated.new_records, 0)
        self.assertEqual(paths[0].read_bytes(), saved_record)
        self.client.get_response.assert_not_called()

    def test_quantum_plain_labels_keep_metadata_crawl_and_offline_resume(self) -> None:
        """Обычные подписи полей не останавливают обход и не включают скачивание PDF."""

        self.source = get_source("quantum")
        robots_url = "https://quantum-electronics.ru/robots.txt"
        issue_url = "https://quantum-electronics.ru/2024_54_1/"
        archive_html = f'<div class="entry-content"><a href="{issue_url}">1</a></div>'
        issue_html = (
            '<h1 class="entry-title">2024, том 54, номер 1</h1><div class="entry-content">'
            f'<p><a href="{QUANTUM_ARTICLE_URL}">Исследование лазеров</a></p>'
            f'<p><a href="{OBITUARY_URL}">Памяти учёного</a></p></div>'
        )
        article_html = QUANTUM_ARTICLE_HTML.replace(
            "<b>Аннотация:</b>", "Аннотация:",
        ).replace("<b>Образец цитирования:</b>", "Образец цитирования:")
        responses = {
            robots_url: _snapshot(robots_url, b"User-agent: *\nDisallow:\n", "text/plain"),
            self.source.start_url: _snapshot(self.source.start_url, archive_html.encode(), "text/html"),
            issue_url: _snapshot(issue_url, issue_html.encode(), "text/html"),
            QUANTUM_ARTICLE_URL: _snapshot(QUANTUM_ARTICLE_URL, article_html.encode(), "text/html"),
            OBITUARY_URL: _snapshot(OBITUARY_URL, OBITUARY_HTML.encode(), "text/html"),
        }
        self.client.get_response.side_effect = responses.__getitem__
        archive = self._archive(allow_network=True)
        collector = JournalCollector(self.source, archive, download_pdfs=False)
        collector.crawl()

        self.assertEqual(collector.completed, 2)
        self.assertEqual(collector.new_records, 2)
        self.assertEqual(collector.pdf_count, 0)
        self.assertEqual(
            [call.args[0] for call in self.client.get_response.call_args_list],
            [robots_url, self.source.start_url, issue_url, QUANTUM_ARTICLE_URL, OBITUARY_URL],
        )
        records = [json.loads(path.read_text()) for path in (archive.state_dir / "metadata").glob("*.json")]
        record = next(record for record in records if record["document"]["url"] == QUANTUM_ARTICLE_URL)
        self.assertEqual(record["document"]["text"], "Изучены лазеры. Сравнены два режима.")
        self.assertEqual(record["document"]["extra"]["year"], 2024)
        self.assertEqual(record["status"], "metadata_only")
        self.assertNotIn("pdf_response", record)
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")

        self.client.get_response.reset_mock()
        repeated = JournalCollector(self.source, self._archive(), download_pdfs=False)
        repeated.crawl()
        self.assertEqual(repeated.completed, 2)
        self.assertEqual(repeated.new_records, 0)
        self.client.get_response.assert_not_called()

    def test_quantum_relative_redirect_rejects_unrelated_target_before_request(self) -> None:
        """Неожиданный Location сохраняется как свидетельство, но не запрашивается."""

        self.source = get_source("quantum")
        robots_url = "https://quantum-electronics.ru/robots.txt"
        unexpected_url = "https://quantum-electronics.ru/another-article/"
        redirect = replace(
            _snapshot(QUANTUM_NESTED_ARTICLE_URL, b"wrong redirect", "text/html", 301),
            headers=(("location", unexpected_url),),
        )
        responses = {
            robots_url: _snapshot(robots_url, b"User-agent: *\nDisallow:\n", "text/plain"),
            QUANTUM_NESTED_ARTICLE_URL: redirect,
        }
        self.client.get_response.side_effect = responses.__getitem__
        archive = self._archive(allow_network=True)

        with self.assertRaises(ValueError):
            JournalCollector(self.source, archive, download_pdfs=False).collect_article(
                QUANTUM_NESTED_ARTICLE_URL,
            )

        self.assertEqual(
            [call.args[0] for call in self.client.get_response.call_args_list],
            [robots_url, QUANTUM_NESTED_ARTICLE_URL],
        )
        self.assertEqual(self._archive().get_response(QUANTUM_NESTED_ARTICLE_URL), redirect)
        self.assertFalse((archive.state_dir / "metadata").exists())

    def test_quantum_relative_redirect_does_not_follow_second_redirect(self) -> None:
        """После точного переноса корневая карточка по-прежнему должна вернуть 200."""

        self.source = get_source("quantum")
        robots_url = "https://quantum-electronics.ru/robots.txt"
        redirect = replace(
            _snapshot(QUANTUM_NESTED_ARTICLE_URL, b"redirect", "text/html", 301),
            headers=(("location", QUANTUM_ARTICLE_URL),),
        )
        second_redirect = replace(
            _snapshot(QUANTUM_ARTICLE_URL, b"second redirect", "text/html", 301),
            headers=(("location", "https://quantum-electronics.ru/another-article/"),),
        )
        responses = {
            robots_url: _snapshot(robots_url, b"User-agent: *\nDisallow:\n", "text/plain"),
            QUANTUM_NESTED_ARTICLE_URL: redirect,
            QUANTUM_ARTICLE_URL: second_redirect,
        }
        self.client.get_response.side_effect = responses.__getitem__
        archive = self._archive(allow_network=True)

        with self.assertRaisesRegex(IoffeError, "HTTP 301"):
            JournalCollector(self.source, archive, download_pdfs=False).collect_article(
                QUANTUM_NESTED_ARTICLE_URL,
            )

        self.assertEqual(
            [call.args[0] for call in self.client.get_response.call_args_list],
            [robots_url, QUANTUM_NESTED_ARTICLE_URL, QUANTUM_ARTICLE_URL],
        )
        self.assertFalse((archive.state_dir / "metadata").exists())

    def test_koi8_r_and_wrong_content_type(self) -> None:
        """Кодировка старого русского сайта читается строго, PDF не читается как HTML."""

        snapshot = _snapshot(ARTICLE, "Статья".encode("koi8-r"), "text/html; charset=koi8-r")
        self.assertEqual(decode_html(snapshot, "utf-8"), "Статья")

        with self.assertRaises(IoffeError):
            decode_html(self.responses[PDF], "utf-8")

    def test_invalid_urls_are_rejected_before_network(self) -> None:
        """Посторонние узлы, фрагменты и обходы пути не доходят до HTTP."""

        for url in (ARTICLE + "#", ARTICLE + "?token=secret", ORIGIN + "/../article/1",
                    ORIGIN + "/%61rticle/1", "https://evil.example/article/1",
                    "https://user@journal.example/article/1", ARTICLE + "\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.source.validate_url(url)

        self.client.get_response.assert_not_called()

    def test_custom_transport_policy_preserves_default_ioffe_guard(self) -> None:
        """Параметр адаптера не расширяет прежний клиент Иоффе по умолчанию."""

        with self.assertRaises(ValueError):
            IoffeClient(self.root / "rate").get_response(ARTICLE)

        client = IoffeClient(self.root / "rate", validate_url=self.source.validate_url, state_prefix="journal")

        with patch.object(client, "_get_locked_response", return_value=self.responses[ARTICLE]) as request:
            self.assertEqual(client.get_response(ARTICLE).requested_url, ARTICLE)
            request.assert_called_once_with(ARTICLE)

        self.assertEqual(urlsplit(ARTICLE).hostname, "journal.example")

    def test_issue_is_visited_before_sidebar_archive(self) -> None:
        """Боковая навигация по томам не отодвигает скачивание статей текущего выпуска."""

        another_archive = ORIGIN + "/other-archive"

        def classify(url: str) -> str | None:
            """Добавить одну страницу соседнего тома к тестовому сайту."""

            return "archive" if url == another_archive else _classify(url)

        def links(html: str, url: str) -> list[str]:
            """Разместить ссылку бокового меню перед ссылкой выпуска, как на сайте."""

            return [another_archive, ISSUE] if url == ARCHIVE else _links(html, url)

        with patch.object(self.source.module, "classify_url", side_effect=classify), \
                patch.object(self.source.module, "extract_links", side_effect=links):
            JournalCollector(self.source, self.archive).crawl(max_articles=1)

        visited = [call.args[0] for call in self.client.get_response.call_args_list]
        self.assertEqual(visited, [ROBOTS, ARCHIVE, ISSUE, ARTICLE, PDF])

    def test_direct_issue_pdf_has_toc_evidence_and_resumes_offline(self) -> None:
        """PDF из оглавления связан с его HTML, без выдуманной карточки публикации."""

        document = _article("", PDF)
        document.text = ""
        document.extra["metadata_source"] = "issue_toc"
        document.extra["issue_url"] = ISSUE

        with patch.object(self.source.module, "parse_issue_articles", create=True, return_value=[document]), \
                patch.object(self.source.module, "extract_links", return_value=[PDF]):
            collector = JournalCollector(self.source, self.archive)
            collector.crawl(start_url=ISSUE)
            self.assertEqual(collector.pdf_count, 1)
            self.assertEqual([call.args[0] for call in self.client.get_response.call_args_list], [ROBOTS, ISSUE, PDF])
            self.client.get_response.reset_mock()
            JournalCollector(self.source, self._archive()).crawl(start_url=ISSUE)
            self.client.get_response.assert_not_called()

        record = json.loads(next((self.archive.state_dir / "articles").glob("*.json")).read_text())
        self.assertEqual(record["metadata_page_url"], ISSUE)
        self.assertEqual(record["card_response"]["requested_url"], ISSUE)
        self.assertEqual(record["document"]["url"], PDF)

    def test_quantum_section_break_resumes_from_cached_issue_without_pdf_requests(self) -> None:
        """Исправленный разбор рубрики использует сохранённое оглавление и не запрашивает PDF."""

        self.source = get_source("quantum")
        robots_url = "https://quantum-electronics.ru/robots.txt"
        issue_url = "https://quantum-electronics.ru/2019_49_3/"
        archive_html = f'<div class="entry-content"><a href="{issue_url}">3</a></div>'
        issue_html = """
        <h1 class="entry-title">2019, том 49, номер 3</h1>
        <div class="entry-content">
        <p><strong>Управление параметрами лазерного излучения<br /></strong><br />
        <a href="/wp-content/uploads/2019/03/201.pdf">Исследование лазерных импульсов</a><br />
        А. А. Иванов, Б. Б. Петров <strong>201</strong></p>
        <p><a href="/wp-content/uploads/2019/03/209.pdf">Измерение спектра излучения</a><br />
        В. В. Сидоров <strong>209</strong></p></div>
        """
        self.responses = {
            robots_url: _snapshot(robots_url, b"User-agent: *\nDisallow:\n", "text/plain"),
            self.source.start_url: _snapshot(self.source.start_url, archive_html.encode(), "text/html"),
            issue_url: _snapshot(issue_url, issue_html.encode(), "text/html"),
        }
        self.client.get_response.side_effect = self.responses.__getitem__
        self.archive = self._archive(allow_network=True)

        with patch.object(self.source.module, "_split_issue_section", side_effect=ValueError("Ошибка рубрики")), \
                self.assertRaisesRegex(ValueError, "Ошибка рубрики"):
            JournalCollector(self.source, self.archive, download_pdfs=False).crawl()

        self.assertEqual(
            [call.args[0] for call in self.client.get_response.call_args_list],
            [robots_url, self.source.start_url, issue_url],
        )
        raw_before = {path: path.read_bytes() for path in self.archive.output_dir.rglob("*") if path.is_file()}
        self.client.get_response.reset_mock()
        collector = JournalCollector(self.source, self._archive(), download_pdfs=False)
        collector.crawl()
        self.client.get_response.assert_not_called()
        self.assertEqual((collector.completed, collector.new_records, collector.pdf_count), (2, 2, 0))
        self.assertEqual(raw_before, {
            path: path.read_bytes() for path in self.archive.output_dir.rglob("*") if path.is_file()
        })

        records = [
            json.loads(path.read_text()) for path in (self.archive.state_dir / "metadata").glob("*.json")
        ]
        self.assertEqual({record["document"]["extra"]["first_page"] for record in records}, {201, 209})

        for record in records:
            self.assertEqual(record["status"], "metadata_only")
            self.assertEqual(record["document"]["section"], "Управление параметрами лазерного излучения")
            self.assertEqual(record["card_response"]["requested_url"], issue_url)
            self.assertEqual(record["training_eligibility"], "not_assessed")
            self.assertFalse(record["registered"])
            self.assertNotIn("pdf_response", record)

        saved_files = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        repeated = JournalCollector(self.source, self._archive(), download_pdfs=False)
        repeated.crawl()
        self.client.get_response.assert_not_called()
        self.assertEqual((repeated.completed, repeated.new_records, repeated.pdf_count), (2, 0, 0))
        self.assertEqual(saved_files, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_quantum_empty_section_separator_resumes_without_pdf_requests(self) -> None:
        """Пустой выделенный перенос не мешает продолжению по кешу и не меняет исходники."""

        self.source = get_source("quantum")
        robots_url = "https://quantum-electronics.ru/robots.txt"
        issue_url = "https://quantum-electronics.ru/2012_42_12/"
        section = "Специальный выпуск, посвященный 90-летию Н.Г.Басова"
        archive_html = f'<div class="entry-content"><a href="{issue_url}">12</a></div>'
        issue_html = f"""
        <h1 class="entry-title">2012, том 42, номер 12</h1>
        <div class="entry-content">
        <p><strong>{section}</strong><br />
        <strong><br /></strong>
        <a href="/wp-content/uploads/2012/12/15079.pdf">Николай Геннадиевич Басов (фотография)</a></p>
        <p><a href="/wp-content/uploads/2012/12/15080.pdf">Исследование лазерных импульсов</a><br />
        А. А. Иванов, Б. Б. Петров <strong>1001</strong></p></div>
        """
        self.responses = {
            robots_url: _snapshot(robots_url, b"User-agent: *\nDisallow:\n", "text/plain"),
            self.source.start_url: _snapshot(self.source.start_url, archive_html.encode(), "text/html"),
            issue_url: _snapshot(issue_url, issue_html.encode(), "text/html"),
        }
        self.client.get_response.side_effect = self.responses.__getitem__
        self.archive = self._archive(allow_network=True)

        with patch.object(self.source.module, "_split_issue_section", side_effect=ValueError("Ошибка рубрики")), \
                self.assertRaisesRegex(ValueError, "Ошибка рубрики"):
            JournalCollector(self.source, self.archive, download_pdfs=False).crawl()

        self.assertEqual(
            [call.args[0] for call in self.client.get_response.call_args_list],
            [robots_url, self.source.start_url, issue_url],
        )
        raw_before = {path: path.read_bytes() for path in self.archive.output_dir.rglob("*") if path.is_file()}
        self.client.get_response.reset_mock()
        collector = JournalCollector(self.source, self._archive(), download_pdfs=False)
        collector.crawl()
        self.client.get_response.assert_not_called()
        self.assertEqual((collector.completed, collector.new_records, collector.pdf_count), (2, 2, 0))
        self.assertEqual(raw_before, {
            path: path.read_bytes() for path in self.archive.output_dir.rglob("*") if path.is_file()
        })

        records = [json.loads(path.read_text()) for path in (self.archive.state_dir / "metadata").glob("*.json")]
        documents = {record["document"]["title"]: record["document"] for record in records}
        photograph = documents["Николай Геннадиевич Басов (фотография)"]
        self.assertEqual(photograph["authors"], [])
        self.assertNotIn("first_page", photograph["extra"])
        self.assertEqual(documents["Исследование лазерных импульсов"]["authors"], ["А. А. Иванов", "Б. Б. Петров"])
        self.assertEqual(documents["Исследование лазерных импульсов"]["extra"]["first_page"], 1001)

        for record in records:
            self.assertEqual(record["status"], "metadata_only")
            self.assertEqual(record["document"]["section"], section)
            self.assertEqual(record["document"]["text"], "")
            self.assertEqual(record["card_response"]["requested_url"], issue_url)
            self.assertEqual(record["training_eligibility"], "not_assessed")
            self.assertFalse(record["registered"])
            self.assertNotIn("pdf_response", record)

        saved_files = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        repeated = JournalCollector(self.source, self._archive(), download_pdfs=False)
        repeated.crawl()
        self.client.get_response.assert_not_called()
        self.assertEqual((repeated.completed, repeated.new_records, repeated.pdf_count), (2, 0, 0))
        self.assertEqual(saved_files, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_explicit_refresh_repairs_semantic_error_without_losing_raw(self) -> None:
        """Явная перепроверка заменяет указатель ошибочного HTTP 200, сохраняя исходные байты."""

        valid_pdf = self.responses[PDF]
        self.responses[PDF] = _snapshot(PDF, b"temporary error", "text/html")

        with self.assertRaises(IoffeError):
            JournalCollector(self.source, self.archive).crawl()

        old_record = self.archive.evidence(PDF)
        self.responses[PDF] = valid_pdf
        self.client.get_response.reset_mock()
        collector = JournalCollector(self.source, self.archive, refresh_urls=(PDF,))
        collector.crawl()
        self.assertEqual(collector.pdf_count, 1)
        self.assertEqual([call.args[0] for call in self.client.get_response.call_args_list], [ROBOTS, PDF])
        self.assertEqual((self.archive.output_dir / old_record["relative_path"]).read_bytes(), b"temporary error")

    def test_refresh_requires_explicit_network_flag(self) -> None:
        """Адресное обновление не отменяет режим без сети."""

        with self.assertRaisesRegex(ValueError, "allow-network"):
            JournalCollector(self.source, self._archive(), refresh_urls=(PDF,))

    def test_metadata_mode_never_fetches_pdf_or_overwrites_full_record(self) -> None:
        """Сбор метаданных не обращается к PDF и не стирает прежнее полнотекстовое свидетельство."""

        JournalCollector(self.source, self.archive).collect_article(ARTICLE)
        full_path = next((self.archive.state_dir / "articles").glob("*.json"))
        full_record = full_path.read_bytes()
        self.client.get_response.reset_mock()
        collector = JournalCollector(self.source, self.archive, download_pdfs=False)
        record = collector.collect_article(ARTICLE)
        self.assertEqual(record["status"], "metadata_only")
        self.assertEqual(record["pdf_download_status"], "not_requested")
        self.assertEqual(full_path.read_bytes(), full_record)
        self.assertEqual(len(list((self.archive.state_dir / "metadata").glob("*.json"))), 1)
        self.assertEqual([call.args[0] for call in self.client.get_response.call_args_list], [ROBOTS])

    def test_metadata_evidence_matches_parsed_snapshot_during_refresh(self) -> None:
        """Обновление кеша между разбором и записью не подменяет источник метаданных."""

        original_parser = self.source.module.parse_article

        def parse_with_concurrent_update(html: str, url: str) -> Document:
            """Изобразить обновление карточки другим процессом после получения HTML."""

            self.archive.import_response(replace(self.responses[ARTICLE], body=b"new metadata"))

            return original_parser(html, url)

        with patch.object(self.source.module, "parse_article", side_effect=parse_with_concurrent_update):
            record = JournalCollector(self.source, self.archive).collect_article(ARTICLE)

        self.assertEqual((self.archive.output_dir / record["card_response"]["relative_path"]).read_bytes(), b"article")
        self.assertEqual(self.archive.get_response(ARTICLE).body, b"new metadata")

    def test_adapter_without_unlinked_issue_parser_is_unchanged(self) -> None:
        """Другие журналы не объявляют страницу без ссылок распознанным оглавлением."""

        self.assertEqual(self.source.parse_unlinked_issue("unknown", ISSUE), [])

        with patch.object(self.source.module, "extract_links", return_value=[]), \
                self.assertRaisesRegex(IoffeError, "Не найдены ссылки"):
            JournalCollector(self.source, self.archive).crawl(start_url=ISSUE)

        self.assertFalse((self.archive.state_dir / "deferred_issues").exists())

    def test_unlinked_issue_does_not_stop_remaining_articles(self) -> None:
        """Подтверждённое оглавление откладывается, но не мешает другим статьям архива."""

        entries = [{"title": "Отложенная статья", "authors": ["Иванов И. И."],
                    "first_page": 401, "section": "Лазеры"}]
        another_issue = ORIGIN + "/issue/2"
        self.responses[another_issue] = _snapshot(another_issue, b"another issue", "text/html; charset=utf-8")

        def classify(url: str) -> str | None:
            """Добавить соседний выпуск к проверенным адресам."""

            return "issue" if url == another_issue else _classify(url)

        def links(html: str, url: str) -> list[str]:
            """Поставить выпуск без ссылок перед выпуском с доступной статьёй."""

            return {ARCHIVE: [ISSUE, another_issue], another_issue: [ARTICLE]}.get(url, [])

        with patch.object(self.source.module, "parse_unlinked_issue", create=True, return_value=entries), \
                patch.object(self.source.module, "classify_url", side_effect=classify), \
                patch.object(self.source.module, "extract_links", side_effect=links):
            collector = JournalCollector(self.source, self.archive)
            collector.crawl()

        self.assertEqual(collector.completed, 1)
        self.assertEqual(collector.new_records, 1)
        self.assertEqual(collector.pdf_count, 1)
        self.assertEqual(collector.deferred_issues_count, 1)
        self.assertEqual(collector.deferred_entries_count, 1)
        self.assertEqual([call.args[0] for call in self.client.get_response.call_args_list],
                         [ROBOTS, ARCHIVE, ISSUE, another_issue, ARTICLE, PDF])
        record_path = self.archive.state_dir / collector.deferred_issues[ISSUE]["record_path"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "deferred_unlinked_toc")
        self.assertEqual(record["entries"], entries)
        self.assertEqual(record["card_response"], self.archive.evidence(ISSUE))
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")
        self.assertNotIn("pdf_response", record)
        self.assertNotIn("document", record)

    def test_deferred_issue_is_idempotent_and_does_not_request_pdf(self) -> None:
        """Повтор того же снимка не переписывает файл и не придумывает PDF."""

        entries = [{"title": "Статья без ссылки", "authors": ["Автор"], "first_page": 401, "section": None}]

        with patch.object(self.source.module, "parse_unlinked_issue", create=True, return_value=entries), \
                patch.object(self.source.module, "extract_links", return_value=[]):
            collector = JournalCollector(self.source, self.archive)
            collector.crawl(start_url=ISSUE)
            record_path = self.archive.state_dir / collector.deferred_issues[ISSUE]["record_path"]
            original_body = record_path.read_bytes()
            original_mtime = record_path.stat().st_mtime_ns
            self.assertEqual([call.args[0] for call in self.client.get_response.call_args_list], [ROBOTS, ISSUE])
            self.client.get_response.reset_mock()
            repeated = JournalCollector(self.source, self._archive())
            repeated.crawl(start_url=ISSUE)

        self.client.get_response.assert_not_called()
        self.assertEqual(record_path.read_bytes(), original_body)
        self.assertEqual(record_path.stat().st_mtime_ns, original_mtime)
        self.assertEqual(repeated.deferred_issues_count, 1)
        self.assertEqual(repeated.deferred_entries_count, 1)
        self.assertEqual(repeated.completed, 0)
        self.assertEqual(repeated.new_records, 0)
        self.assertEqual(repeated.pdf_count, 0)
        self.assertFalse((self.archive.state_dir / "articles").exists())

    def test_new_deferred_http_snapshot_retains_previous_version(self) -> None:
        """Обновлённое оглавление не затирает прежние записи и HTTP-свидетельства."""

        entries = [{"title": "Статья без ссылки", "authors": ["Автор"], "first_page": 401, "section": None}]

        with patch.object(self.source.module, "parse_unlinked_issue", create=True, return_value=entries), \
                patch.object(self.source.module, "extract_links", return_value=[]):
            collector = JournalCollector(self.source, self.archive)
            collector.crawl(start_url=ISSUE)
            original_path = self.archive.state_dir / collector.deferred_issues[ISSUE]["record_path"]
            original_body = original_path.read_bytes()
            self.responses[ISSUE] = replace(self.responses[ISSUE], body=b"updated issue")
            refreshed = JournalCollector(self.source, self.archive, refresh_urls=(ISSUE,))
            refreshed.crawl(start_url=ISSUE)

        refreshed_path = self.archive.state_dir / refreshed.deferred_issues[ISSUE]["record_path"]
        self.assertNotEqual(refreshed_path, original_path)
        self.assertEqual(original_path.read_bytes(), original_body)
        self.assertEqual(len(list((self.archive.state_dir / "deferred_issues").glob("*.json"))), 2)
        self.assertEqual(refreshed.deferred_issues_count, 1)
        self.assertEqual(refreshed.deferred_entries_count, 1)

    def test_unknown_issue_does_not_become_deferred(self) -> None:
        """Неизвестный HTML или пустой ответ адаптера по-прежнему останавливает обход."""

        with patch.object(self.source.module, "parse_unlinked_issue", create=True, return_value=[]), \
                patch.object(self.source.module, "extract_links", return_value=[]), \
                self.assertRaisesRegex(IoffeError, "Не найдены ссылки"):
            JournalCollector(self.source, self.archive).crawl(start_url=ISSUE)

        self.assertFalse((self.archive.state_dir / "deferred_issues").exists())

    def test_unlinked_archive_does_not_use_issue_fallback(self) -> None:
        """Послабление относится только к оглавлению, а не к сломанной странице архива."""

        with patch.object(self.source.module, "parse_unlinked_issue", create=True) as parser, \
                patch.object(self.source.module, "extract_links", return_value=[]), \
                self.assertRaisesRegex(IoffeError, "Не найдены ссылки"):
            JournalCollector(self.source, self.archive).crawl(start_url=ARCHIVE)

        parser.assert_not_called()

    def test_invalid_deferred_entries_are_rejected(self) -> None:
        """Ошибка адаптера не создаёт видимость проверенного оглавления."""

        with patch.object(self.source.module, "parse_unlinked_issue", create=True, return_value=[{"title": " "}]), \
                patch.object(self.source.module, "extract_links", return_value=[]), \
                self.assertRaisesRegex(IoffeError, "неполное описание"):
            JournalCollector(self.source, self.archive).crawl(start_url=ISSUE)

        self.assertFalse((self.archive.state_dir / "deferred_issues").exists())

    def test_deferred_evidence_pins_the_parsed_response(self) -> None:
        """Обновление кеша другим процессом не подменяет отложенное оглавление."""

        def parse_with_concurrent_update(html: str, url: str) -> list[dict[str, object]]:
            """Обновить указатель кеша после получения прежнего снимка."""

            self.archive.import_response(replace(self.responses[ISSUE], body=b"new issue"))

            return [{"title": "Прежняя статья", "authors": ["Автор"], "first_page": 401, "section": None}]

        with patch.object(self.source.module, "parse_unlinked_issue", create=True,
                          side_effect=parse_with_concurrent_update), \
                patch.object(self.source.module, "extract_links", return_value=[]):
            collector = JournalCollector(self.source, self.archive)
            collector.crawl(start_url=ISSUE)

        record_path = self.archive.state_dir / collector.deferred_issues[ISSUE]["record_path"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual((self.archive.output_dir / record["card_response"]["relative_path"]).read_bytes(), b"issue")
        self.assertEqual(self.archive.get_response(ISSUE).body, b"new issue")


class JetpVolumeCollectionTests(unittest.TestCase):
    """Проверки формы, границ тома и возобновления через настоящий адаптер ЖЭТФ."""

    def setUp(self) -> None:
        """Подготовить опубликованную форму, переход и одну русскую карточку."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = get_source("jetp")
        self.start_url = self.source.start_url
        self.volume_url = self.start_url + "/r/169?a=list"
        self.issue_url = self.start_url + "/r/169/4?a=list"
        self.article_url = self.start_url + "/r/169/4/p309?a=list"
        self.robots_url = "https://www.jetp.ras.ru/robots.txt"
        self.client = MagicMock()
        self.client.delay_seconds = 60.0
        form = '''<form action="/cgi-bin/r/index" method="POST">
        <input type="hidden" name="a" value="select">
        <input type="hidden" name="issues_lang" value="r">
        <select name="volume"><option value="169">Vol. 169, 2026</option></select>
        <input type="image" src="/img/r/go_vol.png"></form>'''
        volume_html = f'''<font class="issue">ЖЭТФ, Том 169, 2026</font>
        <a href="{self.start_url}/r/170?a=list">Текущий том</a>
        <a href="{self.issue_url}">Выпуск 4</a>'''
        issue_html = f'''<a href="{self.article_url}">Статья</a>
        <a href="{self.start_url}/r/170/4/p309?a=list">Другой том</a>
        <a href="{self.start_url}">Архив</a>'''
        self.responses = {
            self.robots_url: _snapshot(self.robots_url, b"User-agent: *\nDisallow:\n", "text/plain"),
            self.start_url: _snapshot(self.start_url, form.encode(), "text/html; charset=utf-8"),
            self.volume_url: _snapshot(self.volume_url, volume_html.encode(), "text/html; charset=utf-8"),
            self.issue_url: _snapshot(self.issue_url, issue_html.encode(), "text/html; charset=utf-8"),
            self.article_url: _snapshot(self.article_url, JETP_ARTICLE_HTML.replace("170", "169").encode(),
                                        "text/html; charset=utf-8"),
        }
        self.client.get_response.side_effect = self.responses.__getitem__
        self.redirect = HttpResponseSnapshot(
            self.start_url, self.start_url, 302,
            (("location", "/cgi-bin/r/index/r/169?a=list"),),
            "2026-10-15T12:00:00+00:00", b"",
        )
        self.client.post_form_response.return_value = self.redirect
        self.archive = self._archive(allow_network=True)
        stdout = patch("sys.stdout", new_callable=io.StringIO)
        stdout.start()
        self.addCleanup(stdout.stop)

    def _archive(self, *, allow_network: bool = False) -> IoffeArchive:
        """Переоткрыть локальный архив с явным разрешением на сеть."""

        return IoffeArchive(self.root / "raw", self.root / "state", self.client,
                             allow_network=allow_network, validate_url=self.source.validate_url,
                             accepted_statuses=self.source.accepted_statuses)

    def test_volume_scope_and_offline_repeat(self) -> None:
        """Обход не уходит в другой том, POST не подменяет GET, повтор использует кеш."""

        collector = JournalCollector(self.source, self.archive, download_pdfs=False)
        collector.crawl(volume=169)
        self.assertEqual(collector.completed, 1)
        self.assertEqual(collector.new_records, 1)
        self.assertEqual(collector.pdf_count, 0)
        self.client.post_form_response.assert_called_once_with(
            self.start_url, b"a=select&issues_lang=r&volume=169",
        )
        self.assertEqual([call.args[0] for call in self.client.get_response.call_args_list], [
            self.robots_url, self.start_url, self.volume_url, self.issue_url, self.article_url,
        ])
        self.assertEqual(self.archive.get_response(self.start_url), self.responses[self.start_url])
        self.client.reset_mock()
        repeated = JournalCollector(self.source, self._archive(), download_pdfs=False)
        repeated.crawl(volume=169)
        self.client.get_response.assert_not_called()
        self.client.post_form_response.assert_not_called()
        self.assertEqual(repeated.completed, 1)
        self.assertEqual(repeated.new_records, 0)

    def test_wrong_volume_heading_stops_before_articles(self) -> None:
        """Правильный адрес с заголовком другого тома не становится источником статей."""

        snapshot = self.responses[self.volume_url]
        self.responses[self.volume_url] = replace(snapshot, body=snapshot.body.replace(b"169", b"170"))

        with self.assertRaises(ValueError):
            JournalCollector(self.source, self.archive).crawl(volume=169)

        self.assertNotIn(self.issue_url, [call.args[0] for call in self.client.get_response.call_args_list])

    def test_interruption_after_form_resumes_without_second_post(self) -> None:
        """Сохранённый переход остаётся пригодным после остановки перед загрузкой тома."""

        def interrupted_response(url: str) -> HttpResponseSnapshot:
            """Остановить обход непосредственно после сохранения ответа формы."""

            if url == self.volume_url:
                raise KeyboardInterrupt

            return self.responses[url]

        self.client.get_response.side_effect = interrupted_response

        with self.assertRaises(KeyboardInterrupt):
            JournalCollector(self.source, self.archive, download_pdfs=False).crawl(volume=169)

        self.client.post_form_response.assert_called_once()
        self.client.reset_mock()
        self.client.get_response.side_effect = self.responses.__getitem__
        collector = JournalCollector(self.source, self.archive, download_pdfs=False)
        collector.crawl(volume=169)
        self.client.post_form_response.assert_not_called()
        self.assertEqual(collector.completed, 1)

    def test_refreshing_form_also_refreshes_its_post(self) -> None:
        """Явное обновление главной страницы повторяет POST, но не загрузку карточек."""

        JournalCollector(self.source, self.archive, download_pdfs=False).crawl(volume=169)
        self.client.reset_mock()
        collector = JournalCollector(self.source, self.archive, download_pdfs=False,
                                     refresh_urls=(self.start_url,))
        collector.crawl(volume=169)
        self.client.post_form_response.assert_called_once()
        self.assertEqual([call.args[0] for call in self.client.get_response.call_args_list], [
            self.robots_url, self.start_url,
        ])
        self.assertEqual(collector.new_records, 0)

    def test_unpublished_volume_is_not_sent(self) -> None:
        """Номер отсутствующего в форме тома не отправляется на сервер."""

        with self.assertRaisesRegex(IoffeError, "отсутствует"):
            JournalCollector(self.source, self.archive).crawl(volume=168)

        self.client.post_form_response.assert_not_called()

    def test_forbidden_form_stops_before_post(self) -> None:
        """Правила robots.txt действуют и на форму выбора тома."""

        self.responses[self.robots_url] = _snapshot(
            self.robots_url, b"User-agent: *\nDisallow: /cgi-bin/r/index\n", "text/plain",
        )

        with self.assertRaisesRegex(IoffeError, "запрещён"):
            JournalCollector(self.source, self.archive).crawl(volume=169)

        self.client.post_form_response.assert_not_called()
        self.assertEqual(self.client.get_response.call_count, 1)

    def test_target_robots_checked_after_form(self) -> None:
        """Разрешённая форма не отменяет запрет доступа к целевому тому."""

        self.responses[self.robots_url] = _snapshot(
            self.robots_url, b"User-agent: *\nDisallow: /cgi-bin/r/index/r/169\n", "text/plain",
        )

        with self.assertRaisesRegex(IoffeError, "запрещён"):
            JournalCollector(self.source, self.archive).crawl(volume=169)

        self.client.post_form_response.assert_called_once()
        self.assertNotIn(self.volume_url, [call.args[0] for call in self.client.get_response.call_args_list])

    def test_invalid_mode_is_rejected_before_network(self) -> None:
        """Неверные аргументы и чужой источник отклоняются до первого запроса."""

        invalid_volumes: tuple[Any, ...] = (0, -1, True, "169")

        for volume in invalid_volumes:
            with self.subTest(volume=volume), self.assertRaises(ValueError):
                JournalCollector(self.source, self.archive).crawl(volume=volume)

        with self.assertRaises(ValueError):
            JournalCollector(self.source, self.archive).crawl(volume=169, start_url=self.volume_url)

        with self.assertRaises(ValueError):
            JournalCollector(_source(), self.archive).crawl(volume=169)

        self.client.get_response.assert_not_called()
        self.client.post_form_response.assert_not_called()
