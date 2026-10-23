"""Офлайн-регистрация новых журнальных загрузок с квитанциями и продолжением после остановки."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import threading
import uuid

from collections.abc import Generator
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from src.collect.ioffe_archive import _write_bytes

from .journal_inventory_input import (
    INVENTORY_SCHEMA, LEGACY_INVENTORY_SCHEMA, INVENTORY_SOURCES, MAX_BATCH_BYTES, MAX_RECORD_BYTES,
    InventoryBatch, _inside, _json_object, _read, _signature, _source_suffix,
    prepare_inventory_batch, read_inventory_batch,
)
from .journal_inventory_registration import deferred_source_report, reconcile_inventory_registration
from .manifests import ManifestConcurrencyError, ManifestStore

STATE_SCHEMA = "continuous-inventory-state-v1"
RECEIPT_SCHEMA = "journal-inventory-receipt-v1"
DEFAULT_STATE_DIR = Path("manifests/imports/continuous_inventory")
MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_JOURNAL_BYTES = 32 * 1024 * 1024
MAX_COMMIT_ATTEMPTS = 3


def _now() -> str:
    """Зафиксировать время служебного события с часовым поясом."""

    return datetime.now(UTC).isoformat(timespec="microseconds")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    """Получить однозначное представление состояния или квитанции."""

    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _state_directory(root: Path, state_dir: Path | None) -> Path:
    """Не разрешать служебные записи вне каталога импортов проекта."""

    directory = _inside(root, state_dir if state_dir is not None else DEFAULT_STATE_DIR)
    imports = _inside(root, Path("manifests/imports"))

    if directory == imports or not directory.is_relative_to(imports):
        raise ValueError("Каталог состояния должен находиться внутри manifests/imports")

    if directory.exists() and not directory.is_dir():
        raise ValueError("Каталог состояния занят обычным файлом")

    return directory


@contextmanager
def _runner_lock(root: Path) -> Generator[None, None, None]:
    """Не допускать двух автоматических регистраторов даже с разными каталогами состояния."""

    path = _inside(root, Path("manifests/imports/.continuous_inventory.lock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)

    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        except BlockingIOError as exception:
            raise ValueError("Автоматическая регистрация уже запущена в этом проекте") from exception

        yield

    finally:
        os.close(descriptor)


@contextmanager
def _stop_signals(stop: threading.Event) -> Generator[None, None, None]:
    """Заканчивать текущую партию при сигнале, не прерывая атомарную запись реестров."""

    if threading.current_thread() is not threading.main_thread():
        raise ValueError("Непрерывную регистрацию нужно запускать из главного потока")

    def request_stop(signum: int, frame: FrameType | None) -> None:
        """Отложить остановку до завершения текущей безопасной операции."""

        stop.set()

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    try:
        for signum in previous:
            signal.signal(signum, request_stop)

        yield

    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _save_state(path: Path, state: dict[str, Any]) -> None:
    """Атомарно заменить контрольную точку только после сохранения необходимых свидетельств."""

    if any(component.is_symlink() for component in (path, *path.parents)):
        raise ValueError("Путь состояния не должен содержать символические ссылки")

    body = _json_bytes(state)

    if len(body) > MAX_STATE_BYTES:
        raise ValueError("Состояние автоматической регистрации превышает допустимый размер")

    _write_bytes(path, body)


def _journal(directory: Path, event: str, **details: Any) -> None:
    """Сохранить отдельное событие запуска с принудительной записью на диск."""

    path = directory / "events.jsonl"

    if any(component.is_symlink() for component in (path, *path.parents)):
        raise ValueError("Журнал регистрации не должен быть символической ссылкой")

    payload = {"at": _now(), "event": event, **details}
    body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)

    with os.fdopen(descriptor, "ab") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def _check_journal(directory: Path) -> None:
    """Не продолжать работу поверх повреждённого или обрезанного журнала."""

    path = directory / "events.jsonl"

    if not path.exists() and not path.is_symlink():
        return

    body = _read(path, MAX_JOURNAL_BYTES)

    if body and not body.endswith(b"\n"):
        raise ValueError("Журнал регистрации оборван; требуется ручная проверка")

    for line in body.splitlines():
        event = _json_object(line)

        if not isinstance(event.get("at"), str) or not isinstance(event.get("event"), str):
            raise ValueError("Некорректное событие журнала регистрации")


def _descriptors(root: Path, batch_path: Path, body: bytes) -> dict[str, str]:
    """Проверить закреплённые карточки без повторного разбора всех PDF завершённых партий."""

    packet = _json_object(body)

    if (
        packet.get("schema_version") not in {INVENTORY_SCHEMA, LEGACY_INVENTORY_SCHEMA} or
        packet.get("status") != "prepared_not_registered"
    ):
        raise ValueError("Неизвестный формат завершённой партии")

    seen: dict[str, str] = {}
    categories = ("records", "skipped", "deferred") if packet["schema_version"] == INVENTORY_SCHEMA else ("records", "skipped")

    if packet["schema_version"] == LEGACY_INVENTORY_SCHEMA and "deferred" in packet:
        raise ValueError("Старая партия не поддерживает отдельные конфликты исходников")

    for category in categories:
        descriptors = packet.get(category)

        if not isinstance(descriptors, list):
            raise ValueError("В партии отсутствует список карточек")

        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise ValueError("Некорректное описание закреплённой карточки")

            source = descriptor.get("source")
            original = descriptor.get("original_path")
            digest = descriptor.get("record_sha256")

            if source not in INVENTORY_SOURCES or not isinstance(original, str) or not isinstance(digest, str):
                raise ValueError("Некорректный источник, путь или контрольная сумма карточки")

            original_path = Path(original)
            parent = Path("manifests/imports") / _source_suffix(source)

            if original_path.parent not in {parent / "articles", parent / "metadata"} or original_path.suffix != ".json":
                raise ValueError("Исходная карточка расположена вне штатных каталогов")

            expected_copy = batch_path.parent / "records" / source / original_path.parent.name / original_path.name
            record_path = descriptor.get("record_path")

            if not isinstance(record_path, str) or _inside(root, record_path) != expected_copy or original in seen:
                raise ValueError("Путь закреплённой карточки повторяется или не соответствует партии")

            if hashlib.sha256(_read(_inside(root, expected_copy), MAX_RECORD_BYTES)).hexdigest() != digest:
                raise ValueError("Изменилась закреплённая карточка завершённой партии")

            seen[original] = digest

    if not seen:
        raise ValueError("Завершённая партия не содержит карточек")

    return seen


def _completion(root: Path, batch_path: Path, receipt_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Связать реальные байты партии с квитанцией успешной записи, а не предварительной проверки."""

    batch_path = _inside(root, batch_path)
    receipt_path = _inside(root, receipt_path)

    if receipt_path.parent != batch_path.parent / "receipts":
        raise ValueError("Квитанция должна находиться в каталоге receipts своей партии")

    batch_body = _read(batch_path, MAX_BATCH_BYTES)
    receipt_body = _read(receipt_path, MAX_STATE_BYTES)
    digest = hashlib.sha256(batch_body).hexdigest()
    receipt = _json_object(receipt_body)
    result = receipt.get("result")

    if receipt.get("schema_version") != RECEIPT_SCHEMA or receipt.get("batch_sha256") != digest:
        raise ValueError("Квитанция не подтверждает выбранную партию")

    if not isinstance(result, dict) or result.get("dry_run") is not False:
        raise ValueError("Нужна квитанция состоявшейся регистрации, не dry-run")

    if receipt.get("rights_changed") is not False or receipt.get("training_eligibility_changed") is not False:
        raise ValueError("Квитанция должна подтверждать неизменность прав и допуска к обучению")

    for action in ("inserted", "updated"):
        counts = result.get(action)

        if not isinstance(counts, dict) or any(
            type(counts.get(kind)) is not int or counts[kind] != 0 for kind in ("rights", "condition_fulfilments")
        ):
            raise ValueError("Квитанция содержит изменение оснований или условий использования")

    packet = _json_object(batch_body)

    if receipt.get("skipped_non_russian") != packet.get("skipped") or not isinstance(receipt.get("report"), list):
        raise ValueError("Квитанция не содержит согласованного отчёта по карточкам партии")

    if packet.get("schema_version") == INVENTORY_SCHEMA:
        deferred = packet.get("deferred")

        if not isinstance(deferred, list) or receipt.get("deferred_source") != deferred:
            raise ValueError("Квитанция не подтверждает отложенные конфликты исходников")

        for entry in deferred_source_report(deferred, root):
            if receipt["report"].count(entry) != 1:
                raise ValueError("Конфликт исходника должен быть однократно отражён в отчёте")

    elif receipt.get("deferred_source", []):
        raise ValueError("Старая партия не подтверждает конфликты исходников из квитанции")

    for entry in receipt["report"]:
        if not isinstance(entry, dict) or entry.get("status") not in {"already_registered", "skipped", "deferred"}:
            raise ValueError("Квитанция содержит неизвестный результат проверки карточки")

        if any(not isinstance(entry.get(field), str) for field in ("reason", "url", "path", "source")):
            raise ValueError("В отчёте квитанции нет контекста рассмотренной карточки")

    completed = {
        "batch_path": batch_path.relative_to(root).as_posix(), "batch_sha256": digest,
        "receipt_path": receipt_path.relative_to(root).as_posix(),
        "receipt_sha256": hashlib.sha256(receipt_body).hexdigest(),
    }

    return completed, _descriptors(root, batch_path, batch_body)


