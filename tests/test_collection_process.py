"""Офлайн-проверки дочерних процессов и сигналов непрерывного сбора."""

from __future__ import annotations

import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from src.collect.continuous_collection import _run_child, _runner_lock, _stop_child, _stop_signals


class StopCollectionProcessTests(unittest.TestCase):
    """Проверять последовательное завершение только выбранного дочернего процесса."""

    def setUp(self) -> None:
        """Создать подмену процесса, не запуская реальную программу."""

        self.process = MagicMock(spec=subprocess.Popen)
        self.process.poll.return_value = None
        self.process.wait.return_value = 130

    def test_finished_process_is_not_signalled(self) -> None:
        """Уже завершённому процессу не отправляются дополнительные сигналы."""

        self.process.poll.return_value = 0
        _stop_child(self.process)
        self.process.send_signal.assert_not_called()
        self.process.wait.assert_not_called()

    def test_sigint_allows_normal_cleanup(self) -> None:
        """Первый сигнал даёт сборщику время записать собственный итог."""

        _stop_child(self.process)
        self.process.send_signal.assert_called_once_with(signal.SIGINT)
        self.process.wait.assert_called_once_with(timeout=10)

    def test_stopping_escalates_after_two_timeouts(self) -> None:
        """SIGTERM и SIGKILL применяются только после истечения предыдущего ожидания."""

        self.process.wait.side_effect = [subprocess.TimeoutExpired("collector", 10),
                                         subprocess.TimeoutExpired("collector", 10), -signal.SIGKILL]
        _stop_child(self.process)
        self.assertEqual(self.process.send_signal.call_args_list,
                         [call(signal.SIGINT), call(signal.SIGTERM), call(signal.SIGKILL)])
        self.assertEqual(self.process.wait.call_args_list, [call(timeout=10)] * 3)

    def test_disappeared_process_is_reaped_without_more_signals(self) -> None:
        """Исчезновение процесса между poll и сигналом не вызывает повторной эскалации."""

        self.process.send_signal.side_effect = ProcessLookupError("процесс уже завершился")
        _stop_child(self.process)
        self.process.send_signal.assert_called_once_with(signal.SIGINT)
        self.process.wait.assert_called_once_with()

    def test_unfinished_process_after_sigkill_is_an_error(self) -> None:
        """Неудача последнего ожидания не выдаётся за подтверждённое завершение."""

        self.process.wait.side_effect = subprocess.TimeoutExpired("collector", 10)

        with self.assertRaisesRegex(RuntimeError, "SIGKILL"):
            _stop_child(self.process)

        self.assertEqual(self.process.send_signal.call_count, 3)


