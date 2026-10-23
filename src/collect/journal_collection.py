"""Возобновляемый обход физических журналов с отдельным сохранением карточек и PDF."""

from __future__ import annotations

import hashlib
import json
import re

from collections.abc import Callable
from email.message import Message
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .base import Document, HttpResponseSnapshot, USER_AGENT
from .ioffe_archive import IoffeArchive, write_json
from .ioffe_collection import _pdf_page_count
from .ioffe_http import IoffeError
from .jetp import validate_volume_page
from .jetp_form_archive import select_volume
from .journal_sources import JournalSource


def decode_html(
    snapshot: HttpResponseSnapshot,
    default_encoding: str,
    *,
    decode_body: Callable[[bytes, str], str] | None = None,
) -> str:
    """Прочитать HTML по charset ответа, не заменяя незнакомые байты молча."""

    values = [value for name, value in snapshot.headers if name == "content-type"]

    if len(values) != 1 or values[0].split(";", 1)[0].strip().lower() not in {
        "text/html", "application/xhtml+xml",
    }:
        raise IoffeError(f"Вместо HTML получен другой формат: {snapshot.requested_url}")

    headers = Message()
    headers["Content-Type"] = values[0]
    encoding = headers.get_content_charset() or default_encoding

    if encoding.lower() not in {"utf-8", "utf8", "koi8-r", "windows-1251", "cp1251"}:
        raise IoffeError(f"Неизвестная кодировка HTML: {encoding}")

    try:
        return decode_body(snapshot.body, encoding) if decode_body is not None else snapshot.body.decode(encoding)

    except UnicodeDecodeError as exception:
        raise IoffeError(f"HTML не соответствует кодировке {encoding}") from exception


