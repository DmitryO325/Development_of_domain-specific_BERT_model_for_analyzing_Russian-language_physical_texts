"""Неизменяемый офлайн-пакет исходников журналов для учёта без подтверждения прав."""

from __future__ import annotations

import hashlib
import json
import re

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Тот же атомарный помощник сохраняет точные байты исходных HTTP-ответов сборщиков.
from src.collect.ioffe_archive import MAX_METADATA_BYTES, _write_bytes

from .journal_import import (
    MAX_RECORD_BYTES,
    AmbiguousIssueEntryError,
    CollectedArticle,
    NonRussianArticleError,
    read_collected_article,
)

LEGACY_INVENTORY_SCHEMA = "journal-inventory-batch-v1"
INVENTORY_SCHEMA = "journal-inventory-batch-v2"
INVENTORY_SOURCES = ("ioffe", "jetp", "jetp_letters", "quantum", "ufn")
MAX_BATCH_BYTES = 8 * 1024 * 1024
PACKET_FIELDS = {"schema_version", "status", "prepared_at", "sources", "raw_roots", "records", "skipped"}
RECORD_FIELDS = {"source", "original_path", "record_path", "record_sha256", "has_pdf"}


@dataclass(frozen=True)
class InventoryBatch:
    """Проверенные исходники фиксированного состава без решения о правомерности получения."""

    records: tuple[CollectedArticle, ...]
    sources: tuple[str, ...]
    prepared_at: str
    sha256: str
    path: Path
    skipped: tuple[dict[str, Any], ...]
    deferred: tuple[dict[str, Any], ...]
    raw_packet: dict[str, Any]


def _timestamp(value: Any) -> datetime:
    """Прочитать дату с обязательным часовым поясом."""

    if not isinstance(value, str):
        raise ValueError("Временная метка должна быть строкой")

    timestamp = datetime.fromisoformat(value)

    if timestamp.utcoffset() is None:
        raise ValueError("Временная метка должна содержать часовой пояс")

    return timestamp


def _now() -> str:
    """Зафиксировать реальное время подготовки отдельно от времени скачивания."""

    return datetime.now(UTC).isoformat(timespec="microseconds")


def _source_suffix(source: str) -> Path:
    """Выбрать штатный каталог конкретного сборщика."""

    return Path("ioffe") if source == "ioffe" else Path("journals") / source


def _sources(values: Any) -> tuple[str, ...]:
    """Отклонить пустой список, повторы и неизвестные источники."""

    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("Нужно явно выбрать хотя бы один источник")

    if any(not isinstance(source, str) or source not in INVENTORY_SOURCES for source in values):
        raise ValueError("В пакете указан неизвестный источник")

    if len(set(values)) != len(values):
        raise ValueError("Источник повторяется в составе пакета")

    return tuple(values)


def _inside(root: Path, value: Path | str) -> Path:
    """Ограничить путь выбранным корнем, запретив переходы и символические ссылки."""

    path = Path(value)

    if ".." in path.parts:
        raise ValueError("Путь пакета не должен содержать переходы в родительские каталоги")

    if path.is_absolute():
        try:
            path = path.relative_to(root)

        except ValueError as exception:
            raise ValueError("Файлы пакета должны находиться внутри проекта") from exception

    result = root

    for part in path.parts:
        result = result / part

        if result.is_symlink():
            raise ValueError(f"Символические ссылки в пакете запрещены: {result}")

    return result


def _read(path: Path, limit: int) -> bytes:
    """Прочитать обычный ограниченный по размеру файл без перехода по ссылке."""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Ожидается обычный файл: {path}")

    with path.open("rb") as source_file:
        body = source_file.read(limit + 1)

    if len(body) > limit:
        raise ValueError(f"Превышен размер файла пакета: {path}")

    return body


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Не принимать неоднозначные JSON-объекты с повторяющимися ключами."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Поле JSON повторяется: {key}")

        result[key] = value

    return result


def _json_object(body: bytes) -> dict[str, Any]:
    """Прочитать однозначный JSON-объект, не принимая другие виды значений."""

    try:
        parsed = json.loads(body, object_pairs_hook=_unique_object)

    except (ValueError, RecursionError) as exception:
        raise ValueError("Не удалось прочитать JSON пакета") from exception

    if not isinstance(parsed, dict):
        raise ValueError("В пакете требуется JSON-объект")

    return parsed


