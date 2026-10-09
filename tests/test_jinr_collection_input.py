"""Проверки отдельного пакета Book chapter, его полнотекстового статуса и происхождения."""

from __future__ import annotations

import copy
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.corpus.jinr_article_input import read_article_batch
from src.corpus.jinr_collection_input import read_collection_batch
from tests.jinr_article_fixtures import ArticleFixture
from tests.jinr_collection_fixtures import CollectionFixture


class CollectionInputTests(unittest.TestCase):
    """Отделить полный материал сборника от журнальной статьи, тезисов и смешанного PDF."""

    def setUp(self) -> None:
        """Создать независимый временный пакет с двумя историями скачивания PDF."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.fixture = CollectionFixture(self.root, aliases=2)

    def tearDown(self) -> None:
        """Удалить только временный каталог текущего теста."""

        self.temporary_directory.cleanup()

    def test_full_collection_paper_preserves_original_type_without_writes_or_network(self) -> None:
        """Чтение не изменяет исходники и не выдаёт проверку ассистента за экспертную разметку."""

        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        with patch("urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена")):
            batch = read_collection_batch(self.root, self.fixture.path)

        material = batch.entries[0]
        self.assertEqual(batch.path, "manifests/imports/collections.json")
        self.assertEqual(material.item, self.fixture.item)
        self.assertEqual(material.item["metadata"]["dc.type"], [{"value": "Book chapter"}])
        self.assertEqual(material.entry["genre"], "collection_paper")
        self.assertIsNone(material.entry["journal_id"])
        self.assertNotIn("journal_title_as_reported", material.entry)
        self.assertEqual(material.entry["review_status"]["human_selection"], "pending")
        self.assertEqual(material.acquisitions, tuple(self.fixture.inventory))
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_readers_do_not_mix_article_and_collection_formats(self) -> None:
        """Журнальный вход остаётся только Article, а отдельный вход не принимает старый пакет."""

        with self.assertRaisesRegex(ValueError, "schema_version"):
            read_article_batch(self.root, self.fixture.path)

        article_fixture = ArticleFixture(self.root)

        with self.assertRaisesRegex(ValueError, "schema_version"):
            read_collection_batch(self.root, article_fixture.path)

        article_fixture.item["metadata"]["dc.type"] = [{"value": "Book chapter"}]
        article_fixture.save_metadata()

        with self.assertRaisesRegex(ValueError, "тип Article"):
            read_article_batch(self.root, article_fixture.path)

    def test_collection_api_type_must_be_exactly_book_chapter(self) -> None:
        """Даже согласованные копии API не превращают иной тип публикации в материал сборника."""

        for document_type in ("Article", "Preprint", "Conference abstract", "Other"):
            with self.subTest(document_type=document_type):
                self.fixture.item["metadata"]["dc.type"] = [{"value": document_type}]
                self.fixture.save_metadata()

                with self.assertRaisesRegex(ValueError, "тип Book chapter"):
                    read_collection_batch(self.root, self.fixture.path)

    def test_review_hash_and_path_are_checked(self) -> None:
        """Пакет не может незаметно подменить или вынести свидетельство за manifests."""

        path = self.root / self.fixture.review_path
        original = path.read_bytes()
        path.write_bytes(original + b"\n")

        with self.assertRaisesRegex(ValueError, "sha256"):
            read_collection_batch(self.root, self.fixture.path)

        path.write_bytes(original)
        self.fixture.entry["material_review"]["path"] = "data/review.json"
        self.fixture.save_batch()

        with self.assertRaisesRegex(ValueError, "каталог"):
            read_collection_batch(self.root, self.fixture.path)

    def test_abstract_mixed_document_and_other_field_are_rejected(self) -> None:
        """Тип Book chapter сам по себе не подтверждает полнотекстовый физический материал."""

        original = copy.deepcopy(self.fixture.report["records"][0])

        for field, value in (
            ("material_kind", "conference_abstract"),
            ("contains_other_publications", True),
            ("completeness", "partial_paper"),
            ("topic_decision", "other_field"),
            ("next_batch_proposal", False),
            ("reviewed_identity_status", "needs_review"),
            ("main_text_language", "en"),
        ):
            with self.subTest(field=field):
                self.fixture.report["records"][0] = copy.deepcopy(original)
                self.fixture.report["records"][0][field] = value
                self.fixture.save_review()

                with self.assertRaisesRegex(ValueError, field):
                    read_collection_batch(self.root, self.fixture.path)

    def test_target_and_review_pages_must_cover_whole_document(self) -> None:
        """Выделение части PDF или пропущенные страницы не проходят регистрацию целого файла."""

        original = copy.deepcopy(self.fixture.report["records"][0])

        for field in ("target_pdf_pages", "reviewed_pdf_pages"):
            for pages in ([1, 2], [2, 3], [1, 3, 2], [1, 2, 2], [True, 2, 3], []):
                with self.subTest(field=field, pages=pages):
                    self.fixture.report["records"][0] = copy.deepcopy(original)
                    self.fixture.report["records"][0][field] = pages
                    self.fixture.save_review()

                    with self.assertRaisesRegex(ValueError, field):
                        read_collection_batch(self.root, self.fixture.path)

    def test_page_count_must_be_positive_integer(self) -> None:
        """Нулевая, строковая или логическая длина не подтверждает проверку всех страниц."""

        for page_count in (0, -1, True, "3", None):
            with self.subTest(page_count=page_count):
                self.fixture.report["records"][0]["extraction_probe"]["pages"] = page_count
                self.fixture.save_review()

                with self.assertRaisesRegex(ValueError, "число страниц"):
                    read_collection_batch(self.root, self.fixture.path)

    def test_review_identity_and_metadata_cannot_be_transferred_to_another_pdf(self) -> None:
        """Проверка должна точно относиться к выбранным PDF, карточке и строке сопоставления."""

        original = copy.deepcopy(self.fixture.report["records"][0])

        for field, value in (
            ("pdf_path", self.fixture.inventory[0]["staged_relative_path"]),
            ("pdf_sha256", "0" * 64),
            ("item_uuid", "00000000-0000-0000-0000-000000000099"),
            ("matches_line", 1),
            ("title_as_reported", "Чужой материал"),
            ("collection_title_as_reported", ["Другой сборник"]),
            ("metadata_source", {}),
            ("metadata_document_type_as_reported", ["Article"]),
        ):
            with self.subTest(field=field):
                self.fixture.report["records"][0] = copy.deepcopy(original)
                self.fixture.report["records"][0][field] = value
                self.fixture.save_review()

                with self.assertRaisesRegex(ValueError, field):
                    read_collection_batch(self.root, self.fixture.path)

    def test_review_inputs_must_refer_to_exact_original_sources(self) -> None:
        """Хешированный обзор не заменяет проверку его ссылок на matches и inventory."""

        original = copy.deepcopy(self.fixture.report["inputs"])
        self.fixture.report["inputs"][0]["sha256"] = "0" * 64
        self.fixture.save_review(sync_sources=False)

        with self.assertRaisesRegex(ValueError, "sha256"):
            read_collection_batch(self.root, self.fixture.path)

        self.fixture.report["inputs"] = original
        self.fixture.report["inputs"][0]["path"] = self.fixture.selection_path
        self.fixture.save_review(sync_sources=False)

        with self.assertRaisesRegex(ValueError, "matches и inventory"):
            read_collection_batch(self.root, self.fixture.path)

    def test_review_requires_explicit_proposal_and_unique_record(self) -> None:
        """Одного присутствия материала в отчёте недостаточно для его регистрации."""

        original = copy.deepcopy(self.fixture.report)
        self.fixture.report["proposed_next_batch"] = []
        self.fixture.save_review()

        with self.assertRaisesRegex(ValueError, "явно предложен"):
            read_collection_batch(self.root, self.fixture.path)

        self.fixture.report = original
        self.fixture.report["records"].append(copy.deepcopy(self.fixture.report["records"][0]))
        self.fixture.save_review()

        with self.assertRaisesRegex(ValueError, "Повторное значение review_id"):
            read_collection_batch(self.root, self.fixture.path)

    def test_collection_genre_and_container_cannot_be_replaced_by_journal(self) -> None:
        """Сборнику нельзя приписать журнальный жанр или переименовать исходное издание."""

        original = copy.deepcopy(self.fixture.entry)

        for field, value in (
            ("genre", "research_article"),
            ("journal_id", "journal:test"),
            ("journal_title", "Журнал"),
            ("journal_title_as_reported", ["Журнал"]),
            ("collection_title_as_reported", ["Другой сборник"]),
        ):
            with self.subTest(field=field):
                self.fixture.entry.clear()
                self.fixture.entry.update(copy.deepcopy(original))
                self.fixture.entry[field] = value
                self.fixture.save_batch()

                with self.assertRaises(ValueError):
                    read_collection_batch(self.root, self.fixture.path)

    def test_selection_retains_collection_name_and_all_pdf_acquisitions(self) -> None:
        """Производный отбор и история файлов не могут расходиться с исходными свидетельствами."""

        self.fixture.selection[0]["collection_title"] = ["Другой сборник"]
        self.fixture.save_sources()

        with self.assertRaisesRegex(ValueError, "collection_title"):
            read_collection_batch(self.root, self.fixture.path)

        self.fixture._sync_metadata_copies()
        self.fixture.entry["acquisition_evidence"][0]["acquisition_method"] = "crawler"
        self.fixture.save_sources()

        with self.assertRaisesRegex(ValueError, "acquisition_method"):
            read_collection_batch(self.root, self.fixture.path)

    def test_material_review_does_not_authorize_labels_or_training(self) -> None:
        """Статус экспертной разметки и допуска не подменяется проверкой полного текста."""

        original = copy.deepcopy(self.fixture.report["records"][0])

        for field, value in (
            ("assigns_grnti_label", True),
            ("assigns_training_eligibility", True),
            ("human_review_status", "approved"),
            ("original_bitstream_item_relation_verified", True),
            ("new_server_relation_verified", True),
            ("registered", True),
        ):
            with self.subTest(field=field):
                self.fixture.report["records"][0] = copy.deepcopy(original)
                self.fixture.report["records"][0][field] = value
                self.fixture.save_review()

                with self.assertRaisesRegex(ValueError, field):
                    read_collection_batch(self.root, self.fixture.path)


if __name__ == "__main__":
    unittest.main()
