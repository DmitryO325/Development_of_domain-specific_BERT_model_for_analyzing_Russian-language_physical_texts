"""Офлайн-регистрация препринтов ОИЯИ по проверенным ответам API и PDF."""

from __future__ import annotations

import copy
import json
import re

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from src.collect.base import HttpResponseSnapshot
from src.collect.jinr_preprints import extract_preprint_urls, validate_preprint_url

from .identity import normalize_doi, resolve_work_identity
from .local_registration import LocalFileRegistration
from .manifests import ManifestPlan, ManifestStore, canonical_json, sha256_bytes
from .profiles import get_source_profile
from .registration import RegistrationOptions, reconcile_document_plan

API_HOST = "pubrepo-api.jinr.ru"
SEARCH_PATH = "/server/api/discover/search/objects"


@dataclass(frozen=True)
class ArchivedResponse:
    """Проверенные байты ответа и пути к его неизменяемым свидетельствам."""

    body_path: str
    body_sha256: str
    response_metadata_path: str
    response_metadata_sha256: str
    snapshot: HttpResponseSnapshot


@dataclass(frozen=True)
class PreprintDownload:
    """Проверенная опись препринта с точной карточкой из исходного API."""

    description_path: str
    item_uuid: str
    item: dict[str, Any]
    metadata_sources: tuple[ArchivedResponse, ...]
    item_source_index: int
    files: tuple[ArchivedResponse, ...]


def read_preprint_download(project_root: Path, path: Path) -> PreprintDownload:
    """Проверить опись, хеши, HTTP и связь PDF с метаданными без сети и записи."""

    root = Path(project_root).resolve()
    description_path = _inside_project(root, path, data_only=False)
    description = _json_object(description_path.read_bytes(), "опись препринта")

    for field, expected in {
        "schema_version": "jinr-preprint-download-v1",
        "status": "downloaded",
        "source_field": "local.publication.uri",
    }.items():
        if description.get(field) != expected:
            raise ValueError(f"Опись препринта: поле {field} должно быть {expected!r}")

    item_uuid = _uuid(description.get("item_uuid"))
    sources = tuple(
        _read_response(root, record, is_pdf=False)
        for record in _record_list(description.get("metadata_sources"), "metadata_sources")
    )
    files = tuple(
        _read_response(root, record, is_pdf=True)
        for record in _record_list(description.get("files"), "files")
    )

    if len({source.snapshot.requested_url for source in sources}) != len(sources):
        raise ValueError("Опись повторяет один источник метаданных")

    matches: list[tuple[int, dict[str, Any]]] = []

    for source_index, source in enumerate(sources):
        for item in _search_items(source.snapshot.body):
            if _uuid(item.get("uuid")) == item_uuid:
                matches.append((source_index, item))

    if len(matches) != 1:
        raise ValueError("В источниках метаданных должна быть ровно одна карточка item_uuid")

    source_index, item = matches[0]
    _validate_item(item, item_uuid)
    titles = _metadata_values(item, "dc.title")

    if not titles or description.get("item_title") != titles[0]:
        raise ValueError("item_title описи не совпадает с названием в исходном API")

    downloaded_urls = [response.snapshot.requested_url for response in files]
    declared_urls = extract_preprint_urls(item)

    if len(set(downloaded_urls)) != len(downloaded_urls):
        raise ValueError("Опись содержит повторную ссылку на PDF")

    if set(downloaded_urls) != set(declared_urls):
        raise ValueError("PDF описи не совпадают с опубликованными local.publication.uri")

    return PreprintDownload(
        description_path=description_path.relative_to(root).as_posix(),
        item_uuid=item_uuid,
        item=item,
        metadata_sources=sources,
        item_source_index=source_index,
        files=files,
    )