def _signature(path: Path) -> tuple[int, int, int, int]:
    """Отметить замену или изменение карточки между перечислением и копированием."""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Карточка должна быть обычным файлом: {path}")

    status = path.stat()

    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns


def _check_response_times(record: dict[str, Any], raw_root: Path, prepared_at: datetime) -> None:
    """Проверить время всех ответов, включая карточки исключённых нерусских статей."""

    for field in ("card_response", "pdf_response"):
        evidence = record.get(field)

        if evidence is None:
            continue

        if not isinstance(evidence, dict) or not isinstance(evidence.get("response_metadata_path"), str):
            raise ValueError("Карточка не содержит пути к HTTP-свидетельству")

        metadata_path = _inside(raw_root, evidence["response_metadata_path"])
        body = _read(metadata_path, MAX_METADATA_BYTES)

        if hashlib.sha256(body).hexdigest() != evidence.get("response_metadata_sha256"):
            raise ValueError("HTTP-свидетельство изменилось во время проверки")

        if _timestamp(_json_object(body).get("retrieved_at")) > prepared_at:
            raise ValueError("HTTP-снимок получен позднее фиксации пакета")


def _verify_card(
    path: Path, *, source: str, raw_root: Path, prepared_at: datetime,
) -> tuple[CollectedArticle | None, str | None, AmbiguousIssueEntryError | None]:
    """Отделить нерусские карточки и подтверждённую неоднозначность оглавления от повреждений."""

    body = _read(path, MAX_RECORD_BYTES)
    record = _json_object(body)
    article = None
    skipped_reason = None
    deferred_error = None

    try:
        article = read_collected_article(path, source=source, raw_root=raw_root)

    except NonRussianArticleError as exception:
        skipped_reason = str(exception)

    except AmbiguousIssueEntryError as exception:
        deferred_error = exception

    _check_response_times(record, raw_root, prepared_at)

    if source == "quantum" and record.get("pdf_response") is not None:
        raise ValueError("Пакет Quantum может включать только метаданные, без PDF")

    if _read(path, MAX_RECORD_BYTES) != body:
        raise ValueError("Карточка изменилась во время проверки исходников")

    return article, skipped_reason, deferred_error


