"""Проверки неизменяемых ответов Иоффе, автономного кеша и HTTP-свидетельств."""

from __future__ import annotations

import fcntl
import hashlib
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from src.collect.base import HttpResponseSnapshot
from src.collect.ioffe_archive import IoffeArchive, write_json
from src.collect.ioffe_http import IoffeError

URL = "https://journals.ioffe.ru/articles/51681"


class IoffeArchiveTests(unittest.TestCase):
    """Проверять сохранение до разбора, целостность и отсутствие повторного скачивания."""

    def setUp(self) -> None:
        """Создать временные каталоги и поддельный клиент HTTP."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output_dir = self.root / "raw"
        self.state_dir = self.root / "state"
        self.client = MagicMock()
        self.snapshot = HttpResponseSnapshot(
            requested_url=URL,
            final_url=URL,
            status_code=200,
            headers=(("content-type", "text/html; charset=utf-8"),),
            retrieved_at="2026-10-12T12:00:00+00:00",
            body=b"<html>article</html>",
        )
        self.client.get_response.return_value = self.snapshot
        self.archive = IoffeArchive(self.output_dir, self.state_dir, self.client, allow_network=True)

    def _cache_path(self) -> Path:
        """Вернуть путь указателя кеша для тестовой статьи."""

        return self.state_dir / "cache" / f"{hashlib.sha256(URL.encode()).hexdigest()}.json"

    def test_missing_cache_does_not_enable_network(self) -> None:
        """Без явного флага даже отсутствие кеша не разрешает сетевой запрос."""

        archive = IoffeArchive(self.output_dir, self.state_dir, self.client)

        with self.assertRaisesRegex(IoffeError, "allow_network"):
            archive.get_response(URL)

        self.client.get_response.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_exact_response_is_saved_and_reused_by_new_instance(self) -> None:
        """Повторный запуск читает проверенные байты без вызова сети."""

        self.assertEqual(self.archive.get_response(URL), self.snapshot)
        record = self.archive.evidence(URL)
        self.assertEqual((self.output_dir / record["relative_path"]).read_bytes(), self.snapshot.body)
        self.assertEqual((self.output_dir / record["response_metadata_path"]).read_bytes(), self.snapshot.canonical_metadata())
        second = IoffeArchive(self.output_dir, self.state_dir, self.client)
        self.assertEqual(second.get_response(URL), self.snapshot)
        self.assertEqual(self.client.get_response.call_count, 1)

    def test_refresh_is_explicit_and_keeps_previous_bytes(self) -> None:
        """Обновление добавляет новый снимок, а не заменяет прежние исходные байты."""

        self.archive.get_response(URL)
        previous_record = self.archive.evidence(URL)
        updated = replace(self.snapshot, body=b"changed", retrieved_at="2026-10-12T12:03:00+00:00")
        self.client.get_response.return_value = updated
        self.assertEqual(self.archive.get_response(URL), self.snapshot)
        self.assertEqual(self.archive.get_response(URL, refresh=True), updated)
        self.assertEqual((self.output_dir / previous_record["relative_path"]).read_bytes(), self.snapshot.body)
        self.assertEqual(self.client.get_response.call_count, 2)

    def test_snapshot_evidence_remains_bound_after_refresh(self) -> None:
        """Свидетельство старого снимка не подменяется новым и не откатывает кеш."""

        previous = self.archive.get_response(URL)
        previous_record = self.archive.evidence(URL)
        updated = replace(previous, body=b"new metadata", retrieved_at="2026-10-12T12:03:00+00:00")
        self.client.get_response.return_value = updated
        self.archive.get_response(URL, refresh=True)
        latest_cache = self._cache_path().read_bytes()
        latest_modified_at = self._cache_path().stat().st_mtime_ns

        self.assertEqual(self.archive.evidence_for(previous), previous_record)
        self.assertEqual(self._cache_path().read_bytes(), latest_cache)
        self.assertEqual(self._cache_path().stat().st_mtime_ns, latest_modified_at)
        self.assertEqual(self.archive.get_response(URL), updated)
        self.assertEqual(self.archive.evidence_for(updated), self.archive.evidence(URL))
        self.assertEqual(self.client.get_response.call_count, 2)

    def test_evidence_for_unsaved_snapshot_does_not_create_files(self) -> None:
        """Проверка отсутствующего снимка не импортирует его и не обращается к сети."""

        with self.assertRaisesRegex(IoffeError, "Не найдено сохранённое"):
            self.archive.evidence_for(self.snapshot)

        self.assertFalse(self.output_dir.exists())
        self.assertFalse(self.state_dir.exists())
        self.client.get_response.assert_not_called()

    def test_snapshot_evidence_does_not_require_latest_cache(self) -> None:
        """Неизменяемые исходники достаточно проверяемы даже без указателя latest."""

        self.archive.import_response(self.snapshot)
        record = self.archive.evidence(URL)
        self._cache_path().unlink()

        self.assertEqual(self.archive.evidence_for(self.snapshot), record)
        self.assertFalse(self._cache_path().exists())
        self.client.get_response.assert_not_called()

    def test_snapshot_evidence_rejects_corrupted_immutable_files(self) -> None:
        """Тело и канонический JSON проверяются по точным байтам, а не наличию пути."""

        self.archive.import_response(self.snapshot)
        record = self.archive.evidence(URL)
        latest_cache = self._cache_path().read_bytes()

        for field in ("relative_path", "response_metadata_path"):
            with self.subTest(field=field):
                path = self.output_dir / record[field]
                original = path.read_bytes()
                path.write_bytes(b"corrupted")

                with self.assertRaisesRegex(IoffeError, "Повреждено свидетельство"):
                    self.archive.evidence_for(self.snapshot)

                self.assertEqual(self._cache_path().read_bytes(), latest_cache)
                path.write_bytes(original)

        self.client.get_response.assert_not_called()

    def test_snapshot_evidence_rejects_other_url_or_timestamp(self) -> None:
        """Совпадающее тело не заменяет свидетельство конкретного URL и момента."""

        self.archive.import_response(self.snapshot)
        other_url = "https://journals.ioffe.ru/articles/51682"

        for snapshot in (
            replace(self.snapshot, requested_url=other_url, final_url=other_url),
            replace(self.snapshot, retrieved_at="2026-10-12T12:03:00+00:00"),
        ):
            with self.subTest(snapshot=snapshot):
                with self.assertRaisesRegex(IoffeError, "Не найдено сохранённое"):
                    self.archive.evidence_for(snapshot)

        self.assertEqual(self.archive.get_response(URL), self.snapshot)
        self.client.get_response.assert_not_called()

    def test_snapshot_evidence_requires_canonical_metadata(self) -> None:
        """JSON с теми же полями, но другими байтами не считается исходным снимком."""

        self.archive.import_response(self.snapshot)
        record = self.archive.evidence(URL)
        metadata_path = self.output_dir / record["response_metadata_path"]
        metadata = json.loads(metadata_path.read_bytes())
        metadata_path.write_text(json.dumps(metadata, indent=2))

        with self.assertRaisesRegex(IoffeError, "Повреждено свидетельство"):
            self.archive.evidence_for(self.snapshot)

        self.client.get_response.assert_not_called()

    def test_snapshot_evidence_rejects_redirected_origin(self) -> None:
        """Нельзя подтвердить теми же байтами иной конечный адрес ответа."""

        self.archive.import_response(self.snapshot)

        with self.assertRaisesRegex(IoffeError, "исходному адресу"):
            self.archive.evidence_for(replace(self.snapshot, final_url="https://example.test/"))

        self.client.get_response.assert_not_called()

    def test_offline_refresh_is_forbidden_even_with_cache(self) -> None:
        """Флаг обновления не отменяет запрет на сеть."""

        self.archive.import_response(self.snapshot)
        archive = IoffeArchive(self.output_dir, self.state_dir, self.client)

        with self.assertRaises(IoffeError):
            archive.get_response(URL, refresh=True)

        self.client.get_response.assert_not_called()

    def test_http_failure_is_saved_but_not_cached(self) -> None:
        """Отказ доступа сохраняется свидетельством и не становится успешным кешем."""

        failure = replace(self.snapshot, status_code=403, body=b"Forbidden")
        self.client.get_response.return_value = failure

        with self.assertRaisesRegex(IoffeError, "HTTP 403.*Свидетельство"):
            self.archive.get_response(URL)

        self.assertFalse(self._cache_path().exists())
        body_paths = list(self.output_dir.rglob("*.html"))
        self.assertEqual(len(body_paths), 1)
        self.assertEqual(body_paths[0].read_bytes(), failure.body)
        self.assertEqual(len(list(self.state_dir.glob("attempts/*.json"))), 1)

    def test_transport_failure_does_not_publish_partial_body(self) -> None:
        """Сетевой обрыв не создаёт ни тело, ни указатель кеша."""

        self.client.get_response.side_effect = IoffeError("Неполный ответ")

        with self.assertRaises(IoffeError):
            self.archive.get_response(URL)

        self.assertFalse(self.output_dir.exists())
        self.assertFalse(self._cache_path().exists())

    def test_corrupt_body_and_metadata_stop_without_network(self) -> None:
        """Нарушение SHA-256 не запускает незаметную повторную загрузку."""

        for field in ("relative_path", "response_metadata_path"):
            with self.subTest(field=field):
                self.archive.import_response(self.snapshot)
                record = self.archive.evidence(URL)
                path = self.output_dir / record[field]
                original = path.read_bytes()
                path.write_bytes(b"corrupted")

                with self.assertRaisesRegex(IoffeError, "Повреждён кеш"):
                    self.archive.get_response(URL, refresh=True)

                path.write_bytes(original)

        self.client.get_response.assert_not_called()

    def test_cache_url_path_and_size_must_match(self) -> None:
        """Подмена URL, пути и размера не проходит проверку локального кеша."""

        self.archive.import_response(self.snapshot)
        original = json.loads(self._cache_path().read_text())

        for field, value in (("requested_url", "https://journals.ioffe.ru/"), ("relative_path", "../../outside"), ("bytes", True)):
            with self.subTest(field=field):
                changed = dict(original)
                changed[field] = value
                self._cache_path().write_text(json.dumps(changed))

                with self.assertRaises(IoffeError):
                    self.archive.get_response(URL)

        self.client.get_response.assert_not_called()

    def test_snapshot_rejects_secrets_redirect_and_partial_body(self) -> None:
        """Импорт не принимает секретные заголовки, другой адрес и неполный ответ."""

        for snapshot in (
            replace(self.snapshot, final_url="https://example.com/"),
            replace(self.snapshot, requested_url="https://journals.ioffe.ru/"),
            replace(self.snapshot, headers=(("set-cookie", "secret"),)),
            replace(self.snapshot, headers=(("content-length", "100"),)),
            replace(self.snapshot, retrieved_at="2026-10-12T12:00:00"),
        ):
            with self.subTest(snapshot=snapshot), self.assertRaises(IoffeError):
                self.client.get_response.return_value = snapshot
                self.archive.get_response(URL)

        self.assertFalse(self.output_dir.exists())

    def test_symbolic_link_cannot_escape_archive(self) -> None:
        """Подставленный каталог responses не может направить запись за пределы архива."""

        self.output_dir.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.output_dir / "responses").symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(IoffeError, "Недопустимый путь"):
            self.archive.import_response(self.snapshot)

        self.assertEqual(list(outside.iterdir()), [])

    def test_pdf_extension_requires_pdf_signature(self) -> None:
        """Ответ с MIME PDF, но страницей ошибки внутри сохраняется как bin."""

        disguised = replace(self.snapshot, headers=(("content-type", "application/pdf"),))
        self.archive.import_response(disguised)
        self.assertTrue(self.archive.evidence(URL)["relative_path"].endswith(".bin"))

        pdf = replace(disguised, body=b"%PDF-1.7\nexample")
        self.archive.import_response(pdf)
        self.assertTrue(self.archive.evidence(URL)["relative_path"].endswith(".pdf"))

    def test_derived_json_does_not_rewrite_unchanged_bytes(self) -> None:
        """Повторная запись неизменённого производного JSON сохраняет время файла."""

        path = self.state_dir / "summary.json"
        write_json(path, {"articles": 1})
        modified_at = path.stat().st_mtime_ns
        write_json(path, {"articles": 1})
        self.assertEqual(path.stat().st_mtime_ns, modified_at)
        write_json(path, {"articles": 2})
        self.assertEqual(json.loads(path.read_text()), {"articles": 2})

    def test_failed_refresh_keeps_successful_cache_but_reports_failure(self) -> None:
        """Неуспешный refresh не подменяет ответ старым кешем и сохраняет обе версии."""

        self.archive.import_response(self.snapshot)
        self.client.get_response.return_value = replace(self.snapshot, status_code=503, body=b"unavailable")

        with self.assertRaisesRegex(IoffeError, "HTTP 503"):
            self.archive.get_response(URL, refresh=True)

        self.assertEqual(self.archive.get_response(URL), self.snapshot)
        self.assertEqual(len(list(self.output_dir.rglob("*.http.json"))), 2)
        self.assertEqual(self.client.get_response.call_count, 1)

    def test_cache_symlink_cannot_escape_state_directory(self) -> None:
        """Указатель кеша вне каталога состояния не читается как доверенная запись."""

        self.archive.import_response(self.snapshot)
        outside = self.root / "external_cache.json"
        outside.write_bytes(self._cache_path().read_bytes())
        self._cache_path().unlink()
        self._cache_path().symlink_to(outside)

        with self.assertRaisesRegex(IoffeError, "Недопустимый путь"):
            self.archive.get_response(URL)

        self.client.get_response.assert_not_called()

    def test_archive_lock_covers_network_and_cache_publication(self) -> None:
        """Во время загрузки другой процесс не может пройти проверку отсутствующего кеша."""

        def get_response(url: str) -> HttpResponseSnapshot:
            """Проверить реальную блокировку архива из отдельного файлового дескриптора."""

            self.assertEqual(url, URL)

            with (
                (self.state_dir / "ioffe_archive.lock").open("a+b") as second_lock,
                self.assertRaises(BlockingIOError),
            ):
                fcntl.flock(second_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            return self.snapshot

        self.client.get_response.side_effect = get_response
        self.archive.get_response(URL)
        second = IoffeArchive(self.output_dir, self.state_dir, self.client, allow_network=True)
        second.get_response(URL)
        self.assertEqual(self.client.get_response.call_count, 1)

        with (self.state_dir / "ioffe_archive.lock").open("a+b") as released_lock:
            fcntl.flock(released_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(released_lock.fileno(), fcntl.LOCK_UN)
