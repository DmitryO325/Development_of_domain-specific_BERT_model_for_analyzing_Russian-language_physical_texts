"""Парсер RSS/Atom (WordPress и др.) для журналов с лентой."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from html import unescape
from typing import Iterator

from .base import Document, fetch_html, html_to_text, normalize_whitespace

# Ленты из плана НИР
DEFAULT_FEEDS = {
    "quantum-electronics.ru": "https://quantum-electronics.ru/feed/",
    "ufn.ru": "https://ufn.ru/ru/articles/rss.xml",
}


class RssScraper:
    def __init__(self, delay_sec: float = 0.5) -> None:
        self.delay_sec = delay_sec

    def parse_feed(
        self,
        feed_url: str,
        *,
        source_name: str | None = None,
        limit: int | None = 30,
    ) -> list[Document]:
        time.sleep(self.delay_sec)
        raw = fetch_html(feed_url, encoding="utf-8")
        root = ET.fromstring(raw)
        source = source_name or _host(feed_url)
        docs: list[Document] = []

        items = root.findall(".//item")
        if not items:
            # Atom
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall(".//atom:entry", ns)

        for item in items:
            if limit is not None and len(docs) >= limit:
                break
            doc = self._parse_item(item, source, feed_url)
            if doc:
                docs.append(doc)
        return docs

    def iter_default_feeds(self, limit_per_feed: int = 15) -> Iterator[Document]:
        for name, url in DEFAULT_FEEDS.items():
            try:
                yield from self.parse_feed(url, source_name=name, limit=limit_per_feed)
            except Exception as exc:  # noqa: BLE001
                yield Document(
                    source=name,
                    url=url,
                    title="feed_error",
                    text="",
                    extra={"error": str(exc), "skipped": True},
                )

    def _parse_item(self, item: ET.Element, source: str, feed_url: str) -> Document | None:
        title = _first_text(item, "title") or ""
        link = _first_text(item, "link") or ""
        if not link:
            link_el = item.find("{http://www.w3.org/2005/Atom}link")
            if link_el is not None:
                link = link_el.attrib.get("href", "")
        pub = _first_text(item, "pubDate") or _first_text(item, "date")

        body = ""
        for tag in (
            "{http://purl.org/rss/1.0/modules/content/}encoded",
            "encoded",
            "description",
            "summary",
        ):
            el = item.find(tag)
            if el is not None and el.text:
                body = el.text
                break
        if not body:
            for child in item:
                if child.tag.endswith("encoded") and child.text:
                    body = child.text
                    break

        text = normalize_whitespace(html_to_text(unescape(body)))
        if len(text) < 30 and title:
            text = title

        if not link:
            return None

        categories = [c.text.strip() for c in item.findall("category") if c.text]

        return Document(
            source=f"{source}:rss",
            url=link.strip(),
            title=unescape(title).strip(),
            text=text,
            published=pub,
            section=categories[0] if categories else None,
            extra={"feed": feed_url, "categories": categories},
        )


def _first_text(item: ET.Element, tag: str) -> str | None:
    el = item.find(tag)
    return el.text.strip() if el is not None and el.text else None


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url
