"""Общие утилиты: HTTP, очистка HTML, сохранение JSONL."""

from __future__ import annotations

import html
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

USER_AGENT = "NIR-corpus-bot/0.1 (+academic research; contact via GitHub DmitryO325)"
RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class Document:
    """Сырой документ, полученный из сайта, RSS или PDF.

    Структура не проверяет типы во время исполнения. Строгая проверка
    выполняется при переносе записи в машинные реестры корпуса.
    """

    source: str
    url: str
    title: str
    text: str
    authors: list[str] = field(default_factory=list)
    published: str | None = None
    section: str | None = None
    pdf_url: str | None = None
    language: str = "ru"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Преобразовать документ в словарь, пригодный для JSON."""
        return asdict(self)


def fetch_bytes(
    url: str,
    *,
    timeout: float = 120.0,
    retries: int = 3,
    delay_seconds: float = 1.0,
) -> bytes:
    """Скачать HTTP(S)-ресурс как байты с повторами временных ошибок.

    Ошибки HTTP, которые не являются временными (например, 404), не
    повторяются.
    """
    if urlsplit(url).scheme.lower() not in {"http", "https"}:
        raise ValueError("Поддерживаются только HTTP(S)-адреса")
    if timeout <= 0:
        raise ValueError("timeout должен быть больше нуля")
    if retries < 1:
        raise ValueError("retries должен быть не меньше 1")
    if delay_seconds < 0:
        raise ValueError("delay_seconds не может быть отрицательным")

    last_error: Exception | None = None

    for attempt in range(retries):
        try:
            request = urllib.request.Request(  # noqa: S310
                url,
                headers={"User-Agent": USER_AGENT},
            )

            # URL-схема проверена перед циклом; локальные схемы запрещены.
            with urllib.request.urlopen(  # noqa: S310
                request,
                timeout=timeout,
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exception:
            status_code = exception.code
            exception.close()
            if status_code not in RETRYABLE_HTTP_STATUS_CODES:
                raise RuntimeError(
                    f"Не удалось загрузить {url}: HTTP {status_code}"
                ) from exception
            last_error = exception
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.HTTPException,
        ) as exception:
            last_error = exception

        if attempt + 1 < retries:
            time.sleep(delay_seconds * (attempt + 1))

    if last_error is None:  # Защита от непредвиденного изменения цикла повторов.
        raise RuntimeError(f"Не удалось загрузить {url}")
    raise RuntimeError(f"Не удалось загрузить {url}: {last_error}") from last_error


def fetch_html(
    url: str,
    *,
    encoding: str | None = None,
    timeout: float = 45.0,
    retries: int = 3,
    delay_seconds: float = 1.0,
) -> str:
    """Скачать страницу и декодировать её в строку.

    По умолчанию используется UTF-8. Для старых HTML-страниц УФН нужно
    явно передавать ``encoding="windows-1251"``.
    """
    raw = fetch_bytes(
        url,
        timeout=timeout,
        retries=retries,
        delay_seconds=delay_seconds,
    )

    if encoding:
        return raw.decode(encoding, errors="replace")

    return raw.decode("utf-8", errors="replace")


def html_to_text(fragment: str) -> str:
    """Грубо преобразовать HTML-фрагмент в обычный текст."""
    # TODO: заменить очистку регулярными выражениями на полноценный HTML-парсер
    # (например, BeautifulSoup + lxml) и извлекать только основное содержимое статьи.

    # Удаление комментариев
    fragment = re.sub(r"(?is)<!--.*?-->", " ", fragment)

    # Удаление JS
    fragment = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", fragment)

    # Удаление CSS
    fragment = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", fragment)

    # Восстановление переносов строк
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"(?i)</(p|section|div|li|h\d|tr)>", "\n", fragment)

    # Удаление оставшихся тегов
    fragment = re.sub(r"<[^>]+>", " ", fragment)

    # Расшифровка HTML-сущностей
    fragment = html.unescape(fragment)
    fragment = fragment.replace("\xa0", " ")

    # Очистка пробелов
    fragment = re.sub(r"[ \t]+", " ", fragment)
    fragment = re.sub(r"\n{3,}", "\n\n", fragment)
    lines = [line.strip() for line in fragment.split("\n") if line.strip()]

    return "\n".join(lines)


def append_jsonl(doc: Document, path: Path) -> None:
    """Дописать документ одной UTF-8-строкой в JSONL-файл.

    Функция не выполняет дедупликацию и не синхронизирует параллельные записи.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(doc.to_dict(), ensure_ascii=False) + "\n")


def normalize_whitespace(text: str) -> str:
    """Убрать краевые пробелы и сократить избыточные пустые строки."""
    return re.sub(r"\n{3,}", "\n\n", text.strip())
