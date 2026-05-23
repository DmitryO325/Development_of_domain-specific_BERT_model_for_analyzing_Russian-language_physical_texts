"""Сбор текстов с сайтов физических журналов."""

from .base import Document, append_jsonl, fetch_html
from .ufn import UfnScraper
from .rss_feed import RssScraper

__all__ = ["Document", "append_jsonl", "fetch_html", "UfnScraper", "RssScraper"]
