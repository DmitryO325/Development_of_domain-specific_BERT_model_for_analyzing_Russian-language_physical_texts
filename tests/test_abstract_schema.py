"""Проверки отдельного жанра тезисов без допуска в исследовательскую выборку."""

from __future__ import annotations

import json
import unittest

from pathlib import Path
from typing import Any

from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AbstractWorkSchemaTests(unittest.TestCase):
    """Проверки обязательных реквизитов тезисов и сохранения ограничений корпуса."""

    def setUp(self) -> None:
        """Загрузить схему и совместимый пример журнальной статьи."""

        self.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")
        template_path = PROJECT_ROOT / "manifests" / "templates" / "works.example.jsonl"
        self.article: dict[str, Any] = json.loads(
            template_path.read_text(encoding="utf-8").splitlines()[0]
        )

    def _conference_abstract(self) -> dict[str, Any]:
        """Создать ожидающие проверки тезисы из сборника с известным годом."""

        return {
            **self.article,
            "source_group_id": "F04_JINR_REPOSITORY",
            "source_id": "F04_JINR_ABSTRACTS_RU",
            "genre": "conference_abstract",
            "journal_id": None,
            "journal_title": None,
            "collection_title": "Вымышленный сборник тезисов конференции по физике",
            "source_document_type": "Book chapter",
            "published_at": None,
            "published_year": 2024,
            "eligibility_status": "pending",
        }

    def test_pending_abstract_preserves_collection_type_and_year(self) -> None:
        """Тезисы сохраняют сборник и год без выдуманных журнала и полной даты."""

        record = self._conference_abstract()
        self.catalog.validate("works", record)

        self.assertIsNone(record["journal_id"])
        self.assertIsNone(record["journal_title"])
        self.assertIsNone(record["published_at"])
        self.assertEqual(record["published_year"], 2024)
        self.assertEqual(record["source_document_type"], "Book chapter")

    def test_abstract_requires_collection_title_and_source_type(self) -> None:
        """Одного жанра тезисов недостаточно без обязательных полей сборника."""

        for field_name in ("collection_title", "source_document_type"):
            with self.subTest(field_name=field_name):
                record = self._conference_abstract()
                del record[field_name]

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_abstract_rejects_empty_or_invalid_collection_title(self) -> None:
        """Название сборника обязательно содержит непробельный текст."""

        for title in (None, "", " \t\n", 2024, []):
            with self.subTest(title=title):
                record = self._conference_abstract()
                record["collection_title"] = title

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_abstract_requires_exact_book_chapter_type(self) -> None:
        """Исходный тип Book chapter сохраняется без переименования в Abstract."""

        for source_type in ("Article", "Abstract", "Conference paper", "book chapter", None):
            with self.subTest(source_type=source_type):
                record = self._conference_abstract()
                record["source_document_type"] = source_type

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_abstract_journal_fields_must_be_null(self) -> None:
        """Сборник тезисов не занимает реквизиты журнала даже пустой строкой."""

        for field_name in ("journal_id", "journal_title"):
            for value in ("", "Название сборника"):
                with self.subTest(field_name=field_name, value=value):
                    record = self._conference_abstract()
                    record[field_name] = value

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)

    def test_article_cannot_be_relabelled_without_collection_metadata(self) -> None:
        """Простая замена жанра статьи не создаёт корректную запись тезисов."""

        record = {**self.article, "genre": "conference_abstract"}

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("works", record)

    def test_abstract_cannot_be_marked_eligible(self) -> None:
        """Полные дата и аннотация не допускают тезисы в исследовательскую выборку."""

        record = {
            **self._conference_abstract(),
            "published_at": self.article["published_at"],
            "abstract": "Содержательная аннотация тезисов конференции.",
            "eligibility_status": "eligible",
        }

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("works", record)

    def test_adjacent_article_groups_are_forbidden_for_abstracts(self) -> None:
        """Смежные группы журнальных статей не расширяются на тезисы."""

        for group_id in (
            "adjacent_experimental_computing",
            "adjacent_computational_methods",
        ):
            with self.subTest(group_id=group_id):
                record = {**self._conference_abstract(), "corpus_group_id": group_id}

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_rejected_and_quarantined_abstracts_require_reason(self) -> None:
        """Исключение или карантин тезисов требуют явной причины."""

        for status in ("rejected", "quarantined"):
            with self.subTest(status=status):
                record = {**self._conference_abstract(), "eligibility_status": status}

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

                record["exclusion_reason"] = "Синтетическая причина исключения"
                self.catalog.validate("works", record)


if __name__ == "__main__":
    unittest.main()
