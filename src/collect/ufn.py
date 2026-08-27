"""Парсер журнала «Успехи физических наук» (ufn.ru)."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .base import Document, fetch_html, html_to_text, normalize_whitespace
from .pdf_text import (
    MIN_PDF_TEXT_CHARS,
    pdf_filename,
    pdf_to_text,
    pdf_url_from_article_path,
)
from .rss_feed import RssScraper

BASE = "https://ufn.ru"
ENCODING = "windows-1251"
SOURCE = "ufn.ru"
VALID_TEXT_MODES = {"html", "pdf", "pdf+html"}

LOGGER = logging.getLogger(__name__)

ISSUE_LINK_RE = re.compile(
    r"href=[\"'](/ru/articles/(\d{4})/(\d+)/?)[\"']",
    re.IGNORECASE,
)
MONTH_PREFIX_RE = re.compile(
    r"^(?:январ|феврал|март|апрел|май|июн|июл|август|"
    r"сентябр|октябр|ноябр|декабр)",
    re.IGNORECASE,
)
SECTION_RE = re.compile(
    r"(Обзоры актуальных проблем|Конференции и симпозиумы|От редакционной коллегии|"
    r"История физики|Методы исследования|Перспективные проблемы)",
    re.IGNORECASE,
)
INITIALS_PATTERN = r"(?:[A-ZА-ЯЁ]\.\s*){1,2}"
SURNAME_PATTERN = r"[A-ZА-ЯЁ][a-zа-яё]+(?:-[A-ZА-ЯЁ][a-zа-яё]+)?"
AUTHOR_RE = re.compile(
    rf"(?<!\w)(?:{INITIALS_PATTERN}{SURNAME_PATTERN}\b|"
    rf"{SURNAME_PATTERN}\s+{INITIALS_PATTERN})(?=$|[\s,;])"
)


class UfnScraper:
    """Сборщик метаданных и текстов журнала «Успехи физических наук»."""

    def __init__(
        self,
        delay_seconds: float = 1.0,
        *,
        text_mode: str = "pdf+html",
        pdf_dir: Path | str = "data/raw/pdf",
        pdf_text_dir: Path | str | None = None,
        min_pdf_chars: int = MIN_PDF_TEXT_CHARS,
        try_ocr: bool = True,
    ) -> None:
        """Создать сборщик с выбранным источником текста и параметрами PDF/OCR."""
        if delay_seconds < 0:
            raise ValueError("delay_seconds не может быть отрицательным")
        if text_mode not in VALID_TEXT_MODES:
            choices = ", ".join(sorted(VALID_TEXT_MODES))
            raise ValueError(f"text_mode должен быть одним из: {choices}")
        if min_pdf_chars < 1:
            raise ValueError("min_pdf_chars должен быть не меньше 1")
        self.delay_seconds = delay_seconds
        self.text_mode = text_mode
        self.pdf_dir = Path(pdf_dir)
        self.pdf_text_dir = Path(pdf_text_dir or Path(pdf_dir).parent / "pdf_text")
        self.min_pdf_chars = min_pdf_chars
        self.try_ocr = try_ocr

    def _get(self, path_or_url: str) -> str:
        """Загрузить HTML-страницу УФН после вежливой паузы."""
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else urljoin(BASE, path_or_url)
        )
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return fetch_html(url, encoding=ENCODING)

    def list_issues(self, limit: int | None = None) -> list[str]:
        """Получить пути выпусков вида ``/ru/articles/2024/1/``."""
        _validate_limit(limit, "limit")
        if limit == 0:
            return []
        html = self._get("/ru/articles/")

        issue_keys: dict[str, tuple[int, int]] = {}
        for matched in ISSUE_LINK_RE.finditer(html):
            path = f"{matched.group(1).rstrip('/')}/"
            issue_keys[path] = (int(matched.group(2)), int(matched.group(3)))

        issues = sorted(issue_keys, key=issue_keys.__getitem__, reverse=True)

        if limit is not None:
            issues = issues[:limit]

        return issues

    def list_article_paths(self, issue_path: str) -> list[tuple[str, str | None]]:
        """Получить пары ``(путь, подсказка названия)`` для статей выпуска."""
        html = self._get(issue_path)
        issue_prefix = re.escape(issue_path.rstrip("/"))
        link_pattern = re.compile(
            rf"href=[\"']({issue_prefix}/[a-z]/?)[\"']",
            re.IGNORECASE,
        )
        paths = sorted(set(link_pattern.findall(html)))

        titles: dict[str, str | None] = {}
        title_pattern = re.compile(
            rf"href=[\"']({issue_prefix}/[a-z]/?)[\"'][^>]*>(.*?)</a>",
            re.IGNORECASE | re.DOTALL,
        )
        for matched in title_pattern.finditer(html):
            path = matched.group(1)
            title = normalize_whitespace(html_to_text(matched.group(2)))
            titles[path] = title or None

        return [(p, titles.get(p)) for p in paths]

    def _parse_article_html(
        self,
        article_path: str,
    ) -> tuple[
        str,
        str | None,
        str,
        list[str],
        str | None,
        dict[str, Any],
    ]:
        """Извлечь HTML, PDF-адрес и метаданные со страницы статьи."""
        html = self._get(article_path)

        main_match = re.search(
            r"<[^>]*class=[\"'][^\"']*\bmain\b[^\"']*[\"'][^>]*>(.*)",
            html,
            re.DOTALL | re.IGNORECASE,
        )

        if not main_match:
            raise ValueError("нет блока main на странице")

        main_html = main_match.group(1).split("© Успехи")[0]
        pdf_match = re.search(
            r"href=[\"']([^\"']+\.pdf[^\"']*)[\"']",
            main_html,
            re.IGNORECASE,
        )
        pdf_url = urljoin(BASE, pdf_match.group(1)) if pdf_match else None

        if not pdf_url:
            pdf_url = pdf_url_from_article_path(article_path)

        html_text = _drop_nav_prefix(normalize_whitespace(html_to_text(main_html)))
        title = self._extract_title(main_html, html_text)
        authors = self._extract_authors(main_html)
        section = self._extract_section(main_html)
        year_match = re.search(r"/articles/(\d{4})/", article_path)
        issue_match = re.match(r"(/ru/articles/\d{4}/\d+/)", article_path)

        extra = {
            "issue_path": issue_match.group(1) if issue_match else None,
            "year": year_match.group(1) if year_match else None,
        }

        return main_html, pdf_url, title, authors, section, extra

    def parse_article(
        self,
        article_path: str,
        title_hint: str | None = None,
    ) -> Document | None:
        """Собрать одну статью, выбрав PDF, HTML или запасной переход между ними."""
        url = urljoin(BASE, article_path)
        main_html, pdf_url, title, authors, section, extra = self._parse_article_html(
            article_path
        )
        html_text = _drop_nav_prefix(normalize_whitespace(html_to_text(main_html)))

        if title_hint:
            title = title_hint

        if self.text_mode == "html":
            text = html_text
            text_source = "html"
        elif not pdf_url:
            if self.text_mode == "pdf":
                return None
            text = html_text
            text_source = "html"
        else:
            try:
                text, pdf_path, pdf_ok, pdf_method = pdf_to_text(
                    pdf_url,
                    self.pdf_dir,
                    delay_seconds=self.delay_seconds,
                    text_dir=self.pdf_text_dir,
                    try_ocr=self.try_ocr,
                )
            except (ImportError, OSError, RuntimeError, ValueError) as exception:
                if self.text_mode == "pdf":
                    raise RuntimeError(
                        f"Не удалось обработать PDF для {url}"
                    ) from exception
                text = html_text
                text_source = "html"
                extra["pdf_error"] = str(exception)
            else:
                extra.update(
                    {
                        "pdf_path": str(pdf_path),
                        "pdf_file": pdf_filename(pdf_url),
                        "pdf_extract_method": pdf_method,
                        "pdf_text_file": str(
                            self.pdf_text_dir / f"{pdf_path.stem}_{pdf_method}.txt"
                        ),
                    }
                )
                if pdf_ok and len(text) >= self.min_pdf_chars:
                    text_source = pdf_method
                elif self.text_mode == "pdf+html":
                    text = html_text
                    text_source = "html"
                    extra["pdf_unreadable"] = True
                else:
                    return None

        if len(text) < 80:
            return None

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
        """Обойти выпуски УФН и выдавать только успешно собранные статьи."""
        _validate_limit(max_issues, "max_issues")
        _validate_limit(max_articles_per_issue, "max_articles_per_issue")
        _validate_limit(max_docs, "max_docs")
        if max_docs == 0:
            return
        count = 0
        issues = self.list_issues(limit=max_issues)

        for issue in issues:
            LOGGER.info("Обработка выпуска %s", issue)
            try:
                paths = self.list_article_paths(issue)
            except (OSError, RuntimeError, ValueError) as exception:
                LOGGER.warning(
                    "Пропуск выпуска %s: %s",
                    urljoin(BASE, issue),
                    exception,
                )
                continue

            if max_articles_per_issue is not None:
                paths = paths[:max_articles_per_issue]

            for path, hint in paths:
                if max_docs is not None and count >= max_docs:
                    return

                try:
                    document = self.parse_article(path, title_hint=hint)
                except (OSError, RuntimeError, ValueError) as exception:
                    LOGGER.warning(
                        "Пропуск статьи %s: %s",
                        urljoin(BASE, path),
                        exception,
                    )
                    continue

                if document and len(document.text) >= 80:
                    yield document
                    count += 1

                    if max_docs is not None and count >= max_docs:
                        return

    def parse_rss(self, limit: int | None = 50) -> list[Document]:
        """Собрать RSS-карточки УФН через общий RSS/Atom-парсер."""
        return RssScraper(delay_seconds=self.delay_seconds).parse_feed(
            f"{BASE}/ru/articles/rss.xml",
            source_name=SOURCE,
            limit=limit,
        )

    @staticmethod
    def _extract_title(main_html: str, plain: str) -> str:
        """Извлечь название из очищенного текста с HTML-резервом."""
        lines = plain.split("\n")
        skip = {"выпуски", "←", "→", "&larr;", "&rarr;"}

        for line in lines:
            lowered = line.lower()

            if lowered in skip or re.fullmatch(r"\d{4}", line) or len(line) < 4:
                continue
            if MONTH_PREFIX_RE.match(lowered):
                continue
            if SECTION_RE.fullmatch(line):
                continue
            return line[:500]

        matched = re.search(r"<b[^>]*>([^<]{10,300})</b>", main_html, re.IGNORECASE)
        return html_to_text(matched.group(1)).strip() if matched else "Без названия"

    @staticmethod
    def _extract_section(main_html: str) -> str | None:
        """Извлечь одну из известных рубрик журнала."""
        matched = SECTION_RE.search(main_html)
        return matched.group(1) if matched else None

    @staticmethod
    def _extract_authors(main_html: str) -> list[str]:
        """Извлечь имена авторов из выделенных полужирным HTML-фрагментов."""
        chunk = main_html[:8000]
        bold_fragments = re.findall(
            r"<b[^>]*>(.*?)</b>",
            chunk,
            re.IGNORECASE | re.DOTALL,
        )
        names: list[str] = []
        for fragment in bold_fragments:
            plain = html_to_text(fragment)
            names.extend(
                matched.group(0).strip() for matched in AUTHOR_RE.finditer(plain)
            )
        return list(dict.fromkeys(names))[:8]


def _drop_nav_prefix(text: str) -> str:
    """Убрать хлебные крошки и мусор до начала содержательного текста.

    Формулы в ``$...$`` и адреса с ``@`` не удаляются.
    """
    lines = text.split("\n")
    cleaned: list[str] = []
    started = False

    for line in lines:
        # Технические хвосты старой вёрстки встречаются и после статьи.
        if re.search(r"Ud%|-->", line, re.IGNORECASE):
            continue
        if not started and re.search(r"mail\d", line, re.IGNORECASE):
            continue

        lowered = line.lower().strip()

        if not started:
            if lowered in {"выпуски", "/", "←", "→"} or re.fullmatch(r"\d{4}", line):
                continue
            if MONTH_PREFIX_RE.match(lowered):
                continue
            if re.fullmatch(r"[\s/←→\d]+", line):
                continue
            started = True

        if started:
            cleaned.append(line)

    return "\n".join(cleaned) if cleaned else text


def _validate_limit(limit: int | None, name: str) -> None:
    """Проверить, что необязательный лимит не отрицателен."""
    if limit is not None and limit < 0:
        raise ValueError(f"{name} не может быть отрицательным")
