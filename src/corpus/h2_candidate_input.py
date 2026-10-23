"""Проверка полного локального снимка метаданных ОИЯИ для очереди кандидатов H2."""

from __future__ import annotations

import re

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .jinr_registration import _inside_project, _json_object, _read_response, _uuid
from .manifests import sha256_bytes


@dataclass(frozen=True)
class CandidateSnapshot:
    """Исходные карточки и проверенное происхождение без тематического допуска."""

    items: tuple[dict[str, Any], ...]
    sources: dict[str, dict[str, Any]]
    provenance: dict[str, Any]


def read_candidate_snapshot(project_root: Path, snapshot_path: Path) -> CandidateSnapshot:
    """Прочитать полный архив метаданных, проверив все страницы и HTTP-свидетельства."""

    root = Path(project_root).resolve()
    path = _inside_project(root, snapshot_path, data_only=False)
    snapshot_bytes = path.read_bytes()
    snapshot = _json_object(snapshot_bytes, "опись снимка метаданных ОИЯИ")

    if snapshot.get("schema_version") != "jinr-metadata-analysis-v1":
        raise ValueError("Нужна опись jinr-metadata-analysis-v1")

    entries = _source_entries(snapshot.get("source_pages"))
    snapshot_relative_path = path.relative_to(root).as_posix()
    snapshot_sha256 = sha256_bytes(snapshot_bytes)
    items: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    validated_entries: list[dict[str, Any]] = []
    expected_totals: tuple[int, int, int] | None = None

    for entry in entries:
        page_path = _inside_project(root, entry.get("path"))
        http_path = _inside_project(root, page_path.with_name(
            f"{entry['sha256']}.{entry['http_evidence_sha256']}.http.json",
        ))
        http_metadata = _json_object(http_path.read_bytes(), "HTTP-свидетельство страницы")
        response = _read_response(root, {
            "body_path": page_path,
            "body_sha256": entry["sha256"],
            "response_metadata_path": http_path,
            "response_metadata_sha256": entry["http_evidence_sha256"],
            "requested_url": http_metadata.get("requested_url"),
        }, is_pdf=False)
        payload = _json_object(response.snapshot.body, "страница метаданных ОИЯИ")
        result = _search_result(payload)
        page_number, page_size, total_pages, total_items = _pagination(result.get("page"))
        _validate_query(response.snapshot.requested_url, page_number, page_size)

        if page_number != entry["page"]:
            raise ValueError("Номер страницы API не совпадает с описью")

        totals = page_size, total_pages, total_items

        if expected_totals is None:
            expected_totals = totals

        if totals != expected_totals:
            raise ValueError("Размер страницы или итоги пагинации различаются между ответами API")

        if len(entries) != max(1, total_pages):
            raise ValueError("Опись содержит не все страницы снимка метаданных")

        page_items = _page_items(result)
        expected_count = min(page_size, max(0, total_items - page_number * page_size))

        if len(page_items) != expected_count or entry["items"] != len(page_items):
            raise ValueError("Количество карточек страницы не совпадает с пагинацией или описью")

        source = {
            "snapshot_path": snapshot_relative_path,
            "snapshot_sha256": snapshot_sha256,
            "page_path": response.body_path,
            "page_sha256": response.body_sha256,
            "http_evidence_path": response.response_metadata_path,
            "http_evidence_sha256": response.response_metadata_sha256,
            "requested_url": response.snapshot.requested_url,
            "retrieved_at": response.snapshot.retrieved_at,
            "page_number": page_number,
        }

        for item_index, item in enumerate(page_items):
            item_uuid = _uuid(item.get("uuid"))
            _validate_metadata(item)

            if item_uuid in sources:
                raise ValueError(f"Снимок повторяет UUID карточки: {item_uuid}")

            sources[item_uuid] = {**source, "item_index": item_index}
            items.append(item)

        validated_entries.append({
            **entry,
            "path": response.body_path,
            "http_evidence_path": response.response_metadata_path,
            "requested_url": response.snapshot.requested_url,
            "retrieved_at": response.snapshot.retrieved_at,
        })

    _validate_snapshot_counts(snapshot.get("snapshot"), len(entries), len(items))

    return CandidateSnapshot(
        items=tuple(items),
        sources=sources,
        provenance={
            "snapshot_path": snapshot_relative_path,
            "snapshot_sha256": snapshot_sha256,
            "source_pages": validated_entries,
            "total_items": len(items),
            "total_pages": len(entries),
        },
    )


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    """Принять целое число заданного диапазона, не подменяя его логическим значением."""

    if type(value) is not int or value < minimum:
        raise ValueError(f"{label}: нужно целое число не меньше {minimum}")

    return value


def _source_entries(value: Any) -> list[dict[str, Any]]:
    """Проверить опись страниц и вернуть непрерывную последовательность по номеру."""

    if not isinstance(value, list) or not value:
        raise ValueError("source_pages должен содержать непустую опись страниц")

    entries: list[dict[str, Any]] = []

    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Запись source_pages должна быть JSON-объектом")

        _integer(entry.get("page"), "source_pages.page")
        _integer(entry.get("items"), "source_pages.items")

        for field in ("sha256", "http_evidence_sha256"):
            digest = entry.get(field)

            if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
                raise ValueError(f"source_pages.{field}: нужен SHA-256 в нижнем регистре")

        entries.append(entry)

    entries.sort(key=lambda entry: entry["page"])

    if [entry["page"] for entry in entries] != list(range(len(entries))):
        raise ValueError("В описи пропущена или повторена страница")

    return entries