class RunCollectionProcessTests(unittest.TestCase):
    """Проверять безопасный запуск и закрытие журнала при любых исходах попытки."""

    def setUp(self) -> None:
        """Подготовить временные пути и подмены процесса без сетевых запросов."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project_root = Path(temporary.name).resolve()
        self.log_path = self.project_root / "output.log"
        self.command = [sys.executable, "-B", "-c", "print('локальная проверка')"]
        self.process = MagicMock(spec=subprocess.Popen)
        self.process.poll.side_effect = [None, 7, 7]
        self.process.wait.return_value = 7
        self.stop = MagicMock(spec=threading.Event)
        self.stop.wait.return_value = False
        process_factory = patch("src.collect.continuous_collection.subprocess.Popen", return_value=self.process)
        self.factory = process_factory.start()
        self.addCleanup(process_factory.stop)

    def test_launch_preserves_arguments_log_and_lock_descriptor(self) -> None:
        """Команда запускается без оболочки, с отдельным журналом и наследуемой блокировкой."""

        self.assertEqual(_run_child(self.command, self.project_root, self.log_path, 123, self.stop), 7)
        positional, options = self.factory.call_args
        self.assertEqual(positional, (self.command,))
        self.assertEqual(options["cwd"], self.project_root)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.STDOUT)
        self.assertEqual(options["pass_fds"], (123,))
        self.assertTrue(options["start_new_session"])
        self.assertNotIn("shell", options)
        self.assertEqual(options["stdout"].name, str(self.log_path))
        self.assertTrue(options["stdout"].closed)
        self.stop.wait.assert_called_once_with(1)
        self.process.send_signal.assert_not_called()

    def test_stop_event_requests_cleanup_and_preserves_exit_code(self) -> None:
        """Сигнал остановки достигает дочернего процесса, а finally повторно проверяет завершение."""

        self.stop.wait.return_value = True
        self.process.poll.side_effect = [None, 130]
        self.process.wait.return_value = 130

        with patch("src.collect.continuous_collection._stop_child") as cleanup:
            self.assertEqual(_run_child(self.command, self.project_root, self.log_path, 123, self.stop), 130)

        self.assertEqual(cleanup.call_args_list, [call(self.process), call(self.process)])
        self.assertTrue(self.factory.call_args.kwargs["stdout"].closed)

    def test_exception_during_wait_still_stops_child_and_closes_log(self) -> None:
        """Неожиданная ошибка ожидания не оставляет дочерний процесс без попытки завершения."""

        self.stop.wait.side_effect = RuntimeError("ошибка ожидания")

        with patch("src.collect.continuous_collection._stop_child") as cleanup, \
                self.assertRaisesRegex(RuntimeError, "ошибка ожидания"):
            _run_child(self.command, self.project_root, self.log_path, 123, self.stop)

        cleanup.assert_called_once_with(self.process)
        self.assertTrue(self.factory.call_args.kwargs["stdout"].closed)

    def test_popen_failure_closes_log_without_nonexistent_child_cleanup(self) -> None:
        """Неудачный запуск закрывает файл журнала, не пытаясь завершить отсутствующий процесс."""

        self.factory.side_effect = OSError("программа недоступна")

        with patch("src.collect.continuous_collection._stop_child") as cleanup, \
                self.assertRaisesRegex(OSError, "программа недоступна"):
            _run_child(self.command, self.project_root, self.log_path, 123, self.stop)

        cleanup.assert_not_called()
        self.assertTrue(self.factory.call_args.kwargs["stdout"].closed)

    def test_existing_log_is_not_overwritten_or_reused(self) -> None:
        """Повторный путь попытки отклоняется до запуска и сохраняет прежний журнал."""

        self.log_path.write_bytes(b"previous attempt")

        with self.assertRaises(FileExistsError):
            _run_child(self.command, self.project_root, self.log_path, 123, self.stop)

        self.assertEqual(self.log_path.read_bytes(), b"previous attempt")
        self.factory.assert_not_called()

    def test_log_symlink_is_rejected_before_launch(self) -> None:
        """Журнал попытки не может перенаправить запись в другой файл."""

        target = self.project_root / "another.log"
        target.write_bytes(b"unchanged")
        self.log_path.symlink_to(target)

        with self.assertRaisesRegex(ValueError, "символические ссылки"):
            _run_child(self.command, self.project_root, self.log_path, 123, self.stop)

        self.factory.assert_not_called()
        self.assertEqual(target.read_bytes(), b"unchanged")


class CollectionStopSignalTests(unittest.TestCase):
    """Проверять временные обработчики сигналов без отправки сигналов процессу тестов."""

    def test_handlers_only_set_event_and_previous_handlers_are_restored(self) -> None:
        """Оба сигнала устанавливают событие, а выход восстанавливает прежнюю конфигурацию."""

        stop = threading.Event()
        previous = {signal.SIGINT: signal.SIG_DFL, signal.SIGTERM: signal.SIG_IGN}

        with patch("src.collect.continuous_collection.signal.getsignal", side_effect=previous.__getitem__), \
                patch("src.collect.continuous_collection.signal.signal") as configure:
            with _stop_signals(stop):
                installed = configure.call_args_list[:2]
                self.assertEqual([entry.args[0] for entry in installed], [signal.SIGINT, signal.SIGTERM])

                for entry in installed:
                    stop.clear()
                    entry.args[1](entry.args[0], None)
                    self.assertTrue(stop.is_set())

            self.assertEqual(configure.call_args_list[-2:],
                             [call(signal.SIGINT, signal.SIG_DFL), call(signal.SIGTERM, signal.SIG_IGN)])

    def test_handlers_are_restored_after_exception(self) -> None:
        """Исключение внутри защищённого участка не оставляет изменённые обработчики."""

        with patch("src.collect.continuous_collection.signal.getsignal", return_value=signal.SIG_DFL), \
                patch("src.collect.continuous_collection.signal.signal") as configure:
            with self.assertRaisesRegex(RuntimeError, "ошибка внутри"), _stop_signals(threading.Event()):
                raise RuntimeError("ошибка внутри")

            self.assertEqual(configure.call_args_list[-2:],
                             [call(signal.SIGINT, signal.SIG_DFL), call(signal.SIGTERM, signal.SIG_DFL)])

    def test_non_main_thread_is_rejected_before_signal_changes(self) -> None:
        """Установка обработчиков вне главного потока запрещена до обращения к signal."""

        with patch("src.collect.continuous_collection.threading.current_thread", return_value=object()), \
                patch("src.collect.continuous_collection.signal.getsignal") as read_signal, \
                patch("src.collect.continuous_collection.signal.signal") as configure, \
                self.assertRaisesRegex(ValueError, "главного потока"), _stop_signals(threading.Event()):
            self.fail("Не следует входить в контекст из другого потока")

        read_signal.assert_not_called()
        configure.assert_not_called()


class RealCollectionProcessTests(unittest.TestCase):
    """Проверять один короткий локальный процесс и наследование файловой блокировки."""

    def test_real_child_captures_output_and_retains_inherited_lock(self) -> None:
        """Дочерний Python удерживает блокировку после закрытия родительского дескриптора."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ready_path = root / "ready"
            release_path = root / "release"
            log_path = root / "output.log"
            stop = threading.Event()
            results: list[int] = []
            errors: list[BaseException] = []
            child_code = (
                "import os, sys, time\n"
                "from pathlib import Path\n"
                "os.fstat(int(sys.argv[1]))\n"
                "Path(sys.argv[2]).touch()\n"
                "print('child stdout', flush=True)\n"
                "print('child stderr', file=sys.stderr, flush=True)\n"
                "deadline = time.monotonic() + 15\n"
                "while not Path(sys.argv[3]).exists():\n"
                "    if time.monotonic() > deadline:\n"
                "        raise SystemExit(99)\n"
                "    time.sleep(0.01)\n"
                "raise SystemExit(7)\n"
            )

            def run_local_child(descriptor: int) -> None:
                """Сохранить итог потока, чтобы не скрыть исключение дочернего запуска."""

                command = [sys.executable, "-B", "-u", "-c", child_code,
                           str(descriptor), str(ready_path), str(release_path)]

                try:
                    results.append(_run_child(command, root, log_path, descriptor, stop))

                except BaseException as exception:
                    errors.append(exception)

            worker: threading.Thread | None = None

            try:
                with _runner_lock(root) as descriptor:
                    worker = threading.Thread(target=run_local_child, args=(descriptor,), daemon=True)
                    worker.start()
                    deadline = time.monotonic() + 8

                    while not ready_path.exists() and worker.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)

                    self.assertTrue(ready_path.exists(), f"Дочерний процесс не запустился: {errors}")

                with self.assertRaisesRegex(ValueError, "уже работает"), _runner_lock(root):
                    self.fail("Унаследованный дескриптор должен удерживать блокировку")

                release_path.touch()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive(), "Локальный дочерний процесс не завершился")
                self.assertEqual(errors, [])
                self.assertEqual(results, [7])
                self.assertEqual(log_path.read_text(encoding="utf-8"), "child stdout\nchild stderr\n")

                with _runner_lock(root) as new_descriptor:
                    self.assertGreaterEqual(new_descriptor, 0)

            finally:
                release_path.touch()
                stop.set()

                if worker is not None:
                    worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
