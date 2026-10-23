"""Проверки локальной очереди архивов без сети и запуска внешних программ."""

from __future__ import annotations

import json
import tempfile
import unittest

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from src.collect import jetp
from src.collect.base import HttpResponseSnapshot
from src.collect.collection_queue import (
    MAX_QUEUE_BYTES,
    TASK_STATUSES,
    build_queue,
    load_queue,
    save_queue,
    validate_queue,
)
from src.collect.ioffe import IOFFE_JOURNALS
from src.collect.ioffe_archive import IoffeArchive
from src.collect.ioffe_http import IoffeError
from src.collect.journal_sources import JournalSource

FORM_HTML = b"""
<html><body><form action="/cgi-bin/r/index" method="POST">
<input type="hidden" name="a" value="select">
<input type="hidden" name="issues_lang" value="r">
<select name="volume"><option value="169">169</option><option value="171">171</option>
<option value="28">28</option></select>
<input type="image" src="/img/r/go_vol.png"></form></body></html>
"""
RUN_ID = "a" * 32


class CollectionQueueTests(unittest.TestCase):
    """Проверить происхождение плана, строгую схему и безопасное сохранение состояния."""

    def setUp(self) -> None:
        """Подготовить настоящий локальный кеш формы с транспортом без сети."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        source = JournalSource(jetp)
        self.archive = IoffeArchive(
            self.root / "data" / "raw" / "journals" / "jetp",
            self.root / "manifests" / "imports" / "journals" / "jetp",
            MagicMock(), validate_url=source.validate_url,
        )
        self.snapshot = HttpResponseSnapshot(
            requested_url=jetp.START_URL, final_url=jetp.START_URL, status_code=200,
            headers=(("content-type", "text/html; charset=koi8-r"),),
            retrieved_at="2026-10-16T12:00:00+00:00", body=FORM_HTML,
        )
        self.archive.import_response(self.snapshot)
        self.queue = build_queue(self.root, ioffe_origin="http://journals.ioffe.ru")
        self.path = self.root / "continuous" / "queue.json"

    def test_build_uses_only_published_volumes_without_network(self) -> None:
        """Даже пробел в номерах формы не заменяется придуманным диапазоном."""

        with patch("src.collect.collection_queue.IoffeClient.get_response") as network:
            queue = build_queue(self.root, ioffe_origin="https://journals.ioffe.ru")

        network.assert_not_called()
        identifiers = [task["task_id"] for task in queue["tasks"]]
        self.assertEqual(identifiers, [
            "ufn-archive", "jetp-171", "jetp-169", "jetp-28", "jetp_letters-archive",
            *[f"ioffe-{journal_id}" for journal_id in IOFFE_JOURNALS], "quantum-archive",
        ])
        self.assertEqual(queue["jetp_form_evidence"], self.archive.evidence_for(self.snapshot))
        self.assertTrue(all(task["status"] == "pending" for task in queue["tasks"]))
        self.assertTrue(all(task["attempts"] == 0 for task in queue["tasks"]))
        self.assertTrue(all(task["metadata_only"] == (task["source_id"] == "quantum") for task in queue["tasks"]))

    def test_missing_cache_does_not_enable_network(self) -> None:
        """Отсутствующий кеш останавливает планирование до любых сетевых запросов."""

        cache_path, = (self.archive.state_dir / "cache").glob("*.json")
        cache_path.unlink()

        with patch("src.collect.collection_queue.IoffeClient.get_response") as network, self.assertRaises(IoffeError):
            build_queue(self.root, ioffe_origin="http://journals.ioffe.ru")

        network.assert_not_called()

    def test_corrupt_cache_cannot_be_used_for_queue(self) -> None:
        """Изменённые байты исходной формы не принимаются по старой контрольной сумме."""

        evidence = self.archive.evidence_for(self.snapshot)
        (self.archive.output_dir / evidence["relative_path"]).write_bytes(FORM_HTML + b"changed")

        with self.assertRaisesRegex(IoffeError, "Повреждён кеш"):
            build_queue(self.root, ioffe_origin="http://journals.ioffe.ru")

    def test_form_validation_is_not_replaced_by_option_scan(self) -> None:
        """Числовые варианты из неподтверждённой формы не попадают в очередь."""

        self.archive.import_response(replace(self.snapshot, body=FORM_HTML.replace(b'method="POST"', b'method="GET"')))

        with self.assertRaisesRegex(ValueError, "метод"):
            build_queue(self.root, ioffe_origin="http://journals.ioffe.ru")

    def test_foreign_and_implicit_origins_are_rejected(self) -> None:
        """Смена схемы допустима только явно, чужие узлы и пути не принимаются."""

        invalid: list[Any] = [None, [], "", "journals.ioffe.ru", "https://example.test", "http://journals.ioffe.ru/"]

        for origin in invalid:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                build_queue(self.root, ioffe_origin=origin)

    def test_round_trip_keeps_state_and_form_evidence(self) -> None:
        """Запись и чтение сохраняют точный план вместе с результатами запусков."""

        task = self.queue["tasks"][0]
        task.update({
            "status": "retry_wait", "attempts": 1, "consecutive_errors": 1,
            "next_retry_at": 1800000000.5, "last_error": "Временный сбой DNS",
            "last_run_id": RUN_ID, "last_log": f"attempts/{RUN_ID}/output.log",
            "last_summary_path": f"attempts/{RUN_ID}/summary.json",
            "updated_at": "2026-10-16T12:05:00Z",
        })
        save_queue(self.path, self.queue)
        original = self.path.read_bytes()
        saved_stat = self.path.stat()
        self.assertEqual(load_queue(self.path), self.queue)
        save_queue(self.path, self.queue)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.path.stat().st_mtime_ns, saved_stat.st_mtime_ns)

    def test_all_supported_statuses_are_allowed(self) -> None:
        """Ожидающие, завершённые и остановленные задания сохраняются одной схемой."""

        for status in TASK_STATUSES:
            with self.subTest(status=status):
                queue = deepcopy(self.queue)
                queue["tasks"][0]["status"] = status
                validate_queue(queue)

    def test_invalid_task_fields_are_rejected(self) -> None:
        """Неизвестные источники, состояния и команды не превращаются в запуск."""

        cases = [
            ("source_id", "unknown"), ("source_id", []), ("task_id", "../ufn"),
            ("volume", 170), ("journal_id", 1), ("status", "finished"),
            ("attempts", True), ("attempts", -1), ("consecutive_errors", 1.5),
            ("next_retry_at", float("nan")), ("next_retry_at", float("inf")),
            ("next_retry_at", True), ("next_retry_at", -1), ("next_retry_at", "tomorrow"),
            ("last_error", []), ("last_run_id", "../run"), ("updated_at", "2026-10-16"),
            ("metadata_only", "false"), ("argv", ["rm", "anything"]),
        ]

        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                queue = deepcopy(self.queue)
                queue["tasks"][0][field] = value
                validate_queue(queue)

    def test_quantum_pdf_and_duplicate_tasks_are_rejected(self) -> None:
        """Ограничение Quantum и уникальность заданий сохраняются после перезапуска."""

        queue = deepcopy(self.queue)
        queue["tasks"][-1]["metadata_only"] = False

        with self.assertRaisesRegex(ValueError, "Quantum"):
            validate_queue(queue)

        queue = deepcopy(self.queue)
        queue["tasks"].append(deepcopy(queue["tasks"][0]))

        with self.assertRaisesRegex(ValueError, "Повторяющаяся"):
            validate_queue(queue)

    def test_missing_sources_and_wrong_selectors_are_rejected(self) -> None:
        """Полная очередь не теряет журнал и не принимает логические числа как номера."""

        queue = deepcopy(self.queue)
        queue["tasks"].pop()

        with self.assertRaisesRegex(ValueError, "всех"):
            validate_queue(queue)

        for source_id, field, value in (("jetp", "volume", True), ("ioffe", "journal_id", 6)):
            with self.subTest(source=source_id), self.assertRaises(ValueError):
                queue = deepcopy(self.queue)
                task = next(task for task in queue["tasks"] if task["source_id"] == source_id)
                task[field] = value
                validate_queue(queue)

    def test_runtime_paths_cannot_escape_attempt_or_change_run(self) -> None:
        """Журнал и итог допускаются только внутри собственного каталога попытки."""

        paths = ["/tmp/output.log", "../output.log", "attempts/../output.log", f"attempts/{'b' * 32}/output.log"]

        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                queue = deepcopy(self.queue)
                queue["tasks"][0].update({"last_run_id": RUN_ID, "last_log": path})
                validate_queue(queue)

    def test_evidence_paths_and_timestamps_are_validated(self) -> None:
        """Ссылка на форму не может вести за пределы кеша или содержать чужой URL."""

        cases = [
            ("relative_path", "../form.html"), ("relative_path", []),
            ("sha256", "a"), ("response_metadata_sha256", None),
            ("requested_url", "https://example.test"), ("bytes", True),
            ("response_metadata_path", "/tmp/form.json"),
        ]

        for field, value in cases:
            with self.subTest(field=field), self.assertRaises(ValueError):
                queue = deepcopy(self.queue)
                queue["jetp_form_evidence"][field] = value
                validate_queue(queue)

        for timestamp in (None, "yesterday", "2026-10-16T12:00:00", "2026-10-16T12:00:00+03:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                queue = deepcopy(self.queue)
                queue["created_at"] = timestamp
                validate_queue(queue)

    def test_load_rejects_corrupt_duplicate_and_oversized_json(self) -> None:
        """Неполная запись и повторяющиеся поля не замещаются новой очередью молча."""

        self.path.parent.mkdir(parents=True)

        for body in (b"{", b'[]', b'{"schema_version":"a","schema_version":"b"}', b" " * (MAX_QUEUE_BYTES + 1)):
            with self.subTest(size=len(body)), self.assertRaises(IoffeError):
                self.path.write_bytes(body)
                load_queue(self.path)

    def test_save_rejects_invalid_queue_without_overwriting(self) -> None:
        """Ошибочное состояние не уничтожает последний корректный снимок очереди."""

        save_queue(self.path, self.queue)
        original = self.path.read_bytes()
        invalid = deepcopy(self.queue)
        invalid["tasks"][-1]["metadata_only"] = False

        with self.assertRaises(ValueError):
            save_queue(self.path, invalid)

        self.assertEqual(self.path.read_bytes(), original)

    def test_save_rejects_too_large_state_without_creating_file(self) -> None:
        """Чрезмерно большой журнал ошибки не создаёт нечитаемый снимок очереди."""

        self.queue["tasks"][0]["last_error"] = "x" * MAX_QUEUE_BYTES

        with self.assertRaisesRegex(ValueError, "размер"):
            save_queue(self.path, self.queue)

        self.assertFalse(self.path.exists())

    def test_queue_and_parent_symlinks_are_rejected(self) -> None:
        """Ни файл очереди, ни его родитель не могут быть символической ссылкой."""

        save_queue(self.path, self.queue)
        file_link = self.root / "linked.json"
        file_link.symlink_to(self.path)
        parent_link = self.root / "linked-directory"
        parent_link.symlink_to(self.path.parent, target_is_directory=True)

        for path in (file_link, parent_link / "queue.json", self.root / "continuous" / ".." / "other.json"):
            with self.subTest(path=path):
                with self.assertRaises(IoffeError):
                    save_queue(path, self.queue)

                with self.assertRaises(IoffeError):
                    load_queue(path)

    def test_save_delegates_to_atomic_json_writer(self) -> None:
        """Публикация очереди использует существующую атомарную запись JSON."""

        with patch("src.collect.collection_queue.write_json") as writer:
            save_queue(self.path, self.queue)

        writer.assert_called_once_with(self.path, self.queue)

    def test_load_rejects_unknown_top_level_fields(self) -> None:
        """Файл с неизвестными исполняемыми параметрами не принимается как очередь."""

        self.path.parent.mkdir(parents=True)
        self.queue["command"] = "echo unexpected"
        self.path.write_text(json.dumps(self.queue), encoding="utf-8")

        with self.assertRaisesRegex(IoffeError, "полей"):
            load_queue(self.path)


if __name__ == "__main__":
    unittest.main()
