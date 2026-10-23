"""Непрерывное выполнение очереди журналов с проверяемыми итогами каждой попытки."""

from __future__ import annotations

import fcntl
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from .collection_queue import load_queue, save_queue
from .ioffe_http import IoffeError

FINISHED_STATUSES = {"complete", "complete_with_deferred"}
MAX_SUMMARY_BYTES = 4 * 1024 * 1024
SOURCES = ("ufn", "jetp", "jetp_letters", "ioffe", "quantum")


def _safe_path(path: Path) -> Path:
    """Проверить путь служебного файла до открытия или создания каталогов."""

    path = path.absolute()

    if ".." in path.parts or any(component.is_symlink() for component in (path, *path.parents)):
        raise ValueError(f"Служебный путь не должен содержать переходы или символические ссылки: {path}")

    return path


@contextmanager
def _runner_lock(project_root: Path) -> Generator[int, None, None]:
    """Не допустить второй запускатель в том же проекте, даже с другой очередью."""

    path = _safe_path(project_root / "manifests/imports/collection_queue/runner.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)

    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        except BlockingIOError as exception:
            raise ValueError("Непрерывный сбор или его дочерний сборщик уже работает в этом проекте") from exception

        yield descriptor

    finally:
        # Блокировку снимает закрытие последнего дескриптора, включая унаследованные дочерними процессами.
        os.close(descriptor)


@contextmanager
def _stop_signals(stop: threading.Event) -> Generator[None, None, None]:
    """Попросить потоки завершить дочерние процессы при Ctrl+C или SIGTERM."""

    if threading.current_thread() is not threading.main_thread():
        raise ValueError("Непрерывный сбор нужно запускать из главного потока программы")

    def request_stop(signum: int, frame: FrameType | None) -> None:
        """Передать сигнал остановки без записи файлов внутри обработчика."""

        stop.set()

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    try:
        for signum in previous:
            signal.signal(signum, request_stop)

        yield

    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _command(
    task: dict[str, Any], queue: dict[str, Any], summary_path: Path, timeout_seconds: float,
) -> list[str]:
    """Составить команду только из разрешённых источников и проверенных селекторов."""

    common = [
        "--max-requests", "0", "--timeout", str(timeout_seconds), "--allow-network",
        "--run-id", task["last_run_id"], "--summary-path", str(summary_path),
    ]

    if task["source_id"] == "ioffe":
        return [
            sys.executable, "-B", "-u", "scripts/download_ioffe.py", "journal", str(task["journal_id"]),
            "--origin", queue["ioffe_origin"], "--max-issues", "0", "--max-articles", "0", *common,
        ]

    command = [
        sys.executable, "-B", "-u", "scripts/download_journals.py", "crawl", task["source_id"],
        "--max-articles", "0", *common,
    ]

    if task["volume"] is not None:
        command.extend(["--volume", str(task["volume"])])

    if task["metadata_only"]:
        command.append("--metadata-only")

    return command


def _unique_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Отклонить повторяющиеся поля, чтобы ошибка не маскировалась поздним значением."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторяющееся поле итога: {key}")

        result[key] = value

    return result


def _read_summary(path: Path) -> dict[str, Any]:
    """Прочитать только отдельный итог текущей попытки с ограничением размера."""

    _safe_path(path)

    with path.open("rb") as summary_file:
        body = summary_file.read(MAX_SUMMARY_BYTES + 1)

    if len(body) > MAX_SUMMARY_BYTES:
        raise ValueError("Итог попытки превышает допустимый размер")

    summary = json.loads(body, object_pairs_hook=_unique_fields)

    if not isinstance(summary, dict):
        raise ValueError("Итог попытки должен содержать JSON-объект")

    return summary