def _load_state(root: Path, directory: Path) -> tuple[dict[str, Any], dict[str, str], bool]:
    """Восстановить рассмотренные версии карточек только из подтверждённых неизменяемых партий."""

    path = _inside(root, directory / "state.json")

    if not path.exists() and not path.is_symlink():
        if (directory / "events.jsonl").exists() or (directory / "batches").exists():
            raise ValueError("Контрольная точка отсутствует при сохранённом журнале или партиях; требуется ручная проверка")

        return {"schema_version": STATE_SCHEMA, "updated_at": _now(), "completed": [], "pending": None}, {}, False

    state = _json_object(_read(path, MAX_STATE_BYTES))

    if set(state) != {"schema_version", "updated_at", "completed", "pending"} or state["schema_version"] != STATE_SCHEMA:
        raise ValueError("Неизвестный формат состояния автоматической регистрации")

    if not isinstance(state["updated_at"], str) or not isinstance(state["completed"], list):
        raise ValueError("Повреждено состояние автоматической регистрации")

    seen: dict[str, str] = {}
    completed_paths: set[str] = set()

    for descriptor in state["completed"]:
        if not isinstance(descriptor, dict) or set(descriptor) != {"batch_path", "batch_sha256", "receipt_path", "receipt_sha256"}:
            raise ValueError("Повреждено описание завершённой партии")

        if any(not isinstance(value, str) for value in descriptor.values()):
            raise ValueError("Пути и контрольные суммы завершённой партии должны быть строками")

        completed, card_hashes = _completion(root, Path(descriptor["batch_path"]), Path(descriptor["receipt_path"]))

        if completed != descriptor or completed["batch_path"] in completed_paths:
            raise ValueError("Завершённая партия повторяется или изменилась после контрольной точки")

        completed_paths.add(completed["batch_path"])
        seen.update(card_hashes)

    pending = state["pending"]

    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {"directory", "selected_paths"}:
            raise ValueError("Повреждено описание незавершённой партии")

        if not isinstance(pending["directory"], str) or not isinstance(pending["selected_paths"], list):
            raise ValueError("Некорректные пути незавершённой партии")

        pending_dir = _inside(root, pending["directory"])

        if pending_dir.parent != directory / "batches":
            raise ValueError("Незавершённая партия находится вне каталога автоматической регистрации")

        selected = pending["selected_paths"]

        if not selected or any(not isinstance(value, str) for value in selected) or len(set(selected)) != len(selected):
            raise ValueError("Состав незавершённой партии пуст или содержит повторы")

        for value in selected:
            _card_source(root, Path(value))

    return state, seen, True


