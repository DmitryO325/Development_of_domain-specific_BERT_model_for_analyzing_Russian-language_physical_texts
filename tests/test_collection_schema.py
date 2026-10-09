"""Проверки отдельного учёта полнотекстовых материалов научных сборников."""

from __future__ import annotations

import json
import unittest

from pathlib import Path
from typing import Any

from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JOURNAL_GENRES = ("research_article", "review_article", "short_communication")
LEGACY_GENRES = (*JOURNAL_GENRES, "preprint", "other")


class CollectionWorkSchemaTests(unittest.TestCase):
    """Проверки жанра сборника без смешения с журналами и расширения допуска."""

    def setUp(self) -> None:
        """Загрузить каталог схем и совместимый пример журнальной статьи."""

        self.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")
        template_path = PROJECT_ROOT / "manifests" / "templates" / "works.example.jsonl"
        self.article: dict[str, Any] = json.loads(
            template_path.read_text(encoding="utf-8").splitlines()[0]
        )

    def _collection_paper(self) -> dict[str, Any]:
        """Создать ожидающую проверки работу сборника с известным годом."""

        return {
            **self.article,
            "genre": "collection_paper",
            "journal_id": None,
            "journal_title": None,
            "collection_title": "Вымышленный сборник научных работ по физике",
            "source_document_type": "Book chapter",
            "published_at": None,
            "published_year": 2024,
            "eligibility_status": "pending",
        }

    def test_pending_collection_paper_preserves_source_type_and_year(self) -> None:
        """Название сборника и исходный тип хранятся без выдуманного журнала."""

        record = self._collection_paper()
        self.catalog.validate("works", record)

        self.assertIsNone(record["journal_id"])
        self.assertIsNone(record["journal_title"])
        self.assertIsNone(record["published_at"])
        self.assertEqual(record["published_year"], 2024)
        self.assertEqual(record["source_document_type"], "Book chapter")

    def test_collection_requires_title_and_original_type(self) -> None:
        """Без названия сборника или исходного типа отдельная запись неполна."""

        for field_name in ("collection_title", "source_document_type"):
            with self.subTest(field_name=field_name):
                record = self._collection_paper()
                del record[field_name]

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_collection_rejects_empty_or_invalid_title(self) -> None:
        """Название сборника не может состоять из пробелов или иметь другой тип."""

        for title in (None, "", " \t\n", 2024, []):
            with self.subTest(title=title):
                record = self._collection_paper()
                record["collection_title"] = title

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_collection_does_not_accept_a_pseudo_journal(self) -> None:
        """Сборник нельзя записать в реквизиты журнала даже при отдельном жанре."""

        for field_name in ("journal_id", "journal_title"):
            for value in ("", "Название сборника"):
                with self.subTest(field_name=field_name, value=value):
                    record = self._collection_paper()
                    record[field_name] = value

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)

    def test_collection_requires_exact_source_document_type(self) -> None:
        """Тезисы, журнальные статьи и произвольные типы не подменяют Book chapter."""

        for source_type in ("Article", "Abstract", "Conference paper", "book chapter", None):
            with self.subTest(source_type=source_type):
                record = self._collection_paper()
                record["source_document_type"] = source_type

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_unspecified_conference_genres_are_not_added(self) -> None:
        """Произвольные жанры конференций не подменяют утверждённые категории."""

        for genre in ("abstract", "conference_paper"):
            with self.subTest(genre=genre):
                record = self._collection_paper()
                record["genre"] = genre

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_abstract_genre_requires_collection_metadata(self) -> None:
        """Замена жанра на тезисы не отменяет обязательных реквизитов сборника."""

        for field_name in ("collection_title", "source_document_type"):
            with self.subTest(field_name=field_name):
                record = self._collection_paper()
                record["genre"] = "conference_abstract"
                del record[field_name]

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_collection_cannot_be_marked_eligible(self) -> None:
        """Даже полные дата и аннотация не дают сборнику допуска к выборке."""

        record = self._collection_paper()
        record["published_at"] = self.article["published_at"]
        record["eligibility_status"] = "eligible"

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("works", record)

    def test_collection_can_be_rejected_with_a_reason(self) -> None:
        """Отдельный жанр сохраняет обычные статусы исключения и карантина."""

        for status in ("rejected", "quarantined"):
            with self.subTest(status=status):
                record = self._collection_paper()
                record["eligibility_status"] = status

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

                record["exclusion_reason"] = "Синтетическая причина исключения"
                self.catalog.validate("works", record)

    def test_existing_genres_remain_valid_without_collection_fields(self) -> None:
        """Исторические статьи и препринты не требуют миграции works-v1."""

        for genre in LEGACY_GENRES:
            with self.subTest(genre=genre):
                record = {**self.article, "genre": genre}

                if genre == "preprint":
                    record["journal_id"] = None
                    record["journal_title"] = None

                self.catalog.validate("works", record)

    def test_collection_fields_do_not_leak_into_other_genres(self) -> None:
        """Реквизиты сборника нельзя добавить к журнальной статье или препринту."""

        collection = self._collection_paper()

        for genre in LEGACY_GENRES:
            for field_name in ("collection_title", "source_document_type"):
                with self.subTest(genre=genre, field_name=field_name):
                    record = {
                        **self.article,
                        "genre": genre,
                        field_name: collection[field_name],
                    }

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)

    def test_existing_eligible_journal_genres_remain_valid(self) -> None:
        """Прежний допуск трёх журнальных жанров не меняется."""

        for genre in JOURNAL_GENRES:
            with self.subTest(genre=genre):
                record = {
                    **self.article,
                    "genre": genre,
                    "eligibility_status": "eligible",
                }
                self.catalog.validate("works", record)


if __name__ == "__main__":
    unittest.main()
