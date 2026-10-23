"""Локальная проверка собранных статей журналов перед регистрацией в корпусе."""

from __future__ import annotations

import hashlib
import json
import re

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.collect.base import Document, HttpResponseSnapshot
from src.collect.ioffe import IOFFE_JOURNALS, parse_article as parse_ioffe_article
from src.collect.ioffe_archive import MAX_METADATA_BYTES, _validate_snapshot
from src.collect.ioffe_collection import _html, _pdf_page_count
from src.collect.ioffe_http import MAX_RESPONSE_BYTES, IoffeError, validate_ioffe_url
from src.collect.journal_collection import decode_html
from src.collect.journal_sources import get_source
from src.collect.ufn_journal import parse_article as parse_ufn_article

from .profiles import SourceProfile, get_source_profile

MAX_RECORD_BYTES = 2 * 1024 * 1024
COLLECTED_SOURCES = frozenset({"ioffe", "jetp", "jetp_letters", "quantum", "ufn"})
IOFFE_PROFILE_KEYS = {
    1: "ioffe_ftt", 2: "ioffe_ftp", 3: "ioffe_jtf",
    4: "ioffe_pjtf", 5: "ioffe_optics",
}


class CollectedArticleError(ValueError):
    """Несовместимая карточка или повреждённое свидетельство её происхождения."""


class NonRussianArticleError(CollectedArticleError):
    """Проверенная исходная карточка помечена сборщиком как нерусскоязычная."""


class AmbiguousIssueEntryError(CollectedArticleError):
    """Проверенная карточка не различает несколько записей исходного оглавления."""

    def __init__(self, *, issue_url: str, article_url: str, candidates: list[Document]) -> None:
        """Сохранить все подтверждённые записи, не выбирая статью по общей ссылке."""

        super().__init__(
            "Оглавление связывает одну ссылку с несколькими статьями; требуется ручная проверка"
        )
        self.details: dict[str, Any] = {
            "issue_url": issue_url,
            "article_url": article_url,
            "candidates": [document.to_dict() for document in candidates],
        }


@dataclass(frozen=True)
class CollectedArticle:
    """Проверенные метаданные статьи и точные исходные ответы без допуска к обучению."""

    document: Document
    profile: SourceProfile
    card_response: HttpResponseSnapshot
    pdf_response: HttpResponseSnapshot | None
    record_path: Path


def _read_bytes(path: Path, limit: int) -> bytes:
    """Прочитать обычный файл, не переходя по символическим ссылкам."""

    if path.is_symlink() or not path.is_file():
        raise CollectedArticleError(f"Ожидается обычный файл без символической ссылки: {path}")

    with path.open("rb") as source_file:
        body = source_file.read(limit + 1)

    if len(body) > limit:
        raise CollectedArticleError(f"Слишком большой локальный файл: {path}")

    return body


def _safe_response_path(root: Path, relative_path: str) -> Path:
    """Разрешить путь внутри каталога исходников без ссылок в промежуточных каталогах."""

    parts = Path(relative_path).parts

    if not parts or Path(relative_path).is_absolute() or ".." in parts:
        raise CollectedArticleError("Недопустимый путь свидетельства")

    path = root

    for part in parts:
        path = path / part

        if path.is_symlink():
            raise CollectedArticleError(f"Путь свидетельства содержит символическую ссылку: {path}")

    if not path.resolve().is_relative_to(root):
        raise CollectedArticleError("Путь свидетельства выходит из каталога исходников")

    return path


