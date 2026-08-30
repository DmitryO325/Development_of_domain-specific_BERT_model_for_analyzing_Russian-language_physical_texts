"""Проверки устойчивой идентификации научных работ корпуса."""

from __future__ import annotations

import unittest

from src.corpus.identity import (
    WORK_ID_NAMESPACE,
    canonicalize_url,
    normalize_doi,
    resolve_work_identity,
)
from src.corpus.profiles import get_source_profile


class ManifestIdentityTests(unittest.TestCase):
    """Проверки нормализации и выбора идентификатора научной работы."""

    def test_doi_normalization(self) -> None:
        """Разные формы записи DOI должны давать одно значение."""

        variants = (
            "doi:10.1000/ABC.42",
            "https://doi.org/10.1000/ABC.42?utm_source=test",
            "HTTP://DX.DOI.ORG/10.1000/abc.42#fragment",
        )
        normalized_dois = {normalize_doi(value) for value in variants}

        self.assertEqual(normalized_dois, {"10.1000/abc.42"})

    def test_work_id_precedence(self) -> None:
        """DOI должен иметь приоритет перед идентификатором источника."""

        identity = resolve_work_identity(
            source_id="S01_UFN_RU",
            title="Название",
            authors=["Иванов И. И."],
            year="2024",
            doi="10.1000/Test",
            native_id="article-2024-1-a",
        )
        self.assertEqual(identity.work_id, "doi:10.1000/test")
        self.assertEqual(identity.confidence, "high")

    def test_native_id_is_second_choice(self) -> None:
        """Идентификатор источника должен использоваться при отсутствии DOI."""

        identity = resolve_work_identity(
            source_id="S01_UFN_RU",
            title="Название",
            authors=[],
            year=2024,
            native_id="Article 2024/1/A",
        )
        self.assertEqual(
            identity.work_id,
            "source:S01_UFN_RU:article-2024/1/a",
        )
        self.assertEqual(identity.confidence, "medium")

    def test_uuid5_fallback_is_stable_after_text_normalization(self) -> None:
        """Резервный UUIDv5 должен быть устойчив к нормализации текста."""

        first = resolve_work_identity(
            source_id="source",
            title="  Квантовая—оптика ",
            authors=["ИВАНОВ, И. И."],
            year="2023",
        )
        second = resolve_work_identity(
            source_id="source",
            title="квантовая оптика",
            authors=["иванов и и"],
            year=2023,
        )
        self.assertEqual(first.work_id, second.work_id)
        self.assertTrue(first.work_id.startswith("uuid:"))
        self.assertEqual(first.confidence, "low")
        self.assertEqual(str(WORK_ID_NAMESPACE), "1d018193-a92a-56b0-bcd2-2b29309b7a96")

    def test_url_canonicalization(self) -> None:
        """Канонизация URL должна удалять технические различия адресов."""

        self.assertEqual(
            canonicalize_url(
                "HTTPS://UFN.RU:443/ru//articles/2024/1/a/?utm_source=x&b=2&a=1#part"
            ),
            "https://ufn.ru/ru/articles/2024/1/a?a=1&b=2",
        )

    def test_explicit_profile_requires_both_source_and_host(self) -> None:
        """Явный профиль должен совпадать и с источником, и с доменом."""

        with self.assertRaises(ValueError):
            get_source_profile(
                "ufn",
                source="ufn.ru",
                url="https://example.org/ru/articles/2024/1/a/",
            )


if __name__ == "__main__":
    unittest.main()
