"""Проверки локального отбора карточек и команды перечней файлов ОИЯИ."""

from __future__ import annotations

import io
import json
import tempfile
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from scripts.download_jinr import (
    JinrArchive,
    _cached_file_items,
    collect_file_lists,
    main,
)
from src.collect.jinr_api import DEFAULT_API_URL, JinrApiError
from tests.test_jinr_download import _response, _search_url
from tests.test_jinr_files import BITSTREAM_UUID, BUNDLES_URL, _responses


class JinrFileCommandTests(unittest.TestCase):
    """Проверки команды без реального доступа к API и скачивания PDF."""

    def setUp(self) -> None:
        """Подготовить отдельный архив и клиент, запрещающий сетевые запросы."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.client = MagicMock()
        self.client.get_response.side_effect = AssertionError("Неожиданный сетевой запрос")
        self.archive = JinrArchive(self.root / "raw", self.root / "state", self.client)
        self.search_url = _search_url(0, 100, has_files=True)

        output_patch = patch("sys.stdout", new_callable=io.StringIO)
        output_patch.start()
        self.addCleanup(output_patch.stop)

    def _cache_items(self, count: int = 2) -> list[dict[str, Any]]:
        """Сохранить полную страницу карточек с подтверждёнными фильтрами."""

        items = [
            {
                "uuid": f"11111111-2222-4333-8444-{index:012d}",
                "type": "item",
                "metadata": {"dc.title": [{"value": f"Статья {index}"}]},
                "_links": {
                    "bundles": {
                        "href": f"{DEFAULT_API_URL}/core/items/11111111-2222-4333-8444-{index:012d}/bundles",
                    },
                },
            }
            for index in range(count)
        ]
        record = {
            "appliedFilters": [
                {"filter": "Lang", "operator": "equals", "value": "ru"},
                {
                    "filter": "has_content_in_original_bundle",
                    "operator": "equals",
                    "value": "true",
                },
            ],
            "_embedded": {
                "searchResult": {
                    "_embedded": {
                        "objects": [
                            {"_embedded": {"indexableObject": item}} for item in items
                        ],
                    },
                    "page": {
                        "number": 0,
                        "size": 100,
                        "totalElements": count,
                        "totalPages": 1 if count else 0,
                    },
                    "_links": {},
                },
            },
        }
        body = json.dumps(record).encode("utf-8")
        self.archive._save(_response(self.search_url, body), "json")

        return items

    def _cache_bundles(self, item: dict[str, Any], *, total_elements: int) -> str:
        """Сохранить пустой список наборов с заданным сервером числом элементов."""

        source_url = item["_links"]["bundles"]["href"]
        record = {
            "_embedded": {"bundles": []},
            "page": {
                "number": 0,
                "size": 20,
                "totalElements": total_elements,
                "totalPages": 1 if total_elements else 0,
            },
            "_links": {"self": {"href": source_url}},
        }
        self.archive._save(_response(source_url, json.dumps(record).encode("utf-8")), "json")

        return source_url

    def test_missing_cache_never_requests_metadata(self) -> None:
        """Отсутствующий кеш должен останавливать команду до сетевого запроса."""

        with self.assertRaisesRegex(ValueError, "Не хватает кеша"):
            collect_file_lists(self.archive)

        self.client.get_response.assert_not_called()

    def test_corrupted_cache_never_requests_replacement(self) -> None:
        """Повреждённый исходник нельзя молча заменить новым ответом сервера."""

        self._cache_items()
        evidence = self.archive.json_evidence(self.search_url)
        Path(evidence["body_path"]).write_bytes(b"{}")

        with self.assertRaisesRegex(ValueError, "целостность"):
            _cached_file_items(self.archive)

        self.client.get_response.assert_not_called()

    def test_incomplete_page_stops_before_collecting_files(self) -> None:
        """Неполную страницу нельзя принять за весь доступный набор карточек."""

        self._cache_items()
        record = self.archive.cached_json(self.search_url)
        record["_embedded"]["searchResult"]["_embedded"]["objects"].pop()
        body = json.dumps(record).encode("utf-8")
        self.archive._save(_response(self.search_url, body), "json")

        with (
            patch("scripts.download_jinr.collect_item_files") as collector,
            self.assertRaisesRegex(ValueError, "неполную страницу"),
        ):
            collect_file_lists(self.archive)

        collector.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_empty_selection_is_valid(self) -> None:
        """Подтверждённая пустая выдача должна завершаться без запросов файлов."""

        self._cache_items(0)

        with patch("scripts.download_jinr.collect_item_files") as collector:
            self.assertEqual(collect_file_lists(self.archive, max_items=0), 0)

        collector.assert_not_called()
        self.client.get_response.assert_not_called()
        self.assertFalse((self.archive.state_dir / "file_lists").exists())

    def test_unmatched_selection_also_skips_preliminary_matches(self) -> None:
        """Предварительные совпадения исключаются только по явному флагу отбора."""

        items = self._cache_items(3)
        matches_path = self.root / "matches.jsonl"
        matches_path.write_text(
            json.dumps({
                "matched_item": {"item_uuid": items[0]["uuid"]},
                "match_status": "preliminary",
            }) + "\n",
            encoding="utf-8",
        )

        with patch("scripts.download_jinr.collect_item_files") as collector:
            collector.side_effect = [
                {
                    "item_uuid": item["uuid"], "files": [], "source_urls": [],
                    "listing_complete": True, "diagnostics": [],
                }
                for item in items[1:]
            ]
            count = collect_file_lists(
                self.archive,
                max_items=0,
                unmatched_only=True,
                matches_path=matches_path,
            )

        self.assertEqual(count, 2)
        self.assertEqual([call.args[0] for call in collector.call_args_list], items[1:])

        with patch("scripts.download_jinr.collect_item_files") as collector:
            collector.return_value = {
                "item_uuid": items[0]["uuid"], "files": [], "source_urls": [],
                "listing_complete": True, "diagnostics": [],
            }
            self.assertEqual(collect_file_lists(self.archive, matches_path=matches_path), 1)

        self.assertEqual(collector.call_args.args[0], items[0])
        self.client.get_response.assert_not_called()

    def test_failure_preserves_only_completed_item(self) -> None:
        """Ошибка второй карточки не удаляет готовую первую и не создаёт неполную."""

        items = self._cache_items()

        with patch("scripts.download_jinr.collect_item_files") as collector:
            collector.side_effect = [
                {
                    "item_uuid": items[0]["uuid"], "files": [], "source_urls": [],
                    "listing_complete": True, "diagnostics": [],
                },
                ValueError("Неполная вторая карточка"),
            ]

            with self.assertRaisesRegex(ValueError, "Неполная вторая"):
                collect_file_lists(self.archive, max_items=0)

        result_dir = self.archive.state_dir / "file_lists"
        self.assertTrue((result_dir / f"{items[0]['uuid']}.json").is_file())
        self.assertFalse((result_dir / f"{items[1]['uuid']}.json").exists())
        self.client.get_response.assert_not_called()

    def test_repeat_rebuilds_result_from_verified_raw_cache(self) -> None:
        """Повтор проверяет исходные ответы и восстанавливает производный перечень."""

        item = self._cache_items(1)[0]
        source_url = f"{DEFAULT_API_URL}/core/items/{item['uuid']}/bundles"
        self.client.get_response.side_effect = None
        self.client.get_response.return_value = _response(source_url, b'{"files": []}')

        def collect_cached_item(record: dict[str, Any], read_json: Any) -> dict[str, Any]:
            """Прочитать исходный ответ через callback, не загружая содержимое файла."""

            response = read_json(source_url)

            return {
                "item_uuid": record["uuid"],
                "files": response["files"],
                "source_urls": [source_url],
                "listing_complete": True,
                "diagnostics": [],
            }

        with patch("scripts.download_jinr.collect_item_files", side_effect=collect_cached_item):
            self.assertEqual(collect_file_lists(self.archive), 1)
            result_path = self.archive.state_dir / "file_lists" / f"{item['uuid']}.json"
            expected_bytes = result_path.read_bytes()
            result_path.write_text('{"stale": true}', encoding="utf-8")
            self.client.get_response.side_effect = AssertionError("Повтор вышел в сеть")
            self.assertEqual(collect_file_lists(self.archive), 1)

        self.assertEqual(result_path.read_bytes(), expected_bytes)
        result = json.loads(expected_bytes)
        self.assertEqual(result["schema_version"], "jinr-file-list-v2")
        self.assertEqual(result["sources"], [self.archive.json_evidence(source_url)])
        self.assertEqual(result["metadata_sources"], [self.archive.json_evidence(self.search_url)])
        self.client.get_response.assert_called_once_with(source_url)

    def test_cli_routes_files_options_without_pdf_download(self) -> None:
        """Команда files передаёт параметры отбора и не запускает загрузку PDF."""

        matches_path = self.root / "matches.jsonl"

        with (
            patch("scripts.download_jinr.JinrApiClient"),
            patch("scripts.download_jinr.JinrArchive") as archive_class,
            patch("scripts.download_jinr.collect_file_lists", return_value=2) as collector,
        ):
            exit_code = main([
                "--state-dir", str(self.root / "state"),
                "--output-dir", str(self.root / "raw"),
                "files", "--unmatched-only", "--max-items", "0", "--refresh",
                "--matches", str(matches_path),
            ])

        self.assertEqual(exit_code, 0)
        collector.assert_called_once_with(
            archive_class.return_value,
            max_items=0,
            unmatched_only=True,
            matches_path=matches_path,
            refresh=True,
        )
        archive_class.return_value.download_pdf.assert_not_called()

    def test_incomplete_item_is_saved_and_next_item_is_processed(self) -> None:
        """Неполная первая карточка сохраняет причины и не останавливает вторую."""

        items = self._cache_items()
        incomplete_url = self._cache_bundles(items[0], total_elements=1)
        complete_url = self._cache_bundles(items[1], total_elements=0)

        with patch("sys.stdout", new_callable=io.StringIO) as output:
            completed_count = collect_file_lists(self.archive, max_items=0)

        self.assertEqual(completed_count, 1)
        result_dir = self.archive.state_dir / "file_lists"
        incomplete = json.loads((result_dir / f"{items[0]['uuid']}.json").read_text())
        complete = json.loads((result_dir / f"{items[1]['uuid']}.json").read_text())

        self.assertEqual(incomplete["schema_version"], "jinr-file-list-v2")
        self.assertEqual(incomplete["item_uuid"], items[0]["uuid"])
        self.assertEqual(incomplete["scope"], "ORIGINAL")
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertFalse(incomplete["listing_complete"])
        self.assertEqual(incomplete["files"], [])
        self.assertEqual(incomplete["source_urls"], [incomplete_url])
        self.assertEqual(incomplete["sources"], [self.archive.json_evidence(incomplete_url)])
        self.assertEqual(incomplete["metadata_sources"], [self.archive.json_evidence(self.search_url)])
        self.assertEqual(len(incomplete["diagnostics"]), 1)
        diagnostic = incomplete["diagnostics"][0]
        self.assertIsInstance(diagnostic["message"], str)
        self.assertTrue(diagnostic["message"])
        self.assertEqual(
            {key: value for key, value in diagnostic.items() if key != "message"},
            {
                "kind": "count_mismatch",
                "url": incomplete_url,
                "relation": "bundles",
                "page_number": 0,
                "expected_count": 1,
                "actual_count": 0,
                "total_elements": 1,
            },
        )
        self.assertEqual(complete["status"], "complete")
        self.assertTrue(complete["listing_complete"])
        self.assertEqual(complete["files"], [])
        self.assertEqual(complete["diagnostics"], [])
        self.assertEqual(complete["sources"], [self.archive.json_evidence(complete_url)])
        self.assertNotIn("error", complete)
        self.assertIn("полнота не подтверждена", output.getvalue().lower())
        self.assertIn("Итого: полных перечней — 1, с неподтверждённой полнотой — 1", output.getvalue())
        self.assertIn("JSON: кеш", output.getvalue())
        self.assertNotIn("запрос к API", output.getvalue())
        self.client.get_response.assert_not_called()

    def test_incomplete_retry_uses_cache_and_replaces_stale_complete_result(self) -> None:
        """Повтор из кеша сохраняет неполный статус вместо старого успешного перечня."""

        item = self._cache_items(1)[0]
        source_url = self._cache_bundles(item, total_elements=0)
        self.assertEqual(collect_file_lists(self.archive), 1)
        result_path = self.archive.state_dir / "file_lists" / f"{item['uuid']}.json"
        self.assertEqual(json.loads(result_path.read_text())["status"], "complete")
        old_evidence = self.archive.json_evidence(source_url)
        old_source_path = Path(old_evidence["body_path"])
        old_source_bytes = old_source_path.read_bytes()

        self._cache_bundles(item, total_elements=1)
        new_evidence = self.archive.json_evidence(source_url)
        new_source_path = Path(new_evidence["body_path"])
        new_source_bytes = new_source_path.read_bytes()
        self.assertEqual(collect_file_lists(self.archive), 0)
        expected_bytes = result_path.read_bytes()
        self.assertEqual(collect_file_lists(self.archive), 0)

        result = json.loads(result_path.read_text())
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["listing_complete"])
        self.assertEqual(result["files"], [])
        self.assertEqual(result["sources"], [new_evidence])
        self.assertEqual(result_path.read_bytes(), expected_bytes)
        self.assertEqual(old_source_path.read_bytes(), old_source_bytes)
        self.assertEqual(new_source_path.read_bytes(), new_source_bytes)
        self.client.get_response.assert_not_called()

    def test_hard_failures_still_stop_before_following_items(self) -> None:
        """Ошибки структуры, сети и запрет HTTP 403 не записываются как дефицит."""

        items = self._cache_items()

        for exception in (
            ValueError("Повреждённый ответ API"),
            JinrApiError("Сеть недоступна"),
            JinrApiError("HTTP 403: доступ запрещён"),
        ):
            with self.subTest(exception=exception):
                with (
                    patch("scripts.download_jinr.collect_item_files", side_effect=exception) as collector,
                    self.assertRaises(type(exception)) as caught,
                ):
                    collect_file_lists(self.archive, max_items=0)

                self.assertIs(caught.exception, exception)
                self.assertEqual(collector.call_count, 1)
                self.assertEqual(collector.call_args.args[0], items[0])
                self.assertFalse((self.archive.state_dir / "file_lists").exists())

        self.client.get_response.assert_not_called()

    def test_partial_listing_retains_visible_file_and_all_evidence(self) -> None:
        """Видимый файл сохраняется даже при неподтверждённой полноте групп."""

        item = self._cache_items(1)[0]
        bundle_url = item["_links"]["bundles"]["href"]
        responses = _responses(details=True)
        responses[bundle_url] = responses.pop(BUNDLES_URL)
        responses[bundle_url]["page"].update({"size": 2, "totalElements": 2})

        for url, response in responses.items():
            self.archive._save(_response(url, json.dumps(response).encode("utf-8")), "json")

        self.assertEqual(collect_file_lists(self.archive), 0)
        result_path = self.archive.state_dir / "file_lists" / f"{item['uuid']}.json"
        result = json.loads(result_path.read_text())

        self.assertEqual(result["schema_version"], "jinr-file-list-v2")
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["listing_complete"])
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["bitstream_uuid"], BITSTREAM_UUID)
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertEqual(result["diagnostics"][0]["url"], bundle_url)
        self.assertEqual(set(result["source_urls"]), set(responses))
        self.assertEqual(result["sources"], [
            self.archive.json_evidence(url) for url in result["source_urls"]
        ])
        self.client.get_response.assert_not_called()

    def test_refresh_updates_only_selected_listing_not_metadata(self) -> None:
        """Явное обновление не затрагивает снимок метаданных и невыбранные карточки."""

        items = self._cache_items()
        first_url = self._cache_bundles(items[0], total_elements=1)
        other_url = self._cache_bundles(items[1], total_elements=1)
        metadata_evidence = self.archive.json_evidence(self.search_url)
        other_evidence = self.archive.json_evidence(other_url)
        response = self.archive.cached_json(first_url)
        response["page"]["totalElements"] = 0
        self.client.get_response.side_effect = None
        self.client.get_response.return_value = _response(
            first_url, json.dumps(response).encode("utf-8"),
        )

        self.assertEqual(collect_file_lists(self.archive, refresh=True), 1)

        self.client.get_response.assert_called_once_with(first_url)
        self.assertEqual(self.archive.json_evidence(self.search_url), metadata_evidence)
        self.assertEqual(self.archive.json_evidence(other_url), other_evidence)
        result_path = self.archive.state_dir / "file_lists" / f"{items[0]['uuid']}.json"
        result = json.loads(result_path.read_text())
        self.assertEqual(result["sources"], [self.archive.json_evidence(first_url)])
        self.assertEqual(result["status"], "complete")

    def test_refresh_failure_preserves_old_result_without_returning_success(self) -> None:
        """Отказ обновления не объявляет прежний успешный результат новым ответом."""

        item = self._cache_items(1)[0]
        source_url = self._cache_bundles(item, total_elements=0)
        self.assertEqual(collect_file_lists(self.archive), 1)
        result_path = self.archive.state_dir / "file_lists" / f"{item['uuid']}.json"
        previous_result = result_path.read_bytes()
        previous_evidence = self.archive.json_evidence(source_url)
        self.client.get_response.side_effect = JinrApiError("HTTP 403")

        with self.assertRaisesRegex(JinrApiError, "HTTP 403"):
            collect_file_lists(self.archive, refresh=True)

        self.assertEqual(result_path.read_bytes(), previous_result)
        self.assertEqual(self.archive.json_evidence(source_url), previous_evidence)
        self.client.get_response.assert_called_once_with(source_url)

    def test_same_url_is_refreshed_once_per_run(self) -> None:
        """Повторная связь текущего прохода использует уже полученный свежий ответ."""

        item = self._cache_items(1)[0]
        source_url = self._cache_bundles(item, total_elements=0)
        response = self.archive.cached_json(source_url)
        self.client.get_response.side_effect = None
        self.client.get_response.return_value = _response(
            source_url, json.dumps(response).encode("utf-8"),
        )

        def collect_twice(record: dict[str, Any], read_json: Any) -> dict[str, Any]:
            """Прочитать одну связь дважды для проверки сохранения ответа в памяти."""

            self.assertEqual(read_json(source_url), read_json(source_url))

            return {
                "item_uuid": record["uuid"], "files": [], "source_urls": [source_url],
                "listing_complete": True, "diagnostics": [],
            }

        with (
            patch("scripts.download_jinr.collect_item_files", side_effect=collect_twice),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            self.assertEqual(collect_file_lists(self.archive, refresh=True), 1)

        self.client.get_response.assert_called_once_with(source_url)
        self.assertIn("JSON: память текущего запуска", output.getvalue())

    def test_cli_does_not_refresh_by_default(self) -> None:
        """Обычная команда не включает обновление кеша без явного флага."""

        with (
            patch("scripts.download_jinr.JinrApiClient"),
            patch("scripts.download_jinr.JinrArchive"),
            patch("scripts.download_jinr.collect_file_lists", return_value=0) as collector,
        ):
            self.assertEqual(main(["files"]), 0)

        self.assertFalse(collector.call_args.kwargs["refresh"])


if __name__ == "__main__":
    unittest.main()
