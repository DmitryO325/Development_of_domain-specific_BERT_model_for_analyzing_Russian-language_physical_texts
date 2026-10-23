"""Проверяемая локальная очередь полного обхода уже подключённых журналов."""

from __future__ import annotations

import json
import math
import re

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import jetp
from .ioffe import IOFFE_JOURNALS
from .ioffe_archive import IoffeArchive, MAX_RESPONSE_BYTES, write_json
from .ioffe_http import IoffeClient, IoffeError
from .journal_collection import decode_html
from .journal_sources import JournalSource

QUEUE_SCHEMA = "journal-collection-queue-v1"
MAX_QUEUE_BYTES = 4 * 1024 * 1024
TASK_STATUSES = frozenset({
    "pending", "running", "retry_wait", "complete", "complete_with_deferred",
    "blocked", "interrupted",
})
ARCHIVE_SOURCES = frozenset({"ufn", "jetp_letters", "quantum"})
QUEUE_FIELDS = frozenset({
    "schema_version", "created_at", "updated_at", "ioffe_origin",
    "jetp_form_evidence", "tasks",
})
TASK_FIELDS = frozenset({
    "task_id", "source_id", "volume", "journal_id", "metadata_only", "status",
    "attempts", "consecutive_errors", "next_retry_at", "last_error", "last_run_id",
    "last_log", "last_summary_path", "updated_at",
})
EVIDENCE_FIELDS = frozenset({
    "requested_url", "relative_path", "sha256", "bytes",
    "response_metadata_path", "response_metadata_sha256",
})
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


def _validate_origin(origin: str) -> None:
    """Разрешить только явно выбранный корень HTTP или HTTPS сайта Иоффе."""

    if not isinstance(origin, str) or origin not in {"http://journals.ioffe.ru", "https://journals.ioffe.ru"}:
        raise ValueError("Нужен явный HTTP/HTTPS-корень journals.ioffe.ru без дополнительных путей")


def _utc_time(value: Any, field: str) -> None:
    """Проверить время с явно указанным нулевым смещением UTC."""

    if not isinstance(value, str):
        raise ValueError(f"Поле {field} должно содержать строку времени UTC")

    try:
        parsed = datetime.fromisoformat(value)

    except ValueError as exception:
        raise ValueError(f"Некорректное время UTC в поле {field}") from exception

    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"В поле {field} требуется явно указанное время UTC")


def _validate_evidence(evidence: Any) -> None:
    """Проверить точные пути и контрольные суммы исходной формы ЖЭТФ."""

    if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_FIELDS:
        raise ValueError("Некорректные поля свидетельства формы ЖЭТФ")

    digest = evidence["sha256"]
    metadata_digest = evidence["response_metadata_sha256"]

    if (
        evidence["requested_url"] != jetp.START_URL or
        not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None or
        not isinstance(metadata_digest, str) or SHA256_PATTERN.fullmatch(metadata_digest) is None or
        type(evidence["bytes"]) is not int or not 0 < evidence["bytes"] <= MAX_RESPONSE_BYTES
    ):
        raise ValueError("Свидетельство формы ЖЭТФ содержит неверный адрес, размер или SHA-256")

    if (
        not isinstance(evidence["relative_path"], str) or evidence["relative_path"] not in {
            f"responses/{digest[:2]}/{digest}.html", f"responses/{digest[:2]}/{digest}.bin",
        } or
        evidence["response_metadata_path"] != f"responses/{digest[:2]}/{digest}.{metadata_digest}.http.json"
    ):
        raise ValueError("Пути свидетельства формы ЖЭТФ не соответствуют контрольным суммам")


def _new_task(
    source_id: str, *, volume: int | None = None, journal_id: int | None = None,
) -> dict[str, Any]:
    """Подготовить задачу без произвольных команд, сетевых параметров и путей."""

    selector = volume if source_id == "jetp" else journal_id if source_id == "ioffe" else "archive"

    return {
        "task_id": f"{source_id}-{selector}", "source_id": source_id,
        "volume": volume, "journal_id": journal_id,
        "metadata_only": source_id == "quantum", "status": "pending",
        "attempts": 0, "consecutive_errors": 0, "next_retry_at": None,
        "last_error": None, "last_run_id": None, "last_log": None,
        "last_summary_path": None, "updated_at": None,
    }