def _attempt_result(
    task: dict[str, Any], queue: dict[str, Any], summary_path: Path, return_code: int | None,
) -> tuple[str, str | None]:
    """
    Сверить итог с запуском; повторять только явно обозначенный сетевой сбой.

    При восстановлении под общей блокировкой код завершения прежнего процесса
    неизвестен: None разрешает проверить только сохранённый итог, не выдумывая код.
    """

    try:
        summary = _read_summary(summary_path)
        expected = {
            "run_id": task["last_run_id"], "source_id": task["source_id"],
            "command": "journal" if task["source_id"] == "ioffe" else "crawl",
            "selected_volume": task["volume"], "selected_journal": task["journal_id"],
            "metadata_only": task["metadata_only"], "max_requests": 0, "max_articles": 0,
            "max_issues": 0 if task["source_id"] == "ioffe" else None,
        }

        if task["source_id"] == "ioffe":
            expected.update({"origin": queue["ioffe_origin"], "selected_issue": None, "selected_article": None})

        else:
            expected["start_url"] = None

        for key, value in expected.items():
            if key not in summary or type(summary[key]) is not type(value) or summary[key] != value:
                raise ValueError(f"Поле {key} в итоге не соответствует текущей попытке")

        if type(summary.get("retryable_error")) is not bool:
            raise ValueError("В итоге отсутствует явный признак допустимости повтора")

        for key in ("network_requests", "cards_processed", "cards_with_pdf"):
            if type(summary.get(key)) is not int or summary[key] < 0:
                raise ValueError(f"Некорректный счётчик {key} в итоге попытки")

        status = summary.get("status")
        error = summary.get("error")

        if "error" not in summary:
            raise ValueError("В итоге отсутствует поле ошибки")

        if return_code in (None, 0) and isinstance(status, str) and status in FINISHED_STATUSES:
            if error is not None or summary["retryable_error"]:
                raise ValueError("Итог одновременно объявляет завершение и ошибку")

            return status, None if status == "complete" else "Есть отложенные публикации; подробности в итоге попытки"

        if return_code in (None, 130) and status == "interrupted":
            if error is not None or summary["retryable_error"]:
                raise ValueError("Итог одновременно объявляет прерывание и ошибку")

            return "interrupted", "Сборщик остановлен; сохранённые ответы остаются в кеше"

        if return_code in (None, 1) and status == "error" and isinstance(error, str) and error.strip():
            return "retry_wait" if summary["retryable_error"] else "blocked", error

        raise ValueError(f"Несогласованный результат сборщика: код {return_code}, статус {status!r}")

    except (OSError, ValueError, TypeError, RecursionError) as exception:
        return "blocked", f"Нельзя подтвердить результат попытки: {exception}"


def _stop_child(process: subprocess.Popen[bytes]) -> None:
    """Дать сборщику сохранить итог и завершить только этот дочерний процесс."""

    if process.poll() is not None:
        return

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            process.send_signal(signum)
            process.wait(timeout=10)

            return

        except ProcessLookupError:
            process.wait()

            return

        except subprocess.TimeoutExpired:
            continue

    raise RuntimeError("Дочерний сборщик не завершился после SIGKILL")


def _run_child(
    command: list[str], project_root: Path, log_path: Path, lock_descriptor: int, stop: threading.Event,
) -> int:
    """Записать вывод в отдельный журнал и дождаться завершения без оболочки."""

    with _safe_path(log_path).open("xb") as log_file:
        process = subprocess.Popen(
            command, cwd=project_root, stdin=subprocess.DEVNULL, stdout=log_file,
            stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock_descriptor,),
        )

        try:
            while process.poll() is None:
                if stop.wait(1):
                    _stop_child(process)

            return process.wait()

        finally:
            _stop_child(process)


def _retry_delay(errors: int, base_seconds: float, maximum_seconds: float) -> float:
    """Увеличить паузу экспоненциально без переполнения после долгого отсутствия сети."""

    delay = base_seconds

    for _ in range(min(max(errors - 1, 0), 1024)):
        if delay >= maximum_seconds / 2:
            return maximum_seconds

        delay *= 2

    return min(delay, maximum_seconds)


