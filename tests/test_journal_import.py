"""Проверки локального чтения журналов по закреплённым HTTP-свидетельствам."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import pymupdf

from src.collect.base import HttpResponseSnapshot
from src.collect.ioffe import IOFFE_JOURNALS, parse_article as parse_ioffe_article
from src.collect.ioffe_archive import IoffeArchive
from src.collect.ioffe_http import validate_ioffe_url
from src.collect.journal_sources import get_source
from src.corpus.journal_import import (
    COLLECTED_SOURCES,
    AmbiguousIssueEntryError,
    CollectedArticleError,
    NonRussianArticleError,
    read_collected_article,
)
from src.corpus.profiles import SOURCE_PROFILES, get_source_profile
from tests.test_ioffe import ARTICLE_HTML as IOFFE_HTML, ARTICLE_URL as IOFFE_URL
from tests.test_jetp import ARTICLE_HTML as JETP_HTML, ARTICLE_URL as JETP_URL
from tests.test_jetp_letters import ARTICLE_HTML as LETTERS_HTML, ARTICLE_URL as LETTERS_URL
from tests.test_quantum_electronics import ARTICLE_HTML as QUANTUM_HTML, ARTICLE_URL as QUANTUM_URL
from tests.test_ufn_journal import ARTICLE_HTML as UFN_HTML, ARTICLE_URL as UFN_URL, PDF_URL as UFN_PDF_URL
from tests.test_ufn_journal import SUBSCRIPTION_HTML as UFN_SUBSCRIPTION_HTML

ISSUE_URL = "https://quantum-electronics.ru/2024_54_1/"
ISSUE_PDF_URL = "https://quantum-electronics.ru/wp-content/uploads/2024/01/test_article.pdf"
ISSUE_HTML = """
<h1 class="entry-title">2024, том 54, номер 1</h1>
<div class="entry-content"><p><b>Лазеры</b></p>
<p><a href="/wp-content/uploads/2024/01/test_article.pdf">
Исследование импульсов</a><br>А.А. Иванов, Б.Б. Петров 23</p>
<p><a href="/wp-content/uploads/2024/01/other.pdf">
Другие свойства лазеров</a><br>В.В. Сидоров 27</p></div>
"""
AMBIGUOUS_ISSUE_HTML = ISSUE_HTML.replace("/2024/01/other.pdf", "/2024/01/test_article.pdf")


class JournalImportTests(unittest.TestCase):
    """Проверять карточки, PDF и происхождение без сети или изменения рабочих реестров."""

    def setUp(self) -> None:
        """Создать отдельный архив с воспроизводимыми вымышленными ответами."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.raw = self.root / "raw"
        self.state = self.root / "state"
        self.client = MagicMock()

        with pymupdf.open() as document:
            document.new_page()
            self.pdf_bytes = document.tobytes()

    def _fixture(
        self,
        source: str = "jetp",
        *,
        html: str | None = None,
        with_pdf: bool = True,
        issue_toc: bool = False,
        article_url: str | None = None,
    ) -> tuple[Path, dict[str, Any], IoffeArchive]:
        """Сохранить карточку в том же формате, который создают сборщики журналов."""

        fixtures = {
            "ioffe": (IOFFE_URL, IOFFE_HTML), "jetp": (JETP_URL, JETP_HTML),
            "jetp_letters": (LETTERS_URL, LETTERS_HTML), "quantum": (QUANTUM_URL, QUANTUM_HTML),
            "ufn": (UFN_URL, UFN_HTML),
        }
        url, fixture_html = fixtures[source]
        url = article_url or url
        html = (ISSUE_HTML if issue_toc else fixture_html) if html is None else html
        validator = validate_ioffe_url if source == "ioffe" else get_source(source).validate_url
        archive = IoffeArchive(self.raw, self.state, self.client, validate_url=validator)

        if issue_toc:
            url = ISSUE_URL
            document = get_source(source).parse_issue_articles(html, url)[0]

        elif source == "ioffe":
            document = parse_ioffe_article(html, url)

        else:
            document = get_source(source).parse_article(html, url)

        snapshot = HttpResponseSnapshot(
            requested_url=url, final_url=url, status_code=200,
            headers=(("content-type", "text/html; charset=utf-8"),),
            retrieved_at="2026-10-14T09:00:00+00:00", body=html.encode("utf-8"),
        )
        archive.import_response(snapshot)
        record: dict[str, Any] = {
            "schema_version": "ioffe-collected-article-v1" if source == "ioffe" else "journal-collected-article-v1",
            "document": document.to_dict(), "card_response": archive.evidence_for(snapshot),
            "status": "metadata_only", "registered": False, "training_eligibility": "not_assessed",
        }

        if source == "ioffe":
            record["article_id"] = document.extra["article_id"]

        else:
            record["source_id"] = source
            record["metadata_page_url"] = url

        if with_pdf and document.pdf_url is not None:
            pdf_snapshot = replace(snapshot, requested_url=document.pdf_url, final_url=document.pdf_url,
                                   headers=(("content-type", "application/pdf"),), body=self.pdf_bytes)
            archive.import_response(pdf_snapshot)
            record.update(pdf_response=archive.evidence_for(pdf_snapshot), status="pdf_downloaded", page_count=1)

        path = self.state / "articles" / "article.json"
        self._save_record(path, record)

        return path, record, archive

    def _save_record(self, path: Path, record: dict[str, Any]) -> None:
        """Сохранить тестовую производную карточку отдельно от исходных ответов."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def test_all_source_profiles_and_pdfs(self) -> None:
        """Все пять сборщиков читаются с правильной группой, метаданными и PDF."""

        for source, group in (
            ("ioffe", "S05_IOFFE"), ("jetp", "S04_JETP"),
            ("jetp_letters", "S02_JETP_LETTERS"), ("quantum", "S06_QUANTUM_ELECTRONICS"),
            ("ufn", "S01_UFN"),
        ):
            with self.subTest(source=source):
                path, record, _ = self._fixture(source)
                article = read_collected_article(path, source=source, raw_root=self.raw)
                self.assertEqual(article.profile.source_group_id, group)
                self.assertEqual(article.document.to_dict(), record["document"])
                self.assertEqual(article.record_path, path)
                self.assertIsNotNone(article.pdf_response)

    def test_ufn_profile_alias_keeps_existing_source_and_native_identifiers(self) -> None:
        """Имя нового адаптера УФН не меняет прежние идентификаторы источника и статьи."""

        self.assertIn("ufn", COLLECTED_SOURCES)

        for source in ("ufn", "ufn.ru", "www.ufn.ru", "ufn.ru:rss"):
            with self.subTest(source=source):
                profile = get_source_profile("ufn", source=source, url=UFN_URL)
                self.assertEqual(profile.source_group_id, "S01_UFN")
                self.assertEqual(profile.source_id, "S01_UFN_RU")
                self.assertEqual(profile.journal_id, "ufn_ru")
                self.assertEqual(profile.native_id(UFN_URL, {}), "article-2026-8-a")
                self.assertEqual(get_source_profile("auto", source=source, url=UFN_URL), profile)

        with self.assertRaises(ValueError):
            get_source_profile("ufn", source="ufn", url="https://example.test/ru/articles/2026/8/a/")

    def test_ufn_shifted_pdf_letter_is_verified_against_html(self) -> None:
        """Импорт сверяет явный PDF с карточкой, не выводя его имя из буквы URL статьи."""

        html = UFN_HTML.replace("r268a.pdf", "r268z.pdf")
        path, _, _ = self._fixture("ufn", html=html)
        article = read_collected_article(path, source="ufn", raw_root=self.raw)

        self.assertEqual(article.document.url, UFN_URL)
        self.assertEqual(article.document.pdf_url, UFN_PDF_URL.replace("r268a.pdf", "r268z.pdf"))
        self.assertEqual(article.profile.native_id(article.document.url, article.document.extra), "article-2026-8-a")
        self.assertIsNotNone(article.pdf_response)
        assert article.pdf_response is not None
        self.assertEqual(article.pdf_response.requested_url, article.document.pdf_url)
        self.assertEqual(article.document.extra["content_role"], "metadata_only")

    def test_ufn_unrelated_pdf_is_rejected_even_with_matching_hash(self) -> None:
        """Другой допустимый PDF того же выпуска не подменяет явно опубликованный файл."""

        path, record, _ = self._fixture("ufn", html=UFN_HTML.replace("r268a.pdf", "r268z.pdf"))
        snapshot = read_collected_article(path, source="ufn", raw_root=self.raw).pdf_response
        assert snapshot is not None
        self._replace_witness(record["pdf_response"], replace(snapshot, requested_url=UFN_PDF_URL, final_url=UFN_PDF_URL))
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "PDF не соответствует"):
            read_collected_article(path, source="ufn", raw_root=self.raw)

    def test_ufn_stale_derived_pdf_field_requires_collector_replay(self) -> None:
        """Импорт не исправляет молча прежнюю карточку, потерявшую явную PDF-ссылку."""

        path, record, _ = self._fixture("ufn", with_pdf=False, html=UFN_HTML.replace("r268a.pdf", "r268z.pdf"))
        record["document"]["pdf_url"] = None
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "повторным разбором"):
            read_collected_article(path, source="ufn", raw_root=self.raw)

    def _legacy_ufn_fixture(
        self, article_url: str, pdf_url: str,
    ) -> tuple[Path, dict[str, Any], IoffeArchive]:
        """Воспроизвести прежнюю карточку, не распознавшую опубликованный старый PDF."""

        html = UFN_HTML.replace(UFN_PDF_URL, pdf_url).replace(
            urlsplit(UFN_PDF_URL).path, urlsplit(pdf_url).path,
        ).replace(UFN_URL, article_url)
        path, record, archive = self._fixture(
            "ufn", html=html, with_pdf=False, article_url=article_url,
        )
        self.assertEqual(record["document"]["pdf_url"], pdf_url)
        record["document"]["pdf_url"] = None
        self._save_record(path, record)

        return path, record, archive

    def test_ufn_legacy_pdf_policy_preserves_historical_document_and_files(self) -> None:
        """Старые PDF-форматы не обогащают закреплённый документ и не меняют исходники."""

        examples = (
            ("https://ufn.ru/ru/articles/2015/5/i/", "https://ufn.ru/ufn15/ufn15_5/Russian/r155i.pdf"),
            ("https://ufn.ru/ru/articles/2018/3/a/", "https://ufn.ru/ufn2018/ufn2018_3/Russian/r20183a.pdf"),
        )

        for article_url, pdf_url in examples:
            with self.subTest(url=article_url):
                path, record, _ = self._legacy_ufn_fixture(article_url, pdf_url)
                files_before = {item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()}

                with patch("src.corpus.manifests.ManifestStore.commit") as commit:
                    article = read_collected_article(path, source="ufn", raw_root=self.raw)

                self.assertEqual(article.document.to_dict(), record["document"])
                self.assertIsNone(article.document.pdf_url)
                self.assertIsNone(article.pdf_response)
                self.assertEqual(files_before, {
                    item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()
                })
                self.client.fetch.assert_not_called()
                commit.assert_not_called()

    def test_ufn_legacy_pdf_policy_rejects_other_document_changes(self) -> None:
        """Совместимость старого PDF не допускает подмену полей или типов метаданных."""

        mutations = (
            {"title": "Подмена"}, {"authors": ["Другой автор"]}, {"text": "Другой текст"},
            {"pdf_url": "https://ufn.ru/ufn15/ufn15_5/Russian/r155z.pdf"},
        )

        for mutation in mutations:
            with self.subTest(changes=mutation):
                path, record, _ = self._legacy_ufn_fixture(
                    "https://ufn.ru/ru/articles/2015/5/i/", "https://ufn.ru/ufn15/ufn15_5/Russian/r155i.pdf",
                )
                record["document"].update(mutation)
                self._save_record(path, record)

                with self.assertRaisesRegex(CollectedArticleError, "повторным разбором"):
                    read_collected_article(path, source="ufn", raw_root=self.raw)

        path, record, _ = self._legacy_ufn_fixture(
            "https://ufn.ru/ru/articles/2015/5/i/", "https://ufn.ru/ufn15/ufn15_5/Russian/r155i.pdf",
        )
        record["document"]["extra"]["year"] = 2015.0
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "повторным разбором"):
            read_collected_article(path, source="ufn", raw_root=self.raw)

    def test_ufn_legacy_pdf_policy_rejects_download_claims(self) -> None:
        """Прежний пустой PDF не разрешает фиктивный статус скачивания, страницы или свидетельство."""

        for mutation in ({"status": "pdf_downloaded"}, {"page_count": 1}, {"pdf_response": {}}):
            with self.subTest(changes=mutation):
                path, record, _ = self._legacy_ufn_fixture(
                    "https://ufn.ru/ru/articles/2015/5/i/", "https://ufn.ru/ufn15/ufn15_5/Russian/r155i.pdf",
                )
                record.update(mutation)
                self._save_record(path, record)

                with self.assertRaises(CollectedArticleError):
                    read_collected_article(path, source="ufn", raw_root=self.raw)

    def test_ufn_legacy_pdf_policy_does_not_hide_conflicting_original_links(self) -> None:
        """Конфликт явных PDF исходника остаётся ошибкой, даже если старый результат был пустым."""

        path, record, _ = self._legacy_ufn_fixture(
            "https://ufn.ru/ru/articles/2015/5/i/", "https://ufn.ru/ufn15/ufn15_5/Russian/r155i.pdf",
        )
        article = read_collected_article(path, source="ufn", raw_root=self.raw)
        body = article.card_response.body.replace(
            b'href="/ufn15/ufn15_5/Russian/r155i.pdf"', b'href="/ufn15/ufn15_5/Russian/r155z.pdf"',
        )
        self._replace_witness(record["card_response"], replace(article.card_response, body=body))
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "разные русские PDF"):
            read_collected_article(path, source="ufn", raw_root=self.raw)

    def test_ufn_broken_seo_keywords_use_adapter_without_changing_originals(self) -> None:
        """Допустимое исключение адаптера для SEO повторяется при проверке точного исходника."""

        path, record, _ = self._fixture("ufn")
        original = read_collected_article(path, source="ufn", raw_root=self.raw)
        body = original.card_response.body.replace(
            b'<meta name="keywords" content="', b'<meta name="keywords" content="broken \xd0, ',
        )
        snapshot = replace(original.card_response, body=body)
        self._replace_witness(record["card_response"], snapshot)
        self._save_record(path, record)
        files_before = {item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()}

        with self.assertLogs("src.collect.ufn_journal", "WARNING") as captured, \
             patch("src.collect.ioffe_archive.IoffeArchive._locked_archive", side_effect=AssertionError("lock")), \
             patch("src.corpus.manifests.ManifestStore.commit") as commit:
            article = read_collected_article(path, source="ufn", raw_root=self.raw)

        self.assertEqual(article.document, original.document)
        self.assertEqual(article.card_response.body, body)
        self.assertEqual(article.pdf_response, original.pdf_response)
        self.assertTrue(any("SEO-meta keywords" in message for message in captured.output))
        self.assertEqual(files_before, {item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()})
        self.client.get_response.assert_not_called()
        commit.assert_not_called()

    def test_ufn_damaged_visible_utf8_is_not_hidden_by_adapter(self) -> None:
        """Повреждение названия или аннотации по-прежнему останавливает проверку импорта."""

        path, record, _ = self._fixture("ufn")
        original = read_collected_article(path, source="ufn", raw_root=self.raw)
        body = original.card_response.body.replace(b'itemprop="headline">', b'itemprop="headline">\xd0, ')
        self._replace_witness(record["card_response"], replace(original.card_response, body=body))
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "кодировке"):
            read_collected_article(path, source="ufn", raw_root=self.raw)

    def test_ufn_foreign_pdf_witness_does_not_expand_source_scope(self) -> None:
        """Корректный SHA внешнего файла не расширяет разрешённый узел УФН."""

        path, record, _ = self._fixture("ufn")
        snapshot = read_collected_article(path, source="ufn", raw_root=self.raw).pdf_response
        assert snapshot is not None
        url = "https://example.test/ufn2026/ufn2026_8/Russian/r268a.pdf"
        self._replace_witness(record["pdf_response"], replace(snapshot, requested_url=url, final_url=url))
        self._save_record(path, record)

        with self.assertRaises(CollectedArticleError):
            read_collected_article(path, source="ufn", raw_root=self.raw)

    def test_ufn_non_russian_skip_requires_verified_original(self) -> None:
        """Английская карточка УФН пропускается только после сверки её исходника."""

        html = UFN_HTML.replace("Первичные чёрные дыры", "Primordial black holes").replace(
            "Рассмотрены свойства&nbsp;чёрных дыр.", "We study black holes."
        ).replace("Показаны результаты.", "Results are presented.")
        path, record, _ = self._fixture("ufn", html=html, with_pdf=False)
        record["status"] = "skipped_non_russian"
        self._save_record(path, record)

        with self.assertRaises(NonRussianArticleError):
            read_collected_article(path, source="ufn", raw_root=self.raw)

    def test_ufn_metadata_only_does_not_claim_pdf_or_training_permission(self) -> None:
        """Не загруженный PDF и допуск к обучению не выводятся из одной ссылки карточки."""

        path, record, _ = self._fixture("ufn", with_pdf=False)
        article = read_collected_article(path, source="ufn", raw_root=self.raw)

        self.assertIsNone(article.pdf_response)
        self.assertEqual(article.document.pdf_url, UFN_PDF_URL)
        self.assertEqual(article.document.extra["text_source"], "abstract")
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")

    def test_ufn_subscription_reason_is_verified_from_original_without_pdf(self) -> None:
        """Импорт сверяет подписную карточку с исходником, сохраняя причину и прежний допуск."""

        with self.assertLogs("src.collect.ufn_journal", "WARNING"):
            path, record, _ = self._fixture("ufn", html=UFN_SUBSCRIPTION_HTML)
            article = read_collected_article(path, source="ufn", raw_root=self.raw)

        self.assertEqual(record["status"], "metadata_only")
        self.assertIsNone(article.pdf_response)
        self.assertIsNone(article.document.pdf_url)
        self.assertEqual(article.document.extra["pdf_access_status"], "subscription_required")
        self.assertEqual(article.document.extra["pdf_access_reason"], "Полный текст предоставляется по подписке")
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")

    def test_metadata_only_keeps_published_pdf_link_without_claiming_download(self) -> None:
        """Ссылка PDF в HTML не превращается в скачанный файл или полный текст."""

        path, record, _ = self._fixture("quantum", with_pdf=False)
        article = read_collected_article(path, source="quantum", raw_root=self.raw)
        self.assertIsNone(article.pdf_response)
        self.assertIsNotNone(article.document.pdf_url)
        self.assertEqual(article.document.extra["content_role"], "metadata_only")
        self.assertFalse(record["registered"])
        self.assertEqual(record["training_eligibility"], "not_assessed")

    def test_pinned_old_snapshot_survives_cache_refresh(self) -> None:
        """Обновление latest-кеша не меняет исходник уже собранной карточки."""

        path, _, archive = self._fixture(with_pdf=False)
        original = read_collected_article(path, source="jetp", raw_root=self.raw)
        updated = replace(original.card_response, body=JETP_HTML.replace("атомов", "ионов").encode("utf-8"),
                          retrieved_at="2026-10-14T10:00:00+00:00")
        archive.import_response(updated)
        article = read_collected_article(path, source="jetp", raw_root=self.raw)
        self.assertEqual(article.card_response, original.card_response)
        self.assertNotEqual(article.card_response, updated)

    def test_reading_does_not_write_or_contact_network(self) -> None:
        """Чтение не создаёт блокировок и не использует клиент или изменяемые указатели кеша."""

        path, _, _ = self._fixture()
        files_before = {item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()}

        with patch("src.collect.ioffe_archive.IoffeArchive._locked_archive", side_effect=AssertionError("lock")):
            read_collected_article(path, source="jetp", raw_root=self.raw)

        files_after = {item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()}
        self.assertEqual(files_before, files_after)
        self.client.get_response.assert_not_called()

    def test_corrupted_body_or_metadata_is_rejected(self) -> None:
        """Повреждение тела, PDF или HTTP-свидетельства обнаруживается до регистрации."""

        for response, field in (("card_response", "relative_path"), ("pdf_response", "relative_path"),
                                ("card_response", "response_metadata_path")):
            with self.subTest(response=response, field=field):
                path, record, _ = self._fixture()
                target = self.raw / record[response][field]
                original = target.read_bytes()
                target.write_bytes(original + b"x")

                with self.assertRaisesRegex(CollectedArticleError, "Контрольная сумма"):
                    read_collected_article(path, source="jetp", raw_root=self.raw)

                target.write_bytes(original)

    def test_declared_size_is_checked(self) -> None:
        """Даже правильный SHA не разрешает несовпадающий или логический размер."""

        path, record, _ = self._fixture()

        for size in (0, True):
            record["card_response"]["bytes"] = size
            self._save_record(path, record)

            with self.assertRaisesRegex(CollectedArticleError, "размер"):
                read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_path_escape_and_wrong_content_address_are_rejected(self) -> None:
        """Абсолютные пути, выход из корня и неверные имена файлов не читаются."""

        path, record, _ = self._fixture()

        for value in ("../secret", str(self.root / "secret"), "responses/invalid.html"):
            record["card_response"]["relative_path"] = value
            self._save_record(path, record)

            with self.assertRaisesRegex(CollectedArticleError, "Пути свидетельства"):
                read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_body_symlink_is_rejected_even_inside_raw_root(self) -> None:
        """Символическая ссылка запрещена даже при совпадении байтов и нахождении цели в корне."""

        path, record, _ = self._fixture()
        body_path = self.raw / record["card_response"]["relative_path"]
        target = body_path.with_suffix(".copy")
        body_path.rename(target)
        body_path.symlink_to(target)

        with self.assertRaisesRegex(CollectedArticleError, "символическ"):
            read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_intermediate_directory_symlink_is_rejected(self) -> None:
        """Ссылка в промежуточном каталоге исходников также не принимается."""

        path, record, _ = self._fixture()
        directory = (self.raw / record["card_response"]["relative_path"]).parent
        target = self.raw / "moved"
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(CollectedArticleError, "символическ"):
            read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_input_record_and_raw_root_symlinks_are_rejected(self) -> None:
        """Корень и производная карточка не могут подменяться символическими ссылками."""

        path, _, _ = self._fixture()
        record_link = path.with_name("link.json")
        record_link.symlink_to(path)
        root_link = self.root / "linked-raw"
        root_link.symlink_to(self.raw, target_is_directory=True)

        for record_path, raw_root in ((record_link, self.raw), (path, root_link)):
            with self.assertRaisesRegex(CollectedArticleError, "символическ"):
                read_collected_article(record_path, source="jetp", raw_root=raw_root)

    def test_parent_alias_above_explicit_roots_is_allowed(self) -> None:
        """Системный псевдоним вроде /var не запрещает обычные файлы внутри явного корня."""

        path, _, _ = self._fixture()
        alias = self.root / "system-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        article = read_collected_article(
            alias / path.relative_to(self.root), source="jetp", raw_root=alias / "raw",
        )
        canonical = read_collected_article(path, source="jetp", raw_root=self.raw)
        self.assertEqual(article, canonical)

    def test_tampered_document_fields_are_rejected(self) -> None:
        """Название, авторы, текст, журнал и даже true вместо целого сверяются с HTML."""

        for field, value in (("title", "Подмена"), ("authors", ["Другой автор"]), ("text", "Подмена"),
                             ("source", "quantum"), ("extra", {"journal_id": True})):
            with self.subTest(field=field):
                path, record, _ = self._fixture()
                record["document"][field] = value
                self._save_record(path, record)

                with self.assertRaisesRegex(CollectedArticleError, "повторным разбором"):
                    read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_ioffe_boolean_journal_id_cannot_impersonate_integer(self) -> None:
        """Логическое true не принимается за идентификатор журнала 1."""

        path, record, _ = self._fixture("ioffe")
        record["document"]["extra"]["journal_id"] = True
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "повторным разбором"):
            read_collected_article(path, source="ioffe", raw_root=self.raw)

    def test_source_and_card_urls_are_not_interchangeable(self) -> None:
        """Карточку другого источника и ложную связь страницы с документом отклоняем."""

        path, record, _ = self._fixture()

        with self.assertRaises(CollectedArticleError):
            read_collected_article(path, source="quantum", raw_root=self.raw)

        record["metadata_page_url"] = JETP_URL.replace("p309", "p326")
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "страницы метаданных"):
            read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_foreign_witness_url_is_rejected_even_with_valid_checksums(self) -> None:
        """Корректные контрольные суммы не превращают чужой домен в допустимый источник."""

        path, record, _ = self._fixture()
        snapshot = read_collected_article(path, source="jetp", raw_root=self.raw).card_response
        foreign = replace(snapshot, requested_url="https://example.test/article", final_url="https://example.test/article")
        self._replace_witness(record["card_response"], foreign)
        self._save_record(path, record)

        with self.assertRaises(CollectedArticleError):
            read_collected_article(path, source="jetp", raw_root=self.raw)

    def _replace_witness(self, evidence: dict[str, Any], snapshot: HttpResponseSnapshot) -> None:
        """Сохранить вымышленный согласованный снимок для проверки семантических ограничений."""

        metadata = snapshot.canonical_metadata()
        digest = hashlib.sha256(snapshot.body).hexdigest()
        metadata_digest = hashlib.sha256(metadata).hexdigest()
        body_path = f"responses/{digest[:2]}/{digest}.bin"
        metadata_path = f"responses/{digest[:2]}/{digest}.{metadata_digest}.http.json"
        (self.raw / body_path).parent.mkdir(parents=True, exist_ok=True)
        (self.raw / body_path).write_bytes(snapshot.body)
        (self.raw / metadata_path).write_bytes(metadata)
        evidence.update(requested_url=snapshot.requested_url, relative_path=body_path, sha256=digest,
                        bytes=len(snapshot.body), response_metadata_path=metadata_path,
                        response_metadata_sha256=metadata_digest)

    def test_non_200_status_and_wrong_mime_are_rejected(self) -> None:
        """Страница отказа или PDF с MIME HTML не становятся исходником статьи."""

        for status, content_type in ((403, "text/html; charset=utf-8"), (200, "application/pdf")):
            with self.subTest(status=status, content_type=content_type):
                path, record, _ = self._fixture()
                snapshot = read_collected_article(path, source="jetp", raw_root=self.raw).card_response
                changed = replace(snapshot, status_code=status, headers=(("content-type", content_type),))
                self._replace_witness(record["card_response"], changed)
                self._save_record(path, record)

                with self.assertRaises(CollectedArticleError):
                    read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_noncanonical_http_metadata_is_rejected(self) -> None:
        """Пересчитанный SHA красивого JSON не заменяет каноническую сериализацию."""

        path, record, _ = self._fixture()
        evidence = record["card_response"]
        metadata = json.loads((self.raw / evidence["response_metadata_path"]).read_bytes())
        content = json.dumps(metadata, indent=2).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        evidence["response_metadata_sha256"] = digest
        evidence["response_metadata_path"] = f"responses/{evidence['sha256'][:2]}/{evidence['sha256']}.{digest}.http.json"
        (self.raw / evidence["response_metadata_path"]).write_bytes(content)
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "каноническое"):
            read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_valid_but_unrelated_pdf_and_page_count_are_rejected(self) -> None:
        """Открываемый PDF должен принадлежать опубликованной ссылке и иметь ожидаемое число страниц."""

        path, record, _ = self._fixture()
        snapshot = read_collected_article(path, source="jetp", raw_root=self.raw).pdf_response
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        url = snapshot.requested_url.replace("0309", "0326")
        self._replace_witness(record["pdf_response"], replace(snapshot, requested_url=url, final_url=url))
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "PDF не соответствует"):
            read_collected_article(path, source="jetp", raw_root=self.raw)

        path, record, _ = self._fixture()
        record["page_count"] = True
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "Число страниц"):
            read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_malformed_pdf_with_correct_hash_is_rejected(self) -> None:
        """Сам по себе хеш не подтверждает открываемость PDF."""

        path, record, _ = self._fixture()
        snapshot = read_collected_article(path, source="jetp", raw_root=self.raw).pdf_response
        assert snapshot is not None
        self._replace_witness(record["pdf_response"], replace(snapshot, body=b"%PDF-1.7\nbroken"))
        self._save_record(path, record)

        with self.assertRaises(CollectedArticleError):
            read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_quantum_issue_toc_selects_only_exact_pdf(self) -> None:
        """Старая статья получает свои метаданные из оглавления, а не название всего выпуска."""

        path, _, _ = self._fixture("quantum", issue_toc=True)
        article = read_collected_article(path, source="quantum", raw_root=self.raw)
        self.assertEqual(article.document.url, ISSUE_PDF_URL)
        self.assertEqual(article.document.title, "Исследование импульсов")
        self.assertEqual(article.card_response.requested_url, ISSUE_URL)

    def test_quantum_toc_rejects_fabricated_target(self) -> None:
        """Отсутствующий в оглавлении файл не становится отдельной статьёй."""

        path, record, _ = self._fixture("quantum", issue_toc=True, with_pdf=False)
        record["document"]["url"] = ISSUE_PDF_URL.replace("test_article", "unknown")
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "Оглавление") as caught:
            read_collected_article(path, source="quantum", raw_root=self.raw)

        self.assertNotIsInstance(caught.exception, AmbiguousIssueEntryError)

    def test_quantum_ambiguous_toc_preserves_all_confirmed_candidates(self) -> None:
        """Общая ссылка сохраняет обе статьи для проверки, не регистрируя выбранную сборщиком."""

        path, record, _ = self._fixture(
            "quantum", issue_toc=True, with_pdf=False, html=AMBIGUOUS_ISSUE_HTML,
        )
        candidates = [
            document.to_dict()
            for document in get_source("quantum").parse_issue_articles(AMBIGUOUS_ISSUE_HTML, ISSUE_URL)
        ]
        self.assertEqual(len(candidates), 2)
        self.assertNotEqual(candidates[0]["title"], candidates[1]["title"])

        for stored_document in candidates:
            with self.subTest(title=stored_document["title"]):
                record["document"] = stored_document
                self._save_record(path, record)
                files_before = {item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()}

                with self.assertRaises(AmbiguousIssueEntryError) as caught:
                    read_collected_article(path, source="quantum", raw_root=self.raw)

                self.assertEqual(caught.exception.details, {
                    "issue_url": ISSUE_URL, "article_url": ISSUE_PDF_URL, "candidates": candidates,
                })
                self.assertIn("ручная проверка", str(caught.exception))
                self.assertEqual(json.loads(json.dumps(caught.exception.details)), caught.exception.details)
                self.assertEqual(files_before, {
                    item: item.read_bytes() for item in self.root.rglob("*") if item.is_file()
                })
                self.client.get_response.assert_not_called()

    def test_quantum_ambiguous_toc_rejects_fabricated_metadata(self) -> None:
        """Наличие двух статей не позволяет отложить подмену вместо ошибки проверки."""

        for field, value in (
            ("title", "Вымышленная статья"), ("authors", ["Неизвестный автор"]),
            ("language", "en"), ("extra", {"first_page": True}),
        ):
            with self.subTest(field=field):
                path, record, _ = self._fixture(
                    "quantum", issue_toc=True, with_pdf=False, html=AMBIGUOUS_ISSUE_HTML,
                )
                record["document"][field] = value
                self._save_record(path, record)

                with self.assertRaisesRegex(CollectedArticleError, "повторным разбором") as caught:
                    read_collected_article(path, source="quantum", raw_root=self.raw)

                self.assertNotIsInstance(caught.exception, AmbiguousIssueEntryError)

    def test_quantum_ambiguous_toc_rejects_corrupted_witnesses(self) -> None:
        """Подлинность тела и HTTP-свидетельства проверяется до выделения неоднозначности."""

        for field in ("relative_path", "response_metadata_path"):
            with self.subTest(field=field):
                path, record, _ = self._fixture(
                    "quantum", issue_toc=True, with_pdf=False, html=AMBIGUOUS_ISSUE_HTML,
                )
                target = self.raw / record["card_response"][field]
                original = target.read_bytes()
                target.write_bytes(original + b"x")

                with self.assertRaisesRegex(CollectedArticleError, "Контрольная сумма") as caught:
                    read_collected_article(path, source="quantum", raw_root=self.raw)

                self.assertNotIsInstance(caught.exception, AmbiguousIssueEntryError)
                target.write_bytes(original)

    def test_quantum_ambiguous_toc_rejects_bad_http_status_and_mime(self) -> None:
        """Правильный SHA не маскирует отказ сервера или неверный тип оглавления."""

        for status, content_type in ((403, "text/html; charset=utf-8"), (200, "application/pdf")):
            with self.subTest(status=status, content_type=content_type):
                path, record, _ = self._fixture(
                    "quantum", issue_toc=True, with_pdf=False, html=AMBIGUOUS_ISSUE_HTML,
                )
                snapshot = HttpResponseSnapshot(
                    requested_url=ISSUE_URL, final_url=ISSUE_URL, status_code=status,
                    headers=(("content-type", content_type),),
                    retrieved_at="2026-10-14T09:00:00+00:00", body=AMBIGUOUS_ISSUE_HTML.encode("utf-8"),
                )
                self._replace_witness(record["card_response"], snapshot)
                self._save_record(path, record)

                with self.assertRaises(CollectedArticleError) as caught:
                    read_collected_article(path, source="quantum", raw_root=self.raw)

                self.assertNotIsInstance(caught.exception, AmbiguousIssueEntryError)

    def test_quantum_ambiguous_toc_rejects_invalid_status_or_pdf_claims(self) -> None:
        """Отложить можно лишь проверенные метаданные без статуса скачанного PDF и страниц."""

        for changes in (
            {"status": "invalid"}, {"status": "skipped_non_russian"}, {"status": "pdf_downloaded"},
            {"pdf_response": {}}, {"page_count": 1}, {"page_count": None},
            {"metadata_page_url": ISSUE_URL.replace("54_1", "54_2")},
            {"source_id": "jetp"}, {"schema_version": "invalid"},
        ):
            with self.subTest(changes=changes):
                path, record, _ = self._fixture(
                    "quantum", issue_toc=True, with_pdf=False, html=AMBIGUOUS_ISSUE_HTML,
                )
                record.update(changes)
                self._save_record(path, record)

                with self.assertRaises(CollectedArticleError) as caught:
                    read_collected_article(path, source="quantum", raw_root=self.raw)

                self.assertNotIsInstance(caught.exception, AmbiguousIssueEntryError)

        path, _, _ = self._fixture(
            "quantum", issue_toc=True, with_pdf=True, html=AMBIGUOUS_ISSUE_HTML,
        )

        with self.assertRaisesRegex(CollectedArticleError, "только без скачанного PDF") as caught:
            read_collected_article(path, source="quantum", raw_root=self.raw)

        self.assertNotIsInstance(caught.exception, AmbiguousIssueEntryError)

    def test_quantum_ambiguous_toc_checks_language_and_title_before_deferral(self) -> None:
        """Даже подтверждённые разбором язык и название должны отвечать условиям партии."""

        for changes, message in (({"language": "en"}, "русскоязычная"), ({"title": ""}, "названием")):
            with self.subTest(changes=changes):
                path, record, _ = self._fixture(
                    "quantum", issue_toc=True, with_pdf=False, html=AMBIGUOUS_ISSUE_HTML,
                )
                documents = get_source("quantum").parse_issue_articles(AMBIGUOUS_ISSUE_HTML, ISSUE_URL)
                documents = [replace(document, **changes) for document in documents]
                record["document"] = documents[0].to_dict()
                self._save_record(path, record)

                with patch("src.collect.quantum_electronics.parse_issue_articles", return_value=documents), \
                     self.assertRaisesRegex(CollectedArticleError, message) as caught:
                    read_collected_article(path, source="quantum", raw_root=self.raw)

                self.assertNotIsInstance(caught.exception, AmbiguousIssueEntryError)

    def test_quantum_ambiguous_toc_checks_profile_before_deferral(self) -> None:
        """Ошибка определения профиля не превращается в разрешённую неоднозначность."""

        path, _, _ = self._fixture(
            "quantum", issue_toc=True, with_pdf=False, html=AMBIGUOUS_ISSUE_HTML,
        )

        with patch("src.corpus.journal_import._source_profile", side_effect=ValueError("Неверный профиль")), \
             self.assertRaisesRegex(CollectedArticleError, "Неверный профиль") as caught:
            read_collected_article(path, source="quantum", raw_root=self.raw)

        self.assertNotIsInstance(caught.exception, AmbiguousIssueEntryError)

    def test_ioffe_profiles_separate_five_journals(self) -> None:
        """Идентификатор журнала берётся из исходных крошек; все пять source_id различаются."""

        source_ids: set[str] = set()

        for journal_id, title in IOFFE_JOURNALS.items():
            html = IOFFE_HTML.replace('/journals/1">Физика твердого тела', f'/journals/{journal_id}">{title}')
            path, _, _ = self._fixture("ioffe", html=html)
            article = read_collected_article(path, source="ioffe", raw_root=self.raw)
            self.assertEqual(article.profile.journal_title, title)
            self.assertFalse(article.profile.automatic_selection)
            source_ids.add(article.profile.source_id)

        self.assertEqual(len(source_ids), 5)

    def test_ioffe_ambiguous_journal_does_not_guess_profile(self) -> None:
        """Без однозначного идентификатора нельзя назначить журнал по одному домену."""

        html = IOFFE_HTML.replace('<a href="/journals/1">Физика твердого тела</a>', "")
        path, _, _ = self._fixture("ioffe", html=html)

        with self.assertRaisesRegex(CollectedArticleError, "не определён однозначный"):
            read_collected_article(path, source="ioffe", raw_root=self.raw)

    def test_non_russian_skip_is_verified_before_special_exception(self) -> None:
        """Пропуск нерусского текста разрешён только после сверки метаданных и свидетельств."""

        html = LETTERS_HTML.replace("Исследование свойств", "Study of properties").replace("воды", "water")
        html = html.replace("Изучены свойства", "Properties studied").replace("Получены результаты.", "Results obtained.")
        path, record, _ = self._fixture("jetp_letters", html=html, with_pdf=False)
        self.assertEqual(record["document"]["language"], "en")
        record["status"] = "skipped_non_russian"
        self._save_record(path, record)

        with self.assertRaises(NonRussianArticleError):
            read_collected_article(path, source="jetp_letters", raw_root=self.raw)

        record["document"]["title"] = "Подмена"
        self._save_record(path, record)

        with self.assertRaises(CollectedArticleError) as caught:
            read_collected_article(path, source="jetp_letters", raw_root=self.raw)

        self.assertNotIsInstance(caught.exception, NonRussianArticleError)

    def test_false_skip_cannot_hide_russian_article(self) -> None:
        """Пометка пропуска не позволяет проигнорировать корректную русскую карточку."""

        path, record, _ = self._fixture(with_pdf=False)
        record["status"] = "skipped_non_russian"
        self._save_record(path, record)

        with self.assertRaisesRegex(CollectedArticleError, "противоречит"):
            read_collected_article(path, source="jetp", raw_root=self.raw)

    def test_previous_profile_identifiers_are_unchanged(self) -> None:
        """Новые журналы не переименовывают существующие профили УФН и КЭ."""

        self.assertEqual(SOURCE_PROFILES["ufn"].source_id, "S01_UFN_RU")
        self.assertEqual(SOURCE_PROFILES["quantum_electronics"].source_id, "S06_QUANTUM_ELECTRONICS_RU")
        self.assertEqual(get_source_profile("auto", source="jetp", url=JETP_URL).key, "jetp")

        with self.assertRaises(ValueError):
            get_source_profile("auto", source="journals.ioffe.ru", url=IOFFE_URL)


if __name__ == "__main__":
    unittest.main()
