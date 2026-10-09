"""Проверки локального архива и ограниченной выгрузки метаданных ОИЯИ."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts.download_jinr import JinrArchive, collect_metadata, main
from src.collect.base import HttpResponseSnapshot
from src.collect.jinr_api import DEFAULT_API_URL, JinrApiError


API_BASE = DEFAULT_API_URL
BITSTREAM_UUID = "27ab1111-2222-4333-8444-555555555555"


def _response(
    url: str,
    body: bytes,
    *,
    content_type: str = "application/json",
) -> HttpResponseSnapshot:
    """Создать воспроизводимый HTTP-снимок без сетевого запроса."""

    return HttpResponseSnapshot(
        requested_url=url,
        final_url=url,
        status_code=200,
        headers=(("content-type", content_type),),
        retrieved_at="2026-09-17T12:00:00.000000+00:00",
        body=body,
    )


def _search_configuration(*, has_files: bool = False) -> dict[str, Any]:
    """Описать поддерживаемые тестовым сервером фильтры DSpace."""

    configuration: dict[str, Any] = {
        "filters": [
            {
                "filter": "Lang",
                "operators": [{"operator": "equals"}],
            },
        ],
    }

    if has_files:
        configuration["filters"].append({
            "filter": "has_content_in_original_bundle",
            "operators": [{"operator": "equals"}],
        })

    return configuration


def _search_url(
    page_index: int,
    page_size: int = 1,
    *,
    has_files: bool = False,
) -> str:
    """Построить тестовую ссылку на страницу русскоязычных записей."""

    parameters: dict[str, str | int] = {
        "dsoType": "item",
        "f.Lang": "ru,equals",
    }

    if has_files:
        parameters["f.has_content_in_original_bundle"] = "true,equals"

    parameters.update({"page": page_index, "size": page_size})
    query = urlencode(parameters)

    return f"{API_BASE}/discover/search/objects?{query}"


def _search_page(
    page_index: int,
    *,
    total_pages: int = 2,
    next_url: str | None = None,
    has_files: bool = False,
) -> dict[str, Any]:
    """Сформировать страницу HAL с одной записью и применённым фильтром."""

    result: dict[str, Any] = {
        "_embedded": {
            "objects": [
                {
                    "_embedded": {
                        "indexableObject": {
                            "uuid": f"11111111-2222-4333-8444-{page_index:012d}",
                            "type": "item",
                            "metadata": {
                                "dc.title": [{"value": f"Статья {page_index + 1}"}],
                                "dc.language.iso": [{"value": "ru"}],
                            },
                        },
                    },
                },
            ],
        },
        "page": {
            "number": page_index,
            "size": 1,
            "totalElements": total_pages,
            "totalPages": total_pages,
        },
        "_links": {},
    }

    if next_url is not None:
        result["_links"]["next"] = {"href": next_url}

    applied_filters = [{"filter": "Lang", "operator": "equals", "value": "ru"}]

    if has_files:
        applied_filters.append({
            "filter": "has_content_in_original_bundle",
            "operator": "equals",
            "value": "true",
        })

    return {
        "appliedFilters": applied_filters,
        "_embedded": {"searchResult": result},
    }


class JinrArchiveTests(unittest.TestCase):
    """Проверки сохранения ответов, повторного использования и целостности."""

    def setUp(self) -> None:
        """Создать отдельное временное хранилище и подставной клиент."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)

        self.root = Path(temporary_directory.name)
        self.output_dir = self.root / "responses"
        self.state_dir = self.root / "state"
        self.client = MagicMock()
        self.archive = JinrArchive(
            output_dir=self.output_dir,
            state_dir=self.state_dir,
            client=self.client,
        )

    def _body_path(self, body: bytes) -> Path:
        """Найти сохранённое тело ответа, не полагаясь на схему каталогов."""

        paths = [
            path
            for path in self.output_dir.rglob("*")
            if path.is_file() and path.read_bytes() == body
        ]

        self.assertEqual(len(paths), 1)

        return paths[0]

    def test_json_cache_preserves_exact_body_and_evidence(self) -> None:
        """Повторное чтение должно использовать точный снимок с его SHA-256."""

        url = f"{API_BASE}/discover/search"
        body = b'{ "filters": [] }\n'
        self.client.get_response.return_value = _response(url, body)

        self.assertEqual(self.archive.get_json(url), {"filters": []})
        self.assertEqual(self.archive.get_json(url), {"filters": []})
        self.client.get_response.assert_called_once_with(url)
        self.assertEqual(self._body_path(body).read_bytes(), body)

        evidence = "\n".join(
            path.read_text(encoding="utf-8")
            for path in self.state_dir.rglob("*.json")
        )
        self.assertIn(hashlib.sha256(body).hexdigest(), evidence)
        self.assertIn(url, evidence)

        metadata_paths = list(self.output_dir.rglob("*.http.json"))
        self.assertEqual(len(metadata_paths), 1)
        metadata_body = metadata_paths[0].read_bytes()
        metadata = json.loads(metadata_body)

        self.assertEqual(metadata["requested_url"], url)
        self.assertEqual(metadata["status_code"], 200)
        self.assertIn(hashlib.sha256(metadata_body).hexdigest(), evidence)

    def test_json_cache_survives_archive_recreation(self) -> None:
        """Новый экземпляр архива должен использовать прежний локальный снимок."""

        url = f"{API_BASE}/discover/search"
        body = b'{"filters":[]}'
        self.client.get_response.return_value = _response(url, body)
        self.archive.get_json(url)

        restored_archive = JinrArchive(
            output_dir=self.output_dir,
            state_dir=self.state_dir,
            client=self.client,
        )

        self.assertEqual(restored_archive.get_json(url), {"filters": []})
        self.client.get_response.assert_called_once_with(url)

    def test_json_cache_reports_local_read_without_request(self) -> None:
        """Обычное повторное чтение должно явно сообщать о кеше и не выходить в сеть."""

        url = f"{API_BASE}/discover/search"
        self.client.get_response.return_value = _response(url, b'{"filters":[]}')
        self.archive.get_json(url)
        self.client.get_response.reset_mock()

        with patch("sys.stdout", new_callable=io.StringIO) as output:
            result = self.archive.get_json(url)

        self.assertEqual(result, {"filters": []})
        self.client.get_response.assert_not_called()
        self.assertIn("JSON: кеш", output.getvalue())
        self.assertNotIn("JSON: запрос к API", output.getvalue())

    def test_json_refresh_updates_pointer_and_preserves_previous_snapshots(self) -> None:
        """Обновление должно сохранить прошлые исходники и переключить только указатель."""

        url = f"{API_BASE}/discover/search"
        old_body = b'{ "filters": [] }\n'
        new_body = b'{ "filters": ["Lang"] }\n'
        old_snapshot = _response(url, old_body)
        new_snapshot = replace(
            _response(url, new_body),
            retrieved_at="2026-09-28T12:00:00.000000+00:00",
        )
        self.client.get_response.return_value = old_snapshot
        self.archive.get_json(url)
        old_evidence = self.archive.json_evidence(url)
        previous_files = {
            path: path.read_bytes()
            for path in self.output_dir.rglob("*")
            if path.is_file()
        }
        self.client.get_response.reset_mock()
        self.client.get_response.return_value = new_snapshot

        with patch("sys.stdout", new_callable=io.StringIO) as output:
            result = self.archive.get_json(url, refresh=True)

        self.assertEqual(result, {"filters": ["Lang"]})
        self.client.get_response.assert_called_once_with(url)
        self.assertIn("JSON: запрос к API", output.getvalue())
        self.assertIn("обновление кеша", output.getvalue())

        for path, content in previous_files.items():
            self.assertEqual(path.read_bytes(), content)

        new_evidence = self.archive.json_evidence(url)
        self.assertNotEqual(old_evidence["body_path"], new_evidence["body_path"])
        self.assertEqual(Path(new_evidence["body_path"]).read_bytes(), new_body)
        self.assertEqual(new_evidence["body_sha256"], hashlib.sha256(new_body).hexdigest())
        self.assertEqual(
            Path(new_evidence["response_metadata_path"]).read_bytes(),
            new_snapshot.canonical_metadata(),
        )
        self.assertEqual(
            new_evidence["response_metadata_sha256"],
            new_snapshot.metadata_sha256(),
        )
        self.assertEqual(self.archive.get_json(url), {"filters": ["Lang"]})
        self.client.get_response.assert_called_once_with(url)

    def test_json_refresh_with_same_body_preserves_both_http_snapshots(self) -> None:
        """Одинаковое тело не должно скрывать время и свидетельство нового запроса."""

        url = f"{API_BASE}/discover/search"
        body = b'{"filters":[]}'
        first_snapshot = _response(url, body)
        second_snapshot = replace(
            first_snapshot,
            retrieved_at="2026-09-28T12:00:00.000000+00:00",
        )
        self.client.get_response.side_effect = [first_snapshot, second_snapshot]
        self.archive.get_json(url)
        first_evidence = self.archive.json_evidence(url)

        self.assertEqual(self.archive.get_json(url, refresh=True), {"filters": []})
        second_evidence = self.archive.json_evidence(url)

        self.assertEqual(self.client.get_response.call_count, 2)
        self.assertEqual(first_evidence["body_path"], second_evidence["body_path"])
        self.assertEqual(first_evidence["body_sha256"], second_evidence["body_sha256"])
        self.assertNotEqual(
            first_evidence["response_metadata_path"],
            second_evidence["response_metadata_path"],
        )
        self.assertEqual(len(list(self.output_dir.rglob("*.http.json"))), 2)

        for evidence, snapshot in (
            (first_evidence, first_snapshot),
            (second_evidence, second_snapshot),
        ):
            metadata_bytes = Path(evidence["response_metadata_path"]).read_bytes()
            self.assertEqual(metadata_bytes, snapshot.canonical_metadata())
            self.assertEqual(
                hashlib.sha256(metadata_bytes).hexdigest(),
                evidence["response_metadata_sha256"],
            )

        self.assertEqual(self.archive.get_json(url), {"filters": []})
        self.assertEqual(self.client.get_response.call_count, 2)

    def test_failed_json_refresh_does_not_fall_back_or_change_cache(self) -> None:
        """Ошибка обновления должна быть видна вызывающему коду, а прошлый кеш сохранён."""

        for failure in ("forbidden", "status_403", "status_206", "invalid", "array", "null"):
            with self.subTest(failure=failure):
                url = f"{API_BASE}/discover/search?case={failure}"
                old_body = b'{"filters":[]}'
                self.client.get_response.reset_mock()
                self.client.get_response.side_effect = None
                self.client.get_response.return_value = _response(url, old_body)
                self.archive.get_json(url)
                previous_files = {
                    path: path.read_bytes()
                    for path in self.root.rglob("*")
                    if path.is_file()
                }
                self.client.get_response.reset_mock()
                expected_exception: type[Exception] = ValueError

                if failure == "forbidden":
                    self.client.get_response.side_effect = JinrApiError("HTTP 403")
                    expected_exception = JinrApiError

                elif failure.startswith("status_"):
                    self.client.get_response.return_value = replace(
                        _response(url, b'{"filters":["new"]}'),
                        status_code=int(failure.removeprefix("status_")),
                    )

                else:
                    invalid_body = {"invalid": b"not-json", "array": b"[]", "null": b"null"}
                    self.client.get_response.return_value = _response(url, invalid_body[failure])

                with (
                    patch("sys.stdout", new_callable=io.StringIO) as output,
                    self.assertRaises(expected_exception),
                ):
                    self.archive.get_json(url, refresh=True)

                self.client.get_response.assert_called_once_with(url)
                self.assertIn("JSON: запрос к API", output.getvalue())
                self.assertIn("обновление кеша", output.getvalue())
                self.assertEqual(
                    {
                        path: path.read_bytes()
                        for path in self.root.rglob("*")
                        if path.is_file()
                    },
                    previous_files,
                )
                self.assertEqual(self.archive.get_json(url), {"filters": []})
                self.client.get_response.assert_called_once_with(url)

    def test_json_cache_rejects_corrupted_body(self) -> None:
        """Изменённый после загрузки JSON нельзя считать достоверным снимком."""

        url = f"{API_BASE}/discover/search"
        body = b'{"filters":[]}'
        self.client.get_response.return_value = _response(url, body)
        self.archive.get_json(url)
        self._body_path(body).write_bytes(b'{"filters":["changed"]}')

        with self.assertRaises(ValueError):
            self.archive.get_json(url)

        self.client.get_response.assert_called_once_with(url)

    def test_invalid_json_is_not_cached_as_success(self) -> None:
        """Повреждённый JSON и массив вместо объекта не должны блокировать повтор."""

        for invalid_body in (b"not-json", b"[]", b"null"):
            with self.subTest(body=invalid_body):
                url = f"{API_BASE}/discover/search?case={invalid_body.hex()}"
                self.client.get_response.side_effect = [
                    _response(url, invalid_body),
                    _response(url, b'{"filters":[]}'),
                ]

                with self.assertRaises(ValueError):
                    self.archive.get_json(url)

                self.assertEqual(self.archive.get_json(url), {"filters": []})

        self.assertEqual(self.client.get_response.call_count, 6)

    def test_cache_rejects_corrupted_http_metadata(self) -> None:
        """Изменение HTTP-свидетельства должно нарушать целостность локального кеша."""

        url = f"{API_BASE}/discover/search"
        body = b'{"filters":[]}'
        self.client.get_response.return_value = _response(url, body)
        self.archive.get_json(url)

        metadata_paths = list(self.output_dir.rglob("*.http.json"))
        self.assertEqual(len(metadata_paths), 1)
        metadata_paths[0].write_bytes(b'{"status_code":403}')

        with self.assertRaises(ValueError):
            self.archive.get_json(url)

        self.client.get_response.assert_called_once_with(url)

    def test_pdf_cache_preserves_exact_body(self) -> None:
        """Повторное скачивание PDF должно возвращать существующий проверенный файл."""

        url = f"{API_BASE}/core/bitstreams/{BITSTREAM_UUID}/content"
        body = b"%PDF-1.7\noriginal bytes\n%%EOF\n"
        self.client.get_response.return_value = _response(
            url,
            body,
            content_type="application/pdf",
        )

        path = self.archive.download_pdf(BITSTREAM_UUID)

        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(self.archive.download_pdf(BITSTREAM_UUID), path)
        self.client.get_response.assert_called_once_with(url)

    def test_pdf_cache_rejects_corrupted_body(self) -> None:
        """Повреждённый локальный PDF должен приводить к ошибке целостности."""

        url = f"{API_BASE}/core/bitstreams/{BITSTREAM_UUID}/content"
        body = b"%PDF-1.7\noriginal bytes\n%%EOF\n"
        self.client.get_response.return_value = _response(
            url,
            body,
            content_type="application/pdf",
        )

        path = self.archive.download_pdf(BITSTREAM_UUID)
        path.write_bytes(b"%PDF-1.7\nchanged bytes\n%%EOF\n")

        with self.assertRaises(ValueError):
            self.archive.download_pdf(BITSTREAM_UUID)

        self.client.get_response.assert_called_once_with(url)

    def test_html_instead_of_pdf_is_not_cached_as_success(self) -> None:
        """HTML-страницу нельзя признать PDF даже при подходящем HTTP-заголовке."""

        url = f"{API_BASE}/core/bitstreams/{BITSTREAM_UUID}/content"
        pdf_body = b"%PDF-1.7\noriginal bytes\n%%EOF\n"
        self.client.get_response.side_effect = [
            _response(url, b"<html>Access denied</html>", content_type="application/pdf"),
            _response(url, pdf_body, content_type="application/pdf"),
        ]

        with self.assertRaises(ValueError):
            self.archive.download_pdf(BITSTREAM_UUID)

        self.assertEqual(self.archive.download_pdf(BITSTREAM_UUID).read_bytes(), pdf_body)
        self.assertEqual(self.client.get_response.call_count, 2)

    def test_filtered_metadata_uses_separate_reusable_cache(self) -> None:
        """Отбор по файлам не должен подменять кеш общей выдачи или заново получать её."""

        configuration_url = f"{API_BASE}/discover/search"
        search_url = _search_url(0)
        filtered_url = _search_url(0, has_files=True)
        records = (
            (configuration_url, _search_configuration(has_files=True)),
            (search_url, _search_page(0, total_pages=1)),
            (filtered_url, _search_page(0, total_pages=1, has_files=True)),
        )
        self.client.get_response.side_effect = [
            _response(url, json.dumps(record).encode("utf-8"))
            for url, record in records
        ]

        self.assertEqual(collect_metadata(self.archive, page_size=1), 1)
        self.assertEqual(collect_metadata(self.archive, page_size=1, has_files=True), 1)
        self.assertEqual(
            [call.args[0] for call in self.client.get_response.call_args_list],
            [configuration_url, search_url, filtered_url],
        )

        self.client.reset_mock()
        self.client.get_response.side_effect = AssertionError("Сетевой запрос запрещён")
        restored_archive = JinrArchive(
            output_dir=self.output_dir,
            state_dir=self.state_dir,
            client=self.client,
        )

        self.assertEqual(collect_metadata(restored_archive, page_size=1), 1)
        self.assertEqual(collect_metadata(restored_archive, page_size=1, has_files=True), 1)
        self.client.get_response.assert_not_called()


