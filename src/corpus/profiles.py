"""Явные профили источников: сборщик не должен угадывать эти поля."""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SourceProfile:
    """Описание источника и правил распознавания его документов."""

    key: str
    source_group_id: str
    source_id: str
    platform: str | None
    journal_id: str | None
    journal_title: str | None
    accepted_sources: tuple[str, ...]
    automatic_selection: bool = True

    def matches(self, source: str, url: str) -> bool:
        """Проверить, соответствуют ли имя источника и URL этому профилю."""

        source_base = source.casefold().removesuffix(":rss")
        host = (urlsplit(url).hostname or "").casefold()
        source_matches = not source_base or source_base in self.accepted_sources
        host_matches = not host or host in self.accepted_sources

        return bool(source_base or host) and source_matches and host_matches

    def native_id(self, url: str, extra: dict[str, Any]) -> str | None:
        """Извлечь собственный идентификатор работы из метаданных или URL."""

        for key in ("source_work_id", "article_id", "native_id"):
            value = extra.get(key)

            if value:
                return str(value)

        if self.key == "ufn":
            match = re.search(
                r"/ru/articles/(\d{4})/(\d+)/([a-z])(?:/|$)",
                url,
                re.IGNORECASE,
            )

            if match:
                year, issue, letter = match.groups()
                return f"article-{year}-{issue}-{letter.lower()}"

        if self.key == "jinr_preprints":
            parsed = urlsplit(url)
            prefixes = {
                "pubrepo.jinr.ru": "/entities/publication/",
                "pubrepo-api.jinr.ru": "/server/api/core/items/",
            }
            prefix = prefixes.get((parsed.hostname or "").casefold())

            if prefix is not None:
                match = re.fullmatch(
                    re.escape(prefix)
                    + r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?",
                    parsed.path,
                    re.IGNORECASE,
                )

                if match:
                    return match.group(1).lower()

        if self.key in {"jinr_articles", "jinr_collections", "jinr_abstracts"}:
            parsed = urlsplit(url)
            item_prefixes = {
                "pubrepo.jinr.ru": ("/items/", "/entities/publication/"),
                "pubrepo-api.jinr.ru": ("/server/api/core/items/",),
            }

            for prefix in item_prefixes.get((parsed.hostname or "").casefold(), ()):
                match = re.fullmatch(
                    re.escape(prefix)
                    + r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/?",
                    parsed.path,
                    re.IGNORECASE,
                )

                if match:
                    return match.group(1).lower()

        return


SOURCE_PROFILES: dict[str, SourceProfile] = {
    "ufn": SourceProfile(
        key="ufn",
        source_group_id="S01_UFN",
        source_id="S01_UFN_RU",
        platform="ufn.ru",
        journal_id="ufn_ru",
        journal_title="Успехи физических наук",
        accepted_sources=("ufn.ru", "www.ufn.ru"),
    ),

    "quantum_electronics": SourceProfile(
        key="quantum_electronics",
        source_group_id="S06_QUANTUM_ELECTRONICS",
        source_id="S06_QUANTUM_ELECTRONICS_RU",
        platform="quantum-electronics.ru",
        journal_id="quantum_electronics_ru",
        journal_title="Квантовая электроника",
        accepted_sources=("quantum-electronics.ru", "www.quantum-electronics.ru"),
    ),

    "jinr_preprints": SourceProfile(
        key="jinr_preprints",
        source_group_id="F04_JINR_REPOSITORY",
        source_id="F04_JINR_PREPRINTS_RU",
        platform="pubrepo.jinr.ru",
        journal_id=None,
        journal_title=None,
        accepted_sources=("pubrepo.jinr.ru", "pubrepo-api.jinr.ru", "www1.jinr.ru"),
        # Репозиторий содержит и журнальные статьи: одного домена недостаточно.
        automatic_selection=False,
    ),

    "jinr_articles": SourceProfile(
        key="jinr_articles",
        source_group_id="F04_JINR_REPOSITORY",
        source_id="F04_JINR_ARTICLES_RU",
        platform="pubrepo.jinr.ru",
        journal_id=None,
        journal_title=None,
        accepted_sources=("pubrepo.jinr.ru", "pubrepo-api.jinr.ru"),
        # Тип Article и журнал проверяются по конкретной карточке репозитория.
        automatic_selection=False,
    ),

    "jinr_collections": SourceProfile(
        key="jinr_collections",
        source_group_id="F04_JINR_REPOSITORY",
        source_id="F04_JINR_COLLECTIONS_RU",
        platform="pubrepo.jinr.ru",
        journal_id=None,
        journal_title=None,
        accepted_sources=("pubrepo.jinr.ru", "pubrepo-api.jinr.ru"),
        # Book chapter не отличает полную работу от тезисов: нужна проверка PDF.
        automatic_selection=False,
    ),

    "jinr_abstracts": SourceProfile(
        key="jinr_abstracts",
        source_group_id="F04_JINR_REPOSITORY",
        source_id="F04_JINR_ABSTRACTS_RU",
        platform="pubrepo.jinr.ru",
        journal_id=None,
        journal_title=None,
        accepted_sources=("pubrepo.jinr.ru", "pubrepo-api.jinr.ru"),
        # Домен и Book chapter не подтверждают тезисы: нужна отдельная проверка.
        automatic_selection=False,
    ),
}


def get_source_profile(name: str, *, source: str = "", url: str = "") -> SourceProfile:
    """Вернуть именованный профиль либо однозначно выбрать его в режиме auto."""

    if name != "auto":
        try:
            profile = SOURCE_PROFILES[name]

        except KeyError as exception:
            choices = ", ".join(sorted(SOURCE_PROFILES))
            raise ValueError(
                f"Неизвестный профиль {name!r}; доступны: {choices}, auto"
            ) from exception

        if source and not profile.matches(source, url):
            raise ValueError(
                f"Документ source={source!r} не соответствует профилю {name!r}"
            )

        return profile

    matches = [
        profile
        for profile in SOURCE_PROFILES.values()
        if profile.automatic_selection and profile.matches(source, url)
    ]

    if len(matches) != 1:
        raise ValueError(
            f"Не удалось однозначно выбрать профиль для source={source!r}, url={url!r}"
        )

    return matches[0]
