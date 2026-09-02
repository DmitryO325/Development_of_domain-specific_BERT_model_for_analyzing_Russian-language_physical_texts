"""Модульные тесты общих средств и сборщиков корпуса."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.error

from argparse import Namespace
from dataclasses import FrozenInstanceError
from http.client import BadStatusLine, IncompleteRead
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from scripts.scrape import DocumentRecorder, cmd_pilot
from src.collect.base import (
    Document,
    HttpResponseSnapshot,
    fetch_bytes,
    fetch_response,
    html_to_text,
)
from src.collect.pdf_text import (
    PdfPageText,
    _clean_pdf_lines,
    download_pdf,
    extract_best_text,
    pdf_filename,
    pdf_to_text,
    pdf_url_from_article_path,
)
from src.collect.rss_feed import RssScraper
from src.collect.ufn import UfnScraper, _drop_nav_prefix
from src.corpus.manifests import (
    ManifestConcurrencyError,
    ManifestPlan,
    ManifestStore,
)
from src.corpus.profiles import get_source_profile


class BaseCollectionTests(unittest.TestCase):
    """Проверки сетевых и HTML-утилит."""

    def test_fetch_response_captures_safe_evidence_after_full_read(self) -> None:
        """Снимок должен фиксироваться после чтения и не сохранять секреты."""

        events: list[str] = []
        response = MagicMock()
        opened_response = response.__enter__.return_value
        opened_response.status = 206
        opened_response.geturl.return_value = "https://cdn.example.test/article.pdf"
        opened_response.headers.items.return_value = [
            ("Set-Cookie", "session=secret"),
            ("ETag", '"version-1"'),
            ("Content-Type", "application/pdf"),
            ("X-Internal-Token", "secret"),
            ("Content-Length", "7"),
        ]

        def read_body() -> bytes:
            """Зафиксировать момент полного чтения тестового ответа."""

            events.append("read")
            return b"content"

        def current_timestamp() -> str:
            """Вернуть фиксированное время после чтения тестового ответа."""

            events.append("timestamp")
            return "2026-08-30T12:00:00.000000+00:00"

        opened_response.read.side_effect = read_body

        with (
            patch(
                "src.collect.base.urllib.request.urlopen",
                return_value=response,
            ),
            patch(
                "src.collect.base._current_utc_timestamp",
                side_effect=current_timestamp,
            ),
        ):
            snapshot = fetch_response("https://example.test/article.pdf")

        self.assertEqual(events, ["read", "timestamp"])
        self.assertEqual(snapshot.requested_url, "https://example.test/article.pdf")
        self.assertEqual(snapshot.final_url, "https://cdn.example.test/article.pdf")
        self.assertEqual(snapshot.status_code, 206)
        self.assertEqual(snapshot.body, b"content")
        self.assertEqual(
            snapshot.headers,
            (
                ("content-length", "7"),
                ("content-type", "application/pdf"),
                ("etag", '"version-1"'),
            ),
        )

    def test_response_metadata_is_canonical_and_hashed(self) -> None:
        """Порядок заголовков не должен менять JSON метаданных и его SHA-256."""

        first = HttpResponseSnapshot(
            requested_url="https://example.test/source",
            final_url="https://example.test/final",
            status_code=200,
            headers=(("etag", '"one"'), ("content-type", "text/plain")),
            retrieved_at="2026-08-30T12:00:00.000000+00:00",
            body=b"body",
        )
        second = HttpResponseSnapshot(
            requested_url="https://example.test/source",
            final_url="https://example.test/final",
            status_code=200,
            headers=(("content-type", "text/plain"), ("etag", '"one"')),
            retrieved_at="2026-08-30T12:00:00.000000+00:00",
            body=b"body",
        )

        self.assertEqual(first.canonical_metadata(), second.canonical_metadata())
        self.assertEqual(first.metadata_sha256(), second.metadata_sha256())
        self.assertEqual(
            first.metadata_sha256(),
            hashlib.sha256(first.canonical_metadata()).hexdigest(),
        )

        metadata = json.loads(first.canonical_metadata())
        self.assertNotIn("body", metadata)
        self.assertEqual(metadata["status_code"], 200)

    def test_response_snapshot_is_frozen(self) -> None:
        """Зафиксированный снимок ответа нельзя изменять после создания."""

        snapshot = HttpResponseSnapshot(
            requested_url="https://example.test/source",
            final_url="https://example.test/source",
            status_code=200,
            headers=(),
            retrieved_at="2026-08-30T12:00:00.000000+00:00",
            body=b"body",
        )

        with self.assertRaises(FrozenInstanceError):
            setattr(snapshot, "status_code", 201)

    def test_fetch_bytes_retries_temporary_error_with_backoff(self) -> None:
        """Временная сетевая ошибка должна приводить к повтору."""

        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"ok"
        with (
            patch(
                "src.collect.base.urllib.request.urlopen",
                side_effect=[urllib.error.URLError("temporary"), response],
            ) as urlopen,
            patch("src.collect.base.time.sleep") as sleep,
        ):
            result = fetch_bytes(
                "https://example.test/file",
                retries=2,
                delay_seconds=0.25,
            )

        self.assertEqual(result, b"ok")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.25)

    def test_fetch_bytes_does_not_retry_http_404(self) -> None:
        """Постоянная HTTP-ошибка не должна повторяться."""

        response = MagicMock()
        error = urllib.error.HTTPError(
            "https://example.test/missing",
            404,
            "Not Found",
            hdrs=None,
            fp=response,
        )
        with (
            patch(
                "src.collect.base.urllib.request.urlopen",
                side_effect=error,
            ) as urlopen,
            self.assertRaisesRegex(RuntimeError, "HTTP 404"),
        ):
            fetch_bytes("https://example.test/missing")

        urlopen.assert_called_once()
        response.close.assert_called_once()

    def test_fetch_bytes_retries_incomplete_response(self) -> None:
        """Оборванное чтение HTTP-ответа должно повторяться."""

        broken_response = MagicMock()
        broken_response.__enter__.return_value.read.side_effect = IncompleteRead(
            b"partial", 100
        )
        valid_response = MagicMock()
        valid_response.__enter__.return_value.read.return_value = b"complete"

        with (
            patch(
                "src.collect.base.urllib.request.urlopen",
                side_effect=[broken_response, valid_response],
            ) as urlopen,
            patch("src.collect.base.time.sleep") as sleep,
            patch(
                "src.collect.base._current_utc_timestamp",
                return_value="2026-08-30T12:00:00.000000+00:00",
            ) as timestamp,
        ):
            result = fetch_bytes(
                "https://example.test/file",
                retries=2,
                delay_seconds=0.5,
            )

        self.assertEqual(result, b"complete")
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)
        timestamp.assert_called_once_with()

    def test_fetch_bytes_retries_other_http_protocol_errors(self) -> None:
        """Временная ошибка HTTP-протокола должна повторяться."""

        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"ok"
        with (
            patch(
                "src.collect.base.urllib.request.urlopen",
                side_effect=[BadStatusLine("broken"), response],
            ) as urlopen,
            patch("src.collect.base.time.sleep"),
        ):
            result = fetch_bytes("https://example.test/file", retries=2)

        self.assertEqual(result, b"ok")
        self.assertEqual(urlopen.call_count, 2)

    def test_fetch_bytes_validates_options(self) -> None:
        """Недопустимые параметры должны давать понятную ошибку."""

        with self.assertRaises(ValueError):
            fetch_bytes("https://example.test", retries=0)
        with self.assertRaises(ValueError):
            fetch_bytes("https://example.test", delay_seconds=-1)
        with self.assertRaises(ValueError):
            fetch_bytes("file:///tmp/article.pdf")

    def test_html_to_text_handles_uppercase_closing_tags(self) -> None:
        """Регистр HTML-тегов не должен влиять на границы строк."""

        self.assertEqual(html_to_text("<P>Первая</P><P>Вторая</P>"), "Первая\nВторая")


class PdfTextTests(unittest.TestCase):
    """Проверки PDF-адресов, кэша и сопутствующих текстов."""

    def test_ufn_article_path_maps_to_pdf_url(self) -> None:
        """Путь статьи УФН должен преобразовываться в точный PDF-адрес."""

        self.assertEqual(
            pdf_url_from_article_path("/ru/articles/2026/5/a/"),
            "https://ufn.ru/ufn2026/ufn2026_5/Russian/r265a.pdf",
        )
        self.assertIsNone(pdf_url_from_article_path("/ru/news/2026/5/a/"))

    def test_download_pdf_waits_before_request(self) -> None:
        """Заданная пауза должна применяться до сетевого запроса."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "article.pdf"
            with (
                patch(
                    "src.collect.pdf_text.fetch_bytes",
                    return_value=b"%PDF\ncontent\n%%EOF",
                ),
                patch("src.collect.pdf_text.time.sleep") as sleep,
            ):
                download_pdf(
                    "https://example.test/article.pdf",
                    destination,
                    delay_seconds=0.75,
                )

        sleep.assert_called_once_with(0.75)

    def test_pdf_filename_ignores_query_and_fragment(self) -> None:
        """Параметры URL не должны попадать в имя файла."""

        self.assertEqual(
            pdf_filename("https://example.test/a.pdf?download=1#page=2"),
            "a.pdf",
        )

    def test_sidecar_name_matches_reported_method(self) -> None:
        """Имя `.txt` должно совпадать с возвращённым методом."""

        readable_text = "Это читаемый русский физический текст. " * 10
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "article.pdf"
            with patch(
                "src.collect.pdf_text.extract_pages_from_pdf",
                return_value=(PdfPageText(0, 1, readable_text),),
            ):
                text, method, readable = extract_best_text(
                    pdf_path,
                    text_dir=root,
                    try_ocr=False,
                )

            self.assertEqual(text, readable_text.strip())
            self.assertTrue(readable)
            self.assertEqual(method, "pdf")
            self.assertTrue((root / f"article_{method}.txt").is_file())

    def test_invalid_cache_is_downloaded_again(self) -> None:
        """HTML-файл большого размера не должен считаться PDF-кэшем."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_dir = Path(temporary_directory)
            cached = cache_dir / "article.pdf"
            cached.write_bytes(b"<html>" + b"x" * 2000)

            def _fake_download(
                _source_url: str,
                destination: Path,
                **_options: object,
            ) -> Path:
                """Записать корректный тестовый PDF вместо сетевой загрузки."""

                destination.write_bytes(b"%PDF" + b"x" * 2000)
                return destination

            with (
                patch(
                    "src.collect.pdf_text.download_pdf",
                    side_effect=_fake_download,
                ) as download,
                patch(
                    "src.collect.pdf_text.extract_best_text",
                    return_value=("текст", "pdf", True),
                ),
            ):
                pdf_to_text(
                    "https://example.test/article.pdf",
                    cache_dir,
                    text_dir=cache_dir / "text",
                )

            download.assert_called_once()

    def test_only_boundary_page_numbers_are_removed(self) -> None:
        """Числа внутри текста нельзя принимать за колонтитулы."""

        text = "12\nНачало\n2026\n42\nКонец\n13"
        self.assertEqual(_clean_pdf_lines(text), "Начало\n2026\n42\nКонец")

    def test_damaged_cached_pdf_is_downloaded_again(self) -> None:
        """Повреждённый PDF с верной сигнатурой нужно заменить."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_dir = Path(temporary_directory)
            cached = cache_dir / "article.pdf"
            cached.write_bytes(b"%PDF\n" + b"x" * 100 + b"\n%%EOF")

            with (
                patch(
                    "src.collect.pdf_text.extract_best_text",
                    side_effect=[ValueError("broken"), ("текст", "pdf", True)],
                ),
                patch("src.collect.pdf_text.download_pdf") as download,
            ):
                pdf_to_text(
                    "https://example.test/article.pdf",
                    cache_dir,
                    text_dir=cache_dir / "text",
                )

            download.assert_called_once()

    def test_same_filename_from_different_urls_uses_distinct_cache_paths(self) -> None:
        """Разные URL с одним именем не должны затирать PDF друг друга."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_dir = Path(temporary_directory)

            def _fake_download(
                _source_url: str,
                destination: Path,
                **_options: object,
            ) -> Path:
                """Записать тестовый PDF по выбранному пути кэша."""

                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF\ncontent\n%%EOF")
                return destination

            with (
                patch(
                    "src.collect.pdf_text.download_pdf",
                    side_effect=_fake_download,
                ),
                patch(
                    "src.collect.pdf_text.extract_best_text",
                    return_value=("текст", "pdf", True),
                ),
            ):
                _text, first_path, _readable, _method = pdf_to_text(
                    "https://first.test/article.pdf",
                    cache_dir,
                )
                _text, second_path, _readable, _method = pdf_to_text(
                    "https://second.test/article.pdf",
                    cache_dir,
                )

        self.assertNotEqual(first_path, second_path)
        self.assertEqual(first_path.name, "article.pdf")
        self.assertRegex(second_path.name, r"article_[0-9a-f]{12}\.pdf")


class RssScraperTests(unittest.TestCase):
    """Проверки RSS 2.0 и Atom без сетевых запросов."""

    def test_parses_namespaced_atom_entry(self) -> None:
        """Поля Atom должны разбираться вместе с пространством имён."""

        atom_xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Quantum result</title>
            <link rel="self" href="https://example.test/api/1" />
            <link rel="alternate" href="https://example.test/article/1" />
            <updated>2026-08-28T12:00:00Z</updated>
            <summary>A sufficiently long description of the physics article.</summary>
            <category term="quantum physics" />
          </entry>
        </feed>"""
        with patch("src.collect.rss_feed.fetch_bytes", return_value=atom_xml):
            documents = RssScraper(delay_seconds=0).parse_feed(
                "https://example.test/feed.atom"
            )

        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document.title, "Quantum result")
        self.assertEqual(document.url, "https://example.test/article/1")
        self.assertEqual(document.published, "2026-08-28T12:00:00Z")
        self.assertEqual(document.section, "quantum physics")

    def test_zero_limit_returns_no_documents(self) -> None:
        """Нулевой лимит должен давать пустой результат."""

        rss_xml = (
            b"<rss><channel><item><link>"
            b"https://example.test/a"
            b"</link></item></channel></rss>"
        )
        with patch("src.collect.rss_feed.fetch_bytes", return_value=rss_xml) as fetch:
            documents = RssScraper(delay_seconds=0).parse_feed(
                "https://example.test/feed.xml",
                limit=0,
            )
        self.assertEqual(documents, [])
        fetch.assert_not_called()

    def test_atom_xhtml_preserves_paragraph_boundary(self) -> None:
        """Соседние XHTML-абзацы Atom не должны склеиваться."""

        atom_xml = b"""<feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Physics</title>
            <link href="https://example.test/article" />
            <content type="xhtml">
              <div xmlns="http://www.w3.org/1999/xhtml">
                <p>First paragraph.</p><p>Second paragraph.</p>
              </div>
            </content>
          </entry>
        </feed>"""
        with patch("src.collect.rss_feed.fetch_bytes", return_value=atom_xml):
            document = RssScraper(delay_seconds=0).parse_feed(
                "https://example.test/feed.atom"
            )[0]

        self.assertIn("First paragraph. Second paragraph.", document.text)

    def test_rejects_dtd_and_entities(self) -> None:
        """Внешняя RSS-лента не должна разбирать DTD и сущности."""

        unsafe_xml = b"<!DOCTYPE rss [<!ENTITY x 'value'>]><rss>&x;</rss>"
        with (
            patch("src.collect.rss_feed.fetch_bytes", return_value=unsafe_xml),
            self.assertRaisesRegex(ValueError, "DTD"),
        ):
            RssScraper(delay_seconds=0).parse_feed("https://example.test/feed.xml")

    def test_rejects_utf16_dtd_and_entities(self) -> None:
        """Защиту от XML-сущностей нельзя обходить кодировкой UTF-16."""

        unsafe_xml = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE rss [<!ENTITY x "value">]><rss>&x;</rss>'
        ).encode("utf-16")
        with (
            patch("src.collect.rss_feed.fetch_bytes", return_value=unsafe_xml),
            self.assertRaisesRegex(ValueError, "DTD"),
        ):
            RssScraper(delay_seconds=0).parse_feed("https://example.test/feed.xml")

    def test_default_feeds_continue_after_unknown_xml_encoding(self) -> None:
        """Ошибка кодировки одной ленты не должна останавливать обход."""

        scraper = RssScraper(delay_seconds=0)
        valid_document = MagicMock()
        with (
            patch.dict(
                "src.collect.rss_feed.DEFAULT_FEEDS",
                {"broken": "https://bad.test", "valid": "https://ok.test"},
                clear=True,
            ),
            patch.object(
                scraper,
                "parse_feed",
                side_effect=[LookupError("unknown encoding"), [valid_document]],
            ),
        ):
            documents = list(scraper.iter_default_feeds())

        self.assertTrue(documents[0].extra["skipped"])
        self.assertIs(documents[1], valid_document)


