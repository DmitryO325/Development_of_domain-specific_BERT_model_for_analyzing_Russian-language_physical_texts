"""Проверки отдельного профиля тезисов конференций репозитория ОИЯИ."""

from __future__ import annotations

import unittest

from src.corpus.profiles import get_source_profile

ITEM_UUID = "817022d5-88ef-4bf3-b6ea-89a03f0b57bb"


class AbstractProfileTests(unittest.TestCase):
    """Проверки явного выбора тезисов с общей группой источника ОИЯИ."""

    def test_profile_has_distinct_source_and_shared_repository_group(self) -> None:
        """Тезисы имеют свой профиль и источник, сохраняя группу репозитория."""

        profile = get_source_profile("jinr_abstracts")

        self.assertEqual(profile.key, "jinr_abstracts")
        self.assertEqual(profile.source_id, "F04_JINR_ABSTRACTS_RU")
        self.assertEqual(profile.source_group_id, "F04_JINR_REPOSITORY")
        self.assertEqual(profile.platform, "pubrepo.jinr.ru")
        self.assertIsNone(profile.journal_id)
        self.assertIsNone(profile.journal_title)
        self.assertFalse(profile.automatic_selection)
        self.assertEqual(
            profile.accepted_sources,
            ("pubrepo.jinr.ru", "pubrepo-api.jinr.ru"),
        )

    def test_native_id_recognizes_only_item_routes(self) -> None:
        """UUID карточки распознаётся в трёх маршрутах и нормализуется."""

        profile = get_source_profile("jinr_abstracts")

        for prefix in (
            "https://pubrepo.jinr.ru/items/",
            "https://pubrepo.jinr.ru/entities/publication/",
            "https://pubrepo-api.jinr.ru/server/api/core/items/",
        ):
            for suffix in (ITEM_UUID, ITEM_UUID.upper() + "/"):
                with self.subTest(prefix=prefix, suffix=suffix):
                    self.assertEqual(profile.native_id(prefix + suffix, {}), ITEM_UUID)

    def test_native_id_rejects_foreign_nested_and_malformed_paths(self) -> None:
        """Чужой сайт, вложенный ресурс и некорректный UUID не дают идентификатор."""

        profile = get_source_profile("jinr_abstracts")
        invalid_urls = (
            f"https://example.invalid/items/{ITEM_UUID}",
            f"https://pubrepo.jinr.ru.example.invalid/items/{ITEM_UUID}",
            f"https://www1.jinr.ru/items/{ITEM_UUID}",
            f"https://pubrepo-api.jinr.ru/items/{ITEM_UUID}",
            f"https://pubrepo.jinr.ru/server/api/core/items/{ITEM_UUID}",
            f"https://pubrepo.jinr.ru/items/{ITEM_UUID}/bundles",
            f"https://pubrepo.jinr.ru/entities/publication/{ITEM_UUID}/files",
            f"https://pubrepo-api.jinr.ru/server/api/core/items/{ITEM_UUID}/bundles",
            f"https://pubrepo-api.jinr.ru/server/api/core/bitstreams/{ITEM_UUID}/content",
            f"https://pubrepo.jinr.ru/items/{ITEM_UUID}.pdf",
            "https://pubrepo.jinr.ru/items/not-a-uuid",
        )

        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertIsNone(profile.native_id(url, {}))

    def test_native_id_preserves_explicit_metadata_precedence(self) -> None:
        """Явный идентификатор метаданных сохраняет приоритет перед UUID в URL."""

        profile = get_source_profile("jinr_abstracts")

        for field_name in ("source_work_id", "article_id", "native_id"):
            with self.subTest(field_name=field_name):
                self.assertEqual(
                    profile.native_id(
                        f"https://pubrepo.jinr.ru/items/{ITEM_UUID}",
                        {field_name: "explicit-id"},
                    ),
                    "explicit-id",
                )

    def test_repository_domain_does_not_select_abstracts_automatically(self) -> None:
        """Домен репозитория не доказывает жанр тезисов."""

        for host in ("pubrepo.jinr.ru", "pubrepo-api.jinr.ru"):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(ValueError, "однозначно выбрать профиль"),
            ):
                get_source_profile("auto", source=host, url=f"https://{host}/")

    def test_foreign_sources_and_preprint_archive_are_rejected(self) -> None:
        """Профиль тезисов принимает только два домена репозитория."""

        profile = get_source_profile("jinr_abstracts")

        for host in ("example.invalid", "www1.jinr.ru", "pubrepo.jinr.ru.example.invalid"):
            with self.subTest(host=host):
                self.assertFalse(profile.matches("", f"https://{host}/"))

                with self.assertRaisesRegex(ValueError, "не соответствует профилю"):
                    get_source_profile("jinr_abstracts", source=host, url=f"https://{host}/")


if __name__ == "__main__":
    unittest.main()