def build_queue(project_root: Path, *, ioffe_origin: str) -> dict[str, Any]:
    """Спланировать весь подтверждённый архив по локальному кешу, не включая сеть."""

    _validate_origin(ioffe_origin)
    root = Path(project_root).resolve()

    if not root.is_dir():
        raise ValueError("Корень проекта должен быть существующим каталогом")

    source = JournalSource(jetp)
    state_root = root / "manifests" / "imports" / "journals"
    client = IoffeClient(state_root / "rate_limit" / "jetp", validate_url=source.validate_url)
    archive = IoffeArchive(
        root / "data" / "raw" / "journals" / "jetp", state_root / "jetp", client,
        allow_network=False, validate_url=source.validate_url,
        accepted_statuses=source.accepted_statuses,
    )
    snapshot = archive.get_response(jetp.START_URL)
    evidence = archive.evidence_for(snapshot)
    volumes = jetp.extract_volumes(decode_html(snapshot, jetp.DEFAULT_ENCODING), jetp.START_URL)
    tasks = [
        _new_task("ufn"), *[_new_task("jetp", volume=volume) for volume in volumes],
        _new_task("jetp_letters"),
        *[_new_task("ioffe", journal_id=journal_id) for journal_id in sorted(IOFFE_JOURNALS)],
        _new_task("quantum"),
    ]
    timestamp = datetime.now(UTC).isoformat()
    queue = {
        "schema_version": QUEUE_SCHEMA, "created_at": timestamp, "updated_at": timestamp,
        "ioffe_origin": ioffe_origin, "jetp_form_evidence": evidence, "tasks": tasks,
    }
    validate_queue(queue)

    return queue


def _validate_task_identity(task: dict[str, Any]) -> None:
    """Связать идентификатор задачи с единственным разрешённым источником и выбором."""

    source_id = task["source_id"]
    volume = task["volume"]
    journal_id = task["journal_id"]

    if source_id == "jetp":
        if type(volume) is not int or volume <= 0 or journal_id is not None:
            raise ValueError("Задача ЖЭТФ должна содержать только положительный номер тома")

        expected_id = f"jetp-{volume}"

    elif source_id == "ioffe":
        if type(journal_id) is not int or journal_id not in IOFFE_JOURNALS or volume is not None:
            raise ValueError("Задача Иоффе должна содержать номер одного из подключённых журналов")

        expected_id = f"ioffe-{journal_id}"

    elif isinstance(source_id, str) and source_id in ARCHIVE_SOURCES:
        if volume is not None or journal_id is not None:
            raise ValueError("Задача полного архива не допускает номер тома или журнала Иоффе")

        expected_id = f"{source_id}-archive"

    else:
        raise ValueError("Неизвестный источник в очереди сбора")

    if task["task_id"] != expected_id:
        raise ValueError("Идентификатор задачи не соответствует источнику и выбранному архиву")

    if type(task["metadata_only"]) is not bool or task["metadata_only"] != (source_id == "quantum"):
        raise ValueError("Quantum допускает только метаданные; остальные задачи предусматривают PDF")


def _validate_task_runtime(task: dict[str, Any]) -> None:
    """Проверить состояние продолжения, не принимая произвольные пути или команды."""

    if not isinstance(task["status"], str) or task["status"] not in TASK_STATUSES:
        raise ValueError("Неизвестный статус задачи очереди")

    for field in ("attempts", "consecutive_errors"):
        if type(task[field]) is not int or task[field] < 0:
            raise ValueError(f"Поле {field} должно быть целым неотрицательным числом")

    next_retry_at = task["next_retry_at"]

    if next_retry_at is not None:
        try:
            valid_retry = type(next_retry_at) in {int, float} and math.isfinite(next_retry_at) and next_retry_at >= 0

        except OverflowError:
            valid_retry = False

        if not valid_retry:
            raise ValueError("Время следующего повтора должно быть конечным неотрицательным числом")

    if task["last_error"] is not None and not isinstance(task["last_error"], str):
        raise ValueError("Последняя ошибка должна быть строкой или null")

    run_id = task["last_run_id"]

    if run_id is not None and (not isinstance(run_id, str) or RUN_ID_PATTERN.fullmatch(run_id) is None):
        raise ValueError("Идентификатор запуска должен быть UUID в виде 32 шестнадцатеричных символов")

    for field, filename in (("last_log", "output.log"), ("last_summary_path", "summary.json")):
        if task[field] is not None and (run_id is None or task[field] != f"attempts/{run_id}/{filename}"):
            raise ValueError(f"Поле {field} должно указывать на относительный путь своего запуска")

    if task["updated_at"] is not None:
        _utc_time(task["updated_at"], "tasks.updated_at")