def _read_snapshot(
    evidence: Any,
    *,
    raw_root: Path,
    validate_url: Callable[[str], None],
) -> HttpResponseSnapshot:
    """Прочитать именно закреплённый снимок, не обращаясь к изменяемому указателю кеша."""

    if not isinstance(evidence, dict):
        raise CollectedArticleError("Отсутствует HTTP-свидетельство")

    digest = evidence.get("sha256")
    metadata_digest = evidence.get("response_metadata_sha256")

    if (
        not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None or
        not isinstance(metadata_digest, str) or re.fullmatch(r"[0-9a-f]{64}", metadata_digest) is None
    ):
        raise CollectedArticleError("Некорректная контрольная сумма свидетельства")

    requested_url = evidence.get("requested_url")

    if not isinstance(requested_url, str):
        raise CollectedArticleError("HTTP-свидетельство не содержит адреса запроса")

    validate_url(requested_url)
    relative_path = evidence.get("relative_path")
    metadata_path = f"responses/{digest[:2]}/{digest}.{metadata_digest}.http.json"
    allowed_paths = {f"responses/{digest[:2]}/{digest}.{suffix}" for suffix in ("pdf", "html", "txt", "bin")}

    if (
        not isinstance(relative_path, str) or relative_path not in allowed_paths or
        evidence.get("response_metadata_path") != metadata_path
    ):
        raise CollectedArticleError("Пути свидетельства не соответствуют контрольным суммам")

    body = _read_bytes(_safe_response_path(raw_root, relative_path), MAX_RESPONSE_BYTES)
    metadata_bytes = _read_bytes(_safe_response_path(raw_root, metadata_path), MAX_METADATA_BYTES)

    if (
        hashlib.sha256(body).hexdigest() != digest or
        type(evidence.get("bytes")) is not int or evidence["bytes"] != len(body) or
        hashlib.sha256(metadata_bytes).hexdigest() != metadata_digest
    ):
        raise CollectedArticleError("Контрольная сумма или размер HTTP-свидетельства не совпадает")

    metadata = json.loads(metadata_bytes)

    if not isinstance(metadata, dict):
        raise CollectedArticleError("Метаданные HTTP-свидетельства должны быть объектом")

    snapshot = HttpResponseSnapshot(
        requested_url=metadata["requested_url"],
        final_url=metadata["final_url"],
        status_code=metadata["status_code"],
        headers=tuple(tuple(header) for header in metadata["headers"]),
        retrieved_at=metadata["retrieved_at"],
        body=body,
    )
    _validate_snapshot(snapshot, requested_url, validate_url)

    if snapshot.status_code != 200 or snapshot.canonical_metadata() != metadata_bytes:
        raise CollectedArticleError("Ожидалось каноническое свидетельство HTTP 200")

    return snapshot


def _reparse_document(
    record: dict[str, Any], source: str, snapshot: HttpResponseSnapshot
) -> tuple[Document, list[Document]]:
    """Воспроизвести метаданные по исходному HTML, а не доверять производному JSON."""

    stored_document = record.get("document")

    if not isinstance(stored_document, dict) or not isinstance(stored_document.get("url"), str):
        raise CollectedArticleError("В карточке отсутствуют метаданные документа или его URL")

    url = stored_document["url"]
    issue_candidates: list[Document] = []

    if source == "ioffe":
        validate_ioffe_url(url)

        if (
            re.fullmatch(r"/articles/[1-9][0-9]*", urlsplit(url).path) is None or
            snapshot.requested_url != url or record.get("article_id") != url.rsplit("/", 1)[-1]
        ):
            raise CollectedArticleError("Карточка Иоффе не соответствует адресу статьи")

        document = parse_ioffe_article(_html(snapshot), url)

    else:
        adapter = get_source(source)
        adapter.validate_url(url)

        if record.get("metadata_page_url") != snapshot.requested_url:
            raise CollectedArticleError("Адрес страницы метаданных не соответствует свидетельству")

        html = decode_html(snapshot, adapter.default_encoding, decode_body=adapter.decode_body)
        role = adapter.classify_url(snapshot.requested_url)

        if role == "article" and url == snapshot.requested_url:
            document = adapter.parse_article(html, url)

            if (
                source == "ufn" and record.get("status") == "metadata_only" and
                stored_document.get("pdf_url") is None and document.pdf_url is not None and
                json.dumps(stored_document, sort_keys=True) != json.dumps(document.to_dict(), sort_keys=True)
            ):
                # Старые неизменяемые партии сохраняют результат прежней политики
                # PDF-ссылок. Новую ссылку учитываем только после повторного сбора;
                # остальные поля обязаны точно воспроизводиться по исходному HTML.
                legacy_document = parse_ufn_article(html, url, legacy_pdf_links=True)

                if (
                    legacy_document.pdf_url is None and
                    json.dumps(stored_document, sort_keys=True) ==
                    json.dumps(legacy_document.to_dict(), sort_keys=True)
                ):
                    document = legacy_document

        elif source == "quantum" and role == "issue" and adapter.classify_url(url) == "pdf":
            matches = [document for document in adapter.parse_issue_articles(html, snapshot.requested_url)
                       if document.url == url and document.pdf_url == url]

            if not matches:
                raise CollectedArticleError("Оглавление не содержит записи выбранной статьи")

            # Совпадение подтверждает подлинность JSON, но не разрешает конфликт
            # общей ссылки. Все кандидаты сохраняются для отложенной проверки.
            matched_document = next(
                (document for document in matches
                 if json.dumps(stored_document, sort_keys=True) ==
                 json.dumps(document.to_dict(), sort_keys=True)),
                None,
            )

            if matched_document is None:
                raise CollectedArticleError(
                    "Метаданные документа не совпадают с повторным разбором исходного HTML"
                )

            document = matched_document

            if len(matches) > 1:
                issue_candidates = matches

        else:
            raise CollectedArticleError("Страница метаданных не связана с выбранной статьёй")

    # JSON-сравнение различает true и 1, которые равны при обычном сравнении dict.
    if json.dumps(stored_document, sort_keys=True) != json.dumps(document.to_dict(), sort_keys=True):
        raise CollectedArticleError("Метаданные документа не совпадают с повторным разбором исходного HTML")

    if not document.title.strip():
        raise CollectedArticleError("Для регистрации этой партии нужна статья с названием")

    return document, issue_candidates


