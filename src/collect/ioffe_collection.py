"""Сбор открытых карточек и PDF Иоффе без регистрации или запуска OCR."""

from __future__ import annotations

import re

from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import pymupdf

from .base import HttpResponseSnapshot, USER_AGENT
from .ioffe import IOFFE_JOURNALS, extract_article_urls, extract_issue_urls, parse_article
from .ioffe_archive import IoffeArchive, write_json
from .ioffe_http import IoffeError, validate_ioffe_url


def _media_type(snapshot: HttpResponseSnapshot) -> str:
    """Получить единственный MIME-тип ответа, не угадывая формат по URL."""

    values = [value for name, value in snapshot.headers if name.lower() == "content-type"]

    if len(values) != 1:
        raise IoffeError("В ответе должен быть один заголовок Content-Type")

    return values[0].split(";", 1)[0].strip().lower()


def _html(snapshot: HttpResponseSnapshot) -> str:
    """Декодировать завершённый HTML в объявленной кодировке сайта."""

    if _media_type(snapshot) not in {"text/html", "application/xhtml+xml"}:
        raise IoffeError(f"Вместо HTML получен другой формат: {snapshot.requested_url}")

    headers = Message()

    for name, value in snapshot.headers:
        headers[name] = value

    encoding = headers.get_content_charset() or "utf-8"

    if encoding.lower() not in {"utf-8", "utf8", "windows-1251", "cp1251"}:
        raise IoffeError(f"Неизвестная кодировка HTML: {encoding}")

    try:
        return snapshot.body.decode(encoding)

    except UnicodeDecodeError as exception:
        raise IoffeError("HTML не соответствует объявленной кодировке") from exception


def _pdf_page_count(snapshot: HttpResponseSnapshot) -> int:
    """Проверить PDF перед отметкой о загрузке, не извлекая текст и формулы."""

    if _media_type(snapshot) != "application/pdf":
        raise IoffeError(f"Вместо PDF получен другой формат: {snapshot.requested_url}")

    if not snapshot.body.lstrip(b" \t\r\n").startswith(b"%PDF-"):
        raise IoffeError("Ответ не начинается с сигнатуры PDF")

    try:
        with pymupdf.open(stream=snapshot.body, filetype="pdf") as document:
            if document.needs_pass or document.is_repaired or document.page_count < 1:
                raise IoffeError("PDF зашифрован, повреждён или не содержит страниц")

            return document.page_count

    except (pymupdf.FileDataError, RuntimeError, ValueError) as exception:
        raise IoffeError(f"Не удалось открыть PDF: {exception}") from exception


