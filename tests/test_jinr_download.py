"""Проверки локального архива и ограниченной выгрузки метаданных ОИЯИ."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts.download_jinr import JinrArchive, collect_metadata, main
from src.collect.base import HttpResponseSnapshot
from src.collect.jinr_api import DEFAULT_API_URL


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


def _search_configuration() -> dict[str, Any]:
    """Описать языковой фильтр в формате контракта DSpace."""

    return {
        "filters": [
            {
                "filter": "Lang",
                "operators": [{"operator": "equals"}],
            },
        ],
    }


def _search_url(page_index: int, page_size: int = 1) -> str:
    """Построить тестовую ссылку на страницу русскоязычных записей."""

    query = urlencode(
        {
            "dsoType": "item",
            "f.Lang": "ru,equals",
            "page": page_index,
            "size": page_size,
        },
    )

    return f"{API_BASE}/discover/search/objects?{query}"


def _search_page(
    page_index: int,
    *,
    total_pages: int = 2,
    next_url: str | None = None,
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

    return {
        "appliedFilters": [{"filter": "Lang", "operator": "equals", "value": "ru"}],
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


if __name__ == "__main__":
    unittest.main()
