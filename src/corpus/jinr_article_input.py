"""Строгое чтение офлайн-пакета журнальных статей ОИЯИ и его свидетельств."""

from __future__ import annotations

import re

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jinr_registration import (
    API_HOST,
    ArchivedResponse,
    _inside_project,
    _item_doi,
    _json_object,
    _metadata_values,
    _publication_date,
    _read_response,
    _record_list,
    _search_items,
    _timestamp,
    _uuid,
)
from .manifests import sha256_bytes

SOURCE_SCHEMAS = {
    "selection": ("selection_version", "jinr-article-selection-v1"),
    "matches": ("schema_version", "jinr-local-pdf-metadata-match-v1"),
    "inventory": ("inventory_schema_version", "jinr-download-inventory-v1"),
}
SELECTION_RULE = {
    "decision": "physics_candidate",
    "match_status": "document_identity_match",
    "metadata_document_type": "Article",
    "deduplicate_by": "pdf_sha256",
    "human_review_status": "pending",
}
ACQUISITION_FIELDS = {
    "intake_id": "intake_id",
    "bitstream_uuid": "bitstream_uuid",
    "original_filename": "original_filename",
    "source_url": "source_url",
    "retrieved_at_as_recorded": "retrieved_at",
    "acquisition_agent_as_recorded": "acquisition_agent",
    "acquisition_method": "acquisition_method",
    "acquisition_scope": "acquisition_scope",
    "file_modified_at_as_recorded": "file_modified_at",
}
ADJACENT_COMPUTING_GROUP = "adjacent_experimental_computing"
ADJACENT_COMPUTATIONAL_METHODS_GROUP = "adjacent_computational_methods"
APPROVED_CORPUS_GROUPS = frozenset({
    ADJACENT_COMPUTING_GROUP,
    ADJACENT_COMPUTATIONAL_METHODS_GROUP,
})


@dataclass(frozen=True)
class VerifiedArticle:
    """Проверенная статья с исходной карточкой API и историей ручных загрузок."""

    entry: dict[str, Any]
    item: dict[str, Any]
    metadata_response: ArchivedResponse
    acquisitions: tuple[dict[str, Any], ...]
    pdf_path: str
    pdf_sha256: str
    pdf_bytes: int


@dataclass(frozen=True)
class ArticleBatch:
    """Проверенный пакет без решения о поджанре, правах или включении в корпус."""

    path: str
    entries: tuple[VerifiedArticle, ...]


@dataclass(frozen=True)
class _BatchFormat:
    """Разделить форматы пакетов, сохранив общую проверку исходных свидетельств."""

    schema_version: str
    profile: str
    selection_version: str
    selection_prefix: str
    selection_rule: dict[str, str]
    document_type: str
    container_entry_field: str
    container_selection_field: str


ARTICLE_FORMAT = _BatchFormat(
    schema_version="jinr-article-registration-preparation-v1",
    profile="jinr_articles",
    selection_version="jinr-article-selection-v1",
    selection_prefix="jinr-article-selection",
    selection_rule=SELECTION_RULE,
    document_type="Article",
    container_entry_field="journal_title_as_reported",
    container_selection_field="journal",
)


def read_article_batch(project_root: Path, path: Path) -> ArticleBatch:
    """Проверить весь пакет, исходные описи, PDF и HTTP-свидетельства без записи."""

    return _read_batch(project_root, path, ARTICLE_FORMAT)


def _read_batch(project_root: Path, path: Path, batch_format: _BatchFormat) -> ArticleBatch:
    """Проверить общую цепочку происхождения, не смешивая типы исходных публикаций."""

    root = Path(project_root).resolve()
    batch_path = _manifest_path(root, path)
    batch = _json_object(batch_path.read_bytes(), "пакет статей")

    _expect(batch, {
        "schema_version": batch_format.schema_version,
        "status": "prepared_not_registered",
        "proposed_profile": batch_format.profile,
        "registration_performed": False,
        "network_used": False,
        "selection_rule": batch_format.selection_rule,
    }, "пакет статей")
    sources = _read_sources(root, batch.get("source_files"), batch_format.selection_version)
    entries = _record_list(batch.get("entries"), "entries")
    selection_rows = sources["selection"][2]
    _unique(selection_rows, "selection_id")
    _unique(selection_rows, "pdf_sha256")
    _unique(selection_rows, "item_uuid")
    selected = {
        row["selection_id"]: row for row in selection_rows
        if row.get("decision") == "physics_candidate"
        and row.get("identity_status") == "document_identity_match"
    }
    _unique(entries, "selection_id")
    _unique(entries, "item_uuid")

    if {entry["selection_id"] for entry in entries} != set(selected):
        raise ValueError("Пакет должен точно соответствовать отобранным physics_candidate")

    _expect(batch, {"candidate_count": len(entries)}, "число статей")
    results: list[VerifiedArticle] = []
    seen_hashes: set[str] = set()
    seen_paths: set[str] = set()

    for entry in entries:
        article = _read_article(root, entry, selected[entry["selection_id"]], sources, batch_format)
        paths = {row["staged_relative_path"] for row in article.acquisitions}

        if article.pdf_sha256 in seen_hashes or seen_paths & paths:
            raise ValueError("Пакет повторяет SHA-256 или PDF-путь разных статей")

        seen_hashes.add(article.pdf_sha256)
        seen_paths.update(paths)
        results.append(article)

    _expect(batch, {"pdf_path_count": len(seen_paths)}, "число PDF-путей")

    return ArticleBatch(batch_path.relative_to(root).as_posix(), tuple(results))


