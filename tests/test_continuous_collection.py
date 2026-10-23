"""Проверки непрерывной очереди с подменой процессов, сети и ожидания времени."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest

from collections.abc import Callable
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from src.collect import jetp
from src.collect.base import HttpResponseSnapshot
from src.collect.collection_queue import build_queue, load_queue, save_queue
from src.collect.continuous_collection import (
    MAX_SUMMARY_BYTES,
    _QueueRunner,
    _attempt_result,
    _command,
    _retry_delay,
    _runner_lock,
    run_queue,
)
from src.collect.ioffe_archive import IoffeArchive
from src.collect.ioffe_http import IoffeError
from src.collect.journal_sources import JournalSource
from tests.test_collection_queue import FORM_HTML, RUN_ID


def _queue_fixture(root: Path) -> dict[str, Any]:
    """Построить настоящую очередь по небольшому сохранённому HTML без сети."""

    source = JournalSource(jetp)
    archive = IoffeArchive(
        root / "data" / "raw" / "journals" / "jetp",
        root / "manifests" / "imports" / "journals" / "jetp",
        MagicMock(), validate_url=source.validate_url,
    )
    archive.import_response(HttpResponseSnapshot(
        requested_url=jetp.START_URL, final_url=jetp.START_URL, status_code=200,
        headers=(("content-type", "text/html; charset=koi8-r"),),
        retrieved_at="2026-10-16T12:00:00+00:00", body=FORM_HTML,
    ))

    return build_queue(root, ioffe_origin="http://journals.ioffe.ru")


def _summary(task: dict[str, Any], queue: dict[str, Any]) -> dict[str, Any]:
    """Подготовить полный итог, соответствующий выбранной задаче и её запуску."""

    record = {
        "run_id": task["last_run_id"], "source_id": task["source_id"],
        "command": "journal" if task["source_id"] == "ioffe" else "crawl",
        "selected_volume": task["volume"], "selected_journal": task["journal_id"],
        "metadata_only": task["metadata_only"], "max_requests": 0, "max_articles": 0,
        "max_issues": 0 if task["source_id"] == "ioffe" else None,
        "network_requests": 3, "cards_processed": 1, "cards_with_pdf": 0,
        "status": "complete", "error": None, "retryable_error": False,
    }

    if task["source_id"] == "ioffe":
        record.update({"origin": queue["ioffe_origin"], "selected_issue": None, "selected_article": None})

    else:
        record["start_url"] = None

    return record


class _FakeClock:
    """Предоставить управляемое время для повторов без настоящего ожидания."""

    def __init__(self) -> None:
        """Начать с произвольной положительной временной отметки."""

        self.seconds = 1000.0
        self.waits: list[float] = []

    def read(self) -> float:
        """Вернуть текущее модельное время."""

        return self.seconds


class _AdvancingEvent(threading.Event):
    """Заменить только ожидание запускателя продвижением тестовых часов."""

    def __init__(self, clock: _FakeClock) -> None:
        """Сохранить настоящую семантику установки события и подключить тестовые часы."""

        super().__init__()
        self.clock = clock

    def wait(self, timeout: float | None = None) -> bool:
        """Пропустить интервал мгновенно, если остановка ещё не запрошена."""

        if self.is_set():
            return True

        if timeout is None or timeout <= 0:
            raise AssertionError("Тестовый запускатель должен ожидать конечный положительный интервал")

        self.clock.waits.append(timeout)
        self.clock.seconds += timeout

        return False


class ContinuousCollectionTests(unittest.TestCase):
    """Проверить подтверждение результатов, безопасный запуск и возобновление очереди."""

    def setUp(self) -> None:
        """Подготовить изолированную очередь и запретить запуск настоящих процессов."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.queue = _queue_fixture(self.root)
        self.queue_path = self.root / "queue" / "queue.json"
        self.summary_path = self.root / "summary.json"
        self.process_patch = patch("src.collect.continuous_collection.subprocess.Popen")
        self.process = self.process_patch.start()
        self.process.side_effect = AssertionError("Настоящие процессы в этих тестах запрещены")
        self.addCleanup(self.process_patch.stop)

    def _task(self, source_id: str = "ufn") -> dict[str, Any]:
        """Выбрать первое задание источника для проверки одного результата."""

        task = next(task for task in self.queue["tasks"] if task["source_id"] == source_id)
        task["last_run_id"] = RUN_ID

        return task

    def _write_summary(self, record: dict[str, Any]) -> None:
        """Сохранить управляемый итог в отдельный временный файл."""

        self.summary_path.write_text(json.dumps(record), encoding="utf-8")

    def _runner(self, *, pending: tuple[str, ...] = ("ufn",)) -> _QueueRunner:
        """Создать запускатель с завершёнными посторонними источниками."""

        for task in self.queue["tasks"]:
            task["status"] = "pending" if task["source_id"] in pending else "complete"

        save_queue(self.queue_path, self.queue)

        return _QueueRunner(
            self.queue_path, self.root, -1, timeout_seconds=60.0,
            retry_base_seconds=60.0, retry_max_seconds=1800.0,
        )

    def _success_child(
        self, command: list[str], project_root: Path, log_path: Path,
        lock_descriptor: int, stop: threading.Event,
    ) -> int:
        """Имитировать завершённый сборщик, проверив запись намерения до запуска."""

        queue = load_queue(self.queue_path)
        run_id = command[command.index("--run-id") + 1]
        task = next(task for task in queue["tasks"] if task["last_run_id"] == run_id)
        self.assertEqual(task["status"], "running")
        self.assertEqual(project_root, self.root)
        self.assertEqual(log_path, self.queue_path.parent / task["last_log"])
        summary_path = Path(command[command.index("--summary-path") + 1])
        self.assertEqual(summary_path, self.queue_path.parent / task["last_summary_path"])
        summary_path.write_text(json.dumps(_summary(task, queue)), encoding="utf-8")
        log_path.write_text("Тестовый запуск без сети\n", encoding="utf-8")

        return 0

    def _unfinished_attempt(
        self, runner: _QueueRunner, *, old_status: str = "running",
        summary_changes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Сохранить незакрытое намерение и отдельный итог исчезнувшего сборщика."""

        task = runner.queue["tasks"][0]
        task.update(
            status=old_status, attempts=2, last_run_id=RUN_ID,
            last_summary_path=f"attempts/{RUN_ID}/summary.json", last_log=f"attempts/{RUN_ID}/output.log",
        )
        summary_path = self.queue_path.parent / task["last_summary_path"]
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary = {**_summary(task, runner.queue), **(summary_changes or {})}
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        save_queue(self.queue_path, runner.queue)

        return task

    def test_matching_success_is_confirmed_for_every_source(self) -> None:
        """Успех принимается только с собственным идентификатором и областью задания."""

        for source_id in ("ufn", "jetp", "jetp_letters", "ioffe", "quantum"):
            with self.subTest(source=source_id):
                task = self._task(source_id)
                self._write_summary(_summary(task, self.queue))
                self.assertEqual(_attempt_result(task, self.queue, self.summary_path, 0), ("complete", None))

    def test_summary_only_recovery_validates_every_source_without_inventing_exit_code(self) -> None:
        """После потери родителя итог подтверждается без подстановки фиктивного кода процесса."""

        for source_id in ("ufn", "jetp", "jetp_letters", "ioffe", "quantum"):
            with self.subTest(source=source_id):
                task = self._task(source_id)
                self._write_summary(_summary(task, self.queue))
                self.assertEqual(_attempt_result(task, self.queue, self.summary_path, None), ("complete", None))

    def test_deferred_success_is_not_plain_complete(self) -> None:
        """Завершение с пропущенными публикациями остаётся отдельным результатом."""

        task = self._task("quantum")
        summary = _summary(task, self.queue)
        summary["status"] = "complete_with_deferred"
        self._write_summary(summary)
        status, reason = _attempt_result(task, self.queue, self.summary_path, 0)
        self.assertEqual(status, "complete_with_deferred")
        self.assertIn("отложенные", reason or "")

    def test_only_explicit_retryable_error_is_retried(self) -> None:
        """Транспортный сбой повторяется, HTTP-ошибка или сбой парсера блокируют источник."""

        task = self._task()

        for retryable in (True, False):
            with self.subTest(retryable=retryable):
                summary = _summary(task, self.queue)
                summary.update(status="error", error="Ошибка соединения", retryable_error=retryable)
                self._write_summary(summary)
                expected = "retry_wait" if retryable else "blocked"
                self.assertEqual(_attempt_result(task, self.queue, self.summary_path, 1), (expected, "Ошибка соединения"))

    def test_wrong_scope_or_stale_run_cannot_confirm_attempt(self) -> None:
        """Чужой запуск, пределы обхода и изменение режима Quantum отклоняются."""

        task = self._task("quantum")
        original = _summary(task, self.queue)
        changes = [
            ("run_id", "b" * 32), ("source_id", "ufn"), ("command", "article"),
            ("selected_volume", 170), ("selected_journal", 1), ("metadata_only", False),
            ("max_requests", 20), ("max_articles", 3), ("max_issues", 1),
            ("start_url", "https://example.test/"), ("max_requests", False),
        ]

        for field, value in changes:
            with self.subTest(field=field, value=value):
                summary = {**original, field: value}
                self._write_summary(summary)
                status, reason = _attempt_result(task, self.queue, self.summary_path, 0)
                self.assertEqual(status, "blocked")
                self.assertIn(field, reason or "")

    def test_ioffe_origin_and_selectors_are_part_of_scope(self) -> None:
        """Итог другого журнала, выпуска или протокола Иоффе не засчитывается."""

        task = self._task("ioffe")

        for field, value in (("origin", "https://journals.ioffe.ru"), ("selected_issue", 1), ("selected_article", 2)):
            with self.subTest(field=field):
                summary = _summary(task, self.queue)
                summary[field] = value
                self._write_summary(summary)
                self.assertEqual(_attempt_result(task, self.queue, self.summary_path, 0)[0], "blocked")

    def test_exit_code_and_status_must_agree(self) -> None:
        """Ограниченный, ошибочный или аварийный запуск не объявляется полным обходом."""

        task = self._task()
        cases = [(1, "complete"), (0, "error"), (0, "request_limit"), (0, "article_limit"), (-9, "complete"), (2, "complete")]

        for return_code, status in cases:
            with self.subTest(return_code=return_code, status=status):
                summary = _summary(task, self.queue)
                summary["status"] = status
                self._write_summary(summary)
                self.assertEqual(_attempt_result(task, self.queue, self.summary_path, return_code)[0], "blocked")

        summary = _summary(task, self.queue)
        summary["status"] = "interrupted"
        self._write_summary(summary)
        self.assertEqual(_attempt_result(task, self.queue, self.summary_path, 130)[0], "interrupted")

    def test_invalid_counters_and_conflicting_success_are_rejected(self) -> None:
        """Неверные типы счётчиков и одновременные успех с ошибкой не принимаются."""

        task = self._task()
        changes = [
            ("network_requests", True), ("cards_processed", -1), ("cards_with_pdf", "1"),
            ("retryable_error", 0), ("retryable_error", True), ("error", "Ошибка"),
        ]

        for field, value in changes:
            with self.subTest(field=field, value=value):
                summary = _summary(task, self.queue)
                summary[field] = value
                self._write_summary(summary)
                self.assertEqual(_attempt_result(task, self.queue, self.summary_path, 0)[0], "blocked")

        summary = _summary(task, self.queue)
        del summary["retryable_error"]
        self._write_summary(summary)
        self.assertEqual(_attempt_result(task, self.queue, self.summary_path, 0)[0], "blocked")

    def test_interrupted_summary_cannot_hide_an_error_or_retry_flag(self) -> None:
        """Прерывание с противоречивой ошибкой блокирует и обычный запуск, и восстановление."""

        task = self._task()

        for return_code in (130, None):
            for changes in ({"error": "HTTP 403"}, {"retryable_error": True}):
                with self.subTest(return_code=return_code, changes=changes):
                    summary = {**_summary(task, self.queue), "status": "interrupted", **changes}
                    self._write_summary(summary)
                    self.assertEqual(_attempt_result(task, self.queue, self.summary_path, return_code)[0], "blocked")

    def test_summary_without_error_field_is_incomplete(self) -> None:
        """Отсутствие обязательного поля нельзя принять за явно записанное отсутствие ошибки."""

        task = self._task()
        summary = _summary(task, self.queue)
        del summary["error"]
        self._write_summary(summary)
        self.assertEqual(_attempt_result(task, self.queue, self.summary_path, None)[0], "blocked")

    def test_missing_malformed_oversized_or_linked_summary_blocks_attempt(self) -> None:
        """Отсутствие отдельного корректного итога не заменяется оптимистичным завершением."""

        task = self._task()
        self.assertEqual(_attempt_result(task, self.queue, self.summary_path, 0)[0], "blocked")

        for body in (b"{", b"[]", b"null", b" " * (MAX_SUMMARY_BYTES + 1)):
            with self.subTest(size=len(body)):
                self.summary_path.write_bytes(body)
                self.assertEqual(_attempt_result(task, self.queue, self.summary_path, 0)[0], "blocked")

        self._write_summary(_summary(task, self.queue))
        link = self.root / "summary-link.json"
        link.symlink_to(self.summary_path)
        self.assertEqual(_attempt_result(task, self.queue, link, 0)[0], "blocked")

    def test_duplicate_summary_fields_are_rejected(self) -> None:
        """Повторяющееся поле статуса не может скрыть ошибку предыдущим или последним значением."""

        task = self._task()
        text = json.dumps(_summary(task, self.queue))
        self.summary_path.write_text('{"status":"error",' + text[1:], encoding="utf-8")
        self.assertEqual(_attempt_result(task, self.queue, self.summary_path, 0)[0], "blocked")

    def test_retry_delay_grows_and_is_capped_without_overflow(self) -> None:
        """Повторные транспортные сбои увеличивают паузу до конечного предела."""

        self.assertEqual([_retry_delay(index, 60.0, 1800.0) for index in range(1, 8)],
                         [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0])
        self.assertEqual(_retry_delay(10 ** 9, 60.0, 1800.0), 1800.0)
        self.assertEqual(_retry_delay(0, 60.0, 60.0), 60.0)

    def test_commands_keep_zero_limits_and_quantum_metadata_only(self) -> None:
        """Команды не ограничивают архив порцией и не разрешают PDF Quantum."""

        for source_id in ("ufn", "jetp", "jetp_letters", "ioffe", "quantum"):
            with self.subTest(source=source_id):
                command = _command(self._task(source_id), self.queue, self.summary_path, 45.0)
                self.assertEqual(command[command.index("--max-requests") + 1], "0")
                self.assertEqual(command[command.index("--max-articles") + 1], "0")
                self.assertEqual(command[command.index("--run-id") + 1], RUN_ID)
                self.assertEqual(command[command.index("--summary-path") + 1], str(self.summary_path))
                self.assertEqual("--metadata-only" in command, source_id == "quantum")
                self.assertIn("--allow-network", command)

                if source_id == "ioffe":
                    self.assertEqual(command[command.index("--max-issues") + 1], "0")
                    self.assertEqual(command[command.index("--origin") + 1], self.queue["ioffe_origin"])
                    self.assertEqual(command[4:6], ["journal", "1"])

                if source_id == "jetp":
                    self.assertEqual(command[command.index("--volume") + 1], "171")

    def test_runner_lock_rejects_duplicate_and_releases_on_exit(self) -> None:
        """Два запускателя одного проекта не могут одновременно владеть блокировкой."""

        with (
            _runner_lock(self.root),
            self.assertRaisesRegex(ValueError, "уже работает"),
            _runner_lock(self.root),
        ):
            self.fail("Повторный запуск не должен получать блокировку")

        with _runner_lock(self.root) as descriptor:
            self.assertGreaterEqual(descriptor, 0)

    def test_run_queue_requires_valid_explicit_network_parameters(self) -> None:
        """Сеть не включается неявно и аргументы проверяются до запуска дочернего процесса."""

        invalid: list[dict[str, Any]] = [
            {}, {"allow_network": 1}, {"allow_network": True, "timeout_seconds": float("inf")},
            {"allow_network": True, "timeout_seconds": True}, {"allow_network": True, "retry_base_seconds": 30},
            {"allow_network": True, "retry_max_seconds": 20}, {"allow_network": True, "retry_base_seconds": float("nan")},
        ]

        with patch("src.collect.continuous_collection._run_child") as child:
            for arguments in invalid:
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    run_queue(self.queue_path, self.root, **arguments)

        child.assert_not_called()
        self.process.assert_not_called()

    def test_corrupt_queue_is_rejected_before_process_start(self) -> None:
        """Неверная сохранённая очередь не запускает ни один источник."""

        self.queue_path.parent.mkdir()
        self.queue_path.write_text('{"invalid":true}', encoding="utf-8")

        with patch("src.collect.continuous_collection._run_child") as child, self.assertRaises((ValueError, IoffeError)):
            run_queue(self.queue_path, self.root, allow_network=True)

        child.assert_not_called()

    def test_completed_queue_does_not_start_children(self) -> None:
        """Подтверждённо завершённые задачи пропускаются при повторном запуске."""

        self._runner(pending=())

        with patch("src.collect.continuous_collection._run_child") as child, \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()):
            self.assertEqual(run_queue(self.queue_path, self.root, allow_network=True), 0)

        child.assert_not_called()

    def test_finished_queue_with_deferred_keeps_nonzero_completeness_code(self) -> None:
        """Отложенные публикации не превращаются в подтверждение полного скачивания."""

        runner = self._runner(pending=())
        runner.queue["tasks"][-1]["status"] = "complete_with_deferred"
        save_queue(self.queue_path, runner.queue)

        with patch("src.collect.continuous_collection._run_child") as child, \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()):
            self.assertEqual(run_queue(self.queue_path, self.root, allow_network=True), 2)

        child.assert_not_called()

    def test_interrupted_and_running_tasks_are_restored_before_attempt(self) -> None:
        """Только подтверждённое прерывание разрешает новую попытку незакрытого задания."""

        for old_status in ("running", "interrupted"):
            with self.subTest(status=old_status):
                runner = self._runner()
                task = self._unfinished_attempt(runner, old_status=old_status, summary_changes={"status": "interrupted"})
                task["next_retry_at"] = 2000.0
                save_queue(self.queue_path, runner.queue)
                before_attempt: list[str] = []
                original_prepare = runner._prepare_attempt

                def prepare(
                    current_task: dict[str, Any], *, observed: list[str] = before_attempt,
                    original: Callable[[dict[str, Any]], tuple[Path, Path]] = original_prepare,
                ) -> tuple[Path, Path]:
                    """Запомнить восстановленное состояние перед записью новой попытки."""

                    observed.append(current_task["status"])
                    self.assertIsNone(current_task["next_retry_at"])

                    return original(current_task)

                with patch.object(runner, "_prepare_attempt", side_effect=prepare), \
                     patch("src.collect.continuous_collection._run_child", side_effect=self._success_child) as child, \
                     patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run(), 0)

                self.assertEqual(before_attempt, ["pending"])
                self.assertEqual(child.call_count, 1)
                saved_task = load_queue(self.queue_path)["tasks"][0]
                self.assertEqual(saved_task["attempts"], 3)
                self.assertEqual(saved_task["status"], "complete")
                self.assertNotEqual(saved_task["last_run_id"], RUN_ID)

    def test_recovery_preserves_finished_results_and_does_not_start_children(self) -> None:
        """Сохранённые полные итоги не запускаются повторно после потери управляющего процесса."""

        for old_status in ("running", "interrupted"):
            for completed_status, exit_code in (("complete", 0), ("complete_with_deferred", 2)):
                with self.subTest(old_status=old_status, completed_status=completed_status):
                    runner = self._runner()
                    self._unfinished_attempt(runner, old_status=old_status,
                                             summary_changes={"status": completed_status})

                    with patch("src.collect.continuous_collection._run_child") as child, \
                         patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
                         redirect_stdout(io.StringIO()):
                        self.assertEqual(runner.run(), exit_code)
                        saved_queue = load_queue(self.queue_path)
                        self.assertEqual(runner.run(), exit_code)
                        self.assertEqual(load_queue(self.queue_path), saved_queue)

                    child.assert_not_called()
                    task = saved_queue["tasks"][0]
                    self.assertEqual(task["status"], completed_status)
                    self.assertEqual(task["attempts"], 2)
                    self.assertEqual(task["last_run_id"], RUN_ID)

    def test_recovery_preserves_nonretryable_error_even_from_interrupted_queue_state(self) -> None:
        """Прежний HTTP 403 не превращается в новое разрешение сети при восстановлении."""

        for old_status in ("running", "interrupted"):
            with self.subTest(old_status=old_status):
                runner = self._runner()
                self._unfinished_attempt(runner, old_status=old_status,
                                         summary_changes={"status": "error", "error": "HTTP 403"})

                with patch("src.collect.continuous_collection._run_child") as child, \
                     patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run(), 1)
                    saved_queue = load_queue(self.queue_path)
                    self.assertEqual(runner.run(), 1)
                    self.assertEqual(load_queue(self.queue_path), saved_queue)

                child.assert_not_called()
                self.assertEqual(saved_queue["tasks"][0]["status"], "blocked")
                self.assertEqual(saved_queue["tasks"][0]["last_error"], "HTTP 403")

    def test_recovery_retry_backoff_is_saved_once_without_resetting_error_count(self) -> None:
        """Восстановленный сетевой сбой увеличивает паузу, повторное чтение её не переносит."""

        for previous_errors, expected_delay in ((0, 60.0), (2, 240.0), (100, 1800.0)):
            with self.subTest(previous_errors=previous_errors):
                runner = self._runner()
                task = self._unfinished_attempt(runner, summary_changes={
                    "status": "error", "error": "Временный сбой DNS", "retryable_error": True,
                })
                task["consecutive_errors"] = previous_errors

                with patch("src.collect.continuous_collection.time.time", return_value=1000.0), \
                     redirect_stdout(io.StringIO()):
                    runner._recover_attempt(task)

                saved_queue = load_queue(self.queue_path)
                saved_task = saved_queue["tasks"][0]
                self.assertEqual(saved_task["status"], "retry_wait")
                self.assertEqual(saved_task["consecutive_errors"], previous_errors + 1)
                self.assertEqual(saved_task["next_retry_at"], 1000.0 + expected_delay)
                self.assertEqual(saved_task["attempts"], 2)

                with patch("src.collect.continuous_collection.time.time", return_value=2000.0), \
                     redirect_stdout(io.StringIO()):
                    runner._recover_attempt(task)

                self.assertEqual(load_queue(self.queue_path), saved_queue)

    def test_recovered_retry_wait_is_observed_before_new_attempt(self) -> None:
        """После восстановления новая попытка начинается только после назначенной минутной паузы."""

        runner = self._runner()
        self._unfinished_attempt(runner, summary_changes={
            "status": "error", "error": "Временный сбой DNS", "retryable_error": True,
        })
        clock = _FakeClock()
        runner.stop = _AdvancingEvent(clock)

        with patch("src.collect.continuous_collection._run_child", side_effect=self._success_child) as child, \
             patch("src.collect.continuous_collection.time.time", side_effect=clock.read), \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(), 0)

        child.assert_called_once()
        self.assertEqual(clock.waits, [30.0, 30.0])
        self.assertEqual(load_queue(self.queue_path)["tasks"][0]["attempts"], 3)

    def test_recovery_rejects_missing_or_corrupt_summary_without_launch(self) -> None:
        """Потерянный или повреждённый итог не разрешает повтор даже при отсутствии журнала."""

        for body in (None, b"{", b"[]", b" " * (MAX_SUMMARY_BYTES + 1)):
            with self.subTest(size=None if body is None else len(body)):
                runner = self._runner()
                task = self._unfinished_attempt(runner)
                summary_path = self.queue_path.parent / task["last_summary_path"]

                if body is None:
                    summary_path.unlink()

                else:
                    summary_path.write_bytes(body)

                with patch("src.collect.continuous_collection._run_child") as child, \
                     patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run(), 1)

                child.assert_not_called()
                saved_task = load_queue(self.queue_path)["tasks"][0]
                self.assertEqual(saved_task["status"], "blocked")
                self.assertIn("Нельзя подтвердить", saved_task["last_error"])
                self.assertEqual(saved_task["attempts"], 2)

    def test_recovery_rejects_foreign_scope_or_invalid_interruption(self) -> None:
        """Чужой запуск, изменённые пределы и противоречивое прерывание остаются заблокированными."""

        cases = [
            {"run_id": "b" * 32}, {"source_id": "quantum"}, {"selected_volume": 171},
            {"max_articles": 10}, {"status": "interrupted", "error": "HTTP 403"},
            {"status": "interrupted", "retryable_error": True},
        ]

        for changes in cases:
            with self.subTest(changes=changes):
                runner = self._runner()
                self._unfinished_attempt(runner, summary_changes=changes)

                with patch("src.collect.continuous_collection._run_child") as child, \
                     patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run(), 1)

                child.assert_not_called()
                self.assertEqual(load_queue(self.queue_path)["tasks"][0]["status"], "blocked")

    def test_recovery_blocks_intention_without_summary_reference(self) -> None:
        """Незакрытая задача без пути итога не считается безопасно не начатой попыткой."""

        runner = self._runner()
        task = runner.queue["tasks"][0]
        task.update(status="running", attempts=1)
        save_queue(self.queue_path, runner.queue)

        with patch("src.collect.continuous_collection._run_child") as child, \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(), 1)

        child.assert_not_called()
        self.assertEqual(task["status"], "blocked")
        self.assertIn("отсутствует идентификатор", task["last_error"])

    def test_new_attempt_clears_previous_error_before_launch(self) -> None:
        """Новое намерение не хранит старую ошибку как будто она относится к текущему запуску."""

        runner = self._runner()
        task = runner.queue["tasks"][0]
        task["last_error"] = "Временный сбой предыдущей попытки"
        runner._prepare_attempt(task)
        self.assertIsNone(load_queue(self.queue_path)["tasks"][0]["last_error"])

    def test_blocked_task_stops_only_its_source(self) -> None:
        """Заблокированный том не допускает перехода к следующим томам того же сайта."""

        runner = self._runner(pending=("ufn", "jetp"))
        jetp_tasks = [task for task in runner.queue["tasks"] if task["source_id"] == "jetp"]
        jetp_tasks[0]["status"] = "blocked"
        save_queue(self.queue_path, runner.queue)

        with patch("src.collect.continuous_collection._run_child", side_effect=self._success_child) as child, \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(), 1)

        self.assertEqual(child.call_count, 1)
        self.assertEqual(child.call_args.args[0][5], "ufn")
        self.assertEqual([task["status"] for task in jetp_tasks], ["blocked", "pending", "pending"])

    def test_nonretryable_result_blocks_remaining_jobs_without_retry(self) -> None:
        """Новая HTTP-ошибка останавливает источник без повторов и перехода к следующему журналу."""

        runner = self._runner(pending=("ioffe",))

        def fail_child(
            command: list[str], project_root: Path, log_path: Path,
            lock_descriptor: int, stop: threading.Event,
        ) -> int:
            """Сохранить свидетельство ошибки доступа текущего задания."""

            task = next(task for task in runner.queue["tasks"] if task["status"] == "running")
            summary = _summary(task, runner.queue)
            summary.update(status="error", error="HTTP 403", retryable_error=False)
            Path(command[command.index("--summary-path") + 1]).write_text(json.dumps(summary), encoding="utf-8")

            return 1

        with patch("src.collect.continuous_collection._run_child", side_effect=fail_child) as child, \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(), 1)

        self.assertEqual(child.call_count, 1)
        tasks = [task for task in load_queue(self.queue_path)["tasks"] if task["source_id"] == "ioffe"]
        self.assertEqual([task["status"] for task in tasks], ["blocked", "pending", "pending", "pending", "pending"])

    def test_transient_failure_retries_after_backoff_then_completes(self) -> None:
        """Временные сбои сохраняют попытки, увеличивают паузу и не мешают последующему успеху."""

        runner = self._runner()
        clock = _FakeClock()
        runner.stop = _AdvancingEvent(clock)
        observed_run_ids: list[str] = []

        def transient_child(
            command: list[str], project_root: Path, log_path: Path,
            lock_descriptor: int, stop: threading.Event,
        ) -> int:
            """Дважды имитировать сбой DNS, затем записать корректный успешный итог."""

            task = runner.queue["tasks"][0]
            observed_run_ids.append(task["last_run_id"])

            if len(observed_run_ids) == 3:
                return self._success_child(command, project_root, log_path, lock_descriptor, stop)

            summary = _summary(task, runner.queue)
            summary.update(status="error", error="Временный сбой DNS", retryable_error=True)
            Path(command[command.index("--summary-path") + 1]).write_text(json.dumps(summary), encoding="utf-8")

            return 1

        with patch("src.collect.continuous_collection._run_child", side_effect=transient_child), \
             patch("src.collect.continuous_collection.time.time", side_effect=clock.read), \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(), 0)

        self.assertEqual(len(set(observed_run_ids)), 3)
        self.assertEqual(clock.waits, [30.0] * 6)
        task = load_queue(self.queue_path)["tasks"][0]
        self.assertEqual(task["status"], "complete")
        self.assertEqual(task["attempts"], 3)
        self.assertEqual(task["consecutive_errors"], 0)
        self.assertIsNone(task["next_retry_at"])
        self.assertIsNone(task["last_error"])

    def test_stop_event_does_not_allow_retry_without_summary(self) -> None:
        """Остановка без сохранённого итога не разрешает повтор неизвестного результата."""

        runner = self._runner()

        def stop_child(
            command: list[str], project_root: Path, log_path: Path,
            lock_descriptor: int, stop: threading.Event,
        ) -> int:
            """Остановить очередь во время первой попытки без настоящего процесса."""

            stop.set()

            return 130

        with patch("src.collect.continuous_collection._run_child", side_effect=stop_child) as child, \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(), 130)

        self.assertEqual(child.call_count, 1)
        task = load_queue(self.queue_path)["tasks"][0]
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["attempts"], 1)
        self.assertIsNone(task["next_retry_at"])

    def test_valid_interruption_stops_queue_and_remains_resumable(self) -> None:
        """Подтверждённое дочерним сборщиком прерывание сохраняется без немедленного повтора."""

        runner = self._runner()

        def interrupt_child(
            command: list[str], project_root: Path, log_path: Path,
            lock_descriptor: int, stop: threading.Event,
        ) -> int:
            """Записать действительный итог прерывания текущего сборщика."""

            task = runner.queue["tasks"][0]
            summary = {**_summary(task, runner.queue), "status": "interrupted"}
            Path(command[command.index("--summary-path") + 1]).write_text(json.dumps(summary), encoding="utf-8")

            return 130

        with patch("src.collect.continuous_collection._run_child", side_effect=interrupt_child) as child, \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(), 130)

        child.assert_called_once()
        self.assertEqual(load_queue(self.queue_path)["tasks"][0]["status"], "interrupted")

    def test_stop_event_preserves_terminal_summary_instead_of_interruption(self) -> None:
        """Одновременная остановка родителя не стирает завершение, HTTP-запрет или временный сбой."""

        cases = [
            ({"status": "complete"}, 0, "complete"),
            ({"status": "complete_with_deferred"}, 0, "complete_with_deferred"),
            ({"status": "error", "error": "HTTP 403"}, 1, "blocked"),
            ({"status": "error", "error": "Временный сбой DNS", "retryable_error": True}, 1, "retry_wait"),
        ]

        for changes, return_code, expected_status in cases:
            with self.subTest(status=expected_status):
                runner = self._runner()

                def stop_child(
                    command: list[str], project_root: Path, log_path: Path,
                    lock_descriptor: int, stop: threading.Event,
                ) -> int:
                    """Сохранить итог одновременно с запросом остановки очереди."""

                    task = runner.queue["tasks"][0]
                    summary = {**_summary(task, runner.queue), **changes}
                    summary_path = Path(command[command.index("--summary-path") + 1])
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                    stop.set()

                    return return_code

                with patch("src.collect.continuous_collection._run_child", side_effect=stop_child) as child, \
                     patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(runner.run(), 130)

                child.assert_called_once()
                task = load_queue(self.queue_path)["tasks"][0]
                self.assertEqual(task["status"], expected_status)
                self.assertEqual(task["attempts"], 1)

                if expected_status == "blocked":
                    self.assertEqual(task["last_error"], "HTTP 403")

    def test_launch_failure_is_saved_as_blocked(self) -> None:
        """Ошибка создания процесса не остаётся вечным состоянием «выполняется»."""

        runner = self._runner()

        with patch("src.collect.continuous_collection._run_child", side_effect=OSError("Нельзя создать журнал")), \
             patch("src.collect.continuous_collection._stop_signals", return_value=nullcontext()), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.run(), 1)

        task = load_queue(self.queue_path)["tasks"][0]
        self.assertEqual(task["status"], "blocked")
        self.assertIn("Нельзя создать журнал", task["last_error"])


if __name__ == "__main__":
    unittest.main()
