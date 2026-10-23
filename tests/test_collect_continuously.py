"""Проверки команды непрерывного сбора без сети и запуска дочерних процессов."""

from __future__ import annotations

import io
import tempfile
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts.collect_continuously import DEFAULT_QUEUE, build_parser, main
from src.collect.continuous_collection import _runner_lock
from src.collect.ioffe_http import IoffeError


def _task(task_id: str, *, status: str = "pending", source_id: str = "ufn") -> dict[str, Any]:
    """Подготовить минимальную задачу для проверки представления состояния."""

    return {
        "task_id": task_id,
        "source_id": source_id,
        "volume": None,
        "journal_id": None,
        "metadata_only": source_id == "quantum",
        "status": status,
        "attempts": 0,
        "consecutive_errors": 0,
        "next_retry_at": None,
        "last_error": None,
        "last_run_id": None,
        "last_log": None,
        "last_summary_path": None,
        "updated_at": None,
    }


class CollectContinuouslyTests(unittest.TestCase):
    """Проверять явное разрешение сети и безопасное управление очередью."""

    def setUp(self) -> None:
        """Изолировать файловый путь, зависимости команды и вывод сообщений."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.queue_path = self.directory / "queue.json"
        self.queue = {"tasks": [_task("ufn")], "private_payload": "НЕ ВЫВОДИТЬ"}
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

        for patcher in (patch("sys.stdout", self.stdout), patch("sys.stderr", self.stderr),
                        patch("scripts.collect_continuously.PROJECT_ROOT", self.directory)):
            patcher.start()
            self.addCleanup(patcher.stop)

        queue_builder = patch("scripts.collect_continuously.build_queue", return_value=self.queue)
        queue_loader = patch("scripts.collect_continuously.load_queue", return_value=self.queue)
        queue_saver = patch("scripts.collect_continuously.save_queue")
        queue_runner = patch("scripts.collect_continuously.run_queue", return_value=0)
        self.builder = queue_builder.start()
        self.loader = queue_loader.start()
        self.saver = queue_saver.start()
        self.runner = queue_runner.start()

        for patcher in (queue_builder, queue_loader, queue_saver, queue_runner):
            self.addCleanup(patcher.stop)

    def test_run_defaults_disable_network(self) -> None:
        """Обычный запуск не даёт сетевого разрешения и использует заданные паузы."""

        arguments = build_parser().parse_args(["run"])
        self.assertFalse(arguments.allow_network)
        self.assertEqual(arguments.queue, DEFAULT_QUEUE)
        self.assertEqual(arguments.timeout, 60)
        self.assertEqual(arguments.retry_base, 60)
        self.assertEqual(arguments.retry_max, 1800)

    def test_run_requires_network_flag_before_engine(self) -> None:
        """Без разрешения не запускаются даже чтение очереди и движок сбора."""

        self.assertEqual(main(["run", "--queue", str(self.queue_path)]), 1)
        self.runner.assert_not_called()
        self.loader.assert_not_called()
        self.saver.assert_not_called()
        self.assertIn("--allow-network", self.stderr.getvalue())

    def test_init_is_offline_and_uses_https_default(self) -> None:
        """Создание очереди только передаёт подготовленное состояние для сохранения."""

        self.assertEqual(main(["init", "--queue", str(self.queue_path)]), 0)
        self.builder.assert_called_once_with(self.directory, ioffe_origin="https://journals.ioffe.ru")
        self.saver.assert_called_once_with(self.queue_path, self.queue)
        self.runner.assert_not_called()
        self.assertIn("Сеть не использовалась", self.stdout.getvalue())

    def test_init_accepts_explicit_supported_http_origin(self) -> None:
        """Использование HTTP у Иоффе требует явного выбора поддержанного адреса."""

        self.assertEqual(main(["init", "--queue", str(self.queue_path),
                               "--ioffe-origin", "http://journals.ioffe.ru"]), 0)
        self.builder.assert_called_once_with(self.directory, ioffe_origin="http://journals.ioffe.ru")

    def test_init_does_not_overwrite_existing_file(self) -> None:
        """Существующая очередь остаётся неизменной, подготовка даже не начинается."""

        self.queue_path.write_text("исходная очередь", encoding="utf-8")
        self.assertEqual(main(["init", "--queue", str(self.queue_path)]), 1)
        self.assertEqual(self.queue_path.read_text(encoding="utf-8"), "исходная очередь")
        self.builder.assert_not_called()
        self.saver.assert_not_called()

    def test_init_rejects_existing_directory(self) -> None:
        """Каталог с именем очереди не принимается за свободный путь."""

        self.queue_path.mkdir()
        self.assertEqual(main(["init", "--queue", str(self.queue_path)]), 1)
        self.builder.assert_not_called()
        self.saver.assert_not_called()

    def test_init_rejects_broken_symlink(self) -> None:
        """Отсутствие целевого файла не разрешает перезаписать символическую ссылку."""

        self.queue_path.symlink_to(self.directory / "missing.json")
        self.assertEqual(main(["init", "--queue", str(self.queue_path)]), 1)
        self.assertTrue(self.queue_path.is_symlink())
        self.builder.assert_not_called()
        self.saver.assert_not_called()

    def test_init_rechecks_path_after_build(self) -> None:
        """Файл, появившийся при подготовке очереди, не затирается сохранением."""

        def create_conflicting_file(*arguments: object, **options: object) -> dict[str, Any]:
            """Смоделировать появление другой очереди во время подготовки."""

            self.queue_path.write_text("чужая очередь", encoding="utf-8")

            return self.queue

        self.builder.side_effect = create_conflicting_file
        self.assertEqual(main(["init", "--queue", str(self.queue_path)]), 1)
        self.saver.assert_not_called()
        self.assertEqual(self.queue_path.read_text(encoding="utf-8"), "чужая очередь")

    def test_init_rejects_active_runner_lock(self) -> None:
        """Создание очереди не конкурирует с уже работающим сбором в проекте."""

        with _runner_lock(self.directory):
            self.assertEqual(main(["init", "--queue", str(self.queue_path)]), 1)

        self.builder.assert_not_called()
        self.saver.assert_not_called()
        self.assertIn("уже работает", self.stderr.getvalue())

    def test_init_holds_shared_lock_until_saved(self) -> None:
        """Сохранение защищено общей блокировкой, которая освобождается после init."""

        def check_lock_is_held(*arguments: object, **options: object) -> None:
            """Проверить невозможность параллельного запуска непосредственно при записи."""

            with self.assertRaises(ValueError), _runner_lock(self.directory):
                self.fail("Общая блокировка должна оставаться занятой до сохранения")

        self.saver.side_effect = check_lock_is_held
        self.assertEqual(main(["init", "--queue", str(self.queue_path)]), 0)
        self.saver.assert_called_once()

        with _runner_lock(self.directory) as descriptor:
            self.assertGreaterEqual(descriptor, 0)

    def test_run_passes_explicit_parameters(self) -> None:
        """Явные параметры ожидания доходят до движка без изменения значений."""

        self.assertEqual(main(["run", "--queue", str(self.queue_path), "--allow-network",
                               "--timeout", "80", "--retry-base", "120", "--retry-max", "600"]), 0)
        self.runner.assert_called_once_with(self.queue_path, self.directory, allow_network=True,
                                            timeout_seconds=80, retry_base_seconds=120,
                                            retry_max_seconds=600)
        self.builder.assert_not_called()
        self.loader.assert_not_called()
        self.saver.assert_not_called()

    def test_run_preserves_engine_exit_status(self) -> None:
        """Команда не маскирует блокировку, пропуски или прерывание успешным кодом."""

        for exit_code in (0, 1, 2, 130):
            with self.subTest(exit_code=exit_code):
                self.runner.return_value = exit_code
                self.assertEqual(main(["run", "--allow-network"]), exit_code)

    def test_run_rejects_inverted_retry_bounds(self) -> None:
        """Максимальная пауза не может быть меньше начальной."""

        self.assertEqual(main(["run", "--allow-network", "--retry-base", "120", "--retry-max", "60"]), 1)
        self.runner.assert_not_called()

    def test_run_rejects_retry_before_one_minute(self) -> None:
        """Временный сетевой сбой не разрешает новый запуск чаще раза в минуту."""

        self.assertEqual(main(["run", "--allow-network", "--retry-base", "59"]), 1)
        self.runner.assert_not_called()
        self.assertIn("не меньше 60 секунд", self.stderr.getvalue())

    def test_invalid_seconds_are_rejected_by_parser(self) -> None:
        """Нулевые, отрицательные и бесконечные интервалы не доходят до движка."""

        for option in ("--timeout", "--retry-base", "--retry-max"):
            for value in ("0", "-1", "nan", "inf", "-inf", "text"):
                with self.subTest(option=option, value=value), self.assertRaises(SystemExit):
                    build_parser().parse_args(["run", f"{option}={value}"])

        self.runner.assert_not_called()

    def test_arbitrary_origin_is_rejected(self) -> None:
        """Нельзя подменить адрес Иоффе произвольным сервером через команду."""

        with self.assertRaises(SystemExit):
            build_parser().parse_args(["init", "--ioffe-origin", "https://example.org"])

        self.builder.assert_not_called()

    def test_status_is_read_only_and_does_not_dump_payload(self) -> None:
        """Просмотр читает состояние без сети, сохранения и вывода исходного JSON."""

        self.assertEqual(main(["status", "--queue", str(self.queue_path)]), 0)
        self.loader.assert_called_once_with(self.queue_path)
        self.saver.assert_not_called()
        self.runner.assert_not_called()
        self.builder.assert_not_called()
        self.assertIn("Задач: 1", self.stdout.getvalue())
        self.assertIn("не количество статей", self.stdout.getvalue())
        self.assertNotIn("НЕ ВЫВОДИТЬ", self.stdout.getvalue())

    def test_status_shows_blocked_retry_running_and_deferred(self) -> None:
        """Причины остановки, путь к журналу и время повтора показываются отдельно."""

        blocked = _task("jetp-168", status="blocked", source_id="jetp")
        blocked.update({"volume": 168, "last_error": "HTTP 403\nForbidden", "last_log": "runs/log.txt"})
        retry = _task("ioffe-1", status="retry_wait", source_id="ioffe")
        retry.update({"journal_id": 1, "next_retry_at": 0})
        self.queue["tasks"] = [blocked, retry, _task("ufn", status="running"),
                               _task("quantum", status="complete_with_deferred", source_id="quantum"),
                               _task("letters", status="complete"), _task("interrupted", status="interrupted")]
        self.assertEqual(main(["status"]), 0)
        output = self.stdout.getvalue()

        for expected in ("jetp-168", "том 168", "HTTP 403 Forbidden", "runs/log.txt", "ioffe-1",
                         "журнал 1", "1970-01-01 03:00:00 МСК", "только метаданные",
                         "обход завершён с пропусками", "не подтверждает, что процесс ещё работает"):
            with self.subTest(expected=expected):
                self.assertIn(expected, output)

    def test_status_limits_details_and_error_length(self) -> None:
        """Большая очередь и длинная ошибка не превращают краткий просмотр в дамп."""

        tasks = [_task(f"task-{index}", status="blocked") for index in range(25)]
        tasks[0]["last_error"] = "ошибка" * 1000
        self.queue["tasks"] = tasks
        self.assertEqual(main(["status"]), 0)
        output = self.stdout.getvalue()
        self.assertIn("Ещё задач с подробностями: 5", output)
        self.assertIn("task-19", output)
        self.assertNotIn("task-20", output)
        self.assertNotIn("ошибка" * 100, output)

    def test_expected_errors_are_reported_without_traceback(self) -> None:
        """Проблемы чтения и проверки очереди возвращают явную ошибку команды."""

        for exception in (FileNotFoundError("нет очереди"), ValueError("повреждена очередь"),
                          IoffeError("ошибка чтения очереди")):
            with self.subTest(exception=exception):
                self.loader.side_effect = exception
                self.assertEqual(main(["status"]), 1)
                self.assertIn(str(exception), self.stderr.getvalue())

        self.runner.assert_not_called()

    def test_status_reports_unrepresentable_timestamp(self) -> None:
        """Повреждённое время повтора не вызывает необработанный OverflowError."""

        self.queue["tasks"] = [_task("ufn", status="retry_wait")]
        self.queue["tasks"][0]["next_retry_at"] = 1e308
        self.assertEqual(main(["status"]), 1)
        self.assertIn("диапазон дат", self.stderr.getvalue())

    def test_keyboard_interrupt_returns_130(self) -> None:
        """Прерывание движка не выдаётся за завершённый сбор."""

        self.runner.side_effect = KeyboardInterrupt
        self.assertEqual(main(["run", "--allow-network"]), 130)
        self.assertIn("Сохранённые файлы не удалены", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
