"""Проверки отдельного учёта смежной тематики без изменения допуска корпуса."""

from __future__ import annotations

import json
import unittest

from pathlib import Path
from typing import Any

from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORPUS_GROUP_IDS = ("adjacent_experimental_computing", "adjacent_computational_methods")


class CorpusGroupSchemaTests(unittest.TestCase):
    """Проверки необязательной тематической группы в works-v1."""

    def setUp(self) -> None:
        """Загрузить схему и прежний пример журнальной работы."""

        self.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")
        template = PROJECT_ROOT / "manifests" / "templates" / "works.example.jsonl"
        self.article: dict[str, Any] = json.loads(
            template.read_text(encoding="utf-8").splitlines()[0]
        )

    def test_group_preserves_journal_genres_and_source(self) -> None:
        """Тематическая группа не меняет журнал, источник или поджанр статьи."""

        for group in CORPUS_GROUP_IDS:
            for genre in ("research_article", "review_article", "short_communication"):
                with self.subTest(group=group, genre=genre):
                    record = {
                        **self.article,
                        "corpus_group_id": group,
                        "genre": genre,
                        "eligibility_status": "pending",
                    }
                    self.catalog.validate("works", record)

                    self.assertEqual(record["source_id"], self.article["source_id"])
                    self.assertEqual(record["journal_id"], self.article["journal_id"])

    def test_existing_article_does_not_require_a_group(self) -> None:
        """Отсутствие новой группы не требует миграции старой статьи."""

        self.catalog.validate("works", self.article)
        self.assertNotIn("corpus_group_id", self.article)

    def test_unknown_or_invalid_group_is_rejected(self) -> None:
        """Произвольная строка или иной тип не становятся утверждённой группой."""

        for value in (None, "", "physics_core", "other", 1, 1.5, True, [], {}):
            with self.subTest(value=value):
                record = {**self.article, "corpus_group_id": value}

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_group_does_not_enable_training_eligibility(self) -> None:
        """Смежные статьи пока нельзя объявить допущенными к основному корпусу."""

        for group in CORPUS_GROUP_IDS:
            with self.subTest(group=group):
                record = {
                    **self.article,
                    "corpus_group_id": group,
                    "eligibility_status": "eligible",
                }

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_group_rejection_requires_a_reason(self) -> None:
        """Карантин и исключение сохраняют обязательное пояснение причины."""

        for group in CORPUS_GROUP_IDS:
            for status in ("rejected", "quarantined"):
                with self.subTest(group=group, status=status):
                    record = {
                        **self.article,
                        "corpus_group_id": group,
                        "eligibility_status": status,
                        "exclusion_reason": None,
                    }

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)

                    record["exclusion_reason"] = "Синтетическая причина проверки"
                    self.catalog.validate("works", record)

    def test_group_is_not_assigned_to_other_genres(self) -> None:
        """Решение по журнальным статьям не распространяется на остальные жанры."""

        for group in CORPUS_GROUP_IDS:
            for genre in ("preprint", "collection_paper", "other"):
                with self.subTest(group=group, genre=genre):
                    record = {
                        **self.article,
                        "corpus_group_id": group,
                        "genre": genre,
                        "eligibility_status": "pending",
                    }

                    if genre in {"preprint", "collection_paper"}:
                        record["journal_id"] = None
                        record["journal_title"] = None

                    if genre == "collection_paper":
                        record["collection_title"] = "Синтетический сборник"
                        record["source_document_type"] = "Book chapter"

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)


if __name__ == "__main__":
    unittest.main()
