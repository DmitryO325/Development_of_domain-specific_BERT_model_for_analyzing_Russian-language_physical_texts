"""Проверка опубликованных ссылок на PDF-препринты ОИЯИ без построения адресов."""

from __future__ import annotations

import re

from typing import Any
from urllib.parse import unquote, urlsplit

PREPRINT_HOST = "www1.jinr.ru"
PREPRINT_URI_FIELD = "local.publication.uri"
_PREPRINT_PATH = re.compile(r"/Preprints/[0-9]{4}/[A-Za-z0-9][A-Za-z0-9_().-]*\.pdf")


def validate_preprint_url(url: str) -> None:
    """Разрешить только точную опубликованную ссылку на PDF архива препринтов ОИЯИ."""

    if not isinstance(url, str) or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in url
    ):
        raise ValueError("Адрес препринта должен быть строкой без пробелов и управляющих символов")

    try:
        parsed = urlsplit(url)
        port = parsed.port

    except ValueError as exception:
        raise ValueError("Некорректный адрес препринта ОИЯИ") from exception

    expected_port = {"http": 80, "https": 443}.get(parsed.scheme)
    decoded_path = unquote(parsed.path)

    # Кодированные разделители и повторное декодирование не должны менять границы пути.
    if (
        expected_port is None
        or parsed.hostname != PREPRINT_HOST
        or port not in {None, expected_port}
        or parsed.netloc.lower() not in {PREPRINT_HOST, f"{PREPRINT_HOST}:{expected_port}"}
        or parsed.username is not None
        or parsed.password is not None
        or "?" in url
        or "#" in url
        or "\\" in url
        or "%" in decoded_path
        or decoded_path.count("/") != parsed.path.count("/")
        or _PREPRINT_PATH.fullmatch(decoded_path) is None
    ):
        raise ValueError(
            "Разрешены только HTTP(S)-адреса www1.jinr.ru/Preprints/YYYY/<имя>.pdf "
            "без параметров, фрагментов и переходов между каталогами"
        )


def extract_preprint_urls(item: dict[str, Any]) -> list[str]:
    """Выбрать допустимые PDF-ссылки только из local.publication.uri проверенной карточки."""

    if not isinstance(item, dict):
        raise ValueError("Карточка публикации должна быть JSON-объектом")

    metadata = item.get("metadata", {})

    if not isinstance(metadata, dict):
        raise ValueError("Метаданные публикации должны быть JSON-объектом")

    entries = metadata.get(PREPRINT_URI_FIELD, [])

    if not isinstance(entries, list):
        raise ValueError(f"Поле {PREPRINT_URI_FIELD} должно содержать список значений")

    urls: list[str] = []
    seen_urls: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            raise ValueError(f"Значение {PREPRINT_URI_FIELD} должно содержать строковое поле value")

        url = entry["value"]

        try:
            validate_preprint_url(url)

        except ValueError:
            continue

        if url not in seen_urls:
            urls.append(url)
            seen_urls.add(url)

    return urls