def prepare_inventory_batch(
    project_root: Path,
    output_dir: Path,
    sources: tuple[str, ...] = INVENTORY_SOURCES,
    *,
    selected_paths: tuple[Path, ...] | None = None,
) -> Path:
    """Скопировать фиксированный состав карточек, проверив исходники без сети и реестров."""

    root = Path(project_root).resolve()

    if not root.is_dir():
        raise ValueError("Корень проекта должен быть существующим каталогом")

    selected_sources = _sources(sources)
    destination = _inside(root, output_dir)
    requested: set[Path] | None = None

    if selected_paths is not None:
        if not isinstance(selected_paths, tuple) or not selected_paths:
            raise ValueError("Нужен непустой кортеж путей выбранных карточек")

        requested = set()

        for path in selected_paths:
            if not isinstance(path, Path):
                raise ValueError("Путь выбранной карточки должен быть объектом Path")

            normalized = _inside(root, path)

            if normalized in requested:
                raise ValueError("Выбранная карточка повторяется")

            requested.add(normalized)

    if destination.exists():
        raise ValueError("Каталог неизменяемого пакета уже существует")

    prepared_at = _now()
    timestamp = _timestamp(prepared_at)
    raw_roots = {source: (Path("data/raw") / _source_suffix(source)).as_posix() for source in selected_sources}
    selected: list[tuple[str, Path, tuple[int, int, int, int]]] = []

    # Состав и состояние каждого исходника фиксируются до чтения и копирования любых карточек.
    for source in selected_sources:
        input_root = _inside(root, Path("manifests/imports") / _source_suffix(source))
        raw_root = _inside(root, raw_roots[source])

        if not input_root.is_dir() or not raw_root.is_dir():
            raise ValueError(f"Нет штатного каталога карточек или исходников: {source}")

        for directory_name in ("articles", "metadata"):
            directory = _inside(root, input_root / directory_name)

            if directory.exists() and not directory.is_dir():
                raise ValueError(f"Ожидается каталог карточек: {directory}")

            for path in sorted(directory.glob("*.json")):
                if requested is not None and path not in requested:
                    continue

                selected.append((source, _inside(root, path), _signature(path)))

    if requested is not None and requested != {path for _, path, _ in selected}:
        raise ValueError("Выбранные карточки должны существовать в штатных каталогах указанных источников")

    if not selected:
        raise ValueError("В выбранных источниках нет сохранённых карточек")

    destination.mkdir(parents=True, exist_ok=False)
    packet: dict[str, Any] = {
        "schema_version": INVENTORY_SCHEMA, "status": "prepared_not_registered",
        "prepared_at": prepared_at, "sources": list(selected_sources), "raw_roots": raw_roots,
        "records": [], "skipped": [], "deferred": [],
    }
    original_hashes: list[tuple[Path, str, tuple[int, int, int, int]]] = []

    for source, original, signature in selected:
        if _signature(_inside(root, original)) != signature:
            raise ValueError(f"Карточка изменилась после фиксации состава: {original}")

        body = _read(original, MAX_RECORD_BYTES)
        digest = hashlib.sha256(body).hexdigest()
        frozen = _inside(root, destination / "records" / source / original.parent.name / original.name)
        _write_bytes(frozen, body, immutable=True)
        article, skipped_reason, deferred_error = _verify_card(
            frozen, source=source, raw_root=_inside(root, raw_roots[source]), prepared_at=timestamp,
        )
        descriptor = {
            "source": source, "original_path": original.relative_to(root).as_posix(),
            "record_path": frozen.relative_to(root).as_posix(), "record_sha256": digest,
            "has_pdf": article is not None and article.pdf_response is not None,
        }

        if deferred_error is not None:
            descriptor.update(reason=str(deferred_error), details=deferred_error.details)
            packet["deferred"].append(descriptor)

        elif skipped_reason is not None:
            descriptor["reason"] = skipped_reason
            packet["skipped"].append(descriptor)

        else:
            packet["records"].append(descriptor)

        original_hashes.append((original, digest, signature))

    for original, digest, signature in original_hashes:
        original = _inside(root, original)

        if _signature(original) != signature or hashlib.sha256(_read(original, MAX_RECORD_BYTES)).hexdigest() != digest:
            raise ValueError(f"Исходная карточка изменилась при подготовке: {original}")

    packet_path = destination / "batch.json"
    body = (json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")

    if len(body) > MAX_BATCH_BYTES:
        raise ValueError("Превышен допустимый размер пакета")

    # Итог публикуется последним: оставшийся после ошибки каталог не считается готовым пакетом.
    _write_bytes(packet_path, body, immutable=True)

    return packet_path


def read_inventory_batch(path: Path, project_root: Path) -> InventoryBatch:
    """Проверить закреплённые копии и HTTP-свидетельства, не читая изменяемые карточки сборщиков."""

    root = Path(project_root).resolve()
    packet_path = _inside(root, path)
    body = _read(packet_path, MAX_BATCH_BYTES)
    packet = _json_object(body)

    schema_version = packet.get("schema_version")

    if schema_version == LEGACY_INVENTORY_SCHEMA:
        categories = ("records", "skipped")
        expected_packet_fields = PACKET_FIELDS

    elif schema_version == INVENTORY_SCHEMA:
        categories = ("records", "skipped", "deferred")
        expected_packet_fields = PACKET_FIELDS | {"deferred"}

    else:
        raise ValueError("Неизвестный формат инвентаризационного пакета журналов")

    if set(packet) != expected_packet_fields:
        raise ValueError("Неизвестный формат инвентаризационного пакета журналов")

    if packet["status"] != "prepared_not_registered":
        raise ValueError("Пакет должен иметь статус prepared_not_registered")

    selected_sources = _sources(packet["sources"])
    raw_roots = {source: (Path("data/raw") / _source_suffix(source)).as_posix() for source in selected_sources}

    if packet["raw_roots"] != raw_roots:
        raise ValueError("Корни исходников не соответствуют выбранным сборщикам")

    timestamp = _timestamp(packet["prepared_at"])

    if any(not isinstance(packet[category], list) for category in categories):
        raise ValueError("Категории карточек пакета должны быть списками")

    if not any(packet[category] for category in categories):
        raise ValueError("Инвентаризационный пакет пуст")

    articles: list[CollectedArticle] = []
    record_sources: list[str] = []
    seen_originals: set[str] = set()
    seen_copies: set[Path] = set()

    for category in categories:
        for descriptor in packet[category]:
            expected_fields = RECORD_FIELDS

            if category == "skipped":
                expected_fields = expected_fields | {"reason"}

            elif category == "deferred":
                expected_fields = expected_fields | {"reason", "details"}

            if not isinstance(descriptor, dict) or set(descriptor) != expected_fields:
                raise ValueError("Некорректные поля описания карточки")

            source = descriptor["source"]

            if not isinstance(source, str) or source not in selected_sources or type(descriptor["has_pdf"]) is not bool:
                raise ValueError("Источник карточки или отметка PDF недопустимы")

            if not isinstance(descriptor["original_path"], str) or not isinstance(descriptor["record_path"], str):
                raise ValueError("Пути карточки должны быть строками")

            original = Path(descriptor["original_path"])
            expected_parent = Path("manifests/imports") / _source_suffix(source)

            if original.parent not in {expected_parent / "articles", expected_parent / "metadata"}:
                raise ValueError("Исходная карточка не относится к выбранному сборщику")

            if original.suffix != ".json" or original.name in {".json", "..json"}:
                raise ValueError("Некорректное имя исходной карточки")

            frozen = _inside(root, descriptor["record_path"])
            expected_copy = packet_path.parent / "records" / source / original.parent.name / original.name

            if frozen != expected_copy or frozen in seen_copies or descriptor["original_path"] in seen_originals:
                raise ValueError("Карточка повторяется или её копия находится вне пакета")

            seen_originals.add(descriptor["original_path"])
            seen_copies.add(frozen)
            digest = descriptor["record_sha256"]

            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("Некорректная SHA-256 карточки")

            if hashlib.sha256(_read(frozen, MAX_RECORD_BYTES)).hexdigest() != digest:
                raise ValueError("Контрольная сумма закреплённой карточки изменилась")

            article, skipped_reason, deferred_error = _verify_card(
                frozen, source=source, raw_root=_inside(root, raw_roots[source]), prepared_at=timestamp,
            )

            if hashlib.sha256(_read(frozen, MAX_RECORD_BYTES)).hexdigest() != digest:
                raise ValueError("Закреплённая карточка изменилась во время проверки")

            if category == "deferred":
                if (
                    deferred_error is None or descriptor["has_pdf"]
                    or descriptor["reason"] != str(deferred_error)
                    or json.dumps(descriptor["details"], sort_keys=True, allow_nan=False)
                    != json.dumps(deferred_error.details, sort_keys=True, allow_nan=False)
                ):
                    raise ValueError("Причина отложения не подтверждается исходной карточкой")

            elif category == "skipped":
                if skipped_reason is None or descriptor["reason"] != skipped_reason or descriptor["has_pdf"]:
                    raise ValueError("Причина исключения не подтверждается исходной карточкой")

            else:
                if deferred_error is not None:
                    raise ValueError("Неоднозначная карточка должна находиться в deferred")

                if article is None or skipped_reason is not None:
                    raise ValueError("Нерусская карточка должна находиться в skipped")

                if descriptor["has_pdf"] != (article.pdf_response is not None):
                    raise ValueError("Отметка PDF не соответствует проверенным исходникам")

                articles.append(article)
                record_sources.append(source)

    if _read(_inside(root, packet_path), MAX_BATCH_BYTES) != body:
        raise ValueError("Пакет изменился во время проверки")

    return InventoryBatch(
        records=tuple(articles), sources=tuple(record_sources), prepared_at=packet["prepared_at"],
        sha256=hashlib.sha256(body).hexdigest(), path=packet_path,
        skipped=tuple(packet["skipped"]), deferred=tuple(packet.get("deferred", [])), raw_packet=packet,
    )
