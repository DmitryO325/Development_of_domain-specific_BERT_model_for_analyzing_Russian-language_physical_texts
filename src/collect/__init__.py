"""Общие средства и сборщики текстов физических журналов."""

from .base import Document, append_jsonl, fetch_html
from .rss_feed import RssScraper
from .ufn import UfnScraper

__all__ = ["Document", "RssScraper", "UfnScraper", "append_jsonl", "fetch_html"]
