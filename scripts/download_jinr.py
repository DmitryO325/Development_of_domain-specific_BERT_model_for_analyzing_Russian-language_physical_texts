#!/usr/bin/env python3
"""
Получение материалов ОИЯИ через DSpace API и явные ссылки на препринты.

Примеры:
  python scripts/download_jinr.py probe
  python scripts/download_jinr.py metadata --max-pages 1
  python scripts/download_jinr.py metadata --has-files --max-pages 0 --page-size 100
  python scripts/download_jinr.py files --unmatched-only --max-items 1
  python scripts/download_jinr.py preprints --unmatched-only --max-items 1
  python scripts/download_jinr.py preprints --unmatched-only --max-items 1 --download
  python scripts/download_jinr.py pdf UUID_ФАЙЛА
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Прямой запуск из scripts/ должен находить модули проекта.
sys.path.insert(0, str(PROJECT_ROOT))

from src.collect.base import HttpResponseSnapshot  # noqa: E402
from src.collect.jinr_api import (  # noqa: E402
    DEFAULT_API_URL,
    JinrApiClient,
    JinrApiError,
)
from src.collect.jinr_files import collect_item_files  # noqa: E402
from src.collect.jinr_preprints import (  # noqa: E402
    extract_preprint_urls,
    validate_preprint_url,
)

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "jinr_api"
DEFAULT_STATE_DIR = PROJECT_ROOT / "manifests" / "imports" / "jinr_api"
DEFAULT_MATCHES_PATH = PROJECT_ROOT / "manifests" / "imports" / "jinr_pdf_metadata_matches_20260921.jsonl"
HAS_FILES_FILTER = "has_content_in_original_bundle"


def _write_bytes(path: Path, content: bytes, *, immutable: bool = False) -> None:
    """Записать файл атомарно, не заменяя уже сохранённые исходные байты."""

    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)

        try:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())

        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    try:
        if immutable:
            try:
                # Жёсткая ссылка публикует готовый файл без перезаписи существующего.
                os.link(temporary_path, path)

            except FileExistsError as exception:
                if path.read_bytes() != content:
                    raise ValueError(f"Нельзя заменить исходные байты: {path}") from exception

        else:
            os.replace(temporary_path, path)

    finally:
        temporary_path.unlink(missing_ok=True)


def _json_bytes(record: dict[str, Any]) -> bytes:
    """Сериализовать служебную запись без изменения исходного ответа API."""

    return (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _validate_preprint_pdf(body: bytes, metadata: dict[str, Any], url: str) -> None:
    """Проверить полный ответ, адрес, MIME-тип и начальную сигнатуру PDF."""

    if (
        metadata.get("requested_url") != url or metadata.get("final_url") != url or
        metadata.get("status_code") != 200
    ):
        raise ValueError("PDF должен быть получен ответом HTTP 200 с исходного адреса без перенаправлений")

    headers = metadata.get("headers")

    if not isinstance(headers, list) or any(
        not isinstance(header, list) or len(header) != 2 or
        any(not isinstance(value, str) for value in header)
        for header in headers
    ):
        raise ValueError("Некорректные заголовки HTTP-свидетельства PDF")

    content_types = [value for name, value in headers if name.casefold() == "content-type"]

    if (
        len(content_types) != 1 or
        content_types[0].split(";", 1)[0].strip().casefold() != "application/pdf"
    ):
        raise ValueError("Сервер не подтвердил MIME-тип application/pdf; файл не принят")

    if not body[:1024].lstrip(b" \t\r\n").startswith(b"%PDF-"):
        raise ValueError("Ответ не начинается с сигнатуры PDF; файл не принят")


class JinrArchive:
    """Хранить точные ответы API, происхождение и проверяемый локальный кеш."""

    def __init__(
        self,
        output_dir: Path,
        state_dir: Path,
        client: JinrApiClient,
    ) -> None:
        """Задать отдельные каталоги исходников и изменяемого состояния."""

        self.output_dir = output_dir.resolve()
        self.state_dir = state_dir.resolve()
        self.client = client

    def _cache_path(self, url: str) -> Path:
        """Получить локальный ключ запроса без использования URL как пути."""

        url_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()

        return self.state_dir / "cache" / f"{url_digest}.json"

    def _cached_path(self, url: str, kind: str) -> Path | None:
        """Проверить происхождение, путь, размер и SHA-256 сохранённого ответа."""

        cache_path = self._cache_path(url)

        if not cache_path.exists():
            return

        record = json.loads(cache_path.read_text(encoding="utf-8"))

        if not isinstance(record, dict):
            raise ValueError(f"Повреждена запись кеша: {cache_path}")

        if record.get("requested_url") != url or record.get("kind") != kind:
            raise ValueError(f"Кеш не соответствует запросу: {cache_path}")

        relative_path = record.get("relative_path")

        if not isinstance(relative_path, str):
            raise ValueError(f"В кеше отсутствует путь к ответу: {cache_path}")

        body_path = (self.output_dir / relative_path).resolve()

        if not body_path.is_relative_to(self.output_dir) or not body_path.is_file():
            raise ValueError(f"В кеше недопустимый или отсутствующий файл: {cache_path}")

        body = body_path.read_bytes()

        if (
            record.get("sha256") != hashlib.sha256(body).hexdigest() or
            record.get("bytes") != len(body)
        ):
            raise ValueError(f"Нарушена целостность сохранённого ответа: {body_path}")

        metadata_relative_path = record.get("response_metadata_path")

        if not isinstance(metadata_relative_path, str):
            raise ValueError(f"В кеше отсутствует путь к HTTP-свидетельству: {cache_path}")

        metadata_path = (self.output_dir / metadata_relative_path).resolve()

        if not metadata_path.is_relative_to(self.output_dir) or not metadata_path.is_file():
            raise ValueError(f"Недопустимое или отсутствующее HTTP-свидетельство: {cache_path}")

        metadata_bytes = metadata_path.read_bytes()

        if record.get("response_metadata_sha256") != hashlib.sha256(metadata_bytes).hexdigest():
            raise ValueError(f"Нарушена целостность HTTP-свидетельства: {metadata_path}")

        metadata = json.loads(metadata_bytes)

        if (
            not isinstance(metadata, dict) or
            metadata.get("requested_url") != url or metadata.get("status_code") != 200
        ):
            raise ValueError(f"HTTP-свидетельство не соответствует запросу: {metadata_path}")

        return body_path

    def _save(self, snapshot: HttpResponseSnapshot, kind: str) -> Path:
        """Сохранить исходные байты и свидетельство HTTP-ответа до записи кеша."""

        body_digest = hashlib.sha256(snapshot.body).hexdigest()
        relative_path = Path("responses") / body_digest[:2] / f"{body_digest}.{kind}"
        body_path = self.output_dir / relative_path
        metadata_bytes = snapshot.canonical_metadata()
        metadata_digest = hashlib.sha256(metadata_bytes).hexdigest()
        metadata_path = body_path.with_name(f"{body_digest}.{metadata_digest}.http.json")

        _write_bytes(body_path, snapshot.body, immutable=True)
        _write_bytes(metadata_path, metadata_bytes, immutable=True)

        record = {
            "requested_url": snapshot.requested_url,
            "kind": kind,
            "relative_path": relative_path.as_posix(),
            "sha256": body_digest,
            "bytes": len(snapshot.body),
            "response_metadata_path": metadata_path.relative_to(self.output_dir).as_posix(),
            "response_metadata_sha256": metadata_digest,
        }

        _write_bytes(self._cache_path(snapshot.requested_url), _json_bytes(record))

        return body_path

    def get_json(self, url: str, *, refresh: bool = False) -> dict[str, Any]:
        """Прочитать кеш или явно обновить JSON одним запросом без отката при ошибке."""

        cached_path = self._cached_path(url, "json")
        snapshot: HttpResponseSnapshot | None = None

        if cached_path is not None and not refresh:
            print(f"  JSON: кеш → {url}", flush=True)
            body = cached_path.read_bytes()

        else:
            refresh_note = " (обновление кеша)" if refresh else ""
            print(f"  JSON: запрос к API{refresh_note} → {url}", flush=True)
            snapshot = self.client.get_response(url)

            if snapshot.status_code != 200:
                raise ValueError(f"Ожидался полный HTTP 200, получен {snapshot.status_code}")

            body = snapshot.body

        try:
            record = json.loads(body)

        except (json.JSONDecodeError, UnicodeDecodeError) as exception:
            raise ValueError(f"API вернул некорректный JSON: {url}") from exception

        if not isinstance(record, dict):
            raise ValueError(f"API должен вернуть JSON-объект: {url}")

        if snapshot is not None:
            self._save(snapshot, "json")

        return record

    def download_pdf(self, bitstream_uuid: str) -> Path:
        """Сохранить один известный файл PDF по UUID или вернуть проверенную копию."""

        try:
            file_uuid = str(UUID(bitstream_uuid))

        except ValueError as exception:
            raise ValueError("Нужен корректный UUID файла bitstream") from exception

        url = f"{DEFAULT_API_URL}/core/bitstreams/{file_uuid}/content"
        cached_path = self._cached_path(url, "pdf")

        if cached_path is not None:
            return cached_path

        snapshot = self.client.get_response(url)

        if snapshot.status_code != 200 or b"%PDF-" not in snapshot.body[:1024]:
            raise ValueError("Вместо полного PDF получен другой ответ; файл не принят")

        return self._save(snapshot, "pdf")

    def download_preprint(self, url: str) -> Path:
        """Получить PDF по явной ссылке на препринт или проверить сохранённую копию."""

        validate_preprint_url(url)
        cached_path = self._cached_path(url, "pdf")

        if cached_path is not None:
            record = json.loads(self._cache_path(url).read_text(encoding="utf-8"))
            metadata_path = self.output_dir / record["response_metadata_path"]
            metadata = json.loads(metadata_path.read_bytes())
            _validate_preprint_pdf(cached_path.read_bytes(), metadata, url)
            print(f"  PDF препринта: кеш → {url}", flush=True)

            return cached_path

        print(f"  PDF препринта: запрос к сайту ОИЯИ → {url}", flush=True)
        snapshot = self.client.get_preprint_response(url)
        metadata = json.loads(snapshot.canonical_metadata())
        _validate_preprint_pdf(snapshot.body, metadata, url)

        return self._save(snapshot, "pdf")

    def pdf_evidence(self, url: str) -> dict[str, Any]:
        """Получить проверенные пути, размеры и контрольные суммы сохранённого PDF."""

        body_path = self._cached_path(url, "pdf")

        if body_path is None:
            raise ValueError(f"Нет сохранённого PDF-свидетельства: {url}")

        record = json.loads(self._cache_path(url).read_text(encoding="utf-8"))
        metadata_path = self.output_dir / record["response_metadata_path"]
        metadata = json.loads(metadata_path.read_bytes())
        _validate_preprint_pdf(body_path.read_bytes(), metadata, url)

        return {
            "requested_url": url,
            "body_path": str(self.output_dir / record["relative_path"]),
            "body_sha256": record["sha256"],
            "size_bytes": record["bytes"],
            "response_metadata_path": str(self.output_dir / record["response_metadata_path"]),
            "response_metadata_sha256": record["response_metadata_sha256"],
        }

    def cached_json(self, url: str) -> dict[str, Any]:
        """Прочитать проверенный JSON только локально, не запрашивая отсутствующее."""

        cached_path = self._cached_path(url, "json")

        if cached_path is None:
            raise ValueError(
                "Не хватает кеша метаданных. Сначала выполните metadata "
                "--has-files --max-pages 0 --page-size 100"
            )

        record = json.loads(cached_path.read_bytes())

        if not isinstance(record, dict):
            raise ValueError(f"В кеше ожидался JSON-объект: {url}")

        return record

    def json_evidence(self, url: str) -> dict[str, Any]:
        """Получить проверенную ссылку на исходный ответ и HTTP-свидетельство."""

        if self._cached_path(url, "json") is None:
            raise ValueError(f"Нет сохранённого JSON-свидетельства: {url}")

        record = json.loads(self._cache_path(url).read_text(encoding="utf-8"))

        return {
            "requested_url": url,
            "body_path": str(self.output_dir / record["relative_path"]),
            "body_sha256": record["sha256"],
            "response_metadata_path": str(self.output_dir / record["response_metadata_path"]),
            "response_metadata_sha256": record["response_metadata_sha256"],
        }


def _search_results(
    record: dict[str, Any],
    language_filter: str,
    *,
    has_files: bool = False,
) -> dict[str, Any]:
    """Проверить применённые фильтры и структуру страницы поиска DSpace."""

    applied_filters = record.get("appliedFilters", [])
    expected_filters = {language_filter: "ru"}

    if has_files:
        expected_filters[HAS_FILES_FILTER] = "true"

    for filter_name, filter_value in expected_filters.items():
        expected_filter = {"filter": filter_name, "operator": "equals", "value": filter_value}

        if not isinstance(applied_filters, list) or not any(
            isinstance(applied, dict) and
            all(applied.get(key) == value for key, value in expected_filter.items())
            for applied in applied_filters
        ):
            if filter_name == language_filter:
                raise ValueError("API не подтвердил фильтр русскоязычных публикаций")

            raise ValueError("API не подтвердил фильтр публикаций с файлами")

    embedded = record.get("_embedded")
    results = None

    if isinstance(embedded, dict):
        # ОИЯИ на DSpace 9.1 возвращает searchResult; в контракте есть и plural-вариант.
        result_key = "searchResult" if "searchResult" in embedded else "searchResults"
        results = embedded.get(result_key)

    if not isinstance(results, dict):
        raise ValueError("В ответе нет объекта _embedded.searchResult или searchResults")

    return results


def _validate_filtered_page_url(url: str, language_filter: str) -> None:
    """Проверить сохранение отбора публикаций с файлами до запроса страницы."""

    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    expected_parameters = {
        "dsoType": ["item"],
        f"f.{language_filter}": ["ru,equals"],
        f"f.{HAS_FILES_FILTER}": ["true,equals"],
    }

    for parameter, expected_value in expected_parameters.items():
        if query.get(parameter) != expected_value:
            raise ValueError(f"Ссылка страницы не сохраняет условие отбора {parameter!r}")


def collect_metadata(
    archive: JinrArchive,
    *,
    max_pages: int = 1,
    page_size: int = 20,
    language_filter: str = "Lang",
    has_files: bool = False,
) -> int:
    """Сохранить русскоязычные метаданные, при необходимости только с файлами."""

    if max_pages < 0 or not 1 <= page_size <= 100:
        raise ValueError("max_pages должен быть >= 0, page_size — от 1 до 100")

    if not language_filter or not language_filter.isidentifier():
        raise ValueError("Нужно непустое имя фильтра языка из конфигурации API")

    if has_files and language_filter == HAS_FILES_FILTER:
        raise ValueError("Фильтр языка не должен совпадать с фильтром наличия файлов")

    configuration = archive.get_json(f"{DEFAULT_API_URL}/discover/search")
    filters = configuration.get("filters", [])
    required_filters = [language_filter]

    if has_files:
        required_filters.append(HAS_FILES_FILTER)

    for filter_name in required_filters:
        if not isinstance(filters, list) or not any(
            isinstance(candidate, dict) and
            candidate.get("filter") == filter_name and
            isinstance(candidate.get("operators"), list) and
            any(
                isinstance(operator, dict) and operator.get("operator") == "equals"
                for operator in candidate["operators"]
            )
            for candidate in filters
        ):
            raise ValueError(f"API не объявляет фильтр {filter_name!r} с оператором equals")

    query_parameters: dict[str, str | int] = {
        "dsoType": "item",
        f"f.{language_filter}": "ru,equals",
    }

    if has_files:
        query_parameters[f"f.{HAS_FILES_FILTER}"] = "true,equals"

    # Без нового флага порядок параметров остаётся прежним для совместимости кеша.
    query_parameters.update({"page": 0, "size": page_size})
    query = urlencode(query_parameters)
    next_url = f"{DEFAULT_API_URL}/discover/search/objects?{query}"
    visited: set[str] = set()
    item_count = 0

    while next_url:
        if has_files:
            # Потерю фильтра замечаем до запроса, а не после получения общей выдачи.
            _validate_filtered_page_url(next_url, language_filter)

        if next_url in visited:
            raise ValueError("API вернул цикл ссылок на страницы поиска")

        visited.add(next_url)
        results = _search_results(
            archive.get_json(next_url), language_filter, has_files=has_files,
        )
        embedded = results.get("_embedded", {})
        objects = embedded.get("objects", []) if isinstance(embedded, dict) else None
        page = results.get("page")

        if not isinstance(objects, list) or not isinstance(page, dict):
            raise ValueError("Неверная структура списка или пагинации поиска")

        page_number = page.get("number")
        total_pages = page.get("totalPages")
        total_elements = page.get("totalElements")

        if (
            type(page_number) is not int or type(total_pages) is not int or
            type(total_elements) is not int or total_elements < 0 or
            page_number != len(visited) - 1 or total_pages < 0 or
            (total_pages > 0 and page_number >= total_pages) or
            (total_elements > 0 and not objects) or
            (total_elements == 0 and objects)
        ):
            raise ValueError("Некорректная последовательность страниц поиска")

        item_count += len(objects)

        print(
            f"Страница {page_number + 1}/{max(total_pages, 1)}: "
            f"обработано записей {item_count} из {total_elements} (включая кеш).",
            flush=True,
        )

        if max_pages and len(visited) >= max_pages:
            break

        links = results.get("_links", {})
        next_link = links.get("next", {}) if isinstance(links, dict) else {}
        next_href = next_link.get("href") if isinstance(next_link, dict) else None

        if not next_href:
            if page_number + 1 < total_pages:
                raise ValueError("Ссылка на следующую страницу отсутствует до конца выдачи")

            break

        if not isinstance(next_href, str):
            raise ValueError("Ссылка на следующую страницу должна быть строкой")

        next_url = urljoin(next_url, next_href)

    return item_count


def _record_uuid(record: dict[str, Any], field: str) -> str:
    """Получить канонический UUID, не принимая пустое или ошибочное значение."""

    value = record.get(field)

    if not isinstance(value, str):
        raise ValueError(f"Ожидался строковый UUID в поле {field!r}")

    try:
        return str(UUID(value))

    except ValueError as exception:
        raise ValueError(f"Некорректный UUID в поле {field!r}: {value!r}") from exception


def _cached_file_items(archive: JinrArchive) -> tuple[list[dict[str, Any]], list[str]]:
    """Прочитать весь кеш выборки с файлами и проверить полноту уникальных карточек."""

    query = urlencode({
        "dsoType": "item",
        "f.Lang": "ru,equals",
        f"f.{HAS_FILES_FILTER}": "true,equals",
        "page": 0,
        "size": 100,
    })
    next_url = f"{DEFAULT_API_URL}/discover/search/objects?{query}"
    source_urls: list[str] = []
    items: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    expected_pagination: tuple[int, int, int] | None = None

    while next_url:
        _validate_filtered_page_url(next_url, "Lang")
        parsed = urlsplit(next_url)

        if (
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}" !=
            f"{DEFAULT_API_URL}/discover/search/objects" or parsed.fragment
        ):
            raise ValueError("Кеш следующей страницы должен относиться к тому же поиску ОИЯИ")

        if next_url in source_urls:
            raise ValueError("В кеше обнаружен цикл ссылок на страницы")

        results = _search_results(archive.cached_json(next_url), "Lang", has_files=True)
        page = results.get("page")
        embedded = results.get("_embedded", {})
        objects = embedded.get("objects", []) if isinstance(embedded, dict) else None

        if not isinstance(page, dict) or not isinstance(objects, list):
            raise ValueError("В кеше неверная структура страницы поиска")

        number, size, total_pages, total_elements = (
            page.get(field) for field in ("number", "size", "totalPages", "totalElements")
        )

        if (
            type(number) is not int or type(size) is not int or
            type(total_pages) is not int or type(total_elements) is not int or
            number != len(source_urls) or size <= 0 or total_elements < 0 or
            total_pages not in ({0, 1} if total_elements == 0 else {(total_elements + size - 1) // size})
        ):
            raise ValueError("В кеше некорректная пагинация поиска")

        pagination = (size, total_pages, total_elements)

        if expected_pagination is not None and pagination != expected_pagination:
            raise ValueError("Количество результатов изменилось между сохранёнными страницами")

        expected_pagination = pagination

        if len(objects) != min(size, max(0, total_elements - len(items))):
            raise ValueError("Кеш содержит неполную страницу поиска")

        for search_object in objects:
            embedded_item = search_object.get("_embedded") if isinstance(search_object, dict) else None
            item = embedded_item.get("indexableObject") if isinstance(embedded_item, dict) else None

            if not isinstance(item, dict) or item.get("type") != "item":
                raise ValueError("Результат поиска не содержит карточку item")

            item_uuid = _record_uuid(item, "uuid")

            if item_uuid in seen_items:
                raise ValueError(f"В сохранённых страницах повторяется UUID {item_uuid}")

            seen_items.add(item_uuid)
            items.append(item)

        source_urls.append(next_url)
        links = results.get("_links", {})
        next_link = links.get("next") if isinstance(links, dict) else None
        next_href = next_link.get("href") if isinstance(next_link, dict) else None

        if number + 1 < total_pages:
            if not isinstance(next_href, str) or not next_href:
                raise ValueError("В кеше нет ссылки на следующую страницу поиска")

            next_url = urljoin(next_url, next_href)

        else:
            if next_link is not None:
                raise ValueError("Последняя страница кеша содержит лишнюю ссылку next")

            if len(items) != total_elements:
                raise ValueError("Число уникальных карточек не соответствует итогу поиска")

            break

    return items, source_urls


def _matched_item_ids(path: Path) -> set[str]:
    """Прочитать локальные сопоставления, включая явно предварительные записи."""

    item_ids: set[str] = set()

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue

        try:
            record = json.loads(line)
            matched_item = record.get("matched_item") if isinstance(record, dict) else None

            if not isinstance(matched_item, dict):
                raise ValueError("Нет объекта matched_item")

            item_ids.add(_record_uuid(matched_item, "item_uuid"))

        except ValueError as exception:
            raise ValueError(f"Ошибка сопоставления {path}, строка {line_number}: {exception}") from exception

    return item_ids


def collect_file_lists(
    archive: JinrArchive,
    *,
    max_items: int = 1,
    unmatched_only: bool = False,
    matches_path: Path = DEFAULT_MATCHES_PATH,
    refresh: bool = False,
) -> int:
    """Сохранить видимые файлы и диагностику полноты; обновлять ответы только явно."""

    if max_items < 0:
        raise ValueError("max_items должен быть >= 0; 0 означает все выбранные карточки")

    items, metadata_urls = _cached_file_items(archive)

    if unmatched_only:
        matched_ids = _matched_item_ids(matches_path)
        items = [item for item in items if _record_uuid(item, "uuid") not in matched_ids]

    selected_count = len(items)

    if max_items:
        items = items[:max_items]

    print(
        f"Выбрано карточек: {selected_count}; в этом запуске: {len(items)}. "
        "Получаем описания файлов, не их содержимое.",
        flush=True,
    )

    metadata_sources = [archive.json_evidence(url) for url in metadata_urls] if items else []
    responses: dict[str, dict[str, Any]] = {}
    completed_count = 0
    incomplete_count = 0

    def read_json(url: str) -> dict[str, Any]:
        """Прочитать связь, не обновляя один URL повторно внутри текущего прохода."""

        if url in responses:
            print(f"  JSON: память текущего запуска → {url}", flush=True)

        else:
            responses[url] = archive.get_json(url, refresh=refresh)

        return responses[url]

    for item_index, item in enumerate(items, start=1):
        item_uuid = _record_uuid(item, "uuid")
        print(f"[{item_index}/{len(items)}] Публикация {item_uuid}", flush=True)

        result = collect_item_files(item, read_json)
        result["status"] = "complete" if result["listing_complete"] else "incomplete"
        result["schema_version"] = "jinr-file-list-v2"
        result["metadata_sources"] = metadata_sources
        result["sources"] = [archive.json_evidence(url) for url in result["source_urls"]]
        result_path = archive.state_dir / "file_lists" / f"{item_uuid}.json"

        # Явный статус заменяет прошлый производный результат; исходные ответы остаются в кеше.
        _write_bytes(result_path, _json_bytes(result))

        if result["status"] == "incomplete":
            incomplete_count += 1
            print(
                f"[{item_index}/{len(items)}] {item_uuid}: полнота не подтверждена; "
                f"получено описаний ORIGINAL-файлов — {len(result['files'])}. "
                f"Перечень и диагностика → {result_path}",
                flush=True,
            )

            for diagnostic in result["diagnostics"]:
                print(f"  {diagnostic['message']}", flush=True)

            continue

        completed_count += 1
        print(
            f"[{item_index}/{len(items)}] {result['item_uuid']}: "
            f"файлов ORIGINAL — {len(result['files'])}; перечень → {result_path}",
            flush=True,
        )

    print(
        f"Итого: полных перечней — {completed_count}, "
        f"с неподтверждённой полнотой — {incomplete_count}; "
        "PDF не скачивались.",
        flush=True,
    )

    return completed_count


def collect_preprints(
    archive: JinrArchive,
    *,
    max_items: int = 1,
    unmatched_only: bool = False,
    matches_path: Path = DEFAULT_MATCHES_PATH,
    download: bool = False,
) -> int:
    """Показать явные PDF-ссылки из кеша; скачивать только при отдельном разрешающем флаге."""

    if max_items < 0:
        raise ValueError("max_items должен быть >= 0; 0 означает все выбранные карточки")

    items, metadata_urls = _cached_file_items(archive)

    if unmatched_only:
        matched_ids = _matched_item_ids(matches_path)
        items = [item for item in items if _record_uuid(item, "uuid") not in matched_ids]

    candidates = [
        (item, urls) for item in items
        if (urls := extract_preprint_urls(item))
    ]
    selected_count = len(candidates)

    if max_items:
        candidates = candidates[:max_items]

    metadata_sources = [archive.json_evidence(url) for url in metadata_urls] if candidates else []
    mode = "скачивание PDF" if download else "просмотр ссылок без сетевых запросов"
    print(
        f"Карточек с прямыми PDF-ссылками на препринты ОИЯИ: {selected_count}; "
        f"в этом запуске: {len(candidates)}. Режим: {mode}.",
        flush=True,
    )
    completed_count = 0

    for item_index, (item, urls) in enumerate(candidates, start=1):
        item_uuid = _record_uuid(item, "uuid")
        titles = item.get("metadata", {}).get("dc.title", [])
        item_title = next(
            (
                entry["value"] for entry in titles
                if isinstance(entry, dict) and isinstance(entry.get("value"), str)
            ),
            None,
        ) if isinstance(titles, list) else None
        print(f"[{item_index}/{len(candidates)}] {item_uuid}: {item_title or 'Без названия'}", flush=True)
        files: list[dict[str, Any]] = []

        for url in urls:
            print(f"  local.publication.uri → {url}", flush=True)

            if download:
                path = archive.download_preprint(url)
                files.append(archive.pdf_evidence(url))
                print(f"  PDF сохранён или проверен → {path}", flush=True)

        if download:
            # Внешний источник не выдаётся за bitstream репозитория или допуск к обучению.
            result = {
                "schema_version": "jinr-preprint-download-v1",
                "status": "downloaded",
                "item_uuid": item_uuid,
                "item_title": item_title,
                "source_field": "local.publication.uri",
                "metadata_sources": metadata_sources,
                "files": files,
            }
            result_path = archive.state_dir / "preprint_downloads" / f"{item_uuid}.json"
            _write_bytes(result_path, _json_bytes(result))

        completed_count += 1

    if download:
        print(f"Карточек с сохранёнными или проверенными PDF: {completed_count}.", flush=True)

    else:
        print("PDF не скачивались. Для загрузки добавьте --download.", flush=True)

    return completed_count


def main(argv: list[str] | None = None) -> int:
    """Запустить работу с API либо явными PDF-ссылками на сайт препринтов ОИЯИ."""

    parser = argparse.ArgumentParser(description="Материалы ОИЯИ: не чаще одного запроса в минуту")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--timeout", type=float, default=30.0)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="Один запрос к корню API")
    metadata_parser = commands.add_parser("metadata", help="Русскоязычные метаданные")
    metadata_parser.add_argument("--max-pages", type=int, default=1, help="0 — вся выдача")
    metadata_parser.add_argument("--page-size", type=int, default=20)
    metadata_parser.add_argument("--language-filter", default="Lang")
    metadata_parser.add_argument(
        "--has-files",
        action="store_true",
        help="Только метаданные публикаций с файлами в ORIGINAL; без скачивания файлов",
    )
    files_parser = commands.add_parser("files", help="Перечни файлов для карточек из кеша")
    files_parser.add_argument("--max-items", type=int, default=1, help="0 — все выбранные карточки")
    files_parser.add_argument(
        "--unmatched-only",
        action="store_true",
        help="Исключить UUID из локальных сопоставлений PDF, включая предварительные",
    )
    files_parser.add_argument("--matches", type=Path, default=DEFAULT_MATCHES_PATH)
    files_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Заново запросить описания файлов, сохранив прежние исходники; метаданные не обновлять",
    )
    preprints_parser = commands.add_parser("preprints", help="Явные PDF-ссылки на препринты из кеша")
    preprints_parser.add_argument("--max-items", type=int, default=1, help="0 — все подходящие карточки")
    preprints_parser.add_argument(
        "--unmatched-only",
        action="store_true",
        help="Исключить UUID из локальных сопоставлений PDF, включая предварительные",
    )
    preprints_parser.add_argument("--matches", type=Path, default=DEFAULT_MATCHES_PATH)
    preprints_parser.add_argument(
        "--download",
        action="store_true",
        help="Скачать PDF; без флага только показать ссылки из проверенного локального кеша",
    )
    pdf_parser = commands.add_parser("pdf", help="Один PDF по известному UUID bitstream")
    pdf_parser.add_argument("bitstream_uuid")
    arguments = parser.parse_args(argv)

    try:
        client = JinrApiClient(arguments.state_dir, timeout=arguments.timeout)
        archive = JinrArchive(arguments.output_dir, arguments.state_dir, client)

        if arguments.command == "probe":
            archive.get_json(DEFAULT_API_URL)
            print("JSON корня API получен или проверен в локальном кеше.")

        elif arguments.command == "metadata":
            count = collect_metadata(
                archive,
                max_pages=arguments.max_pages,
                page_size=arguments.page_size,
                language_filter=arguments.language_filter,
                has_files=arguments.has_files,
            )
            print(f"Сохранены или проверены метаданные результатов поиска: {count}.")

        elif arguments.command == "files":
            count = collect_file_lists(
                archive,
                max_items=arguments.max_items,
                unmatched_only=arguments.unmatched_only,
                matches_path=arguments.matches,
                refresh=arguments.refresh,
            )
            print(f"Полностью получены и сохранены перечни для карточек: {count}.")

        elif arguments.command == "preprints":
            collect_preprints(
                archive,
                max_items=arguments.max_items,
                unmatched_only=arguments.unmatched_only,
                matches_path=arguments.matches,
                download=arguments.download,
            )

        else:
            path = archive.download_pdf(arguments.bitstream_uuid)
            print(f"PDF сохранён или проверен: {path}")

    except (JinrApiError, OSError, ValueError) as exception:
        print(f"ОШИБКА: {exception}", file=sys.stderr)
        return 1

    except KeyboardInterrupt:
        print("Получение остановлено; уже сохранённые ответы остаются на диске.")
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