class JinrMetadataCollectionTests(unittest.TestCase):
    """Проверки фильтрации и обхода страниц без обращения к серверу."""

    def test_two_pages_follow_next_link(self) -> None:
        """Выгрузка с ключом searchResult должна обходить страницы и считать записи."""

        archive = MagicMock()
        next_url = _search_url(1)
        archive.get_json.side_effect = [
            _search_configuration(),
            _search_page(0, next_url=next_url),
            _search_page(1),
        ]

        count = collect_metadata(archive, max_pages=2, page_size=1)

        self.assertEqual(count, 2)
        self.assertEqual(archive.get_json.call_count, 3)
        self.assertEqual(
            archive.get_json.call_args_list[0].args[0],
            f"{API_BASE}/discover/search",
        )
        self.assertEqual(archive.get_json.call_args_list[2].args[0], next_url)

        first_page_url = archive.get_json.call_args_list[1].args[0]
        self.assertEqual(first_page_url, _search_url(0))
        self.assertEqual(
            parse_qs(urlsplit(first_page_url).query),
            {"dsoType": ["item"], "f.Lang": ["ru,equals"], "page": ["0"], "size": ["1"]},
        )

    def test_plural_search_results_remains_supported(self) -> None:
        """Вариант с ключом searchResults должен сохранять поддержку пагинации."""

        first_page = _search_page(0, next_url=_search_url(1))
        second_page = _search_page(1)

        for page in (first_page, second_page):
            embedded = page["_embedded"]
            embedded["searchResults"] = embedded.pop("searchResult")

        archive = MagicMock()
        archive.get_json.side_effect = [
            _search_configuration(),
            first_page,
            second_page,
        ]

        self.assertEqual(collect_metadata(archive, max_pages=0, page_size=1), 2)
        self.assertEqual(archive.get_json.call_count, 3)
        self.assertEqual(archive.get_json.call_args_list[2].args[0], _search_url(1))

    def test_invalid_singular_result_is_not_hidden_by_plural(self) -> None:
        """Корректный searchResults не должен скрывать повреждённый searchResult."""

        for invalid_result in (None, [], "invalid", 0):
            with self.subTest(result=invalid_result):
                page = _search_page(0, total_pages=1)
                embedded = page["_embedded"]
                embedded["searchResults"] = embedded["searchResult"]
                embedded["searchResult"] = invalid_result

                archive = MagicMock()
                archive.get_json.side_effect = [_search_configuration(), page]

                with self.assertRaises(ValueError):
                    collect_metadata(archive, page_size=1)

                self.assertEqual(archive.get_json.call_count, 2)

    def test_missing_search_result_keys_are_rejected(self) -> None:
        """Отсутствие обоих ключей результата нельзя принимать за пустую выдачу."""

        page = _search_page(0, total_pages=1)
        page["_embedded"] = {}

        archive = MagicMock()
        archive.get_json.side_effect = [_search_configuration(), page]

        with self.assertRaises(ValueError):
            collect_metadata(archive, page_size=1)

        self.assertEqual(archive.get_json.call_count, 2)

    def test_one_page_limit_does_not_follow_next_link(self) -> None:
        """Ограничение в одну страницу должно останавливать дальнейшие запросы."""

        archive = MagicMock()
        archive.get_json.side_effect = [
            _search_configuration(),
            _search_page(0, next_url=_search_url(1)),
        ]

        self.assertEqual(collect_metadata(archive, max_pages=1, page_size=1), 1)
        self.assertEqual(archive.get_json.call_count, 2)

    def test_zero_page_limit_collects_all_pages(self) -> None:
        """Нулевое ограничение должно разрешать обход до последней страницы."""

        archive = MagicMock()
        archive.get_json.side_effect = [
            _search_configuration(),
            _search_page(0, next_url=_search_url(1)),
            _search_page(1),
        ]

        self.assertEqual(collect_metadata(archive, max_pages=0, page_size=1), 2)
        self.assertEqual(archive.get_json.call_count, 3)

    def test_missing_language_filter_stops_before_search(self) -> None:
        """Без объявленного языкового фильтра нельзя начинать общую выгрузку."""

        archive = MagicMock()
        archive.get_json.return_value = {"filters": []}

        with self.assertRaises(ValueError):
            collect_metadata(archive)

        archive.get_json.assert_called_once_with(f"{API_BASE}/discover/search")

    def test_missing_equals_operator_stops_before_search(self) -> None:
        """Фильтр без оператора точного равенства не подходит для отбора по языку."""

        archive = MagicMock()
        archive.get_json.return_value = {
            "filters": [{"filter": "Lang", "operators": [{"operator": "contains"}]}],
        }

        with self.assertRaises(ValueError):
            collect_metadata(archive)

        self.assertEqual(archive.get_json.call_count, 1)

    def test_unapplied_language_filter_is_rejected(self) -> None:
        """Ответ без подтверждения русского языкового фильтра нельзя принять."""

        filter_variants = (
            None,
            [],
            [{"filter": "Lang", "operator": "equals", "value": "en"}],
        )

        for applied_filters in filter_variants:
            with self.subTest(applied_filters=applied_filters):
                page = _search_page(0, total_pages=1)

                if applied_filters is None:
                    del page["appliedFilters"]

                else:
                    page["appliedFilters"] = applied_filters

                archive = MagicMock()
                archive.get_json.side_effect = [_search_configuration(), page]

                with self.assertRaises(ValueError):
                    collect_metadata(archive, page_size=1)

                self.assertEqual(archive.get_json.call_count, 2)

    def test_repeated_next_url_is_rejected(self) -> None:
        """Повтор одной ссылки пагинации не должен создавать бесконечный обход."""

        archive = MagicMock()
        next_url = _search_url(1)
        archive.get_json.side_effect = [
            _search_configuration(),
            _search_page(0, total_pages=3, next_url=next_url),
            _search_page(1, total_pages=3, next_url=next_url),
        ]

        with self.assertRaises(ValueError):
            collect_metadata(archive, max_pages=0, page_size=1)

        self.assertEqual(archive.get_json.call_count, 3)

    def test_missing_next_link_before_last_page_is_rejected(self) -> None:
        """Отсутствие следующей ссылки не должно скрывать неполную выгрузку."""

        archive = MagicMock()
        archive.get_json.side_effect = [
            _search_configuration(),
            _search_page(0),
        ]

        with self.assertRaises(ValueError):
            collect_metadata(archive, max_pages=0, page_size=1)

        self.assertEqual(archive.get_json.call_count, 2)