def _source_profile(source: str, document: Document) -> SourceProfile:
    """Выбрать явный профиль журнала, не объединяя все издания Иоффе по домену."""

    if source == "ioffe":
        journal_id = document.extra.get("journal_id")

        if (
            type(journal_id) is not int or journal_id not in IOFFE_PROFILE_KEYS or
            document.extra.get("journal") != IOFFE_JOURNALS[journal_id]
        ):
            raise CollectedArticleError("В исходной карточке Иоффе не определён однозначный русский журнал")

        name = IOFFE_PROFILE_KEYS[journal_id]

    else:
        name = "quantum_electronics" if source == "quantum" else source

    return get_source_profile(name, source=document.source, url=document.url)


def read_collected_article(path: Path, *, source: str, raw_root: Path) -> CollectedArticle:
    """Проверить локальную карточку и связанные исходники без сети и изменения реестров."""

    if source not in COLLECTED_SOURCES:
        raise CollectedArticleError(f"Неизвестный источник партии: {source}")

    path = Path(path)
    raw_root = Path(raw_root)

    if raw_root.is_symlink() or not raw_root.is_dir():
        raise CollectedArticleError("Каталог исходников отсутствует или является символической ссылкой")

    try:
        record = json.loads(_read_bytes(path, MAX_RECORD_BYTES))
        expected_schema = "ioffe-collected-article-v1" if source == "ioffe" else "journal-collected-article-v1"

        if not isinstance(record, dict) or record.get("schema_version") != expected_schema:
            raise CollectedArticleError("Неизвестный формат собранной карточки")

        if source != "ioffe" and record.get("source_id") != source:
            raise CollectedArticleError("Карточка относится к другому источнику")

        if record.get("status") not in {"metadata_only", "pdf_downloaded", "skipped_non_russian"}:
            raise CollectedArticleError("Неизвестный статус собранной карточки")

        validator = validate_ioffe_url if source == "ioffe" else get_source(source).validate_url
        # На macOS /var может указывать на /private/var. Доверенный корень
        # разрешаем явно; ссылки внутри него по-прежнему запрещены.
        root = raw_root.resolve()
        card = _read_snapshot(record.get("card_response"), raw_root=root, validate_url=validator)
        document, issue_candidates = _reparse_document(record, source, card)
        profile = _source_profile(source, document)
        pdf = None

        if record["status"] == "pdf_downloaded":
            pdf = _read_snapshot(record.get("pdf_response"), raw_root=root, validate_url=validator)

            if document.pdf_url is None or pdf.requested_url != document.pdf_url:
                raise CollectedArticleError("PDF не соответствует опубликованной ссылке статьи")

            pages = _pdf_page_count(pdf)

            if type(record.get("page_count")) is not int or record["page_count"] != pages:
                raise CollectedArticleError("Число страниц PDF не соответствует записи сборщика")

        elif record.get("pdf_response") is not None or "page_count" in record:
            raise CollectedArticleError("Карточка только с метаданными содержит противоречивые сведения о PDF")

        if record["status"] == "skipped_non_russian":
            if document.language == "ru" or source == "ioffe":
                raise CollectedArticleError("Отметка нерусскоязычной статьи противоречит исходной карточке")

            raise NonRussianArticleError(f"Проверенная карточка пропущена как нерусскоязычная: {document.url}")

        if document.language != "ru":
            raise CollectedArticleError("Для регистрации этой партии нужна русскоязычная статья")

        if issue_candidates:
            if record["status"] != "metadata_only":
                raise CollectedArticleError(
                    "Неоднозначная запись оглавления допустима для отложенной проверки только без скачанного PDF"
                )

            raise AmbiguousIssueEntryError(
                issue_url=card.requested_url, article_url=document.url, candidates=issue_candidates,
            )

        return CollectedArticle(document, profile, card, pdf, path.resolve())

    except CollectedArticleError:
        raise

    except (OSError, ValueError, TypeError, KeyError, RecursionError, IoffeError) as exception:
        raise CollectedArticleError(f"Не удалось проверить собранную карточку {path}: {exception}") from exception
