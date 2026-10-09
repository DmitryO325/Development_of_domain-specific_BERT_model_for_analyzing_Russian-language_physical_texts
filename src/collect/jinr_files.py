"""Получение перечня исходных файлов ОИЯИ по связям API без загрузки содержимого."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit
from uuid import UUID

from .jinr_api import DEFAULT_API_URL, _validate_url

JsonGetter = Callable[[str], dict[str, Any]]
CORE_PATH = "/server/api/core"
ACCESS_STATUSES = {"open.access", "metadata.only", "restricted", "embargo", "unknown"}


def _checked_url(href: object, base_url: str, expected_path: str, *, collection: bool = False) -> str:
    """Проверить адрес конкретной связи, исключив содержимое и посторонние параметры."""

    if not isinstance(href, str) or not href:
        raise ValueError("Ссылка API должна содержать непустой адрес")

    url = urljoin(base_url, href)
    _validate_url(url)
    parsed = urlsplit(url)

    if parsed.path != expected_path:
        raise ValueError("Ссылка API ведёт не к ожидаемой связи публикации или файла")

    parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    allowed_parameters = {"page", "size", "sort", "projection"} if collection else set()

    if any(name not in allowed_parameters for name, _ in parameters):
        raise ValueError("В ссылке API обнаружены неподдерживаемые параметры")

    for name in ("page", "size"):
        values = [value for key, value in parameters if key == name]

        if len(values) > 1 or any(not value.isascii() or not value.isdecimal() for value in values):
            raise ValueError("Некорректный параметр страницы в ссылке API")

        if name == "size" and values and int(values[0]) < 1:
            raise ValueError("Размер страницы API должен быть положительным")

    return url


def _link(
    record: dict[str, Any],
    relation: str,
    base_url: str,
    expected_path: str,
    *,
    required: bool = False,
    collection: bool = False,
) -> str | None:
    """Прочитать и проверить объявленную сервером связь без конструирования запроса."""

    links = record.get("_links", {})

    if not isinstance(links, dict):
        raise ValueError("В ответе API ожидался объект _links")

    if relation not in links:
        if required:
            raise ValueError(f"API не предоставил обязательную связь {relation}")

        return

    link = links[relation]

    if not isinstance(link, dict):
        raise ValueError(f"Некорректная связь API: {relation}")

    return _checked_url(link.get("href"), base_url, expected_path, collection=collection)


def _resource_uuid(record: dict[str, Any], resource_type: str) -> str:
    """Проверить тип, UUID и собственную ссылку ресурса DSpace."""

    if not isinstance(record, dict) or record.get("type") != resource_type:
        raise ValueError(f"В ответе API ожидался ресурс типа {resource_type}")

    identifier = record.get("uuid")

    if not isinstance(identifier, str):
        raise ValueError("У ресурса API отсутствует строковый UUID")

    try:
        resource_uuid = str(UUID(identifier))

        if "id" in record and str(UUID(record["id"])) != resource_uuid:
            raise ValueError("Идентификаторы id и uuid ресурса API различаются")

    except (ValueError, TypeError, AttributeError) as exception:
        raise ValueError("У ресурса API некорректный UUID") from exception

    _link(record, "self", DEFAULT_API_URL + "/", f"{CORE_PATH}/{resource_type}s/{resource_uuid}")
    return resource_uuid


def _page_integer(value: object) -> int:
    """Прочитать целочисленное поле пагинации, не принимая логические значения."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Параметры пагинации API должны быть целыми числами")

    return value


