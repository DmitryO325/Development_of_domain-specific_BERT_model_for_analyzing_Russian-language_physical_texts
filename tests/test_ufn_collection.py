"""Автономные проверки сохранения исходников УФН и ограниченного запуска команды."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest

from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pymupdf

from scripts.download_journals import JournalClient, build_parser, main
from src.collect.base import HttpResponseSnapshot
from src.collect.ioffe_archive import IoffeArchive
from src.collect.ioffe_http import IoffeError
from src.collect.journal_collection import JournalCollector
from src.collect.journal_sources import get_source
from src.collect.ufn_journal import parse_article
from tests.test_ufn_journal import ARTICLE_HTML, ARTICLE_URL, PDF_URL, SUBSCRIPTION_HTML

ORIGIN = "https://ufn.ru"
ARCHIVE_URL = ORIGIN + "/ru/articles/"
YEAR_URL = ARCHIVE_URL + "2026/"
ISSUE_URL = YEAR_URL + "8/"
NEXT_ARTICLE_URL = ISSUE_URL + "b/"
ROBOTS_URL = ORIGIN + "/robots.txt"


def _snapshot(url: str, body: bytes, content_type: str, status: int = 200) -> HttpResponseSnapshot:
    """Создать ответ УФН с фиксированным временем, не обращаясь к сайту."""

    return HttpResponseSnapshot(url, url, status, (("content-type", content_type),),
                                "2026-10-16T00:00:00+00:00", body)


def _html_response(url: str, html: str) -> HttpResponseSnapshot:
    """Передать исходные байты UTF-8 с кодировкой по умолчанию текущего сайта."""

    return _snapshot(url, html.encode("utf-8"), "text/html")


class UfnCollectionTests(unittest.TestCase):
    """Проверять настоящий адаптер УФН с общим архивом и подменённым транспортом."""

    def setUp(self) -> None:
        """Подготовить временное хранилище и маленький граф архива УФН."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = get_source("ufn")
        self.client = MagicMock()
        self.client.delay_seconds = 60.0

        with pymupdf.open() as document:
            document.new_page()
            pdf = document.tobytes()

        self.responses = {
            ROBOTS_URL: _snapshot(ROBOTS_URL, b"User-agent: *\nDisallow: /private/\n", "text/plain"),
            ARCHIVE_URL: _html_response(ARCHIVE_URL, f'<a href="{YEAR_URL}">2026</a>'),
            YEAR_URL: _html_response(YEAR_URL, f'<a href="{ISSUE_URL}">Август</a>'),
            ISSUE_URL: _html_response(ISSUE_URL, (
                f'<a href="{YEAR_URL}">2026</a><a href="{ARTICLE_URL}">Статья</a>'
            )),
            ARTICLE_URL: _html_response(ARTICLE_URL, ARTICLE_HTML),
            PDF_URL: _snapshot(PDF_URL, pdf, "application/pdf"),
        }
        self.client.get_response.side_effect = self.responses.__getitem__
        self.archive = self._archive(allow_network=True)

        for patcher in (
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _archive(self, *, allow_network: bool = False) -> IoffeArchive:
        """Открыть тот же локальный кеш с явно выбранным разрешением запросов."""

        return IoffeArchive(self.root / "raw", self.root / "state", self.client,
                             allow_network=allow_network, validate_url=self.source.validate_url,
                             accepted_statuses=self.source.accepted_statuses)

    def _record(self) -> dict[str, Any]:
        """Прочитать единственную завершённую карточку временной партии."""

        paths = list((self.root / "state" / "articles").glob("*.json"))
        self.assertEqual(len(paths), 1)

        return json.loads(paths[0].read_text(encoding="utf-8"))

    def _requested_urls(self) -> list[str]:
        """Вернуть только адреса, которые дошли до подменённого транспорта."""

        return [call.args[0] for call in self.client.get_response.call_args_list]

    def test_crawl_preserves_exact_html_pdf_and_checksums(self) -> None:
        """Карточка связывает неизменённые HTML и PDF с проверяемыми HTTP-свидетельствами."""

        collector = JournalCollector(self.source, self.archive)
        collector.crawl()
        self.assertEqual(self._requested_urls(), [
            ROBOTS_URL, ARCHIVE_URL, YEAR_URL, ISSUE_URL, ARTICLE_URL, PDF_URL,
        ])
        self.assertEqual((collector.completed, collector.new_records, collector.pdf_count), (1, 1, 1))
        record = self._record()
        self.assertEqual(record["source_id"], "ufn")
        self.assertEqual(record["status"], "pdf_downloaded")
        self.assertEqual(record["page_count"], 1)
        self.assertEqual(record["document"]["url"], ARTICLE_URL)
        self.assertEqual(record["document"]["pdf_url"], PDF_URL)

        for key, url in (("card_response", ARTICLE_URL), ("pdf_response", PDF_URL)):
            with self.subTest(evidence=key):
                evidence = record[key]
                body = (self.archive.output_dir / evidence["relative_path"]).read_bytes()
                metadata = (self.archive.output_dir / evidence["response_metadata_path"]).read_bytes()
                self.assertEqual(body, self.responses[url].body)
                self.assertEqual(evidence["bytes"], len(body))
                self.assertEqual(evidence["sha256"], hashlib.sha256(body).hexdigest())
                self.assertEqual(evidence["response_metadata_sha256"], hashlib.sha256(metadata).hexdigest())

    def test_offline_resume_keeps_files_without_requests_or_duplicates(self) -> None:
        """Повторный автономный обход читает кеш и не изменяет сохранённые результаты."""

        JournalCollector(self.source, self.archive).crawl()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.client.get_response.reset_mock()
        collector = JournalCollector(self.source, self._archive())
        collector.crawl()

        self.client.get_response.assert_not_called()
        self.assertEqual((collector.new_records, collector.completed), (0, 1))
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_network_resume_refreshes_only_robots(self) -> None:
        """Сетевой повтор проверяет актуальные правила, но не скачивает готовый PDF ещё раз."""

        JournalCollector(self.source, self.archive).crawl()
        self.client.get_response.reset_mock()
        collector = JournalCollector(self.source, self._archive(allow_network=True))
        collector.crawl()

        self.assertEqual(self._requested_urls(), [ROBOTS_URL])
        self.assertEqual(collector.new_records, 0)

    def test_cached_metadata_record_gains_explicit_shifted_pdf_on_resume(self) -> None:
        """Повтор дополняет прежнюю карточку PDF без перезагрузки и изменения исходного HTML."""

        shifted_pdf_url = PDF_URL.replace("r268a.pdf", "r268z.pdf")
        self.responses[ARTICLE_URL] = _html_response(ARTICLE_URL, ARTICLE_HTML.replace("r268a.pdf", "r268z.pdf"))
        self.responses[shifted_pdf_url] = replace(
            self.responses[PDF_URL], requested_url=shifted_pdf_url, final_url=shifted_pdf_url,
        )
        del self.responses[PDF_URL]

        # Воспроизводим прежний результат узкого парсера, сохраняя настоящий HTML
        # с явной ссылкой, а не создавая новую или исправленную страницу источника.
        with patch("src.collect.ufn_journal._article_pdf", return_value=None):
            previous = JournalCollector(self.source, self.archive).collect_article(ARTICLE_URL)

        self.assertEqual(previous["status"], "metadata_only")
        self.assertIsNone(previous["document"]["pdf_url"])
        original_files = {path: path.read_bytes() for path in self.archive.output_dir.rglob("*") if path.is_file()}
        self.client.get_response.reset_mock()
        collector = JournalCollector(self.source, self._archive(allow_network=True))
        record = collector.collect_article(ARTICLE_URL)

        self.assertEqual(self._requested_urls(), [ROBOTS_URL, shifted_pdf_url])
        self.assertEqual(record["status"], "pdf_downloaded")
        self.assertEqual(record["document"]["pdf_url"], shifted_pdf_url)
        self.assertEqual(record["card_response"], previous["card_response"])
        self.assertEqual(record["page_count"], 1)
        self.assertEqual(collector.new_records, 0)
        self.assertEqual(collector.pdf_count, 1)
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")
        self.assertEqual(original_files, {path: path.read_bytes() for path in original_files})
        self.assertEqual(self._record(), record)

        self.client.get_response.reset_mock()
        repeated = JournalCollector(self.source, self._archive()).collect_article(ARTICLE_URL)
        self.client.get_response.assert_not_called()
        self.assertEqual(repeated, record)

    def test_cached_old_pdf_links_are_completed_on_resume(self) -> None:
        """Новые форматы дополняют прежнюю карточку PDF, сохраняя HTML и автономный повтор."""

        temporary_root = self.root
        cases = (
            ("short_directory", "/ru/articles/2017/12/a/", "/ufn17/ufn17_12/Russian/r1712a.pdf"),
            ("full_filename", "/ru/articles/2018/3/a/", "/ufn2018/ufn2018_3/Russian/r20183a.pdf"),
        )

        for case_name, article_path, pdf_path in cases:
            with self.subTest(format=case_name):
                self.root = temporary_root / case_name
                self.archive = self._archive(allow_network=True)
                article_url = ORIGIN + article_path
                pdf_url = ORIGIN + pdf_path
                html = ARTICLE_HTML.replace(ARTICLE_URL, article_url).replace(
                    PDF_URL, pdf_url,
                ).replace(urlsplit(PDF_URL).path, pdf_path)
                self.responses[article_url] = _html_response(article_url, html)
                self.responses[pdf_url] = replace(
                    self.responses[PDF_URL], requested_url=pdf_url, final_url=pdf_url,
                )
                self.client.get_response.reset_mock()

                # Старый отбор действительно не понимал эти ссылки; исходный HTML
                # сохраняем тем же сборщиком, не подменяя результат разбора вручную.
                with patch("src.collect.ufn_journal.parse_article", side_effect=partial(
                    parse_article, legacy_pdf_links=True,
                )):
                    previous = JournalCollector(self.source, self.archive).collect_article(article_url)

                self.assertEqual(self._requested_urls(), [ROBOTS_URL, article_url])
                self.assertEqual(previous["status"], "metadata_only")
                self.assertIsNone(previous["document"]["pdf_url"])
                self.assertNotIn("pdf_response", previous)
                original_files = {
                    path: path.read_bytes()
                    for path in self.archive.output_dir.rglob("*") if path.is_file()
                }
                self.client.get_response.reset_mock()
                collector = JournalCollector(self.source, self._archive(allow_network=True))
                record = collector.collect_article(article_url)

                self.assertEqual(self._requested_urls(), [ROBOTS_URL, pdf_url])
                self.assertEqual(record["status"], "pdf_downloaded")
                self.assertEqual(record["document"]["pdf_url"], pdf_url)
                self.assertEqual(record["card_response"], previous["card_response"])
                self.assertEqual(record["pdf_response"]["requested_url"], pdf_url)
                self.assertEqual(record["page_count"], 1)
                self.assertEqual((collector.new_records, collector.completed, collector.pdf_count), (0, 1, 1))
                self.assertFalse(record["registered"])
                self.assertEqual(record["training_eligibility"], "not_assessed")
                self.assertEqual(original_files, {path: path.read_bytes() for path in original_files})
                self.assertEqual(self._record(), record)

                before_repeat = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                self.client.get_response.reset_mock()
                repeated = JournalCollector(self.source, self._archive()).collect_article(article_url)

                self.client.get_response.assert_not_called()
                self.assertEqual(repeated, record)
                self.assertEqual(before_repeat, {
                    path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
                })

    def test_conflicting_explicit_pdfs_stop_before_download(self) -> None:
        """Конфликт метаполя и кнопки не приводит к загрузке произвольно выбранного PDF."""

        html = ARTICLE_HTML.replace(
            'href="/ufn2026/ufn2026_8/Russian/r268a.pdf"',
            'href="/ufn2026/ufn2026_8/Russian/r268z.pdf"',
        )
        self.responses[ARTICLE_URL] = _html_response(ARTICLE_URL, html)

        with self.assertRaisesRegex(ValueError, "разные русские PDF"):
            JournalCollector(self.source, self.archive).collect_article(ARTICLE_URL)

        self.assertEqual(self._requested_urls(), [ROBOTS_URL, ARTICLE_URL])
        self.assertFalse((self.archive.state_dir / "articles").exists())

    def test_missing_pdf_link_stays_metadata_only(self) -> None:
        """Путь статьи не используется для угадывания отсутствующей ссылки на PDF."""

        html = ARTICLE_HTML.replace(PDF_URL, "#").replace(urlsplit(PDF_URL).path, "#")
        self.responses[ARTICLE_URL] = _html_response(ARTICLE_URL, html)
        record = JournalCollector(self.source, self.archive).collect_article(ARTICLE_URL)

        self.assertEqual(record["status"], "metadata_only")
        self.assertIsNone(record["document"]["pdf_url"])
        self.assertNotIn("pdf_response", record)
        self.assertEqual(self._requested_urls(), [ROBOTS_URL, ARTICLE_URL])

    def test_subscription_card_is_saved_and_crawl_continues_without_its_pdf(self) -> None:
        """Подписная карточка не блокирует следующую статью и автономное продолжение."""

        next_pdf_url = PDF_URL.replace("r268a.pdf", "r268b.pdf")
        self.responses[ISSUE_URL] = _html_response(ISSUE_URL, (
            f'<a href="{ARTICLE_URL}">Подписная</a><a href="{NEXT_ARTICLE_URL}">Открытая</a>'
        ))
        self.responses[ARTICLE_URL] = _html_response(ARTICLE_URL, SUBSCRIPTION_HTML)
        self.responses[NEXT_ARTICLE_URL] = _html_response(
            NEXT_ARTICLE_URL, ARTICLE_HTML.replace("r268a.pdf", "r268b.pdf"),
        )
        self.responses[next_pdf_url] = replace(
            self.responses.pop(PDF_URL), requested_url=next_pdf_url, final_url=next_pdf_url,
        )

        with self.assertLogs("src.collect.ufn_journal", "WARNING"):
            collector = JournalCollector(self.source, self.archive)
            collector.crawl()

        self.assertEqual(self._requested_urls(), [
            ROBOTS_URL, ARCHIVE_URL, YEAR_URL, ISSUE_URL, ARTICLE_URL, NEXT_ARTICLE_URL, next_pdf_url,
        ])
        self.assertEqual((collector.completed, collector.new_records, collector.pdf_count), (2, 2, 1))
        records = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.archive.state_dir / "articles").glob("*.json")
        ]
        record = next(record for record in records if record["document"]["url"] == ARTICLE_URL)
        self.assertEqual(record["status"], "metadata_only")
        self.assertIsNone(record["document"]["pdf_url"])
        self.assertEqual(record["document"]["extra"]["pdf_access_status"], "subscription_required")
        self.assertEqual(record["document"]["extra"]["pdf_access_reason"], "Полный текст предоставляется по подписке")
        self.assertNotIn("pdf_response", record)
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")
        self.assertEqual(
            (self.archive.output_dir / record["card_response"]["relative_path"]).read_bytes(),
            self.responses[ARTICLE_URL].body,
        )
        original_files = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.client.get_response.reset_mock()

        with self.assertLogs("src.collect.ufn_journal", "WARNING"):
            resumed = JournalCollector(self.source, self._archive())
            resumed.crawl()

        self.client.get_response.assert_not_called()
        self.assertEqual((resumed.completed, resumed.new_records, resumed.pdf_count), (2, 0, 1))
        self.assertEqual(original_files, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_subscription_resume_preserves_previous_pdf_redirect_without_retry(self) -> None:
        """После прежнего отказа PDF читается кеш карточки, а перенаправление остаётся свидетельством."""

        self.responses[ARTICLE_URL] = _html_response(ARTICLE_URL, SUBSCRIPTION_HTML)
        self.responses[PDF_URL] = replace(
            _snapshot(PDF_URL, b"subscription redirect", "text/html", 302),
            headers=(("content-type", "text/html"), ("location", ARTICLE_URL)),
        )

        with patch("src.collect.ufn_journal._article_requires_subscription", return_value=False), \
                self.assertRaisesRegex(IoffeError, "HTTP 302"):
            JournalCollector(self.source, self.archive).collect_article(ARTICLE_URL)

        original_files = {
            path: path.read_bytes() for path in self.archive.output_dir.rglob("*") if path.is_file()
        }
        self.client.get_response.reset_mock()

        with self.assertLogs("src.collect.ufn_journal", "WARNING"):
            record = JournalCollector(self.source, self._archive()).collect_article(ARTICLE_URL)

        self.client.get_response.assert_not_called()
        self.assertEqual(record["status"], "metadata_only")
        self.assertEqual(record["document"]["extra"]["pdf_access_status"], "subscription_required")
        self.assertNotIn("pdf_response", record)
        self.assertEqual(original_files, {path: path.read_bytes() for path in original_files})
        self.assertIn(b"subscription redirect", original_files.values())

    def test_explicit_legacy_charset_is_preserved(self) -> None:
        """Старая кодировка из HTTP-заголовка читается без изменения исходных байтов."""

        self.responses[ARTICLE_URL] = _snapshot(
            ARTICLE_URL, ARTICLE_HTML.encode("windows-1251", errors="xmlcharrefreplace"),
            "text/html; charset=windows-1251",
        )
        record = JournalCollector(self.source, self.archive).collect_article(ARTICLE_URL)
        body_path = self.archive.output_dir / record["card_response"]["relative_path"]

        self.assertEqual(body_path.read_bytes(), self.responses[ARTICLE_URL].body)
        self.assertEqual(record["status"], "pdf_downloaded")
        self.assertEqual(record["document"]["language"], "ru")

    def test_broken_seo_keywords_warn_without_changing_original_response(self) -> None:
        """Повреждённый SEO-тег исключается только при разборе, оставляя исходник и предупреждение."""

        body = (
            b'<html><head><meta name="keywords" content="broken \xd0"></head><body>'
            + self.responses[ISSUE_URL].body + b"</body></html>"
        )
        snapshot = _snapshot(ISSUE_URL, body, "text/html; charset=utf-8")
        self.responses[ISSUE_URL] = snapshot

        with self.assertLogs("src.collect.ufn_journal", level="WARNING") as captured:
            collector = JournalCollector(self.source, self.archive)
            collector.crawl()

        self.assertEqual(collector.pdf_count, 1)
        self.assertTrue(any("SEO-meta keywords" in message for message in captured.output))
        evidence = self.archive.evidence_for(snapshot)
        self.assertEqual((self.archive.output_dir / evidence["relative_path"]).read_bytes(), body)
        self.assertEqual(evidence["sha256"], hashlib.sha256(body).hexdigest())

    def test_broken_visible_utf8_stops_before_article_and_keeps_original(self) -> None:
        """Повреждение видимого текста не скрывается под исправлением служебного SEO-тега."""

        body = (
            b'<html><head><meta name="keywords" content="valid"></head><body>broken \xd0'
            + self.responses[ISSUE_URL].body + b"</body></html>"
        )
        snapshot = _snapshot(ISSUE_URL, body, "text/html; charset=utf-8")
        self.responses[ISSUE_URL] = snapshot

        with self.assertRaisesRegex(IoffeError, "не соответствует кодировке utf-8"):
            JournalCollector(self.source, self.archive).crawl()

        self.assertNotIn(ARTICLE_URL, self._requested_urls())
        self.assertNotIn(PDF_URL, self._requested_urls())
        evidence = self.archive.evidence_for(snapshot)
        self.assertEqual((self.archive.output_dir / evidence["relative_path"]).read_bytes(), body)

    def test_robots_disallow_stops_before_pdf(self) -> None:
        """Запрет PDF в robots.txt не обходит ни новая, ни сохранённая карточка."""

        self.responses[ROBOTS_URL] = _snapshot(
            ROBOTS_URL, b"User-agent: *\nDisallow: /ufn2026/\n", "text/plain",
        )

        with self.assertRaisesRegex(IoffeError, "запрещён"):
            JournalCollector(self.source, self.archive).crawl()

        self.assertNotIn(PDF_URL, self._requested_urls())
        self.assertFalse((self.root / "state" / "articles").exists())

    def test_http_forbidden_and_rate_limit_stop_before_next_article(self) -> None:
        """HTTP 403 и 429 сохраняются без повторов и без перехода к следующей статье."""

        self.responses[ISSUE_URL] = _html_response(ISSUE_URL, (
            f'<a href="{ARTICLE_URL}">Первая</a><a href="{NEXT_ARTICLE_URL}">Вторая</a>'
        ))

        for status in (403, 429):
            with self.subTest(status=status):
                self.responses[PDF_URL] = _snapshot(PDF_URL, b"access denied", "text/html", status)
                self.client.get_response.reset_mock()

                with self.assertRaisesRegex(IoffeError, f"HTTP {status}"):
                    JournalCollector(self.source, self.archive).crawl()

                requested = self._requested_urls()
                self.assertEqual(requested.count(PDF_URL), 1)
                self.assertNotIn(NEXT_ARTICLE_URL, requested)
                self.assertFalse((self.root / "state" / "articles").exists())
                self.assertIn(b"access denied", [
                    path.read_bytes() for path in self.archive.output_dir.rglob("*.html")
                ])

    def test_unexpected_pdf_redirect_still_stops_collection(self) -> None:
        """Перенаправление без подтверждённой подписки не обходится и не скрывается пропуском."""

        self.responses[ISSUE_URL] = _html_response(ISSUE_URL, (
            f'<a href="{ARTICLE_URL}">Первая</a><a href="{NEXT_ARTICLE_URL}">Вторая</a>'
        ))
        self.responses[PDF_URL] = replace(
            _snapshot(PDF_URL, b"unexpected redirect", "text/html", 302),
            headers=(("content-type", "text/html"), ("location", ARTICLE_URL)),
        )

        with self.assertRaisesRegex(IoffeError, "HTTP 302"):
            JournalCollector(self.source, self.archive).crawl()

        requested = self._requested_urls()
        self.assertEqual(requested.count(PDF_URL), 1)
        self.assertEqual(requested.count(ARTICLE_URL), 1)
        self.assertNotIn(NEXT_ARTICLE_URL, requested)
        self.assertFalse((self.root / "state" / "articles").exists())

    def test_pdf_failure_resumes_without_reloading_article(self) -> None:
        """После ошибки PDF новый запуск использует сохранённую карточку и продолжает загрузку."""

        valid_pdf = self.responses[PDF_URL]
        self.responses[PDF_URL] = replace(valid_pdf, status_code=503)

        with self.assertRaises(IoffeError):
            JournalCollector(self.source, self.archive).crawl()

        self.responses[PDF_URL] = valid_pdf
        self.client.get_response.reset_mock()
        collector = JournalCollector(self.source, self.archive)
        collector.crawl()

        self.assertEqual(self._requested_urls(), [ROBOTS_URL, PDF_URL])
        self.assertEqual(collector.pdf_count, 1)

    def test_collection_does_not_register_or_extract_text(self) -> None:
        """Сбор не запускает старый OCR-процесс, регистрацию или допуск к обучению."""

        with patch("src.collect.ufn.pdf_to_text") as old_extraction, \
                patch("src.collect.pdf_text.extract_pages_from_pdf_ocr") as ocr, \
                patch("src.corpus.manifests.ManifestStore.commit") as commit:
            record = JournalCollector(self.source, self.archive).collect_article(ARTICLE_URL)

        old_extraction.assert_not_called()
        ocr.assert_not_called()
        commit.assert_not_called()
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")
        self.assertEqual(record["document"]["extra"]["content_role"], "metadata_only")

    def test_source_and_safe_defaults_are_available_in_cli(self) -> None:
        """Команда принимает УФН, но не включает сеть и безграничный обход по умолчанию."""

        arguments = build_parser().parse_args(["crawl", "ufn"])
        self.assertFalse(arguments.allow_network)
        self.assertEqual(arguments.max_requests, 20)

        with patch("scripts.download_journals.STATE_ROOT", self.root / "rate_limit"):
            client = JournalClient(self.source, max_requests=20, timeout=30)

        self.assertGreaterEqual(client.delay_seconds, 60.0)

    def test_cli_request_budget_and_resume_use_real_collector(self) -> None:
        """Ограниченная порция завершает только свои запросы и продолжает тот же кеш."""

        arguments = [
            "crawl", "ufn", "--allow-network", "--output-dir", str(self.root / "raw"),
            "--state-dir", str(self.root / "state"),
        ]

        with patch("scripts.download_journals.STATE_ROOT", self.root / "rate_limit"), \
                patch("src.collect.ioffe_http.IoffeClient.get_response",
                      side_effect=self.responses.__getitem__) as transport:
            self.assertEqual(main([*arguments, "--max-requests", "4"]), 0)
            self.assertEqual(transport.call_count, 4)
            summary = json.loads((self.root / "state" / "last_run.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["source_id"], "ufn")
            self.assertEqual(summary["status"], "request_limit")
            self.assertEqual(summary["network_requests"], 4)
            self.assertEqual(summary["cards_processed"], 0)
            transport.reset_mock()

            self.assertEqual(main([*arguments, "--max-requests", "6"]), 0)
            self.assertEqual([call.args[0] for call in transport.call_args_list], [
                ROBOTS_URL, ARTICLE_URL, PDF_URL,
            ])

        summary = json.loads((self.root / "state" / "last_run.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["network_requests"], 3)
        self.assertEqual(summary["cards_with_pdf"], 1)
        self._record()

    def test_cli_without_network_does_not_fetch_missing_cache(self) -> None:
        """Даже новый источник не обращается к сайту без явного разрешения сети."""

        with patch("scripts.download_journals.STATE_ROOT", self.root / "rate_limit"), \
                patch("src.collect.ioffe_http.IoffeClient.get_response") as transport:
            result = main([
                "crawl", "ufn", "--output-dir", str(self.root / "raw"),
                "--state-dir", str(self.root / "state"),
            ])

        self.assertEqual(result, 1)
        transport.assert_not_called()
