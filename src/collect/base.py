"""Общие утилиты: HTTP, очистка HTML, сохранение JSONL."""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

USER_AGENT = "NIR-corpus-bot/0.1 (+academic research; contact via GitHub DmitryO325)"


@dataclass
class Document:
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
        return asdict(self)


def fetch_bytes(
    url: str,
    *,
    timeout: float = 120.0,
    retries: int = 3,
    delay_sec: float = 1.0,
) -> bytes:
    """Скачать бинарный файл (PDF и т.д.)."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as exc:
            last_err = exc
            if attempt + 1 < retries:
                time.sleep(delay_sec * (attempt + 1))
    raise RuntimeError(f"Не удалось загрузить {url}: {last_err}") from last_err


def fetch_html(
    url: str,
    *,
    encoding: str | None = None,
    timeout: float = 45.0,
    retries: int = 3,
    delay_sec: float = 1.0,
) -> str:
    """Скачать страницу; encoding=None → utf-8, для ufn.ru передать windows-1251."""
    raw = fetch_bytes(url, timeout=timeout, retries=retries, delay_sec=delay_sec)
    if encoding:
        return raw.decode(encoding, errors="replace")
    return raw.decode("utf-8", errors="replace")


def html_to_text(fragment: str) -> str:
    """Грубая очистка HTML → plain text."""
    fragment = re.sub(r"(?is)<!--.*?-->", " ", fragment)
    fragment = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", fragment)
    fragment = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", fragment)
    fragment = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"</(p|motion|div|li|h\d|tr)>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    fragment = html.unescape(fragment)
    fragment = fragment.replace("\xa0", " ")
    fragment = re.sub(r"[ \t]+", " ", fragment)
    fragment = re.sub(r"\n{3,}", "\n\n", fragment)
    lines = [ln.strip() for ln in fragment.split("\n") if ln.strip()]
    return "\n".join(lines)


def append_jsonl(doc: Document, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(doc.to_dict(), ensure_ascii=False) + "\n")


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text.strip())
