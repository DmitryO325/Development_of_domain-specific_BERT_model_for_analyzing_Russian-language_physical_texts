"""Проверки отдельного профиля журнальных статей репозитория ОИЯИ."""

from __future__ import annotations

import unittest

from src.corpus.profiles import get_source_profile

ITEM_UUID = "c1de2fec-3e07-4e15-903c-370df5e5a9c8"


class ArticleProfileTests(unittest.TestCase):
    """Проверки явного выбора источника и распознавания UUID карточки статьи."""

    def test_profile_keeps_journal_for_each_article(self) -> None:
        """Репозиторий задаёт источник статьи, но не подменяет конкретный журнал."""

        profile = get_source_profile("jinr_articles")

        self.assertEqual(profile.key, "jinr_articles")
        self.assertEqual(profile.source_group_id, "F04_JINR_REPOSITORY")
        self.assertEqual(profile.source_id, "F04_JINR_ARTICLES_RU")
        self.assertEqual(profile.platform, "pubrepo.jinr.ru")
        self.assertIsNone(profile.journal_id)
        self.assertIsNone(profile.journal_title)
        self.assertEqual(
            profile.accepted_sources,
            ("pubrepo.jinr.ru", "pubrepo-api.jinr.ru"),
        )
        self.assertFalse(profile.automatic_selection)

    def test_native_id_recognizes_each_item_url(self) -> None:
        """Три разрешённых адреса карточки возвращают нормализованный UUID."""

        profile = get_source_profile("jinr_articles")
        prefixes = (
            "https://pubrepo.jinr.ru/items/",
            "https://pubrepo.jinr.ru/entities/publication/",
            "https://pubrepo-api.jinr.ru/server/api/core/items/",
        )

        for prefix in prefixes:
            for suffix in (ITEM_UUID, ITEM_UUID.upper() + "/"):
                url = prefix + suffix

                with self.subTest(url=url):
                    self.assertEqual(profile.native_id(url, {}), ITEM_UUID)

    def test_native_id_accepts_explicit_metadata(self) -> None:
        """Явный идентификатор сохраняет общий приоритет метаданных профиля."""

        profile = get_source_profile("jinr_articles")

        for field_name in ("source_work_id", "article_id", "native_id"):
            with self.subTest(field_name=field_name):
                self.assertEqual(
                    profile.native_id(
                        "https://pubrepo.jinr.ru/handle/123456789/1234",
                        {field_name: ITEM_UUID},
                    ),
                    ITEM_UUID,
                )

    def test_native_id_rejects_foreign_nested_and_malformed_paths(self) -> None:
        """Чужие домены, вложенные ресурсы и похожие строки не становятся UUID."""

        profile = get_source_profile("jinr_articles")
        invalid_urls = (
            f"https://example.invalid/items/{ITEM_UUID}",
            f"https://pubrepo.jinr.ru.example.invalid/items/{ITEM_UUID}",
            f"https://www1.jinr.ru/items/{ITEM_UUID}",
            f"https://pubrepo-api.jinr.ru/items/{ITEM_UUID}",
            f"https://pubrepo.jinr.ru/server/api/core/items/{ITEM_UUID}",
            f"https://pubrepo.jinr.ru/entities/collection/{ITEM_UUID}",
            f"https://pubrepo.jinr.ru/other/items/{ITEM_UUID}",
            f"https://pubrepo.jinr.ru/items/{ITEM_UUID}/bundles",
            f"https://pubrepo.jinr.ru/entities/publication/{ITEM_UUID}/files",
            f"https://pubrepo-api.jinr.ru/server/api/core/items/{ITEM_UUID}/bundles",
            f"https://pubrepo-api.jinr.ru/server/api/core/bitstreams/{ITEM_UUID}/content",
            f"https://pubrepo.jinr.ru/items/{ITEM_UUID}.pdf",
            "https://pubrepo.jinr.ru/items/not-a-uuid",
            "https://pubrepo.jinr.ru/items/",
        )

        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertIsNone(profile.native_id(url, {}))

    def test_repository_does_not_select_article_automatically(self) -> None:
        """Домен не сообщает, является документ статьёй или препринтом."""

        for host in ("pubrepo.jinr.ru", "pubrepo-api.jinr.ru"):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(ValueError, "однозначно выбрать профиль"),
            ):
                get_source_profile("auto", source=host, url=f"https://{host}/")

    def test_explicit_profile_rejects_preprint_archive_and_foreign_sources(self) -> None:
        """Архив препринтов и посторонний источник не входят в профиль статей."""

        profile = get_source_profile("jinr_articles")

        for host in ("www1.jinr.ru", "example.invalid", "pubrepo.jinr.ru.example.invalid"):
            with self.subTest(host=host):
                self.assertFalse(profile.matches("", f"https://{host}/"))

                with self.assertRaisesRegex(ValueError, "не соответствует профилю"):
                    get_source_profile("jinr_articles", source=host, url=f"https://{host}/")

    def test_article_item_route_does_not_extend_preprint_profile(self) -> None:
        """Новый путь /items/ добавляется только отдельному профилю статей."""

        profile = get_source_profile("jinr_preprints")

        self.assertIsNone(profile.native_id(f"https://pubrepo.jinr.ru/items/{ITEM_UUID}", {}))


if __name__ == "__main__":
    unittest.main()