class JinrMetadataWithFilesTests(unittest.TestCase):
    """Проверки отбора карточек с файлами и сохранения условий при пагинации."""

    def test_filtered_pages_preserve_query_and_follow_next_link(self) -> None:
        """Отбор должен подтверждаться на обеих страницах и передаваться в запросах."""

        next_url = _search_url(1, has_files=True)
        archive = MagicMock()
        archive.get_json.side_effect = [
            _search_configuration(has_files=True),
            _search_page(0, next_url=next_url, has_files=True),
            _search_page(1, has_files=True),
        ]

        self.assertEqual(
            collect_metadata(archive, max_pages=0, page_size=1, has_files=True),
            2,
        )
        self.assertEqual(
            [call.args[0] for call in archive.get_json.call_args_list],
            [f"{API_BASE}/discover/search", _search_url(0, has_files=True), next_url],
        )

    def test_relative_next_link_is_supported(self) -> None:
        """Относительная ссылка должна разрешаться без утраты условий отбора."""

        next_url = _search_url(1, has_files=True)
        next_query = f"?{urlsplit(next_url).query}"
        archive = MagicMock()
        archive.get_json.side_effect = [
            _search_configuration(has_files=True),
            _search_page(0, next_url=next_query, has_files=True),
            _search_page(1, has_files=True),
        ]

        self.assertEqual(
            collect_metadata(archive, max_pages=0, page_size=1, has_files=True),
            2,
        )
        self.assertEqual(archive.get_json.call_args_list[2].args[0], next_url)

    def test_filtered_plural_search_results_are_supported(self) -> None:
        """Вариант searchResults должен принимать подтверждённый отбор по файлам."""

        page = _search_page(0, total_pages=1, has_files=True)
        embedded = page["_embedded"]
        embedded["searchResults"] = embedded.pop("searchResult")
        archive = MagicMock()
        archive.get_json.side_effect = [_search_configuration(has_files=True), page]

        self.assertEqual(collect_metadata(archive, page_size=1, has_files=True), 1)
        self.assertEqual(archive.get_json.call_count, 2)

    def test_empty_filtered_result_is_accepted(self) -> None:
        """Пустую выдачу можно принять, только если сервер подтвердил условия отбора."""

        page = _search_page(0, total_pages=0, has_files=True)
        page["_embedded"]["searchResult"]["_embedded"]["objects"] = []
        archive = MagicMock()
        archive.get_json.side_effect = [_search_configuration(has_files=True), page]

        self.assertEqual(
            collect_metadata(archive, max_pages=0, page_size=1, has_files=True),
            0,
        )
        self.assertEqual(archive.get_json.call_count, 2)

    def test_missing_file_filter_stops_before_search(self) -> None:
        """Без объявленного фильтра файлов нельзя запрашивать неподтверждённую выборку."""

        archive = MagicMock()
        archive.get_json.return_value = _search_configuration()

        with self.assertRaises(ValueError):
            collect_metadata(archive, has_files=True)

        archive.get_json.assert_called_once_with(f"{API_BASE}/discover/search")

    def test_language_filter_cannot_replace_file_filter(self) -> None:
        """Одно имя нельзя одновременно использовать для отбора по языку и файлам."""

        archive = MagicMock()

        with self.assertRaises(ValueError):
            collect_metadata(
                archive,
                language_filter="has_content_in_original_bundle",
                has_files=True,
            )

        archive.get_json.assert_not_called()

    def test_unsupported_file_filter_operator_stops_before_search(self) -> None:
        """Для фильтра файлов требуется equals, а не любой объявленный оператор."""

        operator_variants = (None, [], [{"operator": "contains"}], ["equals"])

        for operators in operator_variants:
            with self.subTest(operators=operators):
                configuration = _search_configuration(has_files=True)
                configuration["filters"][1]["operators"] = operators
                archive = MagicMock()
                archive.get_json.return_value = configuration

                with self.assertRaises(ValueError):
                    collect_metadata(archive, has_files=True)

                archive.get_json.assert_called_once_with(f"{API_BASE}/discover/search")

    def test_missing_applied_file_filter_is_rejected(self) -> None:
        """Одного подтверждения языка недостаточно для выборки карточек с файлами."""

        archive = MagicMock()
        archive.get_json.side_effect = [
            _search_configuration(has_files=True),
            _search_page(0, total_pages=1),
        ]

        with self.assertRaises(ValueError):
            collect_metadata(archive, page_size=1, has_files=True)

        self.assertEqual(archive.get_json.call_count, 2)

    def test_incorrect_applied_file_filter_is_rejected(self) -> None:
        """Подтверждение файлов должно содержать точное имя, оператор и строку true."""

        invalid_fields = (
            ("filter", "has_content"),
            ("operator", "contains"),
            ("value", "false"),
            ("value", True),
            ("value", None),
        )

        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                page = _search_page(0, total_pages=1, has_files=True)
                page["appliedFilters"][1][field] = value
                archive = MagicMock()
                archive.get_json.side_effect = [_search_configuration(has_files=True), page]

                with self.assertRaises(ValueError):
                    collect_metadata(archive, page_size=1, has_files=True)

                self.assertEqual(archive.get_json.call_count, 2)

    def test_later_page_must_confirm_file_filter(self) -> None:
        """Первая корректная страница не освобождает следующие от проверки фильтров."""

        archive = MagicMock()
        archive.get_json.side_effect = [
            _search_configuration(has_files=True),
            _search_page(0, next_url=_search_url(1, has_files=True), has_files=True),
            _search_page(1),
        ]

        with self.assertRaises(ValueError):
            collect_metadata(archive, max_pages=0, page_size=1, has_files=True)

        self.assertEqual(archive.get_json.call_count, 3)

    def test_file_filter_does_not_replace_language_check(self) -> None:
        """Подтверждённый отбор по файлам не должен отменять проверку русского языка."""

        page = _search_page(0, total_pages=1, has_files=True)
        page["appliedFilters"][0]["value"] = "en"
        archive = MagicMock()
        archive.get_json.side_effect = [_search_configuration(has_files=True), page]

        with self.assertRaises(ValueError):
            collect_metadata(archive, page_size=1, has_files=True)

        self.assertEqual(archive.get_json.call_count, 2)

    def test_next_link_cannot_drop_or_change_required_filters(self) -> None:
        """Потерю или подмену условий в следующей ссылке нужно выявить до запроса."""

        invalid_parameters = (
            ("dsoType", None),
            ("dsoType", ["collection"]),
            ("f.Lang", None),
            ("f.Lang", ["en,equals"]),
            ("f.has_content_in_original_bundle", None),
            ("f.has_content_in_original_bundle", ["false,equals"]),
            ("f.has_content_in_original_bundle", ["true,contains"]),
        )

        for parameter, values in invalid_parameters:
            with self.subTest(parameter=parameter, values=values):
                parameters = parse_qs(urlsplit(_search_url(1, has_files=True)).query)

                if values is None:
                    del parameters[parameter]

                else:
                    parameters[parameter] = values

                next_url = f"{API_BASE}/discover/search/objects?{urlencode(parameters, doseq=True)}"
                archive = MagicMock()
                archive.get_json.side_effect = [
                    _search_configuration(has_files=True),
                    _search_page(0, next_url=next_url, has_files=True),
                ]

                with self.assertRaises(ValueError):
                    collect_metadata(archive, max_pages=0, page_size=1, has_files=True)

                self.assertEqual(archive.get_json.call_count, 2)

    def test_next_link_cannot_repeat_required_parameters(self) -> None:
        """Даже одинаковые дубли условий отбора нельзя передавать серверу неоднозначно."""

        required_parameters = {
            "dsoType": "item",
            "f.Lang": "ru,equals",
            "f.has_content_in_original_bundle": "true,equals",
        }

        for parameter, value in required_parameters.items():
            with self.subTest(parameter=parameter):
                next_url = f"{_search_url(1, has_files=True)}&{urlencode({parameter: value})}"
                archive = MagicMock()
                archive.get_json.side_effect = [
                    _search_configuration(has_files=True),
                    _search_page(0, next_url=next_url, has_files=True),
                ]

                with self.assertRaises(ValueError):
                    collect_metadata(archive, max_pages=0, page_size=1, has_files=True)

                self.assertEqual(archive.get_json.call_count, 2)

    def test_custom_language_filter_is_preserved(self) -> None:
        """Проверки ссылок и ответов должны учитывать настроенное имя фильтра языка."""

        language_filter = "Language"
        configuration = _search_configuration(has_files=True)
        configuration["filters"][0]["filter"] = language_filter
        first_url = _search_url(0, has_files=True).replace("f.Lang=", "f.Language=")
        next_url = _search_url(1, has_files=True).replace("f.Lang=", "f.Language=")
        first_page = _search_page(0, next_url=next_url, has_files=True)
        second_page = _search_page(1, has_files=True)

        for page in (first_page, second_page):
            page["appliedFilters"][0]["filter"] = language_filter

        archive = MagicMock()
        archive.get_json.side_effect = [configuration, first_page, second_page]

        self.assertEqual(
            collect_metadata(
                archive,
                max_pages=0,
                page_size=1,
                language_filter=language_filter,
                has_files=True,
            ),
            2,
        )
        self.assertEqual(archive.get_json.call_args_list[1].args[0], first_url)
        self.assertEqual(archive.get_json.call_args_list[2].args[0], next_url)