def _status(directory: Path, state: dict[str, Any], seen: dict[str, str], exists: bool) -> dict[str, Any]:
    """Сформировать краткое состояние без раскрытия всего списка публикаций."""

    return {
        "state_path": str(directory / "state.json"), "state_exists": exists,
        "completed_batches": len(state["completed"]), "seen_cards": len(seen),
        "pending": state["pending"] is not None,
        "last_receipt": state["completed"][-1]["receipt_path"] if state["completed"] else None,
    }


def load_inventory_status(project_root: Path, *, state_dir: Path | None = None) -> dict[str, Any]:
    """Проверить сохранённое состояние без создания файлов, регистрации или сетевых запросов."""

    root = Path(project_root).resolve()
    directory = _state_directory(root, state_dir)
    _check_journal(directory)
    state, seen, exists = _load_state(root, directory)

    return _status(directory, state, seen, exists)


def _card_source(root: Path, path: Path) -> str:
    """Разрешить только JSON-карточки пяти штатных сборщиков."""

    checked = _inside(root, path)

    for source in INVENTORY_SOURCES:
        parent = root / "manifests/imports" / _source_suffix(source)

        if checked.parent in {parent / "articles", parent / "metadata"} and checked.suffix == ".json":
            return source

    raise ValueError("Выбранная карточка находится вне штатных каталогов сборщиков")