class JournalCollector:
    """Следовать публичным ссылкам без OCR и изменения рабочих реестров."""

    def __init__(
        self,
        source: JournalSource,
        archive: IoffeArchive,
        *,
        refresh_urls: tuple[str, ...] = (),
        download_pdfs: bool = True,
    ) -> None:
        """Подготовить отдельные правила каждого origin и счётчики текущего запуска."""

        self.source = source
        self.archive = archive
        self.robots: dict[str, RobotFileParser] = {}
        self.completed = 0
        self.new_records = 0
        self.pdf_count = 0
        self.deferred_issues: dict[str, dict[str, Any]] = {}
        self.article_limit_reached = False
        self.download_pdfs = download_pdfs
        self.refresh_urls = set(refresh_urls)

        if self.refresh_urls and not archive.allow_network:
            raise ValueError("Для адресного обновления нужен --allow-network")

        for url in self.refresh_urls:
            source.validate_url(url)

    def prepare(self, url: str | None = None) -> None:
        """Проверить robots.txt перед первым запросом на конкретный origin."""

        target = url or self.source.start_url
        self.source.validate_url(target)
        parsed = urlsplit(target)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if origin in self.robots:
            return

        robots_url = f"{origin}/robots.txt"
        snapshot = self.archive.get_response(robots_url, refresh=self.archive.allow_network)
        robots = RobotFileParser(robots_url)

        if snapshot.status_code in {404, 410}:
            # Отсутствие файла не является лицензией; сохраняем именно ответ 404/410.
            robots.parse([])

        else:
            values = [value.split(";", 1)[0].strip().lower() for name, value in snapshot.headers if name == "content-type"]

            if values != ["text/plain"]:
                raise IoffeError("Вместо robots.txt получен ответ другого типа")

            try:
                text = snapshot.body.decode("utf-8-sig")

            except UnicodeDecodeError as exception:
                raise IoffeError("robots.txt не соответствует UTF-8") from exception

            if "<html" in text.lower() or "<!doctype" in text.lower():
                raise IoffeError("Вместо robots.txt получена HTML-страница")

            for line in text.splitlines():
                name, separator, value = line.partition(":")
                rule = value.split("#", 1)[0].strip()

                if separator and name.strip().lower() in {"allow", "disallow"} and any(
                    character in rule for character in "*$"
                ):
                    raise IoffeError("Маски robots.txt требуют отдельной проверки перед обходом")

            robots.parse(text.splitlines())

        delay = float(robots.crawl_delay(USER_AGENT) or 0)
        rate = robots.request_rate(USER_AGENT)

        if rate is not None and rate.requests > 0:
            delay = max(delay, rate.seconds / rate.requests)

        if delay > self.archive.client.delay_seconds:
            self.archive.client.set_minimum_delay(delay)

        self.robots[origin] = robots

    def _get(self, url: str) -> HttpResponseSnapshot:
        """Проверить адрес и robots.txt до чтения кеша или обращения к сети."""

        self.source.validate_url(url)
        self.prepare(url)
        parsed = urlsplit(url)
        robots = self.robots[f"{parsed.scheme}://{parsed.netloc}"]

        if not robots.can_fetch(USER_AGENT, url):
            raise IoffeError(f"Путь запрещён robots.txt: {url}")

        snapshot = self.archive.get_response(url, refresh=url in self.refresh_urls)
        self.refresh_urls.discard(url)

        return snapshot

    def collect_article(self, url: str) -> dict[str, Any]:
        """Сохранить карточку и проверенный PDF без автоматического допуска к обучению."""

        if self.source.classify_url(url) != "article":
            raise ValueError("Для сбора одной статьи нужен адрес карточки")

        snapshot = self._get(url)

        if snapshot.status_code != 200:
            target = self.source.resolve_article_redirect(snapshot)

            if target is None:
                raise IoffeError(f"Неподдержанное перенаправление карточки: {url}")

            # Исходный ответ уже сохранён. Новый адрес проходит собственные
            # проверки robots.txt, кеша и общей паузы; цепочки не обходятся.
            snapshot = self._get(target)

            if snapshot.status_code != 200:
                raise IoffeError(f"Повторное перенаправление карточки запрещено: {target}")

            url = target

        html = decode_html(snapshot, self.source.default_encoding, decode_body=self.source.decode_body)
        document = self.source.parse_article(html, url)

        if document.url != url:
            raise IoffeError("Разобранная карточка не соответствует запрошенному адресу")

        return self._save_document(document, metadata_snapshot=snapshot)

    def _save_document(self, document: Document, *, metadata_snapshot: HttpResponseSnapshot) -> dict[str, Any]:
        """Связать метаданные карточки или оглавления с явно опубликованным PDF."""

        if not isinstance(document.title, str) or not document.title.strip():
            raise IoffeError("Карточка не содержит названия статьи")

        self.source.validate_url(document.url)

        record: dict[str, Any] = {
            "schema_version": "journal-collected-article-v1",
            "source_id": self.source.source_id,
            "document": document.to_dict(),
            "metadata_page_url": metadata_snapshot.requested_url,
            "card_response": self.archive.evidence_for(metadata_snapshot),
            "status": "metadata_only",
            "registered": False,
            "training_eligibility": "not_assessed",
        }

        if document.language != "ru":
            record["status"] = "skipped_non_russian"

        elif document.pdf_url is not None and self.download_pdfs:
            if self.source.classify_url(document.pdf_url) != "pdf":
                raise IoffeError("Ссылка PDF ведёт вне проверенных путей источника")

            snapshot = self._get(document.pdf_url)
            record["page_count"] = _pdf_page_count(snapshot)
            record["pdf_response"] = self.archive.evidence_for(snapshot)
            record["status"] = "pdf_downloaded"

        elif document.pdf_url is not None:
            record["pdf_download_status"] = "not_requested"

        digest = hashlib.sha256(document.url.encode("utf-8")).hexdigest()
        # Режим только метаданных не должен стирать запись ранее скачанного PDF.
        directory = "articles" if self.download_pdfs else "metadata"
        record_path = self.archive.state_dir / directory / f"{digest}.json"
        is_new = not record_path.exists()
        write_json(record_path, record)
        self.completed += 1
        self.new_records += int(is_new)
        self.pdf_count += int(record["status"] == "pdf_downloaded")
        print(f"[{self.source.source_id}: {self.completed}] {record['status']}: {document.title[:100]}", flush=True)

        return record

    @property
    def deferred_issues_count(self) -> int:
        """Вернуть число оглавлений без ссылок, встретившихся в текущем запуске."""

        return len(self.deferred_issues)

    @property
    def deferred_entries_count(self) -> int:
        """Вернуть число отложенных позиций, не включая их в готовые статьи."""

        return sum(record["entries_count"] for record in self.deferred_issues.values())

    def _defer_issue(
        self,
        snapshot: HttpResponseSnapshot,
        entries: list[dict[str, object]],
    ) -> None:
        """Сохранить проверенное оглавление отдельно от готовых карточек и продолжить обход."""

        if not entries or any(
            not isinstance(entry, dict) or not isinstance(entry.get("title"), str) or
            not str(entry["title"]).strip()
            for entry in entries
        ):
            raise IoffeError("Адаптер вернул неполное описание оглавления без ссылок")

        url = snapshot.requested_url
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        metadata_digest = snapshot.metadata_sha256()
        body_digest = hashlib.sha256(snapshot.body).hexdigest()
        # HTTP-метаданные не включают тело: версии различаем по обоим хешам.
        snapshot_digest = hashlib.sha256(f"{body_digest}:{metadata_digest}".encode("ascii")).hexdigest()
        relative_path = f"deferred_issues/{digest}.{snapshot_digest}.json"
        record_path = self.archive.state_dir / relative_path
        record: dict[str, Any] = {
            "schema_version": "journal-deferred-issue-v1",
            "source_id": self.source.source_id,
            "issue_url": url,
            "status": "deferred_unlinked_toc",
            "reason": "В оглавлении нет прямых ссылок на карточки статей или PDF",
            "entries": entries,
            "entries_count": len(entries),
            "card_response": self.archive.evidence_for(snapshot),
            "registered": False,
            "training_eligibility": "not_assessed",
        }

        # Новое HTTP-свидетельство получает отдельный файл; прежняя версия сохраняется.
        if record_path.exists():
            try:
                existing = json.loads(record_path.read_text(encoding="utf-8"))

            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exception:
                raise IoffeError(f"Не удалось проверить отложенное оглавление: {record_path}") from exception

            if existing != record:
                raise IoffeError(f"Отложенное оглавление не совпадает с сохранённой версией: {record_path}")

        write_json(record_path, record)
        self.deferred_issues[url] = {
            "issue_url": url,
            "record_path": relative_path,
            "entries_count": len(entries),
        }
        print(f"ОТЛОЖЕНО [{self.source.source_id}]: {len(entries)} публикаций без прямых ссылок: {url}. "
              f"Статьи не собраны; оглавление сохранено: {record_path}. Обход продолжается.", flush=True)

    def crawl(
        self,
        *,
        start_url: str | None = None,
        max_articles: int = 0,
        volume: int | None = None,
    ) -> None:
        """Обойти опубликованные ссылки, воспроизводя уже пройденный путь из кеша."""

        if type(max_articles) is not int or max_articles < 0:
            raise ValueError("Лимит статей должен быть целым неотрицательным числом")

        if volume is not None:
            if self.source.source_id != "jetp" or type(volume) is not int or volume <= 0:
                raise ValueError("Выбор тома требует источника jetp и положительного целого номера")

            if start_url is not None:
                raise ValueError("Нельзя одновременно выбирать том и задавать начальный URL")

            refresh_form = self.source.start_url in self.refresh_urls
            form_snapshot = self._get(self.source.start_url)
            start_url = select_volume(self.archive, form_snapshot, volume, refresh=refresh_form)

        pending = [start_url or self.source.start_url]
        seen: set[str] = set()

        while pending:
            url = pending.pop()

            if url in seen:
                continue

            seen.add(url)
            role = self.source.classify_url(url)

            if role == "article":
                record = self.collect_article(url)
                seen.add(record["document"]["url"])

                if max_articles and self.completed >= max_articles:
                    self.article_limit_reached = True
                    return

                continue

            if role not in {"archive", "issue"}:
                raise IoffeError(f"Неожиданный тип страницы архива: {url}")

            snapshot = self._get(url)
            html = decode_html(snapshot, self.source.default_encoding, decode_body=self.source.decode_body)

            if volume is not None and url == start_url:
                validate_volume_page(html, url, volume)

            issue_documents = self.source.parse_issue_articles(html, url) if role == "issue" else []

            for document in issue_documents:
                if document.url != document.pdf_url or self.source.classify_url(document.url) != "pdf":
                    raise IoffeError("Оглавление должно явно связывать статью с публичным PDF")

                if document.url in seen:
                    continue

                self._save_document(document, metadata_snapshot=snapshot)
                seen.add(document.url)

                if max_articles and self.completed >= max_articles:
                    self.article_limit_reached = True
                    return

            links = [link for link in self.source.extract_links(html, url)
                     if self.source.classify_url(link) in {"archive", "issue", "article"}]

            if volume is not None:
                # Обратная навигация старых выпусков ведёт также в текущий том.
                volume_path = re.compile(
                    rf"/cgi-bin/r/index/(?:r/)?{volume}(?:/[1-9][0-9]*(?:/p[1-9][0-9]*)?)?"
                )
                links = [link for link in links if volume_path.fullmatch(urlsplit(link).path)]

            if not links and not issue_documents:
                entries = self.source.parse_unlinked_issue(html, url) if role == "issue" else []

                if entries:
                    self._defer_issue(snapshot, entries)
                    continue

                raise IoffeError(f"Не найдены ссылки на выпуски или статьи: {url}")

            # Сначала статьи выпуска, затем следующие страницы; навигационные циклы пропускаются.
            priority = {"article": 0, "issue": 1, "archive": 2}
            links.sort(key=lambda link: priority[self.source.classify_url(link) or "archive"])
            pending.extend(reversed([link for link in links if link not in seen]))