def _search_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Проверить единственный результат поиска и отсутствие дополнительных фильтров."""

    filters = payload.get("appliedFilters")

    if not isinstance(filters, list) or len(filters) != 1 or not isinstance(filters[0], dict):
        raise ValueError("Нужен полный снимок с единственным фильтром языка ru")

    expected_filter = {"filter": "Lang", "operator": "equals", "value": "ru"}

    if any(filters[0].get(key) != value for key, value in expected_filter.items()):
        raise ValueError("API не подтвердил единственный фильтр языка ru")

    if any(payload.get(field) not in (None, "") for field in ("query", "scope", "configuration")):
        raise ValueError("Страница API содержит дополнительное ограничение области поиска")

    embedded = payload.get("_embedded")

    if not isinstance(embedded, dict):
        raise ValueError("Отсутствует объект результатов поиска API")

    keys = [key for key in ("searchResult", "searchResults") if key in embedded]

    if len(keys) != 1 or not isinstance(embedded[keys[0]], dict):
        raise ValueError("Нужен ровно один объект searchResult или searchResults")

    return embedded[keys[0]]


def _pagination(value: Any) -> tuple[int, int, int, int]:
    """Проверить арифметику пагинации без пропущенных страниц или фиктивных итогов."""

    if not isinstance(value, dict):
        raise ValueError("У результата API отсутствует пагинация page")

    number = _integer(value.get("number"), "page.number")
    size = _integer(value.get("size"), "page.size", minimum=1)
    total_pages = _integer(value.get("totalPages"), "page.totalPages")
    total_items = _integer(value.get("totalElements"), "page.totalElements")

    if total_pages != (total_items + size - 1) // size or number >= max(1, total_pages):
        raise ValueError("Пагинация API внутренне противоречива")

    return number, size, total_pages, total_items


def _validate_query(url: str, page_number: int, page_size: int) -> None:
    """Сопоставить параметры исходного запроса с полным русскоязычным снимком."""

    try:
        query = parse_qs(urlsplit(url).query, keep_blank_values=True, strict_parsing=True)

    except ValueError as exception:
        raise ValueError("Некорректные параметры исходного запроса API") from exception

    expected = {
        "dsoType": ["item"],
        "f.Lang": ["ru,equals"],
        "size": [str(page_size)],
        "page": [str(page_number)],
    }

    # DSpace может опускать page=0 в ссылке первой страницы, остальные параметры обязательны.
    if page_number == 0 and "page" not in query:
        expected.pop("page")

    if query != expected:
        raise ValueError("Запрос не соответствует полному снимку ru без отбора по наличию файлов")


def _page_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Извлечь все карточки страницы, не отбирая жанры и не меняя исходные метаданные."""

    embedded = result.get("_embedded")
    objects = embedded.get("objects") if isinstance(embedded, dict) else None

    if not isinstance(objects, list):
        raise ValueError("В результате API отсутствует список objects")

    items: list[dict[str, Any]] = []

    for entry in objects:
        nested = entry.get("_embedded") if isinstance(entry, dict) else None
        item = nested.get("indexableObject") if isinstance(nested, dict) else None

        if not isinstance(item, dict) or item.get("type") != "item":
            raise ValueError("Результат API должен содержать карточку indexableObject типа item")

        items.append(item)

    return items


def _validate_metadata(item: dict[str, Any]) -> None:
    """Отклонить повреждённые метаданные, сохранив пустые строки для диагностики отбора."""

    if "id" in item and item["id"] != item["uuid"]:
        raise ValueError("Поля id и uuid карточки различаются")

    metadata = item.get("metadata")

    if not isinstance(metadata, dict):
        raise ValueError("metadata карточки должен быть JSON-объектом")

    for field, values in metadata.items():
        if not isinstance(field, str) or not field.strip() or not isinstance(values, list):
            raise ValueError("Поле metadata должно иметь непустое имя и список значений")

        if any(not isinstance(value, dict) or not isinstance(value.get("value"), str) for value in values):
            raise ValueError(f"metadata.{field}: каждое значение должно содержать строку value")


def _validate_snapshot_counts(value: Any, page_count: int, item_count: int) -> None:
    """Сверить заявленные агрегаты описи, если они сохранены вместе с анализом."""

    if value is None:
        return

    if not isinstance(value, dict):
        raise ValueError("snapshot должен быть JSON-объектом")

    for field, expected in {
        "pages": page_count, "items": item_count, "unique_uuids": item_count,
    }.items():
        if field in value and _integer(value[field], f"snapshot.{field}") != expected:
            raise ValueError(f"snapshot.{field} не совпадает с проверенными страницами")

    if "page_totals" in value:
        totals = value["page_totals"]

        if not isinstance(totals, list) or len(totals) != 1:
            raise ValueError("snapshot.page_totals должен содержать один общий итог")

        if _integer(totals[0], "snapshot.page_totals") != item_count:
            raise ValueError("snapshot.page_totals не совпадает с числом карточек")