def plan_preprint_download(
    project_root: Path,
    path: Path,
    options: RegistrationOptions,
    collected_at: str | None = None,
    *,
    metadata_rights_record_ids: tuple[str, ...] | None = None,
) -> ManifestPlan:
    """Составить план исходных PDF и событий API без копирования и допуска к обучению."""

    for field, expected in {
        "content_role": "full_text",
        "acquisition_method": "crawler",
        "acquisition_scope": "bulk",
        "response_representation": "pdf",
        "request_context_type": "work",
    }.items():
        if getattr(options, field) != expected:
            raise ValueError(f"options.{field} должен быть {expected!r}")

    metadata_rights = (
        options.rights_record_ids
        if metadata_rights_record_ids is None
        else metadata_rights_record_ids
    )

    if not metadata_rights or any(not isinstance(value, str) or not value.strip() for value in metadata_rights):
        raise ValueError("Для API нужны непустые metadata_rights_record_ids")

    metadata_rights = tuple(sorted(set(metadata_rights)))
    options = replace(options, rights_record_ids=tuple(sorted(set(options.rights_record_ids))))
    download = read_preprint_download(project_root, path)
    profile = get_source_profile("jinr_preprints")
    timestamp = _timestamp(collected_at) if collected_at is not None else (
        datetime.now(timezone.utc).isoformat(timespec="microseconds")
    )
    work = _build_work(download, timestamp)
    metadata_events = [
        _build_event(
            response,
            context_type="source",
            context_id=profile.source_id,
            source_group_id=profile.source_group_id,
            method="api",
            rights_record_ids=metadata_rights,
            timestamp=timestamp,
        )
        for response in download.metadata_sources
    ]
    pdf_events = [
        _build_event(
            response,
            context_type="work",
            context_id=work["work_id"],
            source_group_id=None,
            method="crawler",
            rights_record_ids=options.rights_record_ids,
            timestamp=timestamp,
        )
        for response in download.files
    ]
    source = download.metadata_sources[download.item_source_index]
    source_event = metadata_events[download.item_source_index]
    aliases = [_build_alias(
        work["work_id"], "source_native_id", f"{profile.source_id}:{download.item_uuid}",
        source, source_event, timestamp,
    )]

    if work["doi"] is not None:
        aliases.append(_build_alias(work["work_id"], "doi", work["doi"], source, source_event, timestamp))

    work["work_aliases"] = [f"{alias['alias_type']}:{alias['alias_value']}" for alias in aliases]

    return ManifestPlan(
        works=[work],
        artifacts=[
            _build_artifact(response, event, work["work_id"], options, timestamp)
            for response, event in zip(download.files, pdf_events, strict=True)
        ],
        retrieval_events=[*metadata_events, *pdf_events],
        work_aliases=aliases,
    )


def reconcile_preprint_plan(
    store: ManifestStore,
    plan: ManifestPlan,
) -> tuple[ManifestPlan, dict[str, str]]:
    """Согласовать повторную регистрацию, сохранив даты ранее записанных свидетельств."""

    reconciled, expected_snapshot_hashes = reconcile_document_plan(store, plan)
    existing_events = {
        event["retrieval_id"]: event
        for event in store.records("retrieval_events")
    }

    for index, candidate in enumerate(reconciled.retrieval_events):
        existing = existing_events.get(candidate["retrieval_id"])

        if existing is not None and (
            {key: value for key, value in candidate.items() if key != "created_at"}
            == {key: value for key, value in existing.items() if key != "created_at"}
        ):
            reconciled.retrieval_events[index] = copy.deepcopy(existing)

    alias_timestamps = {"alias_record_id", "created_at", "verified_at"}
    existing_aliases = {
        canonical_json({key: value for key, value in alias.items() if key not in alias_timestamps}): alias
        for alias in store.records("work_aliases")
    }
    aliases: dict[str, dict[str, Any]] = {}

    for candidate in reconciled.work_aliases:
        semantic_key = canonical_json({
            key: value for key, value in candidate.items() if key not in alias_timestamps
        })
        alias = copy.deepcopy(existing_aliases.get(semantic_key, candidate))
        aliases[alias["alias_record_id"]] = alias

    reconciled.work_aliases = list(aliases.values())

    return reconciled, expected_snapshot_hashes


def preprint_extraction_cards(download: PreprintDownload) -> list[LocalFileRegistration]:
    """Подготовить карточки только для извлечения текста из уже зарегистрированных PDF."""

    work = _build_work(download, download.files[0].snapshot.retrieved_at)

    return [
        LocalFileRegistration(
            relative_path=response.body_path,
            source_url=response.snapshot.requested_url,
            canonical_url=work["canonical_url"],
            retrieved_at=response.snapshot.retrieved_at,
            title=work["title"],
            authors=work["authors"],
            doi=work["doi"],
            published_at=work["published_at"],
            section=None,
            language=work["language"],
            genre="preprint",
            abstract=work["abstract"],
            keywords=work["keywords"],
            pacs_codes_raw=[],
            udc_codes_raw=[],
            acquisition_agent="scripts/download_jinr.py preprints --download",
            eligibility_status="pending",
            exclusion_reason=None,
        )
        for response in download.files
    ]