class UfnScraperTests(unittest.TestCase):
    """Проверки обхода архива и очистки текста УФН."""

    def test_issues_are_sorted_numerically(self) -> None:
        """Номер 10 должен сортироваться после номера 9 по числовому значению."""

        archive_html = """
        <a href="/ru/articles/2026/9/">9</a>
        <a href="/ru/articles/2026/10/">10</a>
        <a href="/ru/articles/2025/12/">12</a>
        """
        scraper = UfnScraper(delay_seconds=0)
        with patch.object(scraper, "_get", return_value=archive_html):
            issues = scraper.list_issues()

        self.assertEqual(
            issues,
            [
                "/ru/articles/2026/10/",
                "/ru/articles/2026/9/",
                "/ru/articles/2025/12/",
            ],
        )

    def test_navigation_cleanup_preserves_formula(self) -> None:
        """Очистка навигации не должна удалять LaTeX-формулы."""

        text = (
            "Выпуски\n2026\nОчень длинное название физической статьи\n"
            "Энергия задаётся $E=mc^2$"
        )
        self.assertIn("$E=mc^2$", _drop_nav_prefix(text))

    def test_navigation_cleanup_preserves_short_title_and_early_formula(self) -> None:
        """Короткие заголовки и ранние формулы нельзя терять."""

        text = (
            "Выпуски\n2026\nМай\nКвантовый хаос\n$H=p^2/2m$\n"
            "Это длинная строка аннотации физической статьи."
        )
        cleaned = _drop_nav_prefix(text)
        self.assertTrue(cleaned.startswith("Квантовый хаос"))
        self.assertIn("$H=p^2/2m$", cleaned)

    def test_extracts_initials_before_surname(self) -> None:
        """Типичная для УФН запись автора должна извлекаться."""

        self.assertEqual(
            UfnScraper._extract_authors("<b>Б.Б. Страумал</b>"),
            ["Б.Б. Страумал"],
        )

    def test_bold_title_is_not_treated_as_author(self) -> None:
        """Полужирное название без инициалов не является автором."""

        self.assertEqual(UfnScraper._extract_authors("<b>Quantum Physics</b>"), [])
        self.assertEqual(UfnScraper._extract_authors("<b>Иван Иванов</b>"), [])

    def test_rejects_unknown_text_mode(self) -> None:
        """Опечатка в режиме должна быть заметна сразу."""

        with self.assertRaises(ValueError):
            UfnScraper(text_mode="pdff")

    def test_issue_error_does_not_stop_archive_iteration(self) -> None:
        """Ошибка одного выпуска не должна прерывать обход архива."""

        scraper = UfnScraper(delay_seconds=0)
        document = MagicMock()
        document.text = "Физический текст " * 10
        with (
            patch.object(scraper, "list_issues", return_value=["bad", "good"]),
            patch.object(
                scraper,
                "list_article_paths",
                side_effect=[RuntimeError("temporary"), [("article", None)]],
            ),
            patch.object(scraper, "parse_article", return_value=document),
        ):
            documents = list(scraper.iter_articles())

        self.assertEqual(documents, [document])