class JinrDownloadCommandTests(unittest.TestCase):
    """Проверки кодов завершения команд без сетевых запросов и создания состояния."""

    def test_probe_returns_success_after_json_response(self) -> None:
        """Полученный корень API должен завершать команду успешным кодом."""

        with (
            patch("scripts.download_jinr.JinrApiClient"),
            patch("scripts.download_jinr.JinrArchive") as archive_class,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = main(["probe"])

        self.assertEqual(result, 0)
        archive_class.return_value.get_json.assert_called_once_with(API_BASE)

    def test_invalid_response_returns_failure(self) -> None:
        """Ошибка ответа должна завершать команду без сообщения об успехе."""

        with (
            patch("scripts.download_jinr.JinrApiClient"),
            patch("scripts.download_jinr.JinrArchive") as archive_class,
            patch("sys.stderr", new_callable=io.StringIO) as stderr,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            archive_class.return_value.get_json.side_effect = ValueError("Ответ не JSON")

            result = main(["probe"])

        self.assertEqual(result, 1)
        self.assertIn("Ответ не JSON", stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")

    def test_interruption_returns_standard_exit_code(self) -> None:
        """Прерывание пользователем должно завершать команду кодом 130."""

        with (
            patch("scripts.download_jinr.JinrApiClient"),
            patch("scripts.download_jinr.JinrArchive") as archive_class,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            archive_class.return_value.get_json.side_effect = KeyboardInterrupt

            result = main(["probe"])

        self.assertEqual(result, 130)

    def test_metadata_passes_file_filter_flag(self) -> None:
        """Команда должна передать явный отбор по файлам и остальные ограничения."""

        with (
            patch("scripts.download_jinr.JinrApiClient"),
            patch("scripts.download_jinr.JinrArchive") as archive_class,
            patch("scripts.download_jinr.collect_metadata", return_value=2) as collect,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = main(["metadata", "--has-files", "--max-pages", "0", "--page-size", "100"])

        self.assertEqual(result, 0)
        collect.assert_called_once_with(
            archive_class.return_value,
            max_pages=0,
            page_size=100,
            language_filter="Lang",
            has_files=True,
        )

    def test_metadata_does_not_filter_files_by_default(self) -> None:
        """Без нового флага команда должна продолжать собирать метаданные без отбора файлов."""

        with (
            patch("scripts.download_jinr.JinrApiClient"),
            patch("scripts.download_jinr.JinrArchive") as archive_class,
            patch("scripts.download_jinr.collect_metadata", return_value=1) as collect,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = main(["metadata"])

        self.assertEqual(result, 0)
        collect.assert_called_once_with(
            archive_class.return_value,
            max_pages=1,
            page_size=20,
            language_filter="Lang",
            has_files=False,
        )


if __name__ == "__main__":
    unittest.main()