def _inside_project(root: Path, value: Any, *, data_only: bool = True) -> Path:
    """Разрешить существующий файл, включая символьные ссылки, только внутри проекта."""

    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("Путь свидетельства должен быть непустой строкой")

    candidate = Path(value)
    resolved = (root / candidate).resolve()
    allowed_root = root / "data" if data_only else root

    if not resolved.is_relative_to(allowed_root) or not resolved.is_file():
        raise ValueError(f"Файл свидетельства отсутствует либо выходит за разрешённый каталог: {value}")

    return resolved


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Отклонить неоднозначный JSON с повторяющимися ключами."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторный ключ JSON: {key}")

        result[key] = value

    return result


def _json_object(body: bytes, label: str) -> dict[str, Any]:
    """Прочитать строгий JSON-объект и назвать повреждённое свидетельство."""

    try:
        record = json.loads(body, object_pairs_hook=_object_without_duplicates)

    except (ValueError, UnicodeDecodeError) as exception:
        raise ValueError(f"Некорректный JSON: {label}: {exception}") from exception

    if not isinstance(record, dict):
        raise ValueError(f"{label}: ожидался JSON-объект")

    return record


def _record_list(value: Any, label: str) -> list[dict[str, Any]]:
    """Проверить непустой список объектов в описи загрузки."""

    if not isinstance(value, list) or not value or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{label}: нужен непустой список JSON-объектов")

    return value


def _timestamp(value: Any) -> str:
    """Проверить метку времени, сохранив точную запись HTTP-свидетельства."""

    if not isinstance(value, str):
        raise ValueError("Метка времени должна быть строкой ISO 8601")

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

    except ValueError as exception:
        raise ValueError("Некорректная метка времени ISO 8601") from exception

    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Метка времени должна содержать часовой пояс")

    return value


def _uuid(value: Any) -> str:
    """Принять только канонический строковый UUID публикации."""

    if not isinstance(value, str):
        raise ValueError("Публикация должна иметь строковый UUID")

    try:
        normalized = str(UUID(value))

    except ValueError as exception:
        raise ValueError("Некорректный UUID публикации") from exception

    if normalized != value:
        raise ValueError("UUID публикации должен иметь каноническую запись")

    return normalized


def _read_response(root: Path, evidence: dict[str, Any], *, is_pdf: bool) -> ArchivedResponse:
    """Проверить тело, исходные HTTP-метаданные и точный опубликованный адрес."""

    body_path = _inside_project(root, evidence.get("body_path"))
    metadata_path = _inside_project(root, evidence.get("response_metadata_path"))
    body = body_path.read_bytes()
    metadata_bytes = metadata_path.read_bytes()
    body_sha256 = sha256_bytes(body)
    metadata_sha256 = sha256_bytes(metadata_bytes)

    if evidence.get("body_sha256") != body_sha256:
        raise ValueError("SHA-256 тела ответа не совпадает с описью")

    if evidence.get("response_metadata_sha256") != metadata_sha256:
        raise ValueError("SHA-256 HTTP-метаданных не совпадает с описью")

    metadata = _json_object(metadata_bytes, "HTTP-метаданные")
    requested_url = evidence.get("requested_url")

    if not isinstance(requested_url, str):
        raise ValueError("В описи отсутствует requested_url")

    if is_pdf:
        validate_preprint_url(requested_url)

    else:
        _validate_api_search_url(requested_url)

    if metadata.get("requested_url") != requested_url or metadata.get("final_url") != requested_url:
        raise ValueError("Адреса requested_url/final_url HTTP-ответа не совпадают с описью")

    if type(metadata.get("status_code")) is not int or metadata["status_code"] != 200:
        raise ValueError("Для регистрации нужен сохранённый HTTP 200")

    headers = _headers(metadata.get("headers"))
    mime_types = [value.split(";", 1)[0].strip().lower() for name, value in headers if name == "content-type"]
    allowed_types = {"application/pdf"} if is_pdf else {"application/json", "application/hal+json"}

    if len(mime_types) != 1 or mime_types[0] not in allowed_types:
        raise ValueError("Content-Type HTTP-ответа не соответствует ожидаемому содержимому")

    lengths = [value for name, value in headers if name == "content-length"]

    if lengths and (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit() or int(lengths[0]) != len(body)):
        raise ValueError("Content-Length не совпадает с размером сохранённого ответа")

    if is_pdf:
        if type(evidence.get("size_bytes")) is not int or evidence["size_bytes"] != len(body):
            raise ValueError("Размер PDF не совпадает с описью")

        if not body[:1024].lstrip().startswith(b"%PDF-"):
            raise ValueError("Ответ не содержит PDF-сигнатуру")

    snapshot = HttpResponseSnapshot(
        requested_url=requested_url,
        final_url=requested_url,
        status_code=200,
        headers=headers,
        retrieved_at=_timestamp(metadata.get("retrieved_at")),
        body=body,
    )

    if snapshot.canonical_metadata() != metadata_bytes:
        raise ValueError("HTTP-метаданные не соответствуют каноническому формату архива")

    return ArchivedResponse(
        body_path=body_path.relative_to(root).as_posix(),
        body_sha256=body_sha256,
        response_metadata_path=metadata_path.relative_to(root).as_posix(),
        response_metadata_sha256=metadata_sha256,
        snapshot=snapshot,
    )