def _collection(
    url: str, relation: str, resource_type: str, get_json: JsonGetter
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Собрать видимые записи всех страниц и отдельно отметить расхождения счётчиков."""

    expected_path = urlsplit(url).path
    records: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    visited_urls: set[str] = set()
    seen_uuids: set[str] = set()
    expected_totals: tuple[int, int, int] | None = None
    page_index = 0

    while True:
        if url in visited_urls:
            raise ValueError("API повторил адрес страницы перечня файлов")

        visited_urls.add(url)
        response = get_json(url)
        page = response.get("page")

        if not isinstance(page, dict):
            raise ValueError("API не предоставил сведения о пагинации перечня")

        number, size, total_pages, total_elements = (
            _page_integer(page.get(key)) for key in ("number", "size", "totalPages", "totalElements")
        )

        if number != page_index or size < 1 or total_pages < 0 or total_elements < 0:
            raise ValueError("Некорректная нумерация или размер перечня API")

        if total_elements == 0:
            consistent_pages = total_pages in {0, 1}

        else:
            consistent_pages = total_pages == (total_elements + size - 1) // size

        totals = (size, total_pages, total_elements)

        if not consistent_pages or (expected_totals is not None and totals != expected_totals):
            raise ValueError("Объём перечня API изменился или противоречит размеру страницы")

        expected_totals = totals
        _link(response, "self", url, expected_path, collection=True)
        embedded = response.get("_embedded", {})

        if not isinstance(embedded, dict):
            raise ValueError("В странице API ожидался объект _embedded")

        entries = embedded.get(relation, [])
        expected_count = min(size, max(total_elements - page_index * size, 0))

        if expected_count and relation not in embedded:
            raise ValueError(f"API не предоставил обязательный список {relation}")

        if not isinstance(entries, list) or len(entries) > expected_count:
            raise ValueError("Число ресурсов на странице API не соответствует пагинации")

        for entry in entries:
            resource_uuid = _resource_uuid(entry, resource_type)

            if resource_uuid in seen_uuids:
                raise ValueError("API повторил UUID внутри одного перечня")

            seen_uuids.add(resource_uuid)
            records.append(entry)

        next_url = _link(response, "next", url, expected_path, collection=True)
        has_more = page_index + 1 < total_pages

        # Расхождение счётчика не отменяет проверки структуры и безопасности ссылок.
        if has_more:
            if next_url is None:
                raise ValueError("API не предоставил следующую страницу перечня")

            next_page = dict(parse_qsl(urlsplit(next_url).query)).get("page", "0")

            if int(next_page) != page_index + 1:
                raise ValueError("Ссылка API пропускает или повторяет страницу перечня")

        elif next_url is not None:
            raise ValueError("Последняя страница API содержит лишнюю ссылку next")

        if len(entries) < expected_count:
            # DSpace может фильтровать видимые записи после подсчёта общего количества.
            diagnostics.append({
                "kind": "count_mismatch",
                "message": (
                    f"Расхождение счётчика {relation}: страница {number}, "
                    f"по счётчику {expected_count}, получено {len(entries)}; "
                    f"всего заявлено {total_elements}. Причина расхождения не установлена."
                ),
                "url": url,
                "relation": relation,
                "page_number": number,
                "expected_count": expected_count,
                "actual_count": len(entries),
                "total_elements": total_elements,
            })

        if next_url is None:
            return records, diagnostics

        url = next_url
        page_index += 1


def _optional_resource(
    bitstream: dict[str, Any], bitstream_uuid: str, relation: str, get_json: JsonGetter
) -> dict[str, Any] | None:
    """Взять вложенное описание либо получить только явно объявленную связь файла."""

    base_url = f"{DEFAULT_API_URL}/core/bitstreams/{bitstream_uuid}"
    relation_url = _link(bitstream, relation, base_url, f"{CORE_PATH}/bitstreams/{bitstream_uuid}/{relation}")
    embedded = bitstream.get("_embedded", {})

    if not isinstance(embedded, dict):
        raise ValueError("У файла API ожидался объект _embedded")

    if relation in embedded:
        resource = embedded[relation]

    elif relation_url is not None:
        resource = get_json(relation_url)

    else:
        return

    expected_type = "bitstreamformat" if relation == "format" else "accessStatus"

    if not isinstance(resource, dict) or resource.get("type") != expected_type:
        raise ValueError(f"API вернул некорректное описание {relation}")

    if relation == "format":
        format_id = resource.get("id")

        if format_id is not None and (type(format_id) is not int or format_id < 0):
            raise ValueError("Некорректный идентификатор формата файла API")

        self_path = f"{CORE_PATH}/bitstreamformats/{format_id}"

    else:
        self_path = f"{CORE_PATH}/bitstreams/{bitstream_uuid}/accessStatus"

    _link(resource, "self", relation_url or base_url, self_path)
    return resource


def _file_record(bitstream: dict[str, Any], bundle_uuid: str, get_json: JsonGetter) -> dict[str, Any]:
    """Подготовить описание исходного файла, не проверяя его содержимое загрузкой."""

    bitstream_uuid = _resource_uuid(bitstream, "bitstream")
    name = bitstream.get("name")
    size_bytes = bitstream.get("sizeBytes")

    if name is not None and not isinstance(name, str):
        raise ValueError("Название файла API должно быть строкой или null")

    if size_bytes is not None and (type(size_bytes) is not int or size_bytes < 0):
        raise ValueError("Размер файла API должен быть неотрицательным целым числом")

    checksum = bitstream.get("checkSum")
    server_checksum = None

    if checksum is not None:
        if not isinstance(checksum, dict) or any(
            not isinstance(checksum.get(key), str) or not checksum[key].strip()
            for key in ("checkSumAlgorithm", "value")
        ):
            raise ValueError("Некорректная серверная контрольная сумма файла API")

        server_checksum = {"algorithm": checksum["checkSumAlgorithm"], "value": checksum["value"]}

    content_url = _link(
        bitstream, "content", f"{DEFAULT_API_URL}/core/bitstreams/{bitstream_uuid}",
        f"{CORE_PATH}/bitstreams/{bitstream_uuid}/content",
    )
    file_format = _optional_resource(bitstream, bitstream_uuid, "format", get_json)
    mime_type = None

    if file_format is not None:
        mime_type = file_format.get("mimetype", file_format.get("mimeType"))

        if mime_type is not None and not isinstance(mime_type, str):
            raise ValueError("MIME-тип файла API должен быть строкой или null")

        if "mimeType" in file_format and file_format["mimeType"] != mime_type:
            raise ValueError("API вернул противоречивые значения MIME-типа")

    access = _optional_resource(bitstream, bitstream_uuid, "accessStatus", get_json)
    access_status = "unknown" if access is None else access.get("status")
    embargo_date = None if access is None else access.get("embargoDate")

    if not isinstance(access_status, str) or access_status not in ACCESS_STATUSES:
        raise ValueError("API вернул неизвестное значение статуса доступа к файлу")

    if embargo_date is not None and not isinstance(embargo_date, str):
        raise ValueError("Дата окончания эмбарго API должна быть строкой или null")

    return {
        "bundle_uuid": bundle_uuid,
        "bitstream_uuid": bitstream_uuid,
        "name": name,
        "size_bytes": size_bytes,
        "server_checksum": server_checksum,
        "content_url": content_url,
        "mime_type": mime_type,
        "access_status": access_status,
        "embargo_date": embargo_date,
    }


def collect_item_files(item: dict[str, Any], get_json: JsonGetter) -> dict[str, Any]:
    """
    Собрать видимые ORIGINAL-файлы и отметить расхождения с общими счётчиками.

    listing_complete подтверждает совпадение полученных записей со счётчиками.
    Значение False само по себе не означает запрет доступа или повреждение ответа.
    """

    item_uuid = _resource_uuid(item, "item")
    bundles_url = _link(
        item, "bundles", f"{DEFAULT_API_URL}/core/items/{item_uuid}",
        f"{CORE_PATH}/items/{item_uuid}/bundles", required=True, collection=True,
    )

    if bundles_url is None:
        raise ValueError("API не предоставил перечень наборов файлов публикации")

    source_urls: list[str] = []

    def read_json(url: str) -> dict[str, Any]:
        """Запомнить порядок использованных ответов для последующей проверки источников."""

        response = get_json(url)

        if not isinstance(response, dict):
            raise ValueError("В ответе API ожидался JSON-объект")

        source_urls.append(url)
        return response

    bundles, diagnostics = _collection(bundles_url, "bundles", "bundle", read_json)
    files: list[dict[str, Any]] = []
    file_uuids: set[str] = set()
    original_bundle_count = 0

    for bundle in bundles:
        if not isinstance(bundle.get("name"), str):
            raise ValueError("API не указал название набора файлов")

        if bundle["name"] != "ORIGINAL":
            continue

        original_bundle_count += 1
        bundle_uuid = _resource_uuid(bundle, "bundle")
        bitstreams_url = _link(
            bundle, "bitstreams", f"{DEFAULT_API_URL}/core/bundles/{bundle_uuid}",
            f"{CORE_PATH}/bundles/{bundle_uuid}/bitstreams", required=True, collection=True,
        )

        if bitstreams_url is None:
            raise ValueError("API не предоставил перечень исходных файлов")

        bitstreams, bitstream_diagnostics = _collection(bitstreams_url, "bitstreams", "bitstream", read_json)
        diagnostics.extend(bitstream_diagnostics)

        for bitstream in bitstreams:
            bitstream_uuid = _resource_uuid(bitstream, "bitstream")

            if bitstream_uuid in file_uuids:
                raise ValueError("API повторил исходный файл в разных наборах публикации")

            file_uuids.add(bitstream_uuid)
            files.append(_file_record(bitstream, bundle_uuid, read_json))

    metadata = item.get("metadata", {})
    titles = metadata.get("dc.title", []) if isinstance(metadata, dict) else []
    item_title = None

    if isinstance(titles, list):
        item_title = next(
            (
                entry["value"] for entry in titles
                if isinstance(entry, dict) and isinstance(entry.get("value"), str)
            ),
            None,
        )

    return {
        "item_uuid": item_uuid,
        "item_title": item_title,
        "original_bundle_count": original_bundle_count,
        "files": files,
        "source_urls": source_urls,
        "scope": "ORIGINAL",
        "listing_complete": not diagnostics,
        "diagnostics": diagnostics,
    }
