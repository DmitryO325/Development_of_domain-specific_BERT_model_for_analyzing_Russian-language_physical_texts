"""Парсер журнала «Успехи физических наук» (ufn.ru)."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from html import unescape
from typing import Iterator
from urllib.parse import urljoin

from pathlib import Path

from .base import Document, fetch_html, html_to_text, normalize_whitespace
from .pdf_text import (
    MIN_PDF_TEXT_CHARS,
    is_readable_russian,
    pdf_filename,
    pdf_to_text,
    pdf_url_from_article_path,
)

BASE = "https://ufn.ru"
ENCODING = "windows-1251"
SOURCE = "ufn.ru"


class UfnScraper:
    def __init__(
        self,
        delay_sec: float = 1.0,
        *,
        text_mode: str = "pdf+html",
        pdf_dir: Path | str = "data/raw/pdf",
        pdf_text_dir: Path | str | None = None,
        min_pdf_chars: int = MIN_PDF_TEXT_CHARS,
        try_ocr: bool = True,
    ) -> None:
        self.delay_sec = delay_sec
        self.text_mode = text_mode  # html | pdf | pdf+html (fallback на HTML)
        self.pdf_dir = Path(pdf_dir)
        self.pdf_text_dir = Path(pdf_text_dir or Path(pdf_dir).parent / "pdf_text")
        self.min_pdf_chars = min_pdf_chars
        self.try_ocr = try_ocr

    def _get(self, path_or_url: str) -> str:
        url = path_or_url if path_or_url.startswith("http") else urljoin(BASE, path_or_url)
        time.sleep(self.delay_sec)
        return fetch_html(url, encoding=ENCODING)

    def list_issues(self, limit: int | None = None) -> list[str]:
        """Пути вида /ru/articles/2024/1/."""
        html = self._get("/ru/articles/")
        issues = sorted(
            set(re.findall(r'href="(/ru/articles/\d{4}/\d+/?)"', html)),
            reverse=True,
        )
        if limit:
            issues = issues[:limit]
        return issues

    def list_article_paths(self, issue_path: str) -> list[tuple[str, str | None]]:
        """(path, title_hint) для статей в выпуске."""
        html = self._get(issue_path)
        paths = sorted(
            set(
                re.findall(
                    rf'href="({re.escape(issue_path.rstrip("/"))}/[a-z]/?)"',
                    html,
                )
            )
        )
        titles: dict[str, str | None] = {}
        for m in re.finditer(
            rf'href="({re.escape(issue_path.rstrip("/"))}/[a-z]/?)"[^>]*>([^<]+)</a>',
            html,
            re.I,
        ):
            path, title = m.group(1), unescape(re.sub(r"\s+", " ", m.group(2))).strip()
            titles[path] = title or None
        return [(p, titles.get(p)) for p in paths]

    def _parse_article_html(self, article_path: str) -> tuple[str, str | None, str, list[str], str | None, dict]:
        """Метаданные и HTML-текст со страницы статьи."""
        html = self._get(article_path)
        main_match = re.search(
            r'class="main"[^>]*(?:id="print")?[^>]*>(.*)',
            html,
            re.S | re.I,
        )
        if not main_match:
            raise ValueError("нет блока main на странице")
        main_html = main_match.group(1).split("© Успехи")[0]
        pdf_rel = re.search(r'href="([^"]+\.pdf[^"]*)"', main_html, re.I)
        pdf_url = urljoin(BASE, pdf_rel.group(1)) if pdf_rel else None
        if not pdf_url:
            pdf_url = pdf_url_from_article_path(article_path)
        html_text = _drop_nav_prefix(normalize_whitespace(html_to_text(main_html)))
        title = self._extract_title(main_html, html_text)
        authors = self._extract_authors(main_html)
        section = self._extract_section(main_html)
        year_m = re.search(r"/articles/(\d{4})/", article_path)
        extra = {
            "issue_path": (
                re.match(r"(/ru/articles/\d{4}/\d+/)", article_path).group(1)
                if re.match(r"/ru/articles/", article_path)
                else None
            ),
            "year": year_m.group(1) if year_m else None,
        }
        return main_html, pdf_url, title, authors, section, extra

    def parse_article(self, article_path: str, title_hint: str | None = None) -> Document | None:
        url = urljoin(BASE, article_path)
        try:
            _main, pdf_url, title, authors, section, extra = self._parse_article_html(
                article_path
            )
        except Exception:
            return None

        if title_hint:
            title = title_hint

        text = ""
        text_source = self.text_mode

        if self.text_mode in ("pdf", "pdf+html"):
            if not pdf_url:
                if self.text_mode == "pdf+html":
                    text_source = "html"
                else:
                    return None
            else:
                html_text = _drop_nav_prefix(normalize_whitespace(html_to_text(_main)))
                try:
                    text, pdf_path, pdf_ok, pdf_method = pdf_to_text(
                        pdf_url,
                        self.pdf_dir,
                        delay_sec=self.delay_sec,
                        text_dir=self.pdf_text_dir,
                        try_ocr=self.try_ocr,
                    )
                    extra["pdf_path"] = str(pdf_path)
                    extra["pdf_file"] = pdf_filename(pdf_url)
                    extra["pdf_extract_method"] = pdf_method
                    extra["pdf_text_file"] = str(
                        self.pdf_text_dir / f"{pdf_path.stem}_{pdf_method}.txt"
                    )
                    if pdf_ok and len(text) >= self.min_pdf_chars:
                        text_source = pdf_method if pdf_method.startswith("pdf") else "pdf"
                    elif self.text_mode in ("pdf+html", "html"):
                        extra["pdf_unreadable"] = True
                        text = html_text
                        text_source = "html"
                    else:
                        extra["pdf_unreadable"] = True
                        return None
                except Exception as exc:
                    if self.text_mode in ("pdf+html", "html"):
                        text = html_text
                        text_source = "html"
                        extra["pdf_error"] = str(exc)
                    else:
                        extra["error"] = str(exc)
                        return None

        if self.text_mode == "html" or (not text and self.text_mode == "pdf+html"):
            text = _drop_nav_prefix(normalize_whitespace(html_to_text(_main)))
            text_source = "html"

        if len(text) < 80:
            return None

        if text_source == "pdf" and not is_readable_russian(text):
            text = _drop_nav_prefix(normalize_whitespace(html_to_text(_main)))
            text_source = "html"
            extra["pdf_unreadable"] = True

        extra["text_source"] = text_source

        return Document(
            source=SOURCE,
            url=url,
            title=title,
            text=text,
            authors=authors,
            section=section,
            pdf_url=pdf_url,
            extra=extra,
        )

    def iter_articles(
        self,
        *,
        max_issues: int | None = None,
        max_articles_per_issue: int | None = None,
        max_docs: int | None = None,
    ) -> Iterator[Document]:
        count = 0
        issues = self.list_issues(limit=max_issues)
        for issue in issues:
            print(f"  выпуск {issue} …")
            paths = self.list_article_paths(issue)
            if max_articles_per_issue:
                paths = paths[:max_articles_per_issue]
            for path, hint in paths:
                if max_docs is not None and count >= max_docs:
                    return
                try:
                    doc = self.parse_article(path, title_hint=hint)
                except Exception as exc:  # noqa: BLE001 — логируем и идём дальше
                    yield Document(
                        source=SOURCE,
                        url=urljoin(BASE, path),
                        title=hint or path,
                        text="",
                        extra={"error": str(exc), "skipped": True},
                    )
                    continue
                if doc and len(doc.text) >= 80:
                    yield doc
                    count += 1
                    if max_docs is not None and count >= max_docs:
                        return

    def parse_rss(self, limit: int | None = 50) -> list[Document]:
        """RSS: https://ufn.ru/ru/articles/rss.xml (UTF-8)."""
        time.sleep(self.delay_sec)
        raw = fetch_html(f"{BASE}/ru/articles/rss.xml", encoding="utf-8")
        root = ET.fromstring(raw)
        docs: list[Document] = []
        for item in root.findall(".//item"):
            if limit is not None and len(docs) >= limit:
                break
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = item.findtext("description") or ""
            desc_text = normalize_whitespace(html_to_text(unescape(desc)))
            pub = item.findtext("pubDate")
            if not link or len(desc_text) < 40:
                continue
            docs.append(
                Document(
                    source=f"{SOURCE}:rss",
                    url=link,
                    title=title,
                    text=desc_text,
                    published=pub,
                    extra={"format": "rss_description"},
                )
            )
        return docs

    @staticmethod
    def _extract_title(main_html: str, plain: str) -> str:
        lines = plain.split("\n")
        skip = {"выпуски", "←", "→", "&larr;", "&rarr;"}
        candidates = []
        for ln in lines:
            low = ln.lower()
            if low in skip or re.match(r"^\d{4}$", ln) or len(ln) < 12:
                continue
            if re.match(
                r"^(январ|феврал|март|апрел|май|июн|июл|август|сентябр|октябр|ноябр|декабр)",
                low,
            ):
                continue
            candidates.append(ln)
            if len(candidates) >= 2:
                break
        if candidates:
            return candidates[0][:500]
        m = re.search(r"<b>([^<]{10,300})</b>", main_html)
        return unescape(m.group(1)).strip() if m else "Без названия"

    @staticmethod
    def _extract_section(main_html: str) -> str | None:
        m = re.search(
            r"(Обзоры актуальных проблем|Конференции и симпозиумы|От редакционной коллегии|"
            r"История физики|Методы исследования|Перспективные проблемы)",
            main_html,
            re.I,
        )
        return m.group(1) if m else None

    @staticmethod
    def _extract_authors(main_html: str) -> list[str]:
        chunk = main_html[:8000]
        names = re.findall(
            r"<b>([А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.\s+[А-ЯЁ][а-яё]+)</b>",
            chunk,
        )
        if names:
            return list(dict.fromkeys(unescape(n.strip()) for n in names))
        names2 = re.findall(r"<b>([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)</b>", chunk)
        return list(dict.fromkeys(unescape(n.strip()) for n in names2[:8]))


def _drop_nav_prefix(text: str) -> str:
    """Убрать хлебные крошки и мусор в начале (спам в HTML-комментариях на ufn.ru)."""
    lines = text.split("\n")
    cleaned: list[str] = []
    started = False
    for ln in lines:
        if re.search(r"@|mail\d|Ud%|-->|\$", ln):
            continue
        low = ln.lower().strip()
        if not started:
            if low in {"выпуски", "/", "←", "→"} or re.match(r"^\d{4}$", ln):
                continue
            if re.match(
                r"^(январ|феврал|март|апрел|май|июн|июл|август|сентябр|октябр|ноябр|декабр)",
                low,
            ):
                continue
            if len(ln) > 25 and not re.match(r"^[\s/←→\d]+$", ln):
                started = True
        if started:
            cleaned.append(ln)
    return "\n".join(cleaned) if cleaned else text