def _scan_cards(root: Path, seen: dict[str, str]) -> tuple[Path, ...]:
    """Прочитать только небольшие карточки, не разбирать повторно уже учтённые PDF и HTML."""

    changed: list[Path] = []

    for source in INVENTORY_SOURCES:
        parent = _inside(root, Path("manifests/imports") / _source_suffix(source))

        if parent.exists() and not parent.is_dir():
            raise ValueError(f"Ожидается каталог сборщика: {parent}")

        for name in ("articles", "metadata"):
            directory = _inside(root, parent / name)

            if directory.exists() and not directory.is_dir():
                raise ValueError(f"Ожидается каталог карточек: {directory}")

            for path in sorted(directory.glob("*.json")):
                path = _inside(root, path)
                before = _signature(path)
                body = _read(path, MAX_RECORD_BYTES)

                if _signature(path) != before:
                    raise ValueError(f"Карточка изменилась при перечислении: {path}")

                digest = hashlib.sha256(body).hexdigest()

                if seen.get(path.relative_to(root).as_posix()) != digest:
                    changed.append(path)

    return tuple(changed)


def _write_receipt(batch: InventoryBatch, payload: dict[str, Any]) -> Path:
    """Сохранить неизменяемую квитанцию после успешной атомарной записи реестров."""

    path = _inside(batch.path.parent, Path("receipts/continuous.json"))
    _write_bytes(path, _json_bytes(payload), immutable=True)

    return path


def _register_batch(root: Path, batch: InventoryBatch) -> Path:
    """Повторить идемпотентную сверку при конкурирующей ручной регистрации без расширения прав."""

    receipt_path = _inside(root, batch.path.parent / "receipts/continuous.json")

    if receipt_path.exists():
        _completion(root, batch.path, receipt_path)
        return receipt_path

    store = ManifestStore(project_root=root, manifest_dir=root / "manifests", schema_dir=root / "manifests/schemas")
    registered_at = _now()

    for attempt in range(MAX_COMMIT_ATTEMPTS):
        try:
            plan, expected_hashes, report = reconcile_inventory_registration(store, batch.records, registered_at)
            report.extend(deferred_source_report(batch.deferred, root))

            if plan.rights or plan.condition_fulfilments:
                raise ValueError("Автоматический инвентарный план не должен изменять основания и условия использования")

            result = store.commit(plan, dry_run=False, expected_snapshot_hashes=expected_hashes)

            return _write_receipt(batch, {
                "schema_version": RECEIPT_SCHEMA, "batch_sha256": batch.sha256,
                "registered_at": registered_at, "previous_snapshot_sha256": expected_hashes,
                "result": asdict(result), "report": report, "skipped_non_russian": list(batch.skipped),
                "deferred_source": list(batch.deferred),
                "rights_changed": False, "training_eligibility_changed": False,
            })

        except ManifestConcurrencyError:
            if attempt + 1 == MAX_COMMIT_ATTEMPTS:
                raise

    raise ManifestConcurrencyError("Не удалось завершить инвентарную регистрацию")


def _new_pending(root: Path, directory: Path, selected: tuple[Path, ...]) -> dict[str, Any]:
    """Назначить уникальный каталог новой попытки, не перезаписывая незавершённые исходники."""

    return {
        "directory": (directory / "batches" / uuid.uuid4().hex).relative_to(root).as_posix(),
        "selected_paths": [path.relative_to(root).as_posix() for path in selected],
    }


