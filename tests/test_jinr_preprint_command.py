"""Проверки явных PDF-ссылок препринтов ОИЯИ без реального доступа к сети."""

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

from scripts.download_jinr import JinrArchive, collect_preprints, main
from src.collect.base import HttpResponseSnapshot
from src.collect.jinr_api import JinrApiError
from tests.test_jinr_download import _response, _search_url


PREPRINT_URL = "http://www1.jinr.ru/Preprints/2024/05(P11-2024-5).pdf"
OTHER_PREPRINT_URL = "https://www1.jinr.ru/Preprints/2025/01(P11-2025-1).pdf"
PDF_BODY = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class _PreprintArchiveCase(unittest.TestCase):
    """Общие временные каталоги и клиент с запретом неожиданных запросов."""

    def setUp(self) -> None:
        """Подготовить изолированный архив и запретить оба вида сетевых запросов."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.client = MagicMock()
        self.client.get_response.side_effect = AssertionError("Неожиданный запрос к API")
        self.client.get_preprint_response.side_effect = AssertionError("Неожиданный запрос PDF")
        self.archive = JinrArchive(self.root / "raw", self.root / "state", self.client)
        self.search_url = _search_url(0, 100, has_files=True)

        output_patch = patch("sys.stdout", new_callable=io.StringIO)
        output_patch.start()
        self.addCleanup(output_patch.stop)

    def _cache_items(self, publication_urls: list[list[str]]) -> list[dict[str, Any]]:
        """Сохранить полный подтверждённый снимок карточек с заданными ссылками."""

        items = [
            {
                "uuid": f"11111111-2222-4333-8444-{index:012d}",
                "type": "item",
                "metadata": {
                    "dc.title": [{"value": f"Препринт {index}"}],
                    "dc.language.iso": [{"value": "ru"}],
                    "local.publication.uri": [{"value": url} for url in urls],
                },
            }
            for index, urls in enumerate(publication_urls)
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
                        "totalElements": len(items),
                        "totalPages": 1 if items else 0,
                    },
                    "_links": {},
                },
            },
        }
        body = json.dumps(record).encode("utf-8")
        self.archive._save(_response(self.search_url, body), "json")

        return items

    def _pdf_response(self, url: str = PREPRINT_URL) -> HttpResponseSnapshot:
        """Создать полный PDF-ответ для явно указанного исходного адреса."""

        return _response(url, PDF_BODY, content_type="application/pdf")

    def _allow_pdf_response(self, url: str = PREPRINT_URL) -> None:
        """Разрешить подставному клиенту вернуть один заданный PDF-снимок."""

        self.client.get_preprint_response.side_effect = None
        self.client.get_preprint_response.return_value = self._pdf_response(url)


class JinrPreprintArchiveTests(_PreprintArchiveCase):
    """Проверки строгого приёма PDF, кеша и свидетельств происхождения."""

    def test_pdf_download_keeps_exact_bytes_and_http_evidence(self) -> None:
        """PDF и его HTTP-снимок сохраняются с проверяемыми SHA-256 без API-запроса."""

        self._allow_pdf_response()
        body_path = self.archive.download_preprint(PREPRINT_URL)
        evidence = self.archive.pdf_evidence(PREPRINT_URL)
        snapshot = self._pdf_response()

        self.assertEqual(body_path.read_bytes(), PDF_BODY)
        self.assertEqual(Path(evidence["body_path"]), body_path)
        self.assertEqual(evidence["requested_url"], PREPRINT_URL)
        self.assertEqual(evidence["body_sha256"], hashlib.sha256(PDF_BODY).hexdigest())
        self.assertEqual(
            Path(evidence["response_metadata_path"]).read_bytes(),
            snapshot.canonical_metadata(),
        )
        self.assertEqual(evidence["response_metadata_sha256"], snapshot.metadata_sha256())
        self.client.get_preprint_response.assert_called_once_with(PREPRINT_URL)
        self.client.get_response.assert_not_called()

    def test_recreated_archive_reuses_verified_pdf_without_network(self) -> None:
        """Новый экземпляр архива повторно использует проверенный PDF-снимок."""

        self._allow_pdf_response()
        expected_path = self.archive.download_preprint(PREPRINT_URL)
        self.client.get_preprint_response.side_effect = AssertionError("Повтор вышел в сеть")
        restored = JinrArchive(self.archive.output_dir, self.archive.state_dir, self.client)

        self.assertEqual(restored.download_preprint(PREPRINT_URL), expected_path)
        self.assertEqual(restored.pdf_evidence(PREPRINT_URL), self.archive.pdf_evidence(PREPRINT_URL))
        self.client.get_preprint_response.assert_called_once_with(PREPRINT_URL)
        self.client.get_response.assert_not_called()

    def test_pdf_mime_is_case_insensitive_and_allows_parameters(self) -> None:
        """Регистр MIME и его параметры не меняют тип application/pdf."""

        self._allow_pdf_response()
        self.client.get_preprint_response.return_value = replace(
            self._pdf_response(), headers=(("Content-Type", "Application/PDF; charset=binary"),),
        )

        self.assertEqual(self.archive.download_preprint(PREPRINT_URL).read_bytes(), PDF_BODY)
        self.assertEqual(Path(self.archive.pdf_evidence(PREPRINT_URL)["body_path"]).read_bytes(), PDF_BODY)

    def test_leading_whitespace_is_preserved_before_pdf_signature(self) -> None:
        """Пробелы перед сигнатурой допустимы, но исходные байты не очищаются."""

        self._allow_pdf_response()
        body = b" \t\r\n" + PDF_BODY
        self.client.get_preprint_response.return_value = replace(self._pdf_response(), body=body)

        self.assertEqual(self.archive.download_preprint(PREPRINT_URL).read_bytes(), body)
        self.assertEqual(self.archive.pdf_evidence(PREPRINT_URL)["body_sha256"], hashlib.sha256(body).hexdigest())

    def test_invalid_responses_are_not_saved(self) -> None:
        """HTML, частичный PDF, другой адрес и неподходящий MIME не принимаются."""

        snapshot = self._pdf_response()
        invalid_responses = [
            replace(snapshot, status_code=206),
            replace(snapshot, body=b"<html>Error</html>"),
            replace(snapshot, body=b"<html>%PDF-1.7</html>"),
            replace(snapshot, headers=(("content-type", "text/html"),)),
            replace(snapshot, headers=()),
            replace(snapshot, requested_url=OTHER_PREPRINT_URL),
            replace(snapshot, final_url=OTHER_PREPRINT_URL),
        ]

        for response in invalid_responses:
            with self.subTest(response=response):
                self.client.get_preprint_response.side_effect = None
                self.client.get_preprint_response.return_value = response

                with self.assertRaises(ValueError):
                    self.archive.download_preprint(PREPRINT_URL)

                self.assertFalse(self.archive._cache_path(PREPRINT_URL).exists())
                self.assertFalse(self.archive._cache_path(OTHER_PREPRINT_URL).exists())
                self.assertFalse(self.archive.output_dir.exists())

        self.client.get_response.assert_not_called()

    def test_invalid_cached_pdf_is_not_trusted_or_refetched(self) -> None:
        """Совпадение SHA-256 не делает ошибочный HTTP-снимок допустимым PDF."""

        snapshot = self._pdf_response()
        invalid_responses = [
            replace(snapshot, status_code=206),
            replace(snapshot, body=b"<html>%PDF-1.7</html>"),
            replace(snapshot, headers=(("content-type", "text/html"),)),
            replace(snapshot, headers=()),
            replace(snapshot, final_url=OTHER_PREPRINT_URL),
        ]

        for response in invalid_responses:
            with self.subTest(response=response):
                self.archive._save(response, "pdf")

                with self.assertRaises(ValueError):
                    self.archive.download_preprint(PREPRINT_URL)

                with self.assertRaises(ValueError):
                    self.archive.pdf_evidence(PREPRINT_URL)

        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_corrupted_pdf_stops_before_network(self) -> None:
        """Изменённый сохранённый PDF нельзя молча заменить новой загрузкой."""

        body_path = self.archive._save(self._pdf_response(), "pdf")
        body_path.write_bytes(PDF_BODY + b"changed")

        with self.assertRaisesRegex(ValueError, "целостность"):
            self.archive.download_preprint(PREPRINT_URL)

        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_foreign_url_is_rejected_even_if_pdf_is_cached(self) -> None:
        """Готовый кеш не разрешает загрузку PDF с постороннего сайта."""

        foreign_url = "https://example.org/Preprints/2024/article.pdf"
        self.archive._save(self._pdf_response(foreign_url), "pdf")

        with self.assertRaises(ValueError):
            self.archive.download_preprint(foreign_url)

        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_missing_pdf_evidence_never_downloads_file(self) -> None:
        """Запрос свидетельства отсутствующего PDF не выполняет сетевой загрузки."""

        with self.assertRaises(ValueError):
            self.archive.pdf_evidence(PREPRINT_URL)

        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()


class JinrPreprintCommandTests(_PreprintArchiveCase):
    """Проверки локального отбора и явного запуска скачивания препринтов."""

    def test_missing_metadata_cache_stops_before_transport(self) -> None:
        """Отсутствующий полный снимок метаданных нельзя догружать автоматически."""

        with self.assertRaisesRegex(ValueError, "Не хватает кеша"):
            collect_preprints(self.archive, download=True)

        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_corrupted_metadata_stops_before_transport(self) -> None:
        """Повреждённые исходные метаданные останавливают отбор до обращения к сети."""

        self._cache_items([[PREPRINT_URL]])
        metadata = self.archive.json_evidence(self.search_url)
        Path(metadata["body_path"]).write_bytes(b"{}")

        with self.assertRaisesRegex(ValueError, "целостность"):
            collect_preprints(self.archive, download=True)

        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_incomplete_metadata_page_stops_before_transport(self) -> None:
        """Неполная страница не считается завершённым снимком русскоязычных записей."""

        self._cache_items([[PREPRINT_URL], [OTHER_PREPRINT_URL]])
        record = self.archive.cached_json(self.search_url)
        record["_embedded"]["searchResult"]["_embedded"]["objects"].pop()
        self.archive._save(_response(self.search_url, json.dumps(record).encode()), "json")

        with self.assertRaisesRegex(ValueError, "неполную страницу"):
            collect_preprints(self.archive, download=True)

        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_default_preview_is_offline_and_writes_nothing(self) -> None:
        """Обычный просмотр показывает явную ссылку без скачивания и новых файлов."""

        self._cache_items([[PREPRINT_URL]])
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(collect_preprints(self.archive), 1)

        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertIn(PREPRINT_URL, output.getvalue())
        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_limit_is_applied_after_safe_url_selection(self) -> None:
        """Посторонние источники не расходуют лимит подходящих карточек."""

        items = self._cache_items([
            ["https://elibrary.ru/item.asp?id=123"],
            ["https://example.org/article.pdf"],
            [PREPRINT_URL],
            [OTHER_PREPRINT_URL],
        ])
        self._allow_pdf_response()

        self.assertEqual(collect_preprints(self.archive, max_items=1, download=True), 1)

        self.client.get_preprint_response.assert_called_once_with(PREPRINT_URL)
        result_dir = self.archive.state_dir / "preprint_downloads"
        self.assertEqual([path.name for path in result_dir.glob("*.json")], [f"{items[2]['uuid']}.json"])
        self.client.get_response.assert_not_called()

    def test_no_candidates_does_not_create_download_directory(self) -> None:
        """Пустые поля и чужие ссылки не запускают загрузку или запись результата."""

        self._cache_items([
            [],
            ["https://example.org/article.pdf"],
            ["http://www1.jinr.ru/Preprints/2024/"],
            ["https://www1.jinr.ru.evil.example/Preprints/2024/article.pdf"],
        ])

        self.assertEqual(collect_preprints(self.archive, max_items=0, download=True), 0)
        self.assertFalse((self.archive.state_dir / "preprint_downloads").exists())
        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_unmatched_selection_skips_preliminary_matches(self) -> None:
        """Явный отбор исключает уже сопоставленные карточки, включая предварительные."""

        items = self._cache_items([[OTHER_PREPRINT_URL], [PREPRINT_URL]])
        matches_path = self.root / "matches.jsonl"
        matches_path.write_text(
            json.dumps({
                "matched_item": {"item_uuid": items[0]["uuid"]},
                "match_status": "preliminary",
            }) + "\n",
            encoding="utf-8",
        )
        self._allow_pdf_response()

        self.assertEqual(collect_preprints(
            self.archive, max_items=0, unmatched_only=True,
            matches_path=matches_path, download=True,
        ), 1)

        self.client.get_preprint_response.assert_called_once_with(PREPRINT_URL)
        result_dir = self.archive.state_dir / "preprint_downloads"
        self.assertFalse((result_dir / f"{items[0]['uuid']}.json").exists())
        self.assertTrue((result_dir / f"{items[1]['uuid']}.json").is_file())

    def test_download_records_provenance_without_invented_identifiers(self) -> None:
        """Производная запись ссылается на метаданные и PDF, не выдумывая реестры."""

        item = self._cache_items([[PREPRINT_URL]])[0]
        self._allow_pdf_response()

        self.assertEqual(collect_preprints(self.archive, download=True), 1)
        result_path = self.archive.state_dir / "preprint_downloads" / f"{item['uuid']}.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(result["schema_version"], "jinr-preprint-download-v1")
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(result["item_uuid"], item["uuid"])
        self.assertEqual(result["item_title"], item["metadata"]["dc.title"][0]["value"])
        self.assertEqual(result["source_field"], "local.publication.uri")
        self.assertEqual(result["metadata_sources"], [self.archive.json_evidence(self.search_url)])
        self.assertEqual(result["files"], [self.archive.pdf_evidence(PREPRINT_URL)])

        for key in ("bitstream_uuid", "bundle_uuid", "work_id", "rights", "rights_id"):
            self.assertNotIn(key, result)
            self.assertNotIn(key, result["files"][0])

        self.assertFalse((self.archive.state_dir / "file_lists").exists())
        self.client.get_response.assert_not_called()

    def test_duplicate_urls_are_downloaded_once_across_items(self) -> None:
        """Одинаковая ссылка в одной и нескольких карточках использует общий кеш."""

        items = self._cache_items([[PREPRINT_URL, PREPRINT_URL], [PREPRINT_URL]])
        self._allow_pdf_response()

        self.assertEqual(collect_preprints(self.archive, max_items=0, download=True), 2)
        self.client.get_preprint_response.assert_called_once_with(PREPRINT_URL)

        for item in items:
            result_path = self.archive.state_dir / "preprint_downloads" / f"{item['uuid']}.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["files"], [self.archive.pdf_evidence(PREPRINT_URL)])

    def test_repeat_rebuilds_result_without_touching_file_lists(self) -> None:
        """Повтор восстанавливает производную запись из кеша и сохраняет перечни API."""

        item = self._cache_items([[PREPRINT_URL]])[0]
        file_list_path = self.archive.state_dir / "file_lists" / f"{item['uuid']}.json"
        file_list_path.parent.mkdir()
        file_list_bytes = b'{"status":"incomplete","files":[]}\n'
        file_list_path.write_bytes(file_list_bytes)
        self._allow_pdf_response()
        self.assertEqual(collect_preprints(self.archive, download=True), 1)
        result_path = self.archive.state_dir / "preprint_downloads" / f"{item['uuid']}.json"
        expected = result_path.read_bytes()
        result_path.write_text('{"stale":true}', encoding="utf-8")
        self.client.get_preprint_response.side_effect = AssertionError("Повтор вышел в сеть")

        self.assertEqual(collect_preprints(self.archive, download=True), 1)

        self.assertEqual(result_path.read_bytes(), expected)
        self.assertEqual(file_list_path.read_bytes(), file_list_bytes)
        self.client.get_preprint_response.assert_called_once_with(PREPRINT_URL)
        self.client.get_response.assert_not_called()

    def test_failed_second_file_does_not_mark_item_downloaded(self) -> None:
        """Карточка завершена только после всех PDF; повтор сохраняет удачный кеш."""

        item = self._cache_items([[PREPRINT_URL, OTHER_PREPRINT_URL]])[0]
        self.client.get_preprint_response.side_effect = [
            self._pdf_response(), JinrApiError("HTTP 403"),
        ]

        with self.assertRaisesRegex(JinrApiError, "HTTP 403"):
            collect_preprints(self.archive, download=True)

        result_path = self.archive.state_dir / "preprint_downloads" / f"{item['uuid']}.json"
        self.assertFalse(result_path.exists())
        self.assertEqual(Path(self.archive.pdf_evidence(PREPRINT_URL)["body_path"]).read_bytes(), PDF_BODY)
        self.client.get_preprint_response.reset_mock()
        self._allow_pdf_response(OTHER_PREPRINT_URL)

        self.assertEqual(collect_preprints(self.archive, download=True), 1)

        self.client.get_preprint_response.assert_called_once_with(OTHER_PREPRINT_URL)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["files"], [
            self.archive.pdf_evidence(PREPRINT_URL),
            self.archive.pdf_evidence(OTHER_PREPRINT_URL),
        ])
        self.client.get_response.assert_not_called()

    def test_http_failure_stops_batch_and_preserves_finished_item(self) -> None:
        """Отказы 401, 403 и 429 не повторяются и не скрываются продолжением обхода."""

        for status_code in (401, 403, 429):
            with self.subTest(status_code=status_code):
                archive = JinrArchive(
                    self.root / f"raw-{status_code}",
                    self.root / f"state-{status_code}",
                    self.client,
                )
                self._cache_items([[PREPRINT_URL], [OTHER_PREPRINT_URL], [PREPRINT_URL]])
                metadata = self.archive.cached_json(self.search_url)
                archive._save(_response(self.search_url, json.dumps(metadata).encode()), "json")
                self.client.get_preprint_response.reset_mock()
                self.client.get_preprint_response.side_effect = [
                    self._pdf_response(), JinrApiError(f"HTTP {status_code}"),
                ]

                with self.assertRaisesRegex(JinrApiError, f"HTTP {status_code}"):
                    collect_preprints(archive, max_items=0, download=True)

                result_paths = list((archive.state_dir / "preprint_downloads").glob("*.json"))
                self.assertEqual([path.name for path in result_paths], ["11111111-2222-4333-8444-000000000000.json"])
                self.assertEqual(self.client.get_preprint_response.call_count, 2)
                self.assertEqual(archive.pdf_evidence(PREPRINT_URL)["body_sha256"], hashlib.sha256(PDF_BODY).hexdigest())

        self.client.get_response.assert_not_called()

    def test_negative_limit_fails_before_transport(self) -> None:
        """Отрицательный лимит не допускается независимо от наличия кеша."""

        with self.assertRaises(ValueError):
            collect_preprints(self.archive, max_items=-1, download=True)

        self.client.get_preprint_response.assert_not_called()
        self.client.get_response.assert_not_called()

    def test_cli_routes_preprint_options_with_explicit_download(self) -> None:
        """Команда передаёт явное скачивание и параметры отбора отдельному обработчику."""

        matches_path = self.root / "matches.jsonl"

        with (
            patch("scripts.download_jinr.JinrApiClient"),
            patch("scripts.download_jinr.JinrArchive") as archive_class,
            patch("scripts.download_jinr.collect_preprints", return_value=2) as collector,
        ):
            exit_code = main([
                "--state-dir", str(self.root / "state"),
                "--output-dir", str(self.root / "raw"),
                "preprints", "--unmatched-only", "--max-items", "0", "--download",
                "--matches", str(matches_path),
            ])

        self.assertEqual(exit_code, 0)
        collector.assert_called_once_with(
            archive_class.return_value,
            max_items=0,
            unmatched_only=True,
            matches_path=matches_path,
            download=True,
        )
        archive_class.return_value.download_pdf.assert_not_called()

    def test_cli_defaults_to_one_offline_candidate(self) -> None:
        """Команда без флагов выбирает одну карточку и не включает скачивание."""

        with (
            patch("scripts.download_jinr.JinrApiClient"),
            patch("scripts.download_jinr.JinrArchive"),
            patch("scripts.download_jinr.collect_preprints", return_value=0) as collector,
        ):
            self.assertEqual(main(["preprints"]), 0)

        self.assertEqual(collector.call_args.kwargs["max_items"], 1)
        self.assertFalse(collector.call_args.kwargs["unmatched_only"])
        self.assertFalse(collector.call_args.kwargs["download"])


if __name__ == "__main__":
    unittest.main()