def validate_queue(queue: dict[str, Any]) -> None:
    """Отклонить повреждённую или неполную очередь до запуска внешних сборщиков."""

    if not isinstance(queue, dict) or set(queue) != QUEUE_FIELDS or queue["schema_version"] != QUEUE_SCHEMA:
        raise ValueError("Неизвестная схема или набор полей очереди сбора")

    if not isinstance(queue["ioffe_origin"], str):
        raise ValueError("Адрес источника Иоффе должен быть строкой")

    _validate_origin(queue["ioffe_origin"])
    _utc_time(queue["created_at"], "created_at")
    _utc_time(queue["updated_at"], "updated_at")
    _validate_evidence(queue["jetp_form_evidence"])
    tasks = queue["tasks"]

    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Очередь должна содержать непустой список задач")

    identifiers: set[str] = set()

    for task in tasks:
        if not isinstance(task, dict) or set(task) != TASK_FIELDS:
            raise ValueError("Некорректные поля задачи очереди")

        _validate_task_identity(task)
        _validate_task_runtime(task)
        identifier = task["task_id"]

        if identifier in identifiers:
            raise ValueError(f"Повторяющаяся задача очереди: {identifier}")

        identifiers.add(identifier)

    required = {f"{source}-archive" for source in ARCHIVE_SOURCES}
    required.update(f"ioffe-{journal_id}" for journal_id in IOFFE_JOURNALS)

    if not required.issubset(identifiers) or not any(task["source_id"] == "jetp" for task in tasks):
        raise ValueError("Очередь не содержит всех подключённых журналов")


def _safe_queue_path(path: Path) -> Path:
    """Запретить обход пути, символические ссылки и специальные файлы очереди."""

    path = Path(path)

    if ".." in path.parts or any(ord(character) < 32 or ord(character) == 127 for character in str(path)):
        raise IoffeError("Недопустимый путь очереди сбора")

    if path.suffix != ".json" or "\\" in str(path):
        raise IoffeError("Очередь должна храниться в обычном JSON-файле")

    absolute = path.absolute()

    if any(parent.is_symlink() for parent in (absolute, *absolute.parents)):
        raise IoffeError("Путь очереди не должен содержать символические ссылки")

    if absolute.exists() and not absolute.is_file():
        raise IoffeError("Путь очереди не является обычным файлом")

    return absolute


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Отклонить повторяющиеся JSON-поля вместо молчаливого выбора последнего."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторяющееся поле JSON: {key}")

        result[key] = value

    return result


def load_queue(path: Path) -> dict[str, Any]:
    """Прочитать и проверить сохранённую очередь с ограничением размера файла."""

    target = _safe_queue_path(path)

    try:
        with target.open("rb") as queue_file:
            body = queue_file.read(MAX_QUEUE_BYTES + 1)

        if len(body) > MAX_QUEUE_BYTES:
            raise ValueError("Превышен допустимый размер очереди")

        queue = json.loads(body, object_pairs_hook=_unique_json_object)
        validate_queue(queue)

    except (OSError, ValueError, TypeError, RecursionError) as exception:
        raise IoffeError(f"Не удалось прочитать очередь сбора: {target}: {exception}") from exception

    return queue


def save_queue(path: Path, queue: dict[str, Any]) -> None:
    """Атомарно сохранить проверенное состояние, не затрагивая данные журналов."""

    validate_queue(queue)
    target = _safe_queue_path(path)

    body = (json.dumps(queue, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    if len(body) > MAX_QUEUE_BYTES:
        raise ValueError("Превышен допустимый размер очереди")

    write_json(target, queue)