def _process_pending(root: Path, directory: Path, state: dict[str, Any], seen: dict[str, str]) -> None:
    """Довести подготовленную партию до квитанции и лишь затем закрепить рассмотренные версии."""

    pending = state["pending"]
    selected = tuple(_inside(root, value) for value in pending["selected_paths"])
    destination = _inside(root, pending["directory"])
    batch_path = _inside(root, destination / "batch.json")

    if not batch_path.exists():
        if destination.exists():
            # Подготовка могла оборваться до публикации batch.json: частичные копии остаются для проверки.
            pending = _new_pending(root, directory, selected)
            state["pending"] = pending
            state["updated_at"] = _now()
            _save_state(directory / "state.json", state)
            destination = _inside(root, pending["directory"])

        sources = tuple(source for source in INVENTORY_SOURCES if source in {_card_source(root, path) for path in selected})
        batch_path = prepare_inventory_batch(root, destination, sources, selected_paths=selected)

    batch = read_inventory_batch(batch_path, root)
    frozen_paths = {
        item["original_path"] for key in ("records", "skipped", "deferred")
        for item in batch.raw_packet.get(key, [])
    }

    if frozen_paths != set(pending["selected_paths"]):
        raise ValueError("Состав незавершённой партии не совпадает с контрольной точкой")

    print(
        f"Регистрирую новую партию: карточек — {len(batch.records)}, исключено нерусских — {len(batch.skipped)}, "
        f"отложено конфликтов исходников — {len(batch.deferred)}.", flush=True,
    )
    _journal(directory, "registration_started", batch_path=batch.path.relative_to(root).as_posix())
    receipt_path = _register_batch(root, batch)
    completion, card_hashes = _completion(root, batch.path, receipt_path)
    state["completed"].append(completion)
    state["pending"] = None
    state["updated_at"] = _now()
    _save_state(directory / "state.json", state)
    seen.update(card_hashes)
    receipt = _json_object(_read(receipt_path, MAX_STATE_BYTES))
    counts = Counter(entry["status"] for entry in receipt["report"])
    inserted = receipt["result"]["inserted"]
    summary = {
        "new_works": inserted["works"], "new_artifacts": inserted["artifacts"],
        "already_registered": counts["already_registered"], "deferred": counts["deferred"],
        "skipped_duplicates": counts["skipped"], "skipped_non_russian": len(batch.skipped),
    }
    _journal(directory, "registration_complete", **completion, considered_cards=len(card_hashes), **summary)
    print(
        f"Партия зарегистрирована: новых работ — {summary['new_works']}, артефактов — {summary['new_artifacts']}; "
        f"уже учтено — {summary['already_registered']}, отложено — {summary['deferred']}. "
        f"Квитанция: {receipt_path}", flush=True,
    )


def run_inventory(
    project_root: Path, *, state_dir: Path | None = None, interval_seconds: float = 900,
    once: bool = False, seed_batch: Path | None = None, seed_receipt: Path | None = None,
) -> dict[str, Any]:
    """Периодически учитывать новые версии карточек офлайн; права и допуск к обучению не менять."""

    root = Path(project_root).resolve()

    if not root.is_dir():
        raise ValueError("Корень проекта должен быть существующим каталогом")

    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)):
        raise ValueError("Интервал регистрации должен быть положительным числом секунд")

    if not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("Интервал регистрации должен быть конечным положительным числом")

    if type(once) is not bool or (seed_batch is None) != (seed_receipt is None):
        raise ValueError("Нужно корректно указать once и одновременно задать исходную партию с квитанцией")

    directory = _state_directory(root, state_dir)
    stop = threading.Event()

    with _runner_lock(root), _stop_signals(stop):
        directory.mkdir(parents=True, exist_ok=True)
        _check_journal(directory)
        state, seen, exists = _load_state(root, directory)

        if seed_batch is not None and seed_receipt is not None:
            if exists:
                raise ValueError("Исходную партию можно принять только при первом запуске")

            batch = read_inventory_batch(seed_batch, root)
            completion, seen = _completion(root, batch.path, seed_receipt)
            state["completed"].append(completion)

        if not exists:
            _save_state(directory / "state.json", state)

        _journal(directory, "runner_started", interval_seconds=interval_seconds, once=once)

        try:
            while not stop.is_set():
                if state["pending"] is None:
                    selected = _scan_cards(root, seen)

                    if selected:
                        state["pending"] = _new_pending(root, directory, selected)
                        state["updated_at"] = _now()
                        _save_state(directory / "state.json", state)

                    else:
                        print("Новых версий карточек нет. Реестры не изменялись.", flush=True)
                        _journal(directory, "no_new_cards", seen_cards=len(seen))

                if state["pending"] is not None:
                    _process_pending(root, directory, state, seen)

                if once:
                    break

                stop.wait(interval_seconds)

        except (OSError, ValueError, RuntimeError, TypeError) as exception:
            _journal(directory, "runner_failed", error=str(exception))
            raise

        _journal(directory, "runner_stopped")

        return _status(directory, state, seen, True)
