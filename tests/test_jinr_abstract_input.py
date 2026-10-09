"""Проверки отдельного жанра тезисов, исторических свидетельств и согласия."""

from __future__ import annotations

import copy
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from src.corpus.jinr_abstract_input import read_abstract_batch
from src.corpus.jinr_article_input import read_article_batch
from src.corpus.jinr_collection_input import read_collection_batch
from tests.jinr_abstract_fixtures import AbstractFixture
from tests.jinr_collection_fixtures import CollectionFixture


class AbstractInputTests(unittest.TestCase):
    """Разделить разрешение жанра, проверку PDF и допуск к обучению."""

    def setUp(self) -> None:
        """Создать отдельный пакет тезисов с двумя историями ручного получения."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.fixture = AbstractFixture(self.root, aliases=2)

    def _read(self) -> None:
        """Проверить текущий тестовый пакет без записи."""

        read_abstract_batch(self.root, self.fixture.path)

    def test_separate_approval_preserves_history_without_writes_or_network(self) -> None:
        """Новое согласие принимает тезисы с прежним отказом от полнотекстового пакета."""

        self.fixture.report["proposed_next_batch"] = ["jinr-collection-review:" + "f" * 64]
        self.fixture.save_review()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        with patch("urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена")):
            batch = read_abstract_batch(self.root, self.fixture.path)

        material = batch.entries[0]
        self.assertEqual(batch.path, "manifests/imports/abstracts.json")
        self.assertEqual(material.item, self.fixture.item)
        self.assertEqual(material.entry["genre"], "conference_abstract")
        self.assertEqual(material.item["metadata"]["dc.type"], [{"value": "Book chapter"}])
        self.assertIsNone(material.entry["journal_id"])
        self.assertEqual(material.acquisitions, tuple(self.fixture.inventory))
        self.assertFalse(self.fixture.report["records"][0]["next_batch_proposal"])
        self.assertFalse(self.fixture.report["records"][0]["registered"])
        self.assertEqual(material.entry["review_status"]["human_selection"], "pending")
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_readers_keep_formats_separate(self) -> None:
        """Маршрут тезисов не меняет ограничения журналов и полных материалов сборников."""

        for reader in (read_article_batch, read_collection_batch):
            with self.subTest(reader=reader.__name__), self.assertRaisesRegex(ValueError, "schema_version"):
                reader(self.root, self.fixture.path)

        collection = CollectionFixture(self.root)

        with self.assertRaisesRegex(ValueError, "schema_version"):
            read_abstract_batch(self.root, collection.path)

    def test_api_type_must_remain_exactly_book_chapter(self) -> None:
        """Новый жанр не подменяет исходный тип публикации в API."""

        for document_type in ("Article", "Preprint", "Conference abstract", "Other"):
            with self.subTest(document_type=document_type):
                self.fixture.item["metadata"]["dc.type"] = [{"value": document_type}]
                self.fixture.save_metadata()

                with self.assertRaisesRegex(ValueError, "тип Book chapter"):
                    self._read()

    def test_genre_and_container_cannot_be_replaced(self) -> None:
        """Тезисы нельзя переименовать в полную работу или журнальное издание."""

        original = copy.deepcopy(self.fixture.entry)

        for field, value in (
            ("genre", "collection_paper"),
            ("genre", "research_article"),
            ("journal_id", "journal:test"),
            ("journal_title", "Журнал"),
            ("journal_title_as_reported", ["Журнал"]),
            ("collection_title_as_reported", ["Другой сборник"]),
            ("review_status", {"human_selection": "approved", "registration": "not_started"}),
        ):
            with self.subTest(field=field, value=value):
                self.fixture.entry.clear()
                self.fixture.entry.update(copy.deepcopy(original))
                self.fixture.entry[field] = value
                self.fixture.save_batch()

                with self.assertRaises(ValueError):
                    self._read()

    def test_historical_review_rejects_full_papers_mixed_content_and_admission(self) -> None:
        """Согласие на жанр не разрешает чужой текст, полную статью или экспертный допуск."""

        original = copy.deepcopy(self.fixture.report["records"][0])

        for field, value in (
            ("material_kind", "full_collection_paper"),
            ("contains_other_publications", True),
            ("completeness", "complete_paper"),
            ("completeness", "partial_abstract"),
            ("topic_decision", "other_field"),
            ("next_batch_proposal", True),
            ("registered", True),
            ("reviewed_identity_status", "needs_review"),
            ("main_text_language", "en"),
            ("human_review_status", "approved"),
            ("assigns_grnti_label", True),
            ("assigns_training_eligibility", True),
            ("original_bitstream_item_relation_verified", True),
            ("new_server_relation_verified", True),
        ):
            with self.subTest(field=field, value=value):
                self.fixture.report["records"][0] = copy.deepcopy(original)
                self.fixture.report["records"][0][field] = value
                self.fixture.save_review()

                with self.assertRaisesRegex(ValueError, field):
                    self._read()

    def test_review_identity_and_metadata_must_match_selected_pdf(self) -> None:
        """Историческую проверку нельзя перенести на другие PDF, карточку или издание."""

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
            ("issued_as_reported", ["1999"]),
            ("doi_as_reported", ["10.9999/other"]),
        ):
            with self.subTest(field=field):
                self.fixture.report["records"][0] = copy.deepcopy(original)
                self.fixture.report["records"][0][field] = value
                self.fixture.save_review()

                with self.assertRaisesRegex(ValueError, field):
                    self._read()

    def test_pages_must_cover_whole_document(self) -> None:
        """Чужие страницы, пропуски, дубли и логические номера не проходят проверку."""

        original = copy.deepcopy(self.fixture.report["records"][0])

        for field in ("target_pdf_pages", "reviewed_pdf_pages"):
            for pages in ([1, 2], [2, 3], [1, 3, 2], [1, 2, 2], [True, 2, 3], [1, 2, 3, 4], []):
                with self.subTest(field=field, pages=pages):
                    self.fixture.report["records"][0] = copy.deepcopy(original)
                    self.fixture.report["records"][0][field] = pages
                    self.fixture.save_review()

                    with self.assertRaisesRegex(ValueError, field):
                        self._read()

    def test_probe_requires_same_hash_and_positive_page_count(self) -> None:
        """Пробное извлечение подтверждает тот же PDF с действительным числом страниц."""

        for page_count in (0, -1, True, "3", None):
            with self.subTest(page_count=page_count):
                self.fixture.report["records"][0]["extraction_probe"]["pages"] = page_count
                self.fixture.save_review()

                with self.assertRaisesRegex(ValueError, "число страниц"):
                    self._read()

        self.fixture.report["records"][0]["extraction_probe"] = {"pages": 3, "pdf_sha256": "0" * 64}
        self.fixture.save_review()

        with self.assertRaisesRegex(ValueError, "pdf_sha256"):
            self._read()

    def test_review_hash_sources_and_unique_identity_are_checked(self) -> None:
        """Хешированный обзор сохраняет единственную запись и точные ссылки на исходники."""

        path = self.root / self.fixture.review_path
        original_body = path.read_bytes()
        path.write_bytes(original_body + b"\n")

        with self.assertRaisesRegex(ValueError, "sha256"):
            self._read()

        path.write_bytes(original_body)
        original = copy.deepcopy(self.fixture.report)
        self.fixture.report["inputs"][0]["sha256"] = "0" * 64
        self.fixture.save_review(sync_sources=False)

        with self.assertRaisesRegex(ValueError, "sha256"):
            self._read()

        self.fixture.report = copy.deepcopy(original)
        self.fixture.report["inputs"][0]["path"] = self.fixture.selection_path
        self.fixture.save_review(sync_sources=False)

        with self.assertRaisesRegex(ValueError, "matches и inventory"):
            self._read()

        self.fixture.report = original
        self.fixture.report["records"].append(copy.deepcopy(original["records"][0]))
        self.fixture.save_review()

        with self.assertRaisesRegex(ValueError, "Повторное значение review_id"):
            self._read()

    def test_missing_approval_is_rejected(self) -> None:
        """Проверки тезисов недостаточно без отдельного явного согласия на жанр."""

        self.fixture.entry.pop("genre_approval")
        self.fixture.save_batch()

        with self.assertRaisesRegex(ValueError, "genre_approval"):
            self._read()

    def test_approval_hash_and_paths_stay_inside_manifests(self) -> None:
        """Подмена согласия или ссылка вне manifests отклоняется до регистрации."""

        path = self.root / self.fixture.approval_path
        original = path.read_bytes()
        path.write_bytes(original + b"\n")

        with self.assertRaisesRegex(ValueError, "sha256"):
            self._read()

        path.write_bytes(original)

        for field in ("genre_approval", "material_review"):
            original_reference = copy.deepcopy(self.fixture.entry[field])
            self.fixture.entry[field]["path"] = "data/review.json"
            self.fixture.save_batch()

            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "каталог"):
                self._read()

            self.fixture.entry[field] = original_reference

    def test_approval_scope_genre_decision_and_source_are_exact(self) -> None:
        """Разрешение другого жанра, этапа или исторического обзора неприменимо."""

        original = copy.deepcopy(self.fixture.approval)

        for field, value in (
            ("schema_version", "unknown-v1"),
            ("approved", False),
            ("approved", 1),
            ("genre", "collection_paper"),
            ("scope", "training"),
            ("decision_id", " "),
            ("source_review", {"path": self.fixture.review_path, "sha256": "0" * 64}),
            ("source_review", {"path": "manifests/other.json", "sha256": self.fixture.entry["material_review"]["sha256"]}),
        ):
            with self.subTest(field=field, value=value):
                self.fixture.approval = copy.deepcopy(original)
                self.fixture.approval[field] = value
                self.fixture.save_approval(sync_review=False)

                with self.assertRaisesRegex(ValueError, field):
                    self._read()

        self.fixture.approval = original
        self.fixture.save_approval()
        self.fixture.entry["genre_approval"]["decision_id"] = "DEC-OTHER"
        self.fixture.save_batch()

        with self.assertRaisesRegex(ValueError, "decision_id"):
            self._read()

    def test_approval_requires_selected_review_in_unique_nonempty_list(self) -> None:
        """Согласие на другие тезисы и повторные либо пустые идентификаторы отклоняются."""

        review_id = self.fixture.entry["material_review"]["review_id"]

        for review_ids in (None, [], [review_id, review_id], [" "], [1], ["other-review"]):
            with self.subTest(review_ids=review_ids):
                self.fixture.approval["approved_review_ids"] = review_ids
                self.fixture.save_approval()

                with self.assertRaisesRegex(ValueError, "approved_review_ids|review_id"):
                    self._read()

    def test_pdf_and_api_hashes_are_checked(self) -> None:
        """Разрешение жанра не заменяет проверку сохранённых PDF и ответа API."""

        for path in (
            self.fixture.entry["pdf"]["path"],
            self.fixture.entry["metadata_source"]["api_response_path"],
        ):
            original = (self.root / path).read_bytes()
            (self.root / path).write_bytes(original + b"\n")

            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "sha256|SHA-256"):
                self._read()

            (self.root / path).write_bytes(original)


if __name__ == "__main__":
    unittest.main()
