"""Планы локальной регистрации партий журналов без скачивания и извлечения текста."""

from __future__ import annotations

import copy
import re
import warnings

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.collect.base import Document

from .journal_import import CollectedArticle, NonRussianArticleError, read_collected_article
from .manifests import ManifestConflictError, ManifestPlan
from .registration import RegistrationOptions, plan_http_response


def load_articles(
    input_dir: Path,
    *,
    source: str,
    raw_root: Path,
    max_articles: int = 0,
) -> list[CollectedArticle]:
    """Прочитать фиксированный список карточек и проверить все исходники до записи."""

    if type(max_articles) is not int or max_articles < 0:
        raise ValueError("Лимит статей должен быть целым неотрицательным числом")

    input_dir = Path(input_dir)

    if not input_dir.is_dir() or input_dir.is_symlink():
        raise ValueError("Каталог карточек отсутствует или является символической ссылкой")

    directories = [input_dir / name for name in ("articles", "metadata") if (input_dir / name).is_dir()]
    directories = directories or [input_dir]

    if any(directory.is_symlink() for directory in directories):
        raise ValueError("Каталог карточек не должен быть символической ссылкой")

    paths = sorted(path for directory in directories for path in directory.glob("*.json"))

    if not paths:
        raise ValueError("В выбранном каталоге нет карточек JSON")

    articles: dict[str, CollectedArticle] = {}

    for path in paths:
        try:
            article = read_collected_article(path, source=source, raw_root=raw_root)

        except NonRussianArticleError as exception:
            warnings.warn(str(exception), stacklevel=2)
            continue

        previous = articles.get(article.document.url)

        if previous is not None:
            if previous.document != article.document or previous.card_response != article.card_response:
                raise ManifestConflictError(f"Разные версии одной карточки требуют выбора: {path}")

            if previous.pdf_response is not None and article.pdf_response is not None:
                if previous.pdf_response != article.pdf_response:
                    raise ManifestConflictError(f"Разные PDF одной карточки требуют выбора: {path}")

            if previous.pdf_response is not None:
                continue

        articles[article.document.url] = article

    # Лимит применяется после объединения режимов metadata и articles:
    # иначе PDF той же работы мог бы оказаться за границей выбранной порции.
    result = list(articles.values())

    return result[:max_articles] if max_articles else result


def plan_collected_article(
    article: CollectedArticle,
    *,
    rights_record_ids: tuple[str, ...],
    acquisition_scope: str = "bulk",
    registered_at: str | None = None,
    pdf_rights_record_ids: tuple[str, ...] | None = None,
    scoped_to_response: bool = False,
    inventory_only: bool = False,
) -> ManifestPlan:
    """Связать карточку и PDF с одной работой, не создавая производный TXT."""

    document = copy.deepcopy(article.document)
    published_year = _publication_year(document)

    if document.text.strip():
        document.extra["abstract"] = document.text.strip()

    timestamp = registered_at or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    shared_page = article.card_response.requested_url != document.url
    individual_context = "artifact" if scoped_to_response else "work"
    options = RegistrationOptions(
        content_role="metadata_only",
        acquisition_method="crawler",
        acquisition_scope=acquisition_scope,
        rights_record_ids=rights_record_ids,
        extraction_method="not_started",
        extraction_version="journal-import-v1",
        response_representation="html",
        request_context_type="source" if shared_page else individual_context,
        inventory_only=inventory_only,
    )
    plans = [plan_http_response(document, article.profile, options, timestamp, article.card_response)]

    if article.pdf_response is not None:
        pdf_options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="crawler",
            acquisition_scope=acquisition_scope,
            rights_record_ids=pdf_rights_record_ids if pdf_rights_record_ids is not None else rights_record_ids,
            extraction_method="not_started",
            extraction_version="journal-import-v1",
            response_representation="pdf",
            request_context_type=individual_context,
            inventory_only=inventory_only,
        )
        plans.append(plan_http_response(document, article.profile, pdf_options, timestamp, article.pdf_response))

    combined = combine_article_plans(plans)

    if published_year is not None and not document.published:
        for work in combined.works:
            work["published_year"] = published_year

    return combined


def _publication_year(document: Document) -> int | None:
    """Сохранить известный год отдельно, не подставляя первое января вместо даты."""

    years = []

    for name in ("year", "published_year"):
        value = document.extra.get(name)

        if value is None:
            continue

        if type(value) is not int or not 1000 <= value <= 9999:
            raise ValueError(f"Поле {name} должно содержать четырёхзначный целый год")

        years.append(value)

    if document.published and re.fullmatch(r"[0-9]{4}", document.published):
        years.append(int(document.published))
        document.published = None

    if not years:
        return

    if len(set(years)) != 1 or not 1000 <= years[0] <= 9999:
        raise ValueError("В карточке указаны несовместимые годы публикации")

    document.extra["year"] = years[0]

    return years[0]


def _unique_records(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Убрать только точные повторы записей, остановившись при конфликте ID."""

    unique: dict[str, dict[str, Any]] = {}

    for record in records:
        identifier = record[key]

        if identifier in unique and unique[identifier] != record:
            raise ManifestConflictError(f"Пакет содержит разные записи {key}={identifier!r}")

        unique[identifier] = record

    return list(unique.values())


def combine_article_plans(plans: list[ManifestPlan]) -> ManifestPlan:
    """Собрать атомарный пакет, не скрывая несовместимые идентичности и файлы."""

    combined = ManifestPlan()

    for field_name, key in (
        ("works", "work_id"),
        ("artifacts", "artifact_record_id"),
        ("retrieval_events", "retrieval_id"),
        ("work_aliases", "alias_record_id"),
    ):
        records = [record for plan in plans for record in getattr(plan, field_name)]
        setattr(combined, field_name, _unique_records(records, key))

    blobs = {}

    for plan in plans:
        if plan.rights or plan.identity_conflicts or plan.operation_decisions or plan.condition_fulfilments:
            raise ValueError("Объединять можно только исходные планы импорта, без решений и прав")

        if plan.work_update_reasons or plan.artifact_update_reasons:
            raise ValueError("Объединение выполняется до согласования с рабочими реестрами")

        for blob in plan.blobs:
            if blob.relative_path in blobs and blobs[blob.relative_path] != blob:
                raise ManifestConflictError(f"Разные файлы претендуют на путь {blob.relative_path}")

            blobs[blob.relative_path] = blob

        combined.warnings.extend(plan.warnings)

    combined.blobs = list(blobs.values())

    return combined
