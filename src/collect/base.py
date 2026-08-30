"""Общие утилиты: HTTP, очистка HTML, сохранение JSONL."""

from __future__ import annotations

import hashlib
import html
import http.client
import json
import re
import time

import urllib.error
import urllib.request

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from urllib.parse import urlsplit

USER_AGENT = "NIR-corpus-bot/0.1 (+academic research; contact via GitHub DmitryO325)"
RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
SAFE_RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "content-encoding",
    "etag",
    "last-modified",
    "date",
    "location",
}


@dataclass(frozen=True)
class HttpResponseSnapshot:
    """Неизменяемый снимок успешного HTTP-ответа и его точных байтов."""

    requested_url: str
    final_url: str
    status_code: int
    headers: tuple[tuple[str, str], ...]
    retrieved_at: str
    body: bytes

    def canonical_metadata(self) -> bytes:
        """Сериализовать метаданные ответа в детерминированный UTF-8 JSON."""

        metadata = {
            "final_url": self.final_url,
            "headers": [list(header) for header in sorted(self.headers)],
            "requested_url": self.requested_url,
            "retrieved_at": self.retrieved_at,
            "status_code": self.status_code,
        }

        return json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def metadata_sha256(self) -> str:
        """Вычислить SHA-256 канонических метаданных ответа."""

        return hashlib.sha256(self.canonical_metadata()).hexdigest()


@dataclass
class Document:
    """
    Сырой документ, полученный из сайта, RSS или PDF.

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


def _current_utc_timestamp() -> str:
    """Получить текущую временную метку UTC с микросекундами."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _response_url(response: Any, requested_url: str) -> str:
    """Получить конечный URL ответа с резервом на исходный адрес."""

    get_url = getattr(response, "geturl", None)

    if not callable(get_url):
        return requested_url

    try:
        final_url = get_url()

    except (AttributeError, TypeError, ValueError):
        return requested_url

    return final_url if isinstance(final_url, str) and final_url else requested_url


def _response_status_code(response: Any) -> int:
    """Получить числовой HTTP-статус успешного ответа."""

    status_code = getattr(response, "status", None)

    if isinstance(status_code, int) and not isinstance(status_code, bool):
        return status_code

    get_code = getattr(response, "getcode", None)

    if callable(get_code):
        try:
            status_code = get_code()

        except (AttributeError, TypeError, ValueError):
            status_code = None

    if isinstance(status_code, int) and not isinstance(status_code, bool):
        return status_code

    return 200


def _safe_response_headers(response: Any) -> tuple[tuple[str, str], ...]:
    """Оставить безопасные заголовки ответа в детерминированном порядке."""

    response_headers = getattr(response, "headers", None)
    items = getattr(response_headers, "items", None)

    if not callable(items):
        return ()

    try:
        raw_headers = items()

    except (AttributeError, TypeError, ValueError):
        return ()

    safe_headers: list[tuple[str, str]] = []

    try:
        for raw_name, raw_value in raw_headers:
            name = str(raw_name).strip().casefold()

            if name not in SAFE_RESPONSE_HEADERS:
                continue

            safe_headers.append((name, str(raw_value).strip()))

    except (TypeError, ValueError):
        return ()

    return tuple(sorted(safe_headers))


def fetch_response(
    url: str,
    *,
    timeout: float = 120.0,
    retries: int = 3,
    delay_seconds: float = 1.0,
) -> HttpResponseSnapshot:
    """
    Скачать HTTP(S)-ресурс и зафиксировать доказательство успешного ответа.

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
                body = response.read()
                retrieved_at = _current_utc_timestamp()

                return HttpResponseSnapshot(
                    requested_url=url,
                    final_url=_response_url(response, url),
                    status_code=_response_status_code(response),
                    headers=_safe_response_headers(response),
                    retrieved_at=retrieved_at,
                    body=body,
                )

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


def fetch_bytes(
    url: str,
    *,
    timeout: float = 120.0,
    retries: int = 3,
    delay_seconds: float = 1.0,
) -> bytes:
    """Скачать только байты HTTP(S)-ресурса через совместимую оболочку."""

    response = fetch_response(
        url,
        timeout=timeout,
        retries=retries,
        delay_seconds=delay_seconds,
    )

    return response.body


def fetch_html(
    url: str,
    *,
    encoding: str | None = None,
    timeout: float = 45.0,
    retries: int = 3,
    delay_seconds: float = 1.0,
) -> str:
    """
    Скачать страницу и декодировать её в строку.

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
    """
    Дописать документ одной UTF-8-строкой в JSONL-файл.
    Функция не выполняет дедупликацию и не синхронизирует параллельные записи.
    """

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(doc.to_dict(), ensure_ascii=False) + "\n")


def normalize_whitespace(text: str) -> str:
    """Убрать краевые пробелы и сократить избыточные пустые строки."""

    return re.sub(r"\n{3,}", "\n\n", text.strip())
