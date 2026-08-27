"""Парсер RSS/Atom (WordPress и др.) для журналов с лентой."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from html import unescape
from urllib.parse import urljoin, urlsplit

from .base import Document, fetch_bytes, html_to_text, normalize_whitespace

# Ленты из плана НИР
DEFAULT_FEEDS = {
    "quantum-electronics.ru": "https://quantum-electronics.ru/feed/",
    "ufn.ru": "https://ufn.ru/ru/articles/rss.xml",
}
MAX_FEED_BYTES = 10 * 1024 * 1024
UNSAFE_XML_DECLARATION_RE = re.compile(
    rb"<!\s*(?:DOCTYPE|ENTITY)\b",
    re.IGNORECASE,
)


class RssScraper:
    """Сборщик карточек публикаций из RSS 2.0 и Atom."""

    def __init__(self, delay_seconds: float = 0.5) -> None:
        """Создать сборщик с паузой перед загрузкой каждой ленты."""
        if delay_seconds < 0:
            raise ValueError("delay_seconds не может быть отрицательным")
        self.delay_seconds = delay_seconds

    def parse_feed(
        self,
        feed_url: str,
        *,
        source_name: str | None = None,
        limit: int | None = 30,
    ) -> list[Document]:
        """Загрузить RSS/Atom-ленту и преобразовать её записи в ``Document``."""
        _validate_limit(limit, "limit")
        if limit == 0:
            return []
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        raw = fetch_bytes(feed_url)
        root = _parse_xml(raw)
        source = source_name or _host(feed_url)
        docs: list[Document] = []

        items = [
            element
            for element in root.iter()
            if _local_name(element.tag) in {"item", "entry"}
        ]

        for item in items:
            if limit is not None and len(docs) >= limit:
                break

            doc = self._parse_item(item, source, feed_url)

            if doc:
                docs.append(doc)

        return docs

    def iter_default_feeds(self, limit_per_feed: int = 15) -> Iterator[Document]:
        """Последовательно обойти штатные ленты, не прерываясь на ошибке."""
        _validate_limit(limit_per_feed, "limit_per_feed")
        for name, url in DEFAULT_FEEDS.items():
            try:
                yield from self.parse_feed(url, source_name=name, limit=limit_per_feed)
            except (
                ET.ParseError,
                LookupError,
                OSError,
                RuntimeError,
                ValueError,
            ) as exception:
                yield Document(
                    source=f"{name}:rss",
                    url=url,
                    title="feed_error",
                    text="",
                    extra={"error": str(exception), "skipped": True},
                )

    def _parse_item(
        self, item: ET.Element, source: str, feed_url: str
    ) -> Document | None:
        """Преобразовать один RSS ``item`` или Atom ``entry`` в документ."""
        title = _first_text(item, "title") or ""
        link = _entry_link(item)
        published = (
            _first_text(item, "pubDate")
            or _first_text(item, "published")
            or _first_text(item, "updated")
            or _first_text(item, "date")
        )

        body = ""
        for tag in ("encoded", "content", "description", "summary"):
            body = _first_text(item, tag) or ""
            if body:
                break

        text = normalize_whitespace(html_to_text(body))

        if len(text) < 30 and title:
            text = title

        if not link:
            return None

        categories = _categories(item)

        return Document(
            source=f"{source}:rss",
            url=urljoin(feed_url, link.strip()),
            title=unescape(title).strip(),
            text=text,
            published=published,
            section=categories[0] if categories else None,
            extra={"feed": feed_url, "categories": categories},
        )


def _first_text(item: ET.Element, tag: str) -> str | None:
    """Найти текст прямого дочернего тега без привязки к XML namespace."""
    for child in item:
        if _local_name(child.tag) != tag:
            continue
        text = " ".join(part.strip() for part in child.itertext() if part.strip())
        if text:
            return text
    return None


def _parse_xml(raw: bytes) -> ET.Element:
    """Безопасно разобрать XML ленты без DTD и пользовательских сущностей."""
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError(f"XML-лента превышает лимит {MAX_FEED_BYTES} байт")
    # Нулевые байты убираются только для проверки: так видны
    # запрещённые объявления в UTF-16/UTF-32, а исходный XML не изменяется.
    declaration_probe = raw.replace(b"\x00", b"")
    if UNSAFE_XML_DECLARATION_RE.search(declaration_probe):
        raise ValueError("DTD и XML-сущности в лентах не поддерживаются")

    # DTD/сущности отклонены, а размер входа ограничен выше.
    return ET.fromstring(raw)  # noqa: S314


def _entry_link(item: ET.Element) -> str:
    """Извлечь основную ссылку из RSS- или Atom-записи."""
    fallback = ""
    for child in item:
        if _local_name(child.tag) != "link":
            continue
        candidate = (child.attrib.get("href") or child.text or "").strip()
        if not candidate:
            continue
        relation = child.attrib.get("rel", "alternate")
        if relation == "alternate":
            return candidate
        fallback = fallback or candidate
    return fallback


def _categories(item: ET.Element) -> list[str]:
    """Извлечь рубрики RSS и Atom с сохранением порядка."""
    categories: list[str] = []
    for child in item:
        if _local_name(child.tag) != "category":
            continue
        category = (child.attrib.get("term") or child.text or "").strip()
        if category and category not in categories:
            categories.append(category)
    return categories


def _local_name(tag: str) -> str:
    """Убрать XML namespace из имени тега."""
    return tag.rsplit("}", maxsplit=1)[-1]


def _validate_limit(limit: int | None, name: str) -> None:
    """Проверить, что необязательный лимит не отрицателен."""
    if limit is not None and limit < 0:
        raise ValueError(f"{name} не может быть отрицательным")


def _host(url: str) -> str:
    """Извлечь имя узла из URL для идентификатора источника."""
    return urlsplit(url).hostname or url
