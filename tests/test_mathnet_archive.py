"""Проверки кеша Math-Net и сохранения исходных ответов без сетевых запросов."""

from __future__ import annotations

import fcntl
import hashlib
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from src.collect.base import HttpResponseSnapshot
from src.collect.mathnet_api import DEFAULT_API_URL, MathnetApiClient, MathnetApiError
from src.collect.mathnet_archive import MathnetArchive, MathnetResult, _write_bytes

JOURNALS_URL = f"{DEFAULT_API_URL}/journals/list"
TEXT_URL = f"{DEFAULT_API_URL}/texts/test123"


class MathnetArchiveTests(unittest.TestCase):
    """Проверки автономного режима, целостности и отсутствия скрытых повторов."""

    def setUp(self) -> None:
        """Создать отдельные каталоги и транспорт, не открывающий соединений."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.output_dir = self.root / "raw"
        self.state_dir = self.root / "state"
        self.client = MagicMock(spec=MathnetApiClient)
        self.client.get_response.return_value = self._snapshot()
        self.archive = MathnetArchive(self.output_dir, self.state_dir, self.client)

        network_guard = patch(
            "src.collect.mathnet_api.urllib.request.build_opener",
            side_effect=AssertionError("Настоящий сетевой транспорт в тесте запрещён"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def _snapshot(
        self,
        body: bytes = b'[{"jrnid":"test"}]',
        *,
        url: str = JOURNALS_URL,
        content_type: str = "application/json",
    ) -> HttpResponseSnapshot:
        """Построить синтетическое HTTP-свидетельство с заданными исходными байтами."""

        return HttpResponseSnapshot(
            requested_url=url,
            final_url=url,
            status_code=200,
            headers=(("content-type", content_type),),
            retrieved_at="2026-10-02T09:00:00+00:00",
            body=body,
        )

    def _fetch(self) -> MathnetResult:
        """Выполнить один запрос к поддельному клиенту с явным разрешением."""

        return self.archive.fetch(JOURNALS_URL, kind="json", allow_network=True)

    def _cache_path(self) -> Path:
        """Получить индекс единственного синтетического запроса."""

        return self.state_dir / "cache" / f"{hashlib.sha256(JOURNALS_URL.encode()).hexdigest()}.json"

    def _events(self) -> list[dict[str, Any]]:
        """Прочитать неизменяемые исходы запросов без зависимости от имён файлов."""

        return [json.loads(path.read_text()) for path in (self.state_dir / "attempts").glob("*.json")]

    def test_constructor_has_no_side_effects(self) -> None:
        """Создание архива не создаёт файлы и не обращается к клиенту."""

        self.assertFalse(self.state_dir.exists())
        self.assertFalse(self.output_dir.exists())
        self.client.get_response.assert_not_called()

    def test_cache_miss_is_offline_by_default(self) -> None:
        """Отсутствующий кеш не запускает загрузку без явного разрешения."""

        with self.assertRaisesRegex(ValueError, "allow-network"):
            self.archive.fetch(JOURNALS_URL, kind="json")

        self.client.get_response.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_invalid_arguments_fail_before_side_effects(self) -> None:
        """Адрес, режим обновления и кодировка проверяются до создания состояния."""

        cases: tuple[dict[str, Any], ...] = (
            {"url": "https://example.org/api/journals/list", "kind": "json"},
            {"url": JOURNALS_URL, "kind": "pdf"},
            {"url": JOURNALS_URL, "kind": "json", "refresh": True},
            {"url": JOURNALS_URL, "kind": "json", "encoding": "utf-8"},
            {"url": TEXT_URL, "kind": "text", "encoding": "no-such-codec"},
        )

        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.archive.fetch(**arguments)

        self.assertFalse(self.state_dir.exists())
        self.client.get_response.assert_not_called()

    def test_json_list_saved_exactly_and_reused_offline(self) -> None:
        """Повторный запуск проверяет исходные байты, не делая второй запрос."""

        result = self._fetch()
        self.assertFalse(result.from_cache)
        self.assertEqual(result.data, [{"jrnid": "test"}])
        self.assertEqual(result.body_path.read_bytes(), self.client.get_response.return_value.body)
        original_time = result.body_path.stat().st_mtime_ns
        metadata = json.loads(result.metadata_path.read_bytes())
        self.assertEqual(metadata["requested_url"], JOURNALS_URL)

        cached = MathnetArchive(self.output_dir, self.state_dir).fetch(JOURNALS_URL, kind="json")

        self.assertTrue(cached.from_cache)
        self.assertEqual(cached.data, result.data)
        self.assertEqual(result.body_path.stat().st_mtime_ns, original_time)
        self.client.get_response.assert_called_once_with(JOURNALS_URL)
        self.assertEqual(len(self._events()), 1)

    def test_json_object_and_empty_list_are_valid_envelopes(self) -> None:
        """Архив допускает объекты и списки, не утверждая полноту коллекции."""

        for body in (b'{"title":"article","unknown":1}', b"[]", b"{}"):
            with self.subTest(body=body):
                self.client.get_response.return_value = self._snapshot(body)
                result = self.archive.fetch(JOURNALS_URL, kind="json", allow_network=True, refresh=True)
                self.assertEqual(result.data, json.loads(body))

    def test_plain_text_preserves_encoding_and_original_bytes(self) -> None:
        """Кодировка из Content-Type декодирует текст без изменения исходного файла."""

        body = "Физический текст\r\n".encode("cp1251")
        self.client.get_response.return_value = self._snapshot(
            body, url=TEXT_URL, content_type="text/plain; charset=windows-1251",
        )
        result = self.archive.fetch(TEXT_URL, kind="text", allow_network=True)

        self.assertEqual(result.data, "Физический текст\r\n")
        self.assertEqual(result.body_path.read_bytes(), body)

    def test_explicit_encoding_does_not_rewrite_response(self) -> None:
        """Явное указание кодировки применяется только к чтению текста."""

        body = "Аннотация".encode("cp1251")
        self.client.get_response.return_value = self._snapshot(body, url=TEXT_URL, content_type="text/plain")
        result = self.archive.fetch(TEXT_URL, kind="text", allow_network=True, encoding="cp1251")
        cached = self.archive.fetch(TEXT_URL, kind="text")

        self.assertEqual(result.data, "Аннотация")
        self.assertEqual(cached.data, result.data)
        self.assertEqual(result.body_path.read_bytes(), body)
        self.client.get_response.assert_called_once()

    def test_rejected_encoding_can_be_corrected_offline(self) -> None:
        """Ошибку кодировки можно исправить по исходным байтам без второго запроса."""

        body = "Русский текст".encode("cp1251")
        self.client.get_response.return_value = self._snapshot(body, url=TEXT_URL, content_type="text/plain")

        with self.assertRaisesRegex(ValueError, "encoding"):
            self.archive.fetch(TEXT_URL, kind="text", allow_network=True)

        corrected = MathnetArchive(self.output_dir, self.state_dir).fetch(
            TEXT_URL, kind="text", encoding="cp1251",
        )
        cached = self.archive.fetch(TEXT_URL, kind="text")

        self.assertTrue(corrected.from_cache)
        self.assertEqual(corrected.data, "Русский текст")
        self.assertEqual(cached.data, corrected.data)
        self.assertEqual(corrected.body_path.read_bytes(), body)
        self.client.get_response.assert_called_once_with(TEXT_URL)
        self.assertEqual(len(self._events()), 1)

    def test_unknown_format_saved_for_inspection_not_accepted(self) -> None:
        """Ответ HTTP 200 с HTML сохраняется как свидетельство, но не как успешный кеш."""

        self.client.get_response.return_value = self._snapshot(b"<html>Denied</html>", content_type="text/html")

        with self.assertRaisesRegex(ValueError, "MIME"):
            self._fetch()

        self.assertFalse(self._cache_path().exists())
        self.assertEqual(len(list((self.output_dir / "responses").rglob("*.bin"))), 1)
        self.assertEqual(self._events()[0]["status"], "response_rejected")

    def test_json_scalars_errors_and_invalid_content_are_rejected(self) -> None:
        """Сообщения об ошибке и некорректный JSON не считаются карточками."""

        bodies = (b"null", b'"error"', b"false", b"broken", b"{\"value\":NaN}", b'{"error":"denied"}')

        for body in bodies:
            with self.subTest(body=body):
                self.client.get_response.return_value = self._snapshot(body)

                with self.assertRaises(ValueError):
                    self.archive.fetch(JOURNALS_URL, kind="json", allow_network=True, refresh=True)

        self.assertFalse(self._cache_path().exists())

    def test_empty_html_binary_and_unknown_encoding_text_are_rejected(self) -> None:
        """Пустой текст, HTML под чужим MIME и неподдержанная кодировка не принимаются."""

        cases = (
            (b"  \r\n", "text/plain"),
            (b"<!DOCTYPE html><html>Denied</html>", "text/plain"),
            (b"text\x00", "text/plain"),
            (b"text", "text/plain; charset=not-a-codec"),
            ("Текст".encode("cp1251"), "text/plain"),
            (b'{"error":"denied"}', "application/json"),
        )

        for body, content_type in cases:
            with self.subTest(body=body, content_type=content_type):
                self.client.get_response.return_value = self._snapshot(body, url=TEXT_URL, content_type=content_type)

                with self.assertRaises(ValueError):
                    self.archive.fetch(TEXT_URL, kind="text", allow_network=True, refresh=True)

    def test_corrupted_body_is_not_silently_downloaded_again(self) -> None:
        """Повреждённый кеш останавливает даже запрос с разрешённой сетью."""

        result = self._fetch()
        result.body_path.write_bytes(b"tampered")

        for refresh in (False, True):
            with self.subTest(refresh=refresh), self.assertRaisesRegex(ValueError, "целостность"):
                self.archive.fetch(JOURNALS_URL, kind="json", allow_network=True, refresh=refresh)

        self.client.get_response.assert_called_once()

    def test_corrupted_metadata_is_not_accepted(self) -> None:
        """HTTP-свидетельство также защищено контрольной суммой."""

        result = self._fetch()
        result.metadata_path.write_bytes(b"{}")

        with self.assertRaisesRegex(ValueError, "целостность"):
            self.archive.fetch(JOURNALS_URL, kind="json")

    def test_forged_cache_digest_cannot_escape_archive(self) -> None:
        """Внешний путь нельзя подставить вместо контрольной суммы файла."""

        self._fetch()
        record = json.loads(self._cache_path().read_bytes())
        record["body_sha256"] = "../../outside"
        self._cache_path().write_text(json.dumps(record))

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.archive.fetch(JOURNALS_URL, kind="json")

    def test_symlink_body_is_rejected(self) -> None:
        """Даже совпадающие байты внешнего файла не принимаются через символическую ссылку."""

        result = self._fetch()
        outside = self.root / "outside.bin"
        outside.write_bytes(result.body_path.read_bytes())
        result.body_path.unlink()
        result.body_path.symlink_to(outside)

        with self.assertRaisesRegex(ValueError, "пределы|ссылкой"):
            self.archive.fetch(JOURNALS_URL, kind="json")

    def test_response_status_url_and_timestamp_are_checked(self) -> None:
        """Поддельный транспорт не может сохранить ответ другого ресурса или частичный ответ."""

        snapshot = self._snapshot()
        cases = (
            replace(snapshot, status_code=206),
            replace(snapshot, final_url="https://example.org/"),
            replace(snapshot, retrieved_at="2026-10-02T12:00:00"),
            replace(snapshot, headers=(("set-cookie", "secret"),)),
        )

        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.client.get_response.return_value = candidate

                with self.assertRaises(ValueError):
                    self._fetch()

        self.assertFalse(self._cache_path().exists())

    def test_refresh_retains_old_bytes_and_evidence(self) -> None:
        """Явное обновление меняет индекс, но сохраняет прежние тела и свидетельства."""

        first = self._fetch()
        original_metadata = first.metadata_path.read_bytes()
        self.client.get_response.return_value = self._snapshot(b'[{"jrnid":"next"}]')
        second = self.archive.fetch(JOURNALS_URL, kind="json", allow_network=True, refresh=True)

        self.assertNotEqual(first.body_path, second.body_path)
        self.assertTrue(first.body_path.exists())
        self.assertEqual(first.metadata_path.read_bytes(), original_metadata)
        self.assertEqual(self.archive.fetch(JOURNALS_URL, kind="json").data, second.data)

    def test_failed_refresh_raises_instead_of_using_old_cache(self) -> None:
        """HTTP-ошибка не подменяется молчаливым возвратом устаревшей копии."""

        first = self._fetch()
        previous_index = self._cache_path().read_bytes()
        self.client.get_response.side_effect = MathnetApiError("HTTP 403")

        with self.assertRaisesRegex(MathnetApiError, "403"):
            self.archive.fetch(JOURNALS_URL, kind="json", allow_network=True, refresh=True)

        self.assertEqual(self._cache_path().read_bytes(), previous_index)
        self.assertEqual(self.archive.fetch(JOURNALS_URL, kind="json").data, first.data)
        self.assertIn("request_failed", [event["status"] for event in self._events()])

    def test_error_journal_preserves_http_status_without_message(self) -> None:
        """Журнал различает HTTP-ошибки, не сохраняя возможные секреты из их текста."""

        self.client.get_response.side_effect = MathnetApiError(
            "HTTP 403 private diagnostic", http_status=403, error_code="http_error",
        )

        with self.assertRaises(MathnetApiError):
            self._fetch()

        event = self._events()[0]
        self.assertEqual(event["status"], "request_failed")
        self.assertEqual(event["http_status"], 403)
        self.assertEqual(event["error_code"], "http_error")
        self.assertNotIn("private diagnostic", json.dumps(event))

    def test_bad_refresh_keeps_previous_index_and_new_raw_response(self) -> None:
        """Новый неожиданный формат не заменяет успешный кеш прежнего ответа."""

        self._fetch()
        previous_index = self._cache_path().read_bytes()
        self.client.get_response.return_value = self._snapshot(b"broken")

        with self.assertRaises(ValueError):
            self.archive.fetch(JOURNALS_URL, kind="json", allow_network=True, refresh=True)

        self.assertEqual(self._cache_path().read_bytes(), previous_index)
        self.assertEqual(len(list((self.output_dir / "responses").rglob("*.bin"))), 2)

    def test_concurrent_archive_is_rejected_without_network(self) -> None:
        """Параллельный процесс не проходит проверку кеша и не запускает второй запрос."""

        self.state_dir.mkdir()

        with (self.state_dir / "mathnet_archive.lock").open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            try:
                with self.assertRaisesRegex(MathnetApiError, "Другой процесс"):
                    self._fetch()

            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        self.client.get_response.assert_not_called()

    def test_immutable_writer_keeps_old_content(self) -> None:
        """Публикация файла не перезаписывает существующее неизменяемое содержимое."""

        path = self.root / "immutable.bin"
        _write_bytes(path, b"old", immutable=True)

        with self.assertRaisesRegex(ValueError, "Нельзя заменить"):
            _write_bytes(path, b"new", immutable=True)

        self.assertEqual(path.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