def _headers(value: Any) -> tuple[tuple[str, str], ...]:
    """Проверить сохранённые заголовки без потери повторяющихся значений."""

    if not isinstance(value, list):
        raise ValueError("HTTP headers должны быть списком пар строк")

    result: list[tuple[str, str]] = []

    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(part, str) for part in pair):
            raise ValueError("HTTP-заголовок должен быть парой строк")

        name, header_value = pair

        if re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) is None:
            raise ValueError("Сохранённое имя HTTP-заголовка должно быть нормализовано")

        result.append((name, header_value))

    return tuple(result)


def _validate_api_search_url(value: str) -> None:
    """Разрешить только адрес поиска на исходном API ОИЯИ."""

    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.netloc == API_HOST
            and parsed.path == SEARCH_PATH
            and not parsed.fragment
            and not any(character.isspace() or ord(character) < 32 for character in value)
        )

    except ValueError as exception:
        raise ValueError("Некорректный адрес источника API") from exception

    if not valid:
        raise ValueError("Источник метаданных не является поисковой страницей API ОИЯИ")


def _search_items(body: bytes) -> list[dict[str, Any]]:
    """Извлечь точные карточки публикаций из сохранённого результата поиска."""

    response = _json_object(body, "страница API")

    try:
        objects = response["_embedded"]["searchResult"]["_embedded"]["objects"]

    except (KeyError, TypeError) as exception:
        raise ValueError("У страницы API отсутствует список результатов поиска") from exception

    if not isinstance(objects, list):
        raise ValueError("Результаты поиска API должны быть списком")

    result: list[dict[str, Any]] = []

    for entry in objects:
        try:
            item = entry["_embedded"]["indexableObject"]

        except (KeyError, TypeError) as exception:
            raise ValueError("Результат поиска не содержит indexableObject") from exception

        if not isinstance(item, dict) or item.get("type") != "item":
            raise ValueError("Поиск должен содержать карточки типа item")

        result.append(item)

    return result


def _metadata_values(item: dict[str, Any], field: str) -> list[str]:
    """Прочитать строковые значения конкретного поля без угадывания похожих полей."""

    metadata = item.get("metadata")

    if not isinstance(metadata, dict):
        raise ValueError("Карточка публикации не содержит metadata")

    entries = metadata.get(field, [])

    if not isinstance(entries, list):
        raise ValueError(f"{field} должен быть списком значений")

    values: list[str] = []

    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            raise ValueError(f"{field} должен содержать строковые value")

        value = entry["value"]

        if not value.strip():
            raise ValueError(f"{field} содержит пустое значение")

        if value not in values:
            values.append(value)

    return values