def _expect(record: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    """Сопоставить обязательные поля без смешения bool и целых чисел."""

    for field, value in expected.items():
        actual = record.get(field)

        if field not in record or type(actual) is not type(value) or actual != value:
            raise ValueError(f"{label}: поле {field} не совпадает с исходным свидетельством")


def _object(value: Any, label: str) -> dict[str, Any]:
    """Проверить вложенный объект перед обращением к его полям."""

    if not isinstance(value, dict) or not value:
        raise ValueError(f"{label}: нужен непустой JSON-объект")

    return value


def _text(value: Any, label: str) -> str:
    """Принять непустую строку идентификатора или пути."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}: нужна непустая строка")

    return value


def _unique(records: list[dict[str, Any]], field: str) -> None:
    """Отклонить повторные и отсутствующие идентификаторы исходных строк."""

    values = [_text(record.get(field), field) for record in records]

    if len(set(values)) != len(values):
        raise ValueError(f"Повторное значение {field}")


def _manifest_path(root: Path, value: Any) -> Path:
    """Разрешить ссылку на опись только внутри manifests, включая символьные ссылки."""

    path = _inside_project(root, value, data_only=False)

    if not path.is_relative_to(root / "manifests"):
        raise ValueError("Ссылка на опись выходит за каталог manifests")

    return path


def _read_sources(
    root: Path, value: Any, selection_version: str = "jinr-article-selection-v1",
) -> dict[str, tuple[str, str, list[dict[str, Any]]]]:
    """Проверить хеши трёх JSONL-источников и определить их роли по схеме строк."""

    records = _record_list(value, "source_files")
    source_schemas = {**SOURCE_SCHEMAS, "selection": ("selection_version", selection_version)}
    result: dict[str, tuple[str, str, list[dict[str, Any]]]] = {}
    seen_paths: set[Path] = set()

    for record in records:
        path = _manifest_path(root, record.get("path"))
        body = path.read_bytes()
        digest = sha256_bytes(body)
        _expect(record, {"sha256": digest}, "source_files")

        if path in seen_paths:
            raise ValueError("source_files повторяет путь исходника")

        seen_paths.add(path)
        rows = [_json_object(line, str(path)) for line in body.splitlines()]
        roles = [
            role for role, (field, version) in source_schemas.items()
            if rows and all(row.get(field) == version for row in rows)
        ]

        if len(roles) != 1 or roles[0] in result:
            raise ValueError("source_files содержит неверную или повторную роль исходника")

        role = roles[0]

        if role in {"matches", "inventory"}:
            _unique(rows, "intake_id")
            _unique(rows, "bitstream_uuid")
            _unique(rows, "pdf_path" if role == "matches" else "staged_relative_path")

        result[role] = (path.relative_to(root).as_posix(), digest, rows)

    if set(result) != set(SOURCE_SCHEMAS):
        raise ValueError("source_files должен содержать selection, matches и inventory")

    return result


def _source_row(
    root: Path, reference: dict[str, Any], path_field: str, line_field: str,
    source: tuple[str, str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Получить точную строку описи, проверив путь, роль и номер строки."""

    path = _manifest_path(root, reference.get(path_field)).relative_to(root).as_posix()
    line = reference.get(line_field)

    if path != source[0] or type(line) is not int or not 1 <= line <= len(source[2]):
        raise ValueError("Ссылка на строку исходника имеет неверные путь, роль или номер")

    return source[2][line - 1]


def _read_metadata(root: Path, source: dict[str, Any]) -> ArchivedResponse:
    """Восстановить каноническое HTTP-свидетельство через проверенную запись кеша."""

    cache_path = _manifest_path(root, source.get("cache_record_path"))
    cache_body = cache_path.read_bytes()
    _expect(source, {"cache_record_sha256": sha256_bytes(cache_body)}, "metadata_source")
    cache = _json_object(cache_body, "запись кеша API")
    _expect(cache, {
        "kind": "json",
        "sha256": source.get("api_response_sha256"),
        "requested_url": source.get("api_requested_url"),
    }, "запись кеша API")
    cache_root = root / "data/raw/jinr_api"
    body_path = _inside_project(root, cache_root / _text(cache.get("relative_path"), "relative_path"))
    declared_path = _inside_project(root, source.get("api_response_path"))

    if body_path != declared_path:
        raise ValueError("Путь тела API не совпадает с записью кеша")

    response = _read_response(root, {
        "requested_url": cache["requested_url"],
        "body_path": str(body_path),
        "body_sha256": cache["sha256"],
        "response_metadata_path": str(cache_root / _text(
            cache.get("response_metadata_path"), "response_metadata_path",
        )),
        "response_metadata_sha256": cache.get("response_metadata_sha256"),
    }, is_pdf=False)
    _expect(cache, {"bytes": len(response.snapshot.body)}, "размер ответа API")

    return response


def _validate_article_item(
    item: dict[str, Any], item_uuid: str, document_type: str = "Article",
) -> None:
    """Проверить UUID, собственную ссылку, ожидаемый тип и русский язык карточки."""

    _expect(item, {"uuid": item_uuid, "id": item_uuid, "type": "item"}, "карточка API")
    links = _object(item.get("_links"), "_links")
    self_link = _object(links.get("self"), "self")
    _expect(self_link, {"href": f"https://{API_HOST}/server/api/core/items/{item_uuid}"}, "ссылка API")

    if _metadata_values(item, "dc.type") != [document_type]:
        raise ValueError(f"Пакет принимает только явно обозначенный тип {document_type}")

    if _metadata_values(item, "dc.language.iso") != ["ru"]:
        raise ValueError("Пакет принимает только явно русский язык ru")

    _article_title(item)

    if len(_metadata_values(item, "dc.relation.ispartof")) != 1:
        raise ValueError("Карточка статьи должна содержать одно значение dc.relation.ispartof")

    _publication_date(item)
    _item_doi(item)


def _title_language(record: dict[str, Any]) -> str | None:
    """Прочитать языковую метку названия, не заменяя исходное значение в API."""

    language = record.get("language")

    if language is None:
        return

    if not isinstance(language, str):
        raise ValueError("Языковая метка dc.title должна быть строкой или null")

    primary_language = language.strip().casefold().replace("_", "-").split("-", 1)[0]

    return {"rus": "ru", "eng": "en"}.get(primary_language, primary_language) or None


def _article_title(item: dict[str, Any]) -> str:
    """Выбрать точное русское название, отклоняя неоднозначные многоязычные записи."""

    titles = _metadata_values(item, "dc.title")

    if not titles:
        raise ValueError("Карточка статьи должна содержать непустое dc.title")

    records = item["metadata"]["dc.title"]
    languages_by_title: dict[str, set[str]] = {title: set() for title in titles}

    for record in records:
        language = _title_language(record)

        if language is not None:
            languages_by_title[record["value"]].add(language)

    if any(len(languages) > 1 for languages in languages_by_title.values()):
        raise ValueError("Одно значение dc.title содержит противоречивые языковые метки")

    if len(titles) == 1:
        return titles[0]

    russian_titles = [
        title for title, languages in languages_by_title.items() if languages == {"ru"}
    ]

    if not russian_titles:
        # В старых карточках оба языка бывают null: алфавит применяется только
        # при единственном русском варианте и явно отличимом латинском переводе.
        russian_titles = [
            title for title, languages in languages_by_title.items()
            if not languages and re.search(r"[А-Яа-яЁё]", title)
        ]

    if len(russian_titles) != 1:
        raise ValueError("Карточка статьи содержит неоднозначное русское dc.title")

    title = russian_titles[0]

    if any(
        re.search(r"[\u0400-\u052f]", alternative) or not re.search(r"[A-Za-z]", alternative)
        for alternative in titles if alternative != title
    ):
        raise ValueError("Другие dc.title не позволяют однозначно выбрать русское название")

    return title


def _corpus_group(entry: dict[str, Any], document_type: str) -> str | None:
    """Принять только явно назначенную смежную группу журнальных статей."""

    if "corpus_group_id" not in entry:
        return

    group = entry["corpus_group_id"]

    if (
        document_type != "Article"
        or not isinstance(group, str)
        or group not in APPROVED_CORPUS_GROUPS
    ):
        raise ValueError("Неизвестная или неприменимая corpus_group_id")

    return group


def _validate_metadata_copies(
    entry: dict[str, Any], selection: dict[str, Any], matched: dict[str, Any],
    item: dict[str, Any], batch_format: _BatchFormat = ARTICLE_FORMAT,
) -> None:
    """Сравнить производные название, DOI, журнал и дату с исходной карточкой API."""

    _expect(entry, {"title": _article_title(item)}, "название entry")
    _expect(selection, {"title": entry["title"]}, "название selection")
    _expect(matched, {"title": entry["title"], "item_uuid": item["uuid"]}, "matched_item")

    if len(item["metadata"]["dc.title"]) > 1 or "titles_as_reported" in entry:
        _expect(entry, {"titles_as_reported": item["metadata"]["dc.title"]}, "исходные названия entry")

    if _corpus_group(entry, batch_format.document_type) != _corpus_group(selection, batch_format.document_type):
        raise ValueError("corpus_group_id entry и selection должны совпадать")

    for entry_field, source_field, api_field in (
        ("metadata_document_type_as_reported", "document_type", "dc.type"),
        (batch_format.container_entry_field, "journal", "dc.relation.ispartof"),
        ("issued_as_reported", "issued", "dc.date.issued"),
        ("doi_as_reported", "doi", "dc.identifier.doi"),
    ):
        values = _metadata_values(item, api_field)
        _expect(entry, {entry_field: values}, "метаданные entry")
        _expect(matched, {source_field: values}, "метаданные matched_item")

        if source_field != "document_type":
            selection_field = batch_format.container_selection_field if source_field == "journal" else source_field
            _expect(selection, {selection_field: values}, "метаданные selection")

    _expect(selection, {"language_as_reported": ["ru"]}, "язык selection")
    _expect(matched, {"language": ["ru"]}, "язык matched_item")


def _read_article(
    root: Path, entry: dict[str, Any], selection: dict[str, Any],
    sources: dict[str, tuple[str, str, list[dict[str, Any]]]],
    batch_format: _BatchFormat = ARTICLE_FORMAT,
) -> VerifiedArticle:
    """Проверить цепочку выбора, сопоставления, API и всех экземпляров одной статьи."""

    item_uuid = _uuid(entry.get("item_uuid"))
    pdf = _object(entry.get("pdf"), "pdf")
    pdf_path = _inside_project(root, pdf.get("path")).relative_to(root).as_posix()
    pdf_body = (root / pdf_path).read_bytes()
    digest = sha256_bytes(pdf_body)
    _expect(pdf, {"sha256": digest}, "PDF")
    aliases = pdf.get("aliases")

    if not isinstance(aliases, list) or not aliases or any(not isinstance(value, str) for value in aliases):
        raise ValueError("PDF aliases должен быть непустым списком путей")

    alias_paths = [_inside_project(root, value).relative_to(root).as_posix() for value in aliases]

    if len(set(alias_paths)) != len(aliases) or pdf_path not in alias_paths:
        raise ValueError("PDF aliases повторяет путь или не содержит основной PDF")

    _expect(entry, {"selection_id": f"{batch_format.selection_prefix}:{digest}"}, "selection_id")
    _expect(selection, {
        "item_uuid": item_uuid, "pdf_path": pdf["path"], "pdf_sha256": digest,
        "pdf_aliases": aliases, "display_index": entry.get("selection_display_index"),
        "flags": entry.get("flags"),
    }, "строка selection")
    source = _object(entry.get("metadata_source"), "metadata_source")
    _expect(_object(selection.get("metadata_source"), "metadata_source selection"), {
        key: source.get(key) for key in ("api_response_path", "api_response_sha256")
    }, "источник selection")
    response = _read_metadata(root, source)
    items = _search_items(response.snapshot.body)
    _unique(items, "uuid")
    matching_items = [item for item in items if item.get("uuid") == item_uuid]

    if len(matching_items) != 1:
        raise ValueError("Исходная страница API должна содержать ровно одну карточку item_uuid")

    item = matching_items[0]
    _validate_article_item(item, item_uuid, batch_format.document_type)
    identity = _object(entry.get("identity_evidence"), "identity_evidence")
    primary_match = _source_row(root, identity, "matches_path", "matches_line", sources["matches"])
    _expect(primary_match, {"pdf_path": pdf["path"]}, "основное сопоставление")
    _expect(selection, {"bitstream_uuid": primary_match.get("bitstream_uuid")}, "bitstream selection")

    for field in ("match_status", "confidence", "match_methods", "bitstream_item_relation_verified"):
        _expect(identity, {field: primary_match.get(field)}, "identity_evidence")

    acquisitions = _read_acquisitions(root, entry, alias_paths, pdf_body, sources)
    matches_by_path = {row["pdf_path"]: row for row in sources["matches"][2]}

    for acquisition in acquisitions:
        path = acquisition["staged_relative_path"]
        match = matches_by_path.get(path)

        if match is None:
            raise ValueError("Для PDF alias отсутствует строка сопоставления")

        _expect(match, {
            "pdf_sha256": digest, "match_status": "document_identity_match",
            "bitstream_item_relation_verified": False,
            "intake_id": acquisition["intake_id"], "bitstream_uuid": acquisition["bitstream_uuid"],
            "original_filename": acquisition["original_filename"],
            "download_source_url": acquisition["source_url"], "metadata_source": source,
        }, "строка matches")
        candidates = match.get("candidate_item_ids")

        if not isinstance(candidates, list) or item_uuid not in candidates:
            raise ValueError("Выбранный item_uuid отсутствует среди кандидатов сопоставления")

        candidate_ids = [_uuid(value) for value in candidates]

        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("Повторный UUID среди кандидатов сопоставления")

        methods = match.get("match_methods")

        if not isinstance(methods, list) or not methods or any(not isinstance(value, str) or not value for value in methods):
            raise ValueError("Сопоставление должно сохранять непустые match_methods")

        inventory_reference = _object(match.get("inventory_source"), "inventory_source")
        _expect(inventory_reference, {"sha256": sources["inventory"][1]}, "inventory_source")
        inventory_row = _source_row(root, inventory_reference, "path", "line", sources["inventory"])

        if inventory_row != acquisition:
            raise ValueError("Сопоставление ссылается на другую строку inventory")

        _validate_metadata_copies(
            entry, selection, _object(match.get("matched_item"), "matched_item"), item, batch_format,
        )

    return VerifiedArticle(entry, item, response, tuple(acquisitions), pdf_path, digest, len(pdf_body))


def _read_acquisitions(
    root: Path, entry: dict[str, Any], aliases: list[str], pdf_body: bytes,
    sources: dict[str, tuple[str, str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Проверить ручное получение каждого PDF без приписывания HTTP-ответа файлу."""

    records = _record_list(entry.get("acquisition_evidence"), "acquisition_evidence")
    acquisitions: list[dict[str, Any]] = []

    for record in records:
        acquisition = _source_row(root, record, "inventory_path", "inventory_line", sources["inventory"])
        _expect(record, {
            field: acquisition.get(original) for field, original in ACQUISITION_FIELDS.items()
        }, "acquisition_evidence")
        bitstream_uuid = _uuid(acquisition.get("bitstream_uuid"))
        _expect(acquisition, {
            "intake_id": f"jinr-bitstream:{bitstream_uuid}",
            "source_url": f"https://{API_HOST}/server/api/core/bitstreams/{bitstream_uuid}/content",
            "acquisition_method": "manual_download", "acquisition_scope": "bulk",
            "sha256": sha256_bytes(pdf_body), "bytes": len(pdf_body),
            "pdf_signature_valid": True, "item_uuid": None, "canonical_item_url": None,
        }, "ручная загрузка")
        _timestamp(acquisition.get("retrieved_at"))
        _timestamp(acquisition.get("file_modified_at"))
        _text(acquisition.get("acquisition_agent"), "acquisition_agent")
        _text(acquisition.get("original_filename"), "original_filename")
        path = _inside_project(root, acquisition.get("staged_relative_path"))
        relative_path = path.relative_to(root).as_posix()

        if relative_path not in aliases or acquisition["staged_relative_path"] != relative_path:
            raise ValueError("История загрузки относится к другому PDF alias")

        body = path.read_bytes()

        if body != pdf_body or not body[:1024].lstrip().startswith(b"%PDF-"):
            raise ValueError("PDF alias имеет другие байты или неверную PDF-сигнатуру")

        acquisitions.append(acquisition)

    _unique(acquisitions, "staged_relative_path")

    if {row["staged_relative_path"] for row in acquisitions} != set(aliases):
        raise ValueError("История ручных загрузок не покрывает все PDF aliases")

    known_paths = {
        row["staged_relative_path"] for row in sources["inventory"][2]
        if row.get("sha256") == sha256_bytes(pdf_body)
    }

    if known_paths != set(aliases):
        raise ValueError("Пакет не сохраняет все известные inventory экземпляры PDF")

    return acquisitions
