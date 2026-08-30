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
    journal_id: str
    journal_title: str
    accepted_sources: tuple[str, ...]

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
        if profile.matches(source, url)
    ]

    if len(matches) != 1:
        raise ValueError(
            f"Не удалось однозначно выбрать профиль для source={source!r}, url={url!r}"
        )

    return matches[0]