def _validate_item(item: dict[str, Any], item_uuid: str) -> None:
    """Проверить собственную ссылку, тип препринта и русский язык карточки."""

    if item.get("id", item_uuid) != item_uuid:
        raise ValueError("Поля id и uuid карточки различаются")

    expected_url = f"https://{API_HOST}/server/api/core/items/{item_uuid}"

    try:
        self_url = item["_links"]["self"]["href"]

    except (KeyError, TypeError) as exception:
        raise ValueError("Карточка не содержит собственной ссылки API") from exception

    if self_url != expected_url:
        raise ValueError("Собственная ссылка карточки не соответствует item_uuid")

    if {value.casefold() for value in _metadata_values(item, "dc.type")} != {"preprint"}:
        raise ValueError("Регистратор принимает только явно обозначенные препринты")

    if set(_metadata_values(item, "dc.language.iso")) != {"ru"}:
        raise ValueError("Регистратор принимает только явно русскоязычные препринты")

    _publication_date(item)
    _item_doi(item)


def _publication_date(item: dict[str, Any]) -> tuple[str | None, int | None]:
    """Сохранить известную точность даты: год не превращать в первое января."""

    values = _metadata_values(item, "dc.date.issued")

    if not values:
        return None, None

    if len(values) != 1:
        raise ValueError("У препринта несколько разных дат публикации")

    value = values[0]

    if re.fullmatch(r"[1-9][0-9]{3}", value):
        return None, int(value)

    try:
        published_at = date.fromisoformat(value)

    except ValueError as exception:
        raise ValueError("Дата публикации должна содержать YYYY или YYYY-MM-DD") from exception

    if published_at.isoformat() != value:
        raise ValueError("Полная дата публикации должна иметь формат YYYY-MM-DD")

    return value, published_at.year


def _item_doi(item: dict[str, Any]) -> str | None:
    """Прочитать только DOI самого препринта, не DOI связанной журнальной статьи."""

    values = _metadata_values(item, "dc.identifier.doi")
    normalized = {normalize_doi(value) for value in values}

    if None in normalized or len(normalized) > 1:
        raise ValueError("Карточка содержит неверный или неоднозначный DOI")

    return next(iter(normalized), None)


def _canonical_item_url(download: PreprintDownload) -> str:
    """Предпочесть опубликованный handle, сохранив UUID как собственный ID источника."""

    for value in _metadata_values(download.item, "dc.identifier.uri"):
        parsed = urlsplit(value)

        if (
            parsed.scheme in {"http", "https"}
            and parsed.netloc == "pubrepo.jinr.ru"
            and parsed.path.startswith("/handle/")
            and not parsed.query
            and not parsed.fragment
        ):
            return value

    return download.item["_links"]["self"]["href"]


def _build_work(download: PreprintDownload, timestamp: str) -> dict[str, Any]:
    """Собрать карточку препринта без выдуманных журнала, лицензии и точной даты."""

    item = download.item
    profile = get_source_profile("jinr_preprints")
    title = _metadata_values(item, "dc.title")[0].strip()
    authors = _metadata_values(item, "dc.contributor.author")
    published_at, published_year = _publication_date(item)
    doi = _item_doi(item)
    identity = resolve_work_identity(
        source_id=profile.source_id,
        title=title,
        authors=authors,
        year=published_year,
        doi=doi,
        native_id=download.item_uuid,
    )
    abstracts = _metadata_values(item, "dc.description.abstract")

    return {
        "schema_version": "works-v1",
        "created_at": timestamp,
        "work_id": identity.work_id,
        "work_aliases": [],
        "source_group_id": profile.source_group_id,
        "source_id": profile.source_id,
        "platform": profile.platform,
        "journal_id": None,
        "journal_title": None,
        "canonical_url": _canonical_item_url(download),
        "doi": doi,
        "edn": None,
        "identity_confidence": identity.confidence,
        "title": title,
        "authors": authors,
        "abstract": "\n\n".join(abstracts) or None,
        "keywords": _metadata_values(item, "dc.subject"),
        "published_at": published_at,
        "published_year": published_year,
        "language": "ru",
        "genre": "preprint",
        "pacs_codes_raw": [],
        "udc_codes_raw": [],
        "publisher_section": None,
        "duplicate_of_work_id": None,
        "eligibility_status": "pending",
        "exclusion_reason": None,
        "updated_at": timestamp,
    }


