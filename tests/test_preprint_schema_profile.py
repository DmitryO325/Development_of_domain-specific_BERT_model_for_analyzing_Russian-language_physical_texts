"""Проверки профиля препринтов ОИЯИ и совместимого расширения карточки работы."""

from __future__ import annotations

import json
import unittest

from pathlib import Path
from typing import Any

from src.corpus.profiles import get_source_profile
from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ITEM_UUID = "c1de2fec-3e07-4e15-903c-370df5e5a9c8"


class PreprintProfileTests(unittest.TestCase):
    """Проверки явного выбора профиля и собственных идентификаторов ОИЯИ."""

    def test_profile_does_not_invent_journal(self) -> None:
        """Препринт имеет источник ОИЯИ, но не выдуманные реквизиты журнала."""

        profile = get_source_profile("jinr_preprints")

        self.assertEqual(profile.source_group_id, "F04_JINR_REPOSITORY")
        self.assertEqual(profile.source_id, "F04_JINR_PREPRINTS_RU")
        self.assertEqual(profile.platform, "pubrepo.jinr.ru")
        self.assertIsNone(profile.journal_id)
        self.assertIsNone(profile.journal_title)

    def test_native_id_uses_explicit_metadata(self) -> None:
        """UUID карточки можно передать с метаданными без разбора handle."""

        profile = get_source_profile("jinr_preprints")
        native_id = profile.native_id(
            "https://pubrepo.jinr.ru/handle/123456789/1234",
            {"source_work_id": ITEM_UUID},
        )

        self.assertEqual(native_id, ITEM_UUID)

    def test_native_id_recognizes_item_urls(self) -> None:
        """Идентификатор извлекается из URL публикации и её карточки API."""

        profile = get_source_profile("jinr_preprints")
        urls = (
            f"https://pubrepo.jinr.ru/entities/publication/{ITEM_UUID}",
            f"https://pubrepo.jinr.ru/entities/publication/{ITEM_UUID.upper()}/",
            f"https://pubrepo-api.jinr.ru/server/api/core/items/{ITEM_UUID}",
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(profile.native_id(url, {}), ITEM_UUID)

    def test_native_id_does_not_invent_uuid_from_unrelated_url(self) -> None:
        """Имя PDF, чужой домен и вложенный ресурс не подменяют UUID карточки."""

        profile = get_source_profile("jinr_preprints")
        urls = (
            "http://www1.jinr.ru/Preprints/2024/05(P11-2024-5).pdf",
            f"https://example.invalid/entities/publication/{ITEM_UUID}",
            f"https://pubrepo-api.jinr.ru/server/api/core/items/{ITEM_UUID}/bundles",
            "https://pubrepo.jinr.ru/entities/publication/not-a-uuid",
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(profile.native_id(url, {}))

    def test_repository_domain_does_not_imply_preprint_genre(self) -> None:
        """Домен репозитория не позволяет автоматически угадать жанр работы."""

        with self.assertRaises(ValueError):
            get_source_profile(
                "auto",
                source="pubrepo.jinr.ru",
                url=f"https://pubrepo.jinr.ru/entities/publication/{ITEM_UUID}",
            )

    def test_explicit_profile_matches_repository_and_pdf_source(self) -> None:
        """Явно выбранный профиль принимает адреса карточки и архива препринтов."""

        profile = get_source_profile("jinr_preprints")
        urls = (
            f"https://pubrepo.jinr.ru/entities/publication/{ITEM_UUID}",
            f"https://pubrepo-api.jinr.ru/server/api/core/items/{ITEM_UUID}",
            "http://www1.jinr.ru/Preprints/2024/05(P11-2024-5).pdf",
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertTrue(profile.matches("", url))

    def test_existing_ufn_profile_keeps_automatic_selection(self) -> None:
        """Прежний автоматический выбор профиля УФН не меняется."""

        profile = get_source_profile(
            "auto",
            url="https://ufn.ru/ru/articles/2024/1/a/",
        )

        self.assertEqual(profile.key, "ufn")
        self.assertEqual(
            profile.native_id("https://ufn.ru/ru/articles/2024/1/a/", {}),
            "article-2024-1-a",
        )


class PreprintWorkSchemaTests(unittest.TestCase):
    """Проверки жанра препринта без ослабления прежних условий допуска."""

    def setUp(self) -> None:
        """Подготовить схему и неизменённый пример журнальной статьи."""

        self.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")
        template_path = PROJECT_ROOT / "manifests" / "templates" / "works.example.jsonl"
        self.article: dict[str, Any] = json.loads(
            template_path.read_text(encoding="utf-8").splitlines()[0]
        )

    def _preprint(self) -> dict[str, Any]:
        """Создать карточку препринта с известным годом и неизвестной полной датой."""

        return {
            **self.article,
            "genre": "preprint",
            "journal_id": None,
            "journal_title": None,
            "published_at": None,
            "published_year": 2024,
        }

    def test_existing_article_remains_valid_without_year(self) -> None:
        """Старые записи works-v1 не требуют миграции и добавления года."""

        self.assertNotIn("published_year", self.article)
        self.catalog.validate("works", self.article)

    def test_preprint_accepts_year_without_invented_date(self) -> None:
        """Год публикации хранится отдельно без фиктивного первого января."""

        record = self._preprint()
        self.catalog.validate("works", record)

        self.assertIsNone(record["published_at"])
        self.assertEqual(record["published_year"], 2024)

    def test_publication_year_is_optional_and_nullable(self) -> None:
        """Неизвестный год можно явно оставить пустым или не указывать."""

        record = self._preprint()
        record["published_year"] = None
        self.catalog.validate("works", record)

        del record["published_year"]
        self.catalog.validate("works", record)

    def test_publication_year_boundaries(self) -> None:
        """Год должен быть целым четырёхзначным числом, а не строкой или флагом."""

        record = self._preprint()

        for year in (1000, 9999):
            with self.subTest(year=year):
                record["published_year"] = year
                self.catalog.validate("works", record)

        for invalid_year in (999, 10000, 2024.5, "2024", True):
            with self.subTest(year=invalid_year):
                record["published_year"] = invalid_year

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_only_preprints_allow_missing_journal(self) -> None:
        """У остальных жанров прежние обязательные реквизиты журнала сохраняются."""

        for genre in ("research_article", "review_article", "short_communication", "other"):
            for field_name in ("journal_id", "journal_title"):
                with self.subTest(genre=genre, field_name=field_name):
                    record = {**self.article, "genre": genre, field_name: None}

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)

    def test_preprint_does_not_accept_empty_journal_strings(self) -> None:
        """Отсутствующий журнал обозначается null, а не пустой строкой."""

        for field_name in ("journal_id", "journal_title"):
            with self.subTest(field_name=field_name):
                record = self._preprint()
                record[field_name] = ""

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)

    def test_preprint_cannot_be_marked_eligible(self) -> None:
        """Жанр препринта сам по себе не расширяет допуск к исследовательской выборке."""

        record = {
            **self.article,
            "genre": "preprint",
            "eligibility_status": "eligible",
        }

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("works", record)


if __name__ == "__main__":
    unittest.main()
