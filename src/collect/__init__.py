"""Общие средства и сборщики текстов физических журналов."""

from .base import (
    Document,
    HttpResponseSnapshot,
    append_jsonl,
    fetch_bytes,
    fetch_html,
    fetch_response,
)
from .rss_feed import RssScraper
from .ufn import UfnScraper

__all__ = [
    "Document",
    "HttpResponseSnapshot",
    "RssScraper",
    "UfnScraper",
    "append_jsonl",
    "fetch_bytes",
    "fetch_html",
    "fetch_response",
]