class IoffeCollector:
    """Собирать статьи по явным ссылкам, сохраняя отдельную запись для каждой."""

    def __init__(self, archive: IoffeArchive, state_dir: Path, origin: str) -> None:
        """Проверить источник и подготовить счётчики текущего запуска."""

        if origin not in {"https://journals.ioffe.ru", "http://journals.ioffe.ru"}:
            raise ValueError("Допустим только HTTP или HTTPS origin journals.ioffe.ru")

        self.archive = archive
        self.state_dir = Path(state_dir)
        self.origin = origin
        self.robots: RobotFileParser | None = None
        self.completed = 0
        self.pdf_count = 0
        self.metadata_only_count = 0

    def prepare(self) -> None:
        """Проверить robots.txt до обхода; сетевой запуск всегда обновляет правила."""

        url = f"{self.origin}/robots.txt"
        snapshot = self.archive.get_response(url, refresh=self.archive.allow_network)

        if _media_type(snapshot) != "text/plain":
            raise IoffeError("robots.txt должен быть текстовым ответом, а не HTML-страницей")

        try:
            text = snapshot.body.decode("utf-8-sig")

        except UnicodeDecodeError as exception:
            raise IoffeError("Не удалось прочитать robots.txt как UTF-8") from exception

        if "<html" in text.lower() or "<!doctype" in text.lower():
            raise IoffeError("Вместо robots.txt получен HTML")

        # Стандартный RobotFileParser не реализует маски путей RFC 9309.
        for line in text.splitlines():
            name, separator, value = line.partition(":")
            rule = value.split("#", 1)[0].strip()

            if separator and name.strip().lower() in {"allow", "disallow"} and (
                "*" in rule or "$" in rule
            ):
                raise IoffeError("В robots.txt появились маски путей; нужна проверка правил")

        robots = RobotFileParser(url)
        robots.parse(text.splitlines())
        delay = float(robots.crawl_delay(USER_AGENT) or 0)
        rate = robots.request_rate(USER_AGENT)

        if rate is not None and rate.requests > 0:
            delay = max(delay, rate.seconds / rate.requests)

        if delay > self.archive.client.delay_seconds:
            self.archive.client.set_minimum_delay(delay)

        self.robots = robots

    def _get(self, url: str) -> HttpResponseSnapshot:
        """Проверить источник и robots.txt перед чтением кеша или сетью."""

        validate_ioffe_url(url)

        if f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" != self.origin:
            raise IoffeError("Ссылка ведёт за пределы выбранного origin")

        if self.robots is None:
            raise IoffeError("Перед сбором необходимо проверить robots.txt")

        if not self.robots.can_fetch(USER_AGENT, url):
            raise IoffeError(f"Путь запрещён robots.txt: {url}")

        return self.archive.get_response(url)

    def collect_article(self, url: str) -> dict[str, Any]:
        """Сохранить карточку и доступный PDF, не выдавая это за допуск к обучению."""

        if not re.fullmatch(r"/articles/[1-9][0-9]*", urlsplit(url).path):
            raise ValueError("Нужен адрес карточки /articles/число")

        card_snapshot = self._get(url)
        document = parse_article(_html(card_snapshot), url)
        article_id = urlsplit(url).path.rsplit("/", 1)[-1]
        record: dict[str, Any] = {
            "schema_version": "ioffe-collected-article-v1",
            "article_id": article_id,
            "document": document.to_dict(),
            "card_response": self.archive.evidence_for(card_snapshot),
            "status": "metadata_only",
            "registered": False,
            "training_eligibility": "not_assessed",
        }

        if document.pdf_url is not None:
            snapshot = self._get(document.pdf_url)
            record["page_count"] = _pdf_page_count(snapshot)
            record["pdf_response"] = self.archive.evidence_for(snapshot)
            record["status"] = "pdf_downloaded"

        record_path = self.state_dir / "articles" / f"{article_id}.json"
        write_json(record_path, record)
        self.completed += 1

        if record["status"] == "pdf_downloaded":
            self.pdf_count += 1

        else:
            self.metadata_only_count += 1

        print(f"[{self.completed}] {record['status']}: {document.title[:100]} → {record_path}", flush=True)

        return record

    def article_urls(
        self,
        *,
        journal_id: int | None = None,
        issue_id: int | None = None,
        article_id: int | None = None,
        max_issues: int = 1,
        max_articles: int = 3,
    ) -> Iterator[str]:
        """Обойти выбранный уровень архива; ноль снимает только явный лимит объёма."""

        if sum(value is not None for value in (journal_id, issue_id, article_id)) != 1:
            raise ValueError("Нужно выбрать ровно один журнал, выпуск или статью")

        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (max_issues, max_articles)):
            raise ValueError("Лимиты должны быть целыми неотрицательными числами")

        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
               for value in (journal_id, issue_id, article_id) if value is not None):
            raise ValueError("Идентификаторы должны быть целыми положительными числами")

        if article_id is not None:
            yield f"{self.origin}/articles/{article_id}"
            return

        if issue_id is not None:
            issue_urls = [f"{self.origin}/issues/{issue_id}"]

        else:
            if journal_id not in IOFFE_JOURNALS:
                raise ValueError("Нужно выбрать один из пяти русских журналов Иоффе")

            url = f"{self.origin}/journals/{journal_id}"
            issue_urls = extract_issue_urls(_html(self._get(url)), url)

            if not issue_urls:
                raise IoffeError("На странице журнала не найдены выпуски; сбор остановлен")

        selected_issues = issue_urls[:max_issues] if max_issues else issue_urls
        seen: set[str] = set()

        for issue_url in selected_issues:
            urls = extract_article_urls(_html(self._get(issue_url)), issue_url)

            if not urls:
                raise IoffeError(f"В выпуске не найдены карточки статей: {issue_url}")

            for url in urls:
                if url in seen:
                    continue

                seen.add(url)
                yield url

                if max_articles and len(seen) >= max_articles:
                    return
