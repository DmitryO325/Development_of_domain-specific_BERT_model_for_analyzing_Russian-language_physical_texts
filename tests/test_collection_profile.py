"""Проверки отдельного профиля полных материалов сборников ОИЯИ."""

from __future__ import annotations

import unittest

from src.corpus.profiles import get_source_profile

ITEM_UUID = "817022d5-88ef-4bf3-b6ea-89a03f0b57bb"


class CollectionProfileTests(unittest.TestCase):
    """Проверки явного выбора жанровой группы без нового независимого источника."""

    def test_profile_has_own_source_but_shared_repository_group(self) -> None:
        """Материалы сборников учитываются отдельно, но сохраняют общую группу ОИЯИ."""

        profile = get_source_profile("jinr_collections")

        self.assertEqual(profile.source_id, "F04_JINR_COLLECTIONS_RU")
        self.assertEqual(profile.source_group_id, "F04_JINR_REPOSITORY")
        self.assertEqual(profile.platform, "pubrepo.jinr.ru")
        self.assertIsNone(profile.journal_id)
        self.assertIsNone(profile.journal_title)
        self.assertFalse(profile.automatic_selection)

    def test_native_id_uses_item_routes_only(self) -> None:
        """UUID распознаётся в разрешённых карточках, но не во вложенных ресурсах."""

        profile = get_source_profile("jinr_collections")

        for prefix in (
            "https://pubrepo.jinr.ru/items/",
            "https://pubrepo.jinr.ru/entities/publication/",
            "https://pubrepo-api.jinr.ru/server/api/core/items/",
        ):
            with self.subTest(prefix=prefix):
                self.assertEqual(profile.native_id(prefix + ITEM_UUID.upper() + "/", {}), ITEM_UUID)
                self.assertIsNone(profile.native_id(prefix + ITEM_UUID + "/bundles", {}))

        self.assertIsNone(profile.native_id(f"https://example.invalid/items/{ITEM_UUID}", {}))

    def test_domain_never_selects_collection_profile_automatically(self) -> None:
        """Домен и тип Book chapter сами по себе не подтверждают полный материал."""

        for host in ("pubrepo.jinr.ru", "pubrepo-api.jinr.ru"):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(ValueError, "однозначно выбрать профиль"),
            ):
                get_source_profile("auto", source=host, url=f"https://{host}/")

    def test_foreign_source_is_rejected(self) -> None:
        """Профиль сборников не принимает сторонний сайт или отдельный архив препринтов."""

        for host in ("example.invalid", "www1.jinr.ru"):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(ValueError, "не соответствует профилю"),
            ):
                get_source_profile("jinr_collections", source=host, url=f"https://{host}/")


if __name__ == "__main__":
    unittest.main()