class _QueueRunner:
    """Сохранять переходы состояния и выполнять по одному сборщику на источник."""

    def __init__(
        self, queue_path: Path, project_root: Path, lock_descriptor: int, *,
        timeout_seconds: float, retry_base_seconds: float, retry_max_seconds: float,
    ) -> None:
        """Загрузить очередь уже под общей блокировкой проекта."""

        self.queue_path = queue_path
        self.project_root = project_root
        self.lock_descriptor = lock_descriptor
        self.timeout_seconds = timeout_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.queue = load_queue(queue_path)
        self.mutex = threading.RLock()
        self.stop = threading.Event()

    def _save(self, task: dict[str, Any], **changes: Any) -> None:
        """Сохранить переход одной задачи вместе с общим временем изменения."""

        with self.mutex:
            timestamp = datetime.now(UTC).isoformat()
            task.update(changes, updated_at=timestamp)
            self.queue["updated_at"] = timestamp
            save_queue(self.queue_path, self.queue)

    def _prepare_attempt(self, task: dict[str, Any]) -> tuple[Path, Path]:
        """Создать уникальный каталог и записать намерение до запуска сборщика."""

        run_id = uuid.uuid4().hex
        directory = _safe_path(self.queue_path.parent / "attempts" / run_id)
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.mkdir()
        log_relative = f"attempts/{run_id}/output.log"
        summary_relative = f"attempts/{run_id}/summary.json"
        self._save(
            task, status="running", attempts=task["attempts"] + 1, next_retry_at=None,
            last_run_id=run_id, last_log=log_relative, last_summary_path=summary_relative, last_error=None,
        )

        return directory / "output.log", directory / "summary.json"

    def _attempt(self, task: dict[str, Any]) -> None:
        """Выполнить одну попытку и сохранить её подтверждённое состояние."""

        log_path, summary_path = self._prepare_attempt(task)
        print(f"{task['task_id']}: попытка {task['attempts']}; журнал {log_path}", flush=True)

        try:
            command = _command(task, self.queue, summary_path, self.timeout_seconds)
            return_code = _run_child(command, self.project_root, log_path, self.lock_descriptor, self.stop)
            status, error = _attempt_result(task, self.queue, summary_path, return_code)

        except OSError as exception:
            status, error = "blocked", f"Не удалось запустить сборщик или записать журнал: {exception}"

        if status == "interrupted":
            self.stop.set()

        # Сигнал очереди не отменяет уже подтверждённый успех или запрет дочернего сборщика.
        self._save_result(task, status, error)

    def _save_result(self, task: dict[str, Any], status: str, error: str | None) -> None:
        """Сохранить результат и назначить возрастающую паузу только разрешённому повтору."""

        consecutive_errors = task["consecutive_errors"] + 1 if status == "retry_wait" else 0
        retry_at = None

        if status == "retry_wait":
            delay = _retry_delay(consecutive_errors, self.retry_base_seconds, self.retry_max_seconds)
            retry_at = time.time() + delay
            print(f"{task['task_id']}: временный сетевой сбой; повтор через {delay:g} с", flush=True)

        else:
            print(f"{task['task_id']}: {status}" + (f"; {error}" if error else ""), flush=True)

        self._save(task, status=status, last_error=error, consecutive_errors=consecutive_errors, next_retry_at=retry_at)

    def _recover_attempt(self, task: dict[str, Any]) -> None:
        """Сверить незакрытую попытку с её итогом после получения общей блокировки."""

        if task["status"] not in {"running", "interrupted"}:
            return

        summary_relative = task["last_summary_path"]

        if task["last_run_id"] is None or summary_relative is None:
            status, error = "blocked", "Нельзя восстановить попытку: отсутствует идентификатор запуска или путь итога"

        else:
            status, error = _attempt_result(
                task, self.queue, self.queue_path.parent / summary_relative, return_code=None,
            )

        if status == "interrupted":
            status, error = "pending", None

        # Даже намерение до Popen без итога нельзя отличить от потери файлов после запрета HTTP.
        # Отсутствующий или повреждённый итог остаётся blocked, а не разрешает новый запрос.
        print(f"{task['task_id']}: восстановление по сохранённому итогу; код завершения неизвестен", flush=True)
        self._save_result(task, status, error)

    def _source(self, source_id: str) -> None:
        """Обходить задания одного сайта последовательно, останавливаясь на неподтверждённой ошибке."""

        try:
            for task in self.queue["tasks"]:
                if task["source_id"] != source_id or task["status"] in FINISHED_STATUSES:
                    continue

                while not self.stop.is_set():
                    if task["status"] == "blocked":
                        return

                    retry_at = task["next_retry_at"]
                    remaining = max(0.0, retry_at - time.time()) if retry_at is not None else 0.0

                    if remaining:
                        self.stop.wait(min(remaining, 30.0))

                        continue

                    self._attempt(task)

                    if task["status"] in FINISHED_STATUSES:
                        break

                if self.stop.is_set():
                    return

        except BaseException:
            # Ошибка сохранения очереди должна остановить остальные потоки, а не оставить их без учёта.
            self.stop.set()

            raise

    def run(self) -> int:
        """Продолжить незавершённые задачи, сохранив явные запреты и успешные результаты."""

        for task in self.queue["tasks"]:
            self._recover_attempt(task)

        with _stop_signals(self.stop), ThreadPoolExecutor(max_workers=len(SOURCES)) as executor:
            futures = [executor.submit(self._source, source_id) for source_id in SOURCES]

            for future in futures:
                future.result()

        if self.stop.is_set():
            return 130

        statuses = {task["status"] for task in self.queue["tasks"]}

        if not statuses.issubset(FINISHED_STATUSES):
            return 1

        return 2 if "complete_with_deferred" in statuses else 0


def run_queue(
    queue_path: Path, project_root: Path, *, allow_network: bool = False,
    timeout_seconds: float = 60.0, retry_base_seconds: float = 60.0, retry_max_seconds: float = 1800.0,
) -> int:
    """Запустить сохранённую очередь только с явным разрешением сети и общей блокировкой."""

    if allow_network is not True:
        raise ValueError("Для непрерывного сбора нужно явно разрешить сеть")

    values = (timeout_seconds, retry_base_seconds, retry_max_seconds)

    if any(type(value) not in {int, float} or not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError("Тайм-аут и интервалы повторов должны быть конечными положительными числами")

    if retry_base_seconds < 60 or retry_max_seconds < retry_base_seconds:
        raise ValueError("Начальная пауза должна быть не меньше 60 секунд, максимальная — не меньше начальной")

    root = _safe_path(Path(project_root))

    if not root.is_dir():
        raise ValueError("Не найден каталог проекта")

    queue_path = _safe_path(Path(queue_path))

    with _runner_lock(root) as descriptor:
        runner = _QueueRunner(
            queue_path, root, descriptor, timeout_seconds=timeout_seconds,
            retry_base_seconds=retry_base_seconds, retry_max_seconds=retry_max_seconds,
        )

        try:
            return runner.run()

        except IoffeError as exception:
            raise ValueError(f"Сбор остановлен: {exception}") from exception