def _build_event(
    response: ArchivedResponse,
    *,
    context_type: str,
    context_id: str,
    source_group_id: str | None,
    method: str,
    rights_record_ids: tuple[str, ...],
    timestamp: str,
) -> dict[str, Any]:
    """Отразить настоящий HTTP-запрос, не подменяя API скачиванием вручную."""

    snapshot = response.snapshot
    grouped_headers: dict[str, list[str]] = {}

    for name, value in snapshot.headers:
        grouped_headers.setdefault(name, []).append(value)

    core = {
        "request_context_type": context_type,
        "request_context_id": context_id,
        "source_group_id": source_group_id,
        "requested_url": snapshot.requested_url,
        "final_url": snapshot.final_url,
        "retrieved_at": snapshot.retrieved_at,
        "acquisition_method": method,
        "acquisition_scope": "bulk",
        "rights_record_ids": list(rights_record_ids),
        "http_status": snapshot.status_code,
        "response_headers": {name: ", ".join(values) for name, values in sorted(grouped_headers.items())},
        "response_metadata_sha256": response.response_metadata_sha256,
        "response_sha256": response.body_sha256,
        "response_bytes": len(snapshot.body),
        "outcome": "succeeded",
    }

    return {
        "schema_version": "retrieval-events-v1",
        "retrieval_id": _deterministic_id("retrieval", core),
        "created_at": timestamp,
        **core,
        "response_path": response.body_path,
        "error_code": None,
        "error_detail": None,
    }


def _build_artifact(
    response: ArchivedResponse,
    event: dict[str, Any],
    work_id: str,
    options: RegistrationOptions,
    timestamp: str,
) -> dict[str, Any]:
    """Описать сохранённый PDF без повторной записи и пустого текстового артефакта."""

    return {
        "schema_version": "artifacts-v1",
        "created_at": timestamp,
        "artifact_record_id": f"artifact-record:sha256:{response.body_sha256}",
        "artifact_id": f"sha256:{response.body_sha256}",
        "work_id": work_id,
        "parent_artifact_id": None,
        "retrievals": [{
            "retrieval_id": event["retrieval_id"],
            "retrieved_url": event["requested_url"],
            "retrieved_at": event["retrieved_at"],
            "response_metadata_sha256": event["response_metadata_sha256"],
        }],
        "rights_record_ids": list(options.rights_record_ids),
        "content_role": "full_text",
        "representation": "pdf",
        "mime_type": "application/pdf",
        "path": response.body_path,
        "sha256": response.body_sha256,
        "bytes": len(response.snapshot.body),
        "extraction_method": None,
        "extraction_version": None,
        "ocr_method": None,
        "ocr_version": None,
        "preprocessing_version": None,
        "tokenizer_repo": None,
        "tokenizer_revision": None,
        "characters": None,
        "words": None,
        "subtokens": None,
        "h2_input_sha256": None,
        "label_leakage_audit_version": None,
        "label_leakage_audit_status": "not_checked",
        "acquisition_method": "crawler",
        "acquisition_scope": "bulk",
        "acquisition_status": "retrieved",
        "extraction_status": "not_started",
        "qa_status": "not_evaluated",
        "processing_status": "not_started",
        "error_code": None,
        "error_detail": None,
        "updated_at": timestamp,
    }


def _build_alias(
    work_id: str,
    alias_type: str,
    alias_value: str,
    source: ArchivedResponse,
    event: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    """Связать собственный ID с неизменным API-ответом, где он действительно указан."""

    payload = {
        "work_id": work_id,
        "alias_type": alias_type,
        "alias_value": alias_value,
        "verified_at": timestamp,
        "evidence_sha256": source.body_sha256,
        "source_retrieval_id": event["retrieval_id"],
    }

    return {
        "schema_version": "work-aliases-v1",
        "alias_record_id": _deterministic_id("work-alias", payload),
        "created_at": timestamp,
        **payload,
        "supersedes_alias_record_id": None,
    }


def _deterministic_id(prefix: str, payload: dict[str, Any]) -> str:
    """Построить устойчивый ID из содержимого события или псевдонима."""

    return f"{prefix}:{sha256_bytes(canonical_json(payload).encode('utf-8'))}"