class ScrapeCommandTests(unittest.TestCase):
    """Проверки безопасного запуска команды сбора."""

    @staticmethod
    def _right(operation: str) -> dict[str, Any]:
        """Создать разрешение источника для прямой регистрации."""

        return {
            "schema_version": "rights-v1",
            "created_at": "2026-08-30T10:00:00+03:00",
            "rights_record_id": f"right-{operation}",
            "scope_type": "source",
            "scope_id": "S01_UFN_RU",
            "operation": operation,
            "status": "allowed",
            "access_basis": "Синтетическое разрешение для теста команды.",
            "basis_type": "explicit_license",
            "acquisition_method": "crawler" if operation == "acquisition" else None,
            "acquisition_scope": "sample" if operation == "acquisition" else None,
            "terms_url": "https://example.invalid/test-license",
            "rights_checked_at": "2026-08-30",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "c" * 64,
            "rights_expires_at": None,
            "supersedes_rights_record_id": None,
        }

    def test_document_recorder_writes_directly_to_manifests(self) -> None:
        """Явный режим должен обходить прототипный JSONL и писать реестры."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            manifest_dir = project_root / "manifests"
            store = ManifestStore(
                project_root=project_root,
                manifest_dir=manifest_dir,
                schema_dir=Path(__file__).resolve().parents[1]
                / "manifests"
                / "schemas",
            )
            store.commit(
                ManifestPlan(
                    rights=[self._right("acquisition"), self._right("storage")]
                )
            )
            output = project_root / "prototype.jsonl"
            arguments = Namespace(
                output=str(output),
                fresh=False,
                register_manifests=True,
                manifest_dir=manifest_dir,
                profile="auto",
                content_role="title_abstract",
                acquisition_method="crawler",
                acquisition_scope="sample",
                extraction_method="html_to_text",
                extraction_version="html-v1",
                rights_record_id=["right-acquisition", "right-storage"],
            )
            recorder = DocumentRecorder(
                arguments,
                (get_source_profile("ufn"),),
                project_root=project_root,
            )
            saved = recorder.save(
                Document(
                    source="ufn.ru",
                    url="https://ufn.ru/ru/articles/2024/1/a/",
                    title="Квантовые свойства плазмы",
                    text="Аннотация физической статьи на русском языке.",
                    published="2024-01-10",
                    extra={"year": "2024", "text_source": "html"},
                )
            )

            self.assertTrue(saved)
            self.assertFalse(output.exists())
            self.assertEqual(len(store.records("works")), 1)
            self.assertEqual(len(store.records("artifacts")), 1)
            self.assertEqual(len(store.records("retrieval_events")), 1)
            self.assertEqual(
                store.records("retrieval_events")[0]["outcome"],
                "metadata_only",
            )

    def test_manifest_registration_rejects_fresh_mode(self) -> None:
        """Прямая регистрация не должна принимать удаляющий режим fresh."""

        arguments = Namespace(
            output="unused.jsonl",
            fresh=True,
            register_manifests=True,
            manifest_dir="manifests",
            profile="auto",
        )

        with self.assertRaisesRegex(ValueError, "--fresh несовместим"):
            DocumentRecorder(arguments, (get_source_profile("ufn"),))

    def test_manifest_registration_rejects_unproven_full_text(self) -> None:
        """Команда не должна объявлять полный текст без исходного ответа."""

        arguments = Namespace(
            output="unused.jsonl",
            fresh=False,
            register_manifests=True,
            manifest_dir="manifests",
            profile="auto",
            content_role="full_text",
            acquisition_method="crawler",
            acquisition_scope="sample",
            extraction_method="html_to_text",
            extraction_version="html-v1",
        )

        with self.assertRaisesRegex(ValueError, "full_text"):
            DocumentRecorder(arguments, (get_source_profile("ufn"),))

    def test_manifest_registration_rejects_false_collection_mode(self) -> None:
        """Команда-обходчик не должна записывать manual_download или single."""

        base_arguments = {
            "output": "unused.jsonl",
            "fresh": False,
            "register_manifests": True,
            "manifest_dir": "manifests",
            "profile": "auto",
            "content_role": "metadata_only",
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "extraction_method": "rss_summary_to_text",
            "extraction_version": "rss-v1",
        }

        with self.assertRaisesRegex(ValueError, "crawler"):
            DocumentRecorder(
                Namespace(**base_arguments),
                (get_source_profile("ufn"),),
            )

        base_arguments["acquisition_method"] = "crawler"
        base_arguments["acquisition_scope"] = "single"

        with self.assertRaisesRegex(ValueError, "single"):
            DocumentRecorder(
                Namespace(**base_arguments),
                (get_source_profile("ufn"),),
            )

    def test_fresh_waits_for_first_successful_document(self) -> None:
        """Режим fresh не должен удалять старый файл до валидной записи."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "corpus.jsonl"
            output.write_text("old\n", encoding="utf-8")
            recorder = DocumentRecorder(
                Namespace(
                    output=str(output),
                    fresh=True,
                    register_manifests=False,
                    profile="auto",
                ),
                (),
            )

            self.assertEqual(output.read_text(encoding="utf-8"), "old\n")
            self.assertFalse(
                recorder.save(
                    Document(
                        source="ufn.ru",
                        url="https://ufn.ru/",
                        title="Пропущено",
                        text="",
                    )
                )
            )
            self.assertEqual(output.read_text(encoding="utf-8"), "old\n")

    def test_document_recorder_retries_only_concurrent_snapshot_change(self) -> None:
        """Параллельное CAS-изменение надо согласовать заново без сети."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            manifest_dir = project_root / "manifests"
            store = ManifestStore(
                project_root=project_root,
                manifest_dir=manifest_dir,
                schema_dir=Path(__file__).resolve().parents[1]
                / "manifests"
                / "schemas",
            )
            store.commit(
                ManifestPlan(
                    rights=[self._right("acquisition"), self._right("storage")]
                )
            )
            arguments = Namespace(
                output=str(project_root / "unused.jsonl"),
                fresh=False,
                register_manifests=True,
                manifest_dir=manifest_dir,
                profile="auto",
                content_role="title_abstract",
                acquisition_method="crawler",
                acquisition_scope="sample",
                extraction_method="html_to_text",
                extraction_version="html-v1",
                rights_record_id=["right-acquisition", "right-storage"],
            )
            recorder = DocumentRecorder(
                arguments,
                (get_source_profile("ufn"),),
                project_root=project_root,
            )
            assert recorder.store is not None
            real_commit = recorder.store.commit
            calls = 0

            def commit_with_one_conflict(*args: Any, **kwargs: Any) -> Any:
                """Имитировать одну гонку между согласованием и записью."""

                nonlocal calls
                calls += 1

                if calls == 1:
                    raise ManifestConcurrencyError("синтетическая гонка")

                return real_commit(*args, **kwargs)

            with patch.object(
                recorder.store,
                "commit",
                side_effect=commit_with_one_conflict,
            ):
                saved = recorder.save(
                    Document(
                        source="ufn.ru",
                        url="https://ufn.ru/ru/articles/2024/1/a/",
                        title="Квантовые свойства плазмы",
                        text="Аннотация физической статьи на русском языке.",
                        published="2024-01-10",
                        extra={"year": "2024", "text_source": "html"},
                    )
                )

            self.assertTrue(saved)
            self.assertEqual(calls, 2)
            self.assertEqual(len(store.records("works")), 1)

    def test_pilot_preserves_requested_output_and_supplies_options(self) -> None:
        """Пилот не должен подменять файл или падать из-за аргументов."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "custom.jsonl"
            args = Namespace(
                output=str(output),
                fresh=False,
                delay=0,
            )
            with (
                patch("scripts.scrape.cmd_ufn") as run_ufn,
                patch("scripts.scrape.cmd_rss") as run_rss,
            ):
                cmd_pilot(args)

        self.assertEqual(args.output, str(output))
        self.assertEqual(args.text_source, "pdf+html")
        self.assertEqual(args.max_docs, 4)
        run_ufn.assert_called_once()
        run_rss.assert_called_once()

    def test_registered_pilot_preflights_all_profiles_with_complete_arguments(self) -> None:
        """Пилот должен получить RSS-аргументы до общей проверки."""

        arguments = Namespace(
            output="unused.jsonl",
            fresh=False,
            delay=0,
            register_manifests=True,
            acquisition_scope="sample",
            command="pilot",
            manifest_dir="manifests",
        )

        with (
            patch("scripts.scrape._ufn_profiles", return_value=()) as ufn_profiles,
            patch("scripts.scrape._rss_profiles", return_value=()) as rss_profiles,
            patch("scripts.scrape.DocumentRecorder") as recorder,
            patch("scripts.scrape.cmd_ufn") as run_ufn,
            patch("scripts.scrape.cmd_rss") as run_rss,
        ):
            cmd_pilot(arguments)

        self.assertIsNone(arguments.feed)
        self.assertEqual(arguments.limit, 8)
        ufn_profiles.assert_called_once()
        rss_profiles.assert_called_once()
        recorder.assert_called_once()
        run_ufn.assert_called_once()
        run_rss.assert_called_once()


if __name__ == "__main__":
    unittest.main()
