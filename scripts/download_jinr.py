#!/usr/bin/env python3
"""
Получение материалов ОИЯИ через DSpace API с минутными паузами.

Примеры:
  python scripts/download_jinr.py probe
  python scripts/download_jinr.py metadata --max-pages 1
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
from urllib.parse import urlencode, urljoin
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

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "raw" / "jinr_api"
DEFAULT_STATE_DIR = PROJECT_ROOT / "manifests" / "imports" / "jinr_api"


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

    def get_json(self, url: str) -> dict[str, Any]:
        """Получить JSON-объект из проверенного кеша или одним сетевым запросом."""

        cached_path = self._cached_path(url, "json")
        snapshot: HttpResponseSnapshot | None = None

        if cached_path is not None:
            body = cached_path.read_bytes()

        else:
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


def _search_results(record: dict[str, Any], language_filter: str) -> dict[str, Any]:
    """Проверить применённый язык и структуру страницы поиска DSpace."""

    applied_filters = record.get("appliedFilters", [])
    expected_filter = {"filter": language_filter, "operator": "equals", "value": "ru"}

    if not isinstance(applied_filters, list) or not any(
        isinstance(applied, dict) and
        all(applied.get(key) == value for key, value in expected_filter.items())
        for applied in applied_filters
    ):
        raise ValueError("API не подтвердил фильтр русскоязычных публикаций")

    embedded = record.get("_embedded")
    results = None

    if isinstance(embedded, dict):
        # ОИЯИ на DSpace 9.1 возвращает searchResult; в контракте есть и plural-вариант.
        result_key = "searchResult" if "searchResult" in embedded else "searchResults"
        results = embedded.get(result_key)

    if not isinstance(results, dict):
        raise ValueError("В ответе нет объекта _embedded.searchResult или searchResults")

    return results


def collect_metadata(
    archive: JinrArchive,
    *,
    max_pages: int = 1,
    page_size: int = 20,
    language_filter: str = "Lang",
) -> int:
    """Сохранить страницы русскоязычных метаданных без ограничения по годам."""

    if max_pages < 0 or not 1 <= page_size <= 100:
        raise ValueError("max_pages должен быть >= 0, page_size — от 1 до 100")

    if not language_filter or not language_filter.isidentifier():
        raise ValueError("Нужно непустое имя фильтра языка из конфигурации API")

    configuration = archive.get_json(f"{DEFAULT_API_URL}/discover/search")
    filters = configuration.get("filters", [])

    if not isinstance(filters, list) or not any(
        isinstance(candidate, dict) and
        candidate.get("filter") == language_filter and
        isinstance(candidate.get("operators"), list) and
        any(
            isinstance(operator, dict) and operator.get("operator") == "equals"
            for operator in candidate["operators"]
        )
        for candidate in filters
    ):
        raise ValueError(f"API не объявляет фильтр {language_filter!r} с оператором equals")

    query = urlencode({
        "dsoType": "item",
        f"f.{language_filter}": "ru,equals",
        "page": 0,
        "size": page_size,
    })
    next_url = f"{DEFAULT_API_URL}/discover/search/objects?{query}"
    visited: set[str] = set()
    item_count = 0

    while next_url:
        if next_url in visited:
            raise ValueError("API вернул цикл ссылок на страницы поиска")

        visited.add(next_url)
        results = _search_results(archive.get_json(next_url), language_filter)
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


def main(argv: list[str] | None = None) -> int:
    """Запустить ограниченную проверку, сбор метаданных или получение одного PDF."""

    parser = argparse.ArgumentParser(description="API ОИЯИ: не чаще одного запроса в минуту")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--timeout", type=float, default=30.0)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe", help="Один запрос к корню API")
    metadata_parser = commands.add_parser("metadata", help="Русскоязычные метаданные")
    metadata_parser.add_argument("--max-pages", type=int, default=1, help="0 — вся выдача")
    metadata_parser.add_argument("--page-size", type=int, default=20)
    metadata_parser.add_argument("--language-filter", default="Lang")
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
            )
            print(f"Сохранены или проверены метаданные результатов поиска: {count}.")

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
