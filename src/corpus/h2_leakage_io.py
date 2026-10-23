"""Проверка происхождения резервов H2 и отдельный экспорт предварительных ограничений."""

from __future__ import annotations

import copy
import hashlib
import json
import re

from pathlib import Path
from typing import Any
from uuid import UUID

from .h2_candidate_export import (
    CandidateExportResult,
    _check_existing,
    _checked_path,
    _json_bytes,
    _output_path,
    _sha256,
)
from .h2_candidate_input import CandidateSnapshot

EXPORT_VERSION = "h2-leakage-review-export-v1"
RECORD_FILES = ("constraints", "evaluation_holds", "issues")
PACKET_FILES = ("inputs.jsonl", "private/mapping.jsonl", "private/evaluation_hold.jsonl")
MAX_PACKET_FILE_BYTES = 16 * 1024 * 1024
START_HERE = """Предварительная проверка риска утечки между будущими частями выборки H2

Это предложения ограничений для ручной проверки. Готовое разбиение отсутствует;
автоматическое применение ограничений не выполняется. Защита от утечки
не завершена, данные не допущены к обучению.

Состав пакета:
  constraints.jsonl — предложения по совместному размещению связанных карточек.
  evaluation_holds.jsonl — исходный резерв пробы и проверяемые связанные карточки.
  issues.jsonl — неоднозначности и ограничения проверки.
  summary.json — численности, происхождение и SHA-256 файлов результата.

Резерв для пробы не доказывает, что человек прочитал карточку. Он сохраняется
консервативно для будущей оценки. Совпадения текстов и связи версий требуют
проверки; ни одно такое совпадение само по себе не объединяет публикации.
Предложения не подтверждают полноту обнаружения утечек или безопасность разбиения.

До будущего разбиения разберите issues и предложения constraints, сохраните
решения отдельно и повторно проверьте все части выборки и связанные версии.
Метки H2, рабочие реестры и разбиение этим пакетом не изменяются.

Повторный запуск допустим только при полном совпадении всех пяти файлов.
Изменённый или неполный пакет не перезаписывается: нужен новый каталог версии.
Excel-бланки не читаются. Сетевые запросы не выполняются.
"""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Отклонить повторные ключи JSON вместо молчаливой потери первого значения."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторный ключ JSON: {key}")

        result[key] = value

    return result


def _reject_constant(value: str) -> None:
    """Отклонить числовые константы, не входящие в формат JSON."""

    raise ValueError(f"Недопустимая константа JSON: {value}")


def _read_object(data: bytes) -> dict[str, Any]:
    """Прочитать строгий объект JSON без повторных ключей и специальных чисел."""

    value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)

    if not isinstance(value, dict):
        raise TypeError("Ожидается объект JSON")

    return value


def _digest(value: Any) -> str:
    """Проверить запись SHA-256 в нижнем регистре."""

    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("Нужен SHA-256 из 64 шестнадцатеричных символов нижнего регистра")

    return value


def _text(value: Any, field: str) -> str:
    """Потребовать непустую строку без подмены отсутствующего значения."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Поле {field} должно быть непустой строкой")

    return value


def _file_bytes(root: Path, path: Path) -> bytes:
    """Прочитать ограниченный обычный файл пакета, отклонив ссылки и Excel."""

    checked = _checked_path(root, path)

    if checked.suffix.lower() in {".xlsx", ".xlsm", ".xls"}:
        raise ValueError("Excel-файлы не являются входом предварительной проверки")

    if not checked.is_file() or checked.stat().st_size > MAX_PACKET_FILE_BYTES:
        raise ValueError(f"Нужен обычный файл размером не более 16 МиБ: {checked}")

    with checked.open("rb") as stream:
        data = stream.read(MAX_PACKET_FILE_BYTES + 1)

    if len(data) > MAX_PACKET_FILE_BYTES:
        raise ValueError(f"Файл превышает допустимый размер: {checked}")

    return data


def _inventory(value: Any) -> dict[str, dict[str, Any]]:
    """Проверить непустую опись контрольных сумм и размеров обычных файлов."""

    if not isinstance(value, dict) or not value:
        raise ValueError("Требуется непустая опись файлов с SHA-256 и размерами")

    for name, entry in value.items():
        _text(name, "путь файла")

        if not isinstance(entry, dict) or type(entry.get("bytes")) is not int or entry["bytes"] < 0:
            raise ValueError(f"Некорректный размер файла в описи: {name}")

        _digest(entry.get("sha256"))

    return value


def _packet_files(root: Path, packet_dir: Path, description: dict[str, Any]) -> dict[str, bytes]:
    """Проверить всю опись результатов пакета до связывания его трёх наборов."""

    inventory = _inventory(description.get("output_files"))

    if not set(PACKET_FILES) <= inventory.keys():
        raise ValueError("В описи отсутствуют inputs, private/mapping или private/evaluation_hold")

    result: dict[str, bytes] = {}

    for name, expected in inventory.items():
        relative = Path(name)
        path = _checked_path(root, packet_dir / relative)

        if relative.is_absolute() or not path.is_relative_to(packet_dir):
            raise ValueError("Опись результатов должна ссылаться на файлы внутри пакета")

        data = _file_bytes(root, path)

        if len(data) != expected["bytes"] or _sha256(data) != expected["sha256"]:
            raise ValueError(f"SHA-256 или размер исходного файла изменён: {name}")

        if name in PACKET_FILES:
            result[name] = data

    return result


def _records(data: bytes, count: int, identifier: str) -> list[dict[str, Any]]:
    """Проверить численность и уникальные непустые идентификаторы строк JSONL."""

    records = [_read_object(line) for line in data.splitlines()]

    if len(records) != count:
        raise ValueError("Число записей JSONL не совпадает с паспортом пакета")

    identifiers = [_text(record.get(identifier), identifier) for record in records]

    if len(set(identifiers)) != count:
        raise ValueError(f"Повторное поле {identifier} в исходном пакете")

    return records


def _packet_snapshot(description: dict[str, Any], snapshot: CandidateSnapshot) -> None:
    """Связать паспорт пробы со снимком, уже проверенным основным загрузчиком."""

    source = description.get("snapshot")
    expected = {
        "path": snapshot.provenance.get("snapshot_path"),
        "sha256": snapshot.provenance.get("snapshot_sha256"),
        "validated_pages": snapshot.provenance.get("total_pages"),
        "validated_items": snapshot.provenance.get("total_items"),
    }

    if not isinstance(source, dict) or any(source.get(key) != value for key, value in expected.items()):
        raise ValueError("Снимок пакета пробы не совпадает с текущим снимком по пути, SHA-256 или численности")

    _digest(source.get("sha256"))


def _check_links(
    inputs: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    holds: list[dict[str, Any]],
    snapshot: CandidateSnapshot,
) -> None:
    """Сверить UUID, кандидатов, работы и точные входы без изменения резервов."""

    by_input = {record["annotation_item_id"]: record for record in inputs}
    by_hold = {record["item_uuid"]: record for record in holds}

    if {record["annotation_item_id"] for record in mappings} != by_input.keys():
        raise ValueError("Идентификаторы mapping и inputs различаются")

    mapping_uuids = {_text(record.get("item_uuid"), "item_uuid") for record in mappings}

    if mapping_uuids != by_hold.keys():
        raise ValueError("UUID mapping и evaluation_hold различаются")

    for mapping in mappings:
        item_uuid = _text(mapping.get("item_uuid"), "item_uuid")

        if str(UUID(item_uuid)) != item_uuid or item_uuid not in snapshot.sources:
            raise ValueError("UUID пробы отсутствует в проверенном снимке или записан неканонически")

        hold = by_hold[item_uuid]
        item = by_input[mapping["annotation_item_id"]]

        if mapping.get("candidate_id") != f"jinr:{item_uuid}":
            raise ValueError("candidate_id не соответствует UUID карточки")

        for field in ("candidate_id", "item_uuid", "work_id", "input_sha256"):
            if field not in mapping or field not in hold or mapping[field] != hold[field]:
                raise ValueError(f"Поле {field} различается в mapping и evaluation_hold")

        if mapping["work_id"] is not None:
            _text(mapping["work_id"], "work_id")

        if mapping.get("source") != snapshot.sources[item_uuid]:
            raise ValueError("Происхождение mapping не совпадает с карточкой проверенного снимка")

        if set(item) != {"annotation_item_id", "title", "abstract", "input_sha256"}:
            raise ValueError("Неожиданные поля обезличенного inputs.jsonl")

        text = {field: _text(item.get(field), field) for field in ("title", "abstract")}
        canonical = json.dumps(text, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

        if _sha256(canonical) != _digest(item.get("input_sha256")) or item["input_sha256"] != hold["input_sha256"]:
            raise ValueError("SHA-256 точного входа не совпадает с inputs, mapping или evaluation_hold")

        if (
            hold.get("status") != "reserved_for_practice"
            or hold.get("exclude_from_future_evaluation") is not True
            or hold.get("automatic_split_enforcement") is not False
            or hold.get("related_versions_require_check") is not True
            or "seen_by_annotators" not in hold
            or (hold["seen_by_annotators"] is not None and type(hold["seen_by_annotators"]) is not bool)
        ):
            raise ValueError("Некорректный консервативный резерв исходной пробы")

        _text(hold.get("reason"), "reason")


def _selected_source_lines(
    root: Path, name: str, expected: dict[str, Any], positions: set[int],
) -> dict[int, bytes]:
    """Потоково сверить большой исходный JSONL и сохранить только нужные строки."""

    path = _checked_path(root, Path(name))

    if not path.is_file():
        raise ValueError(f"Не найден исходный JSONL: {path}")

    digest = hashlib.sha256()
    size = 0
    selected: dict[int, bytes] = {}

    with path.open("rb") as stream:
        for position, line in enumerate(stream, 1):
            digest.update(line)
            size += len(line)

            if position in positions:
                selected[position] = line

    if size != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
        raise ValueError(f"SHA-256 или размер исходного JSONL изменён: {name}")

    if selected.keys() != positions:
        raise ValueError(f"В исходном JSONL отсутствует указанная строка: {name}")

    return selected


def _source_inputs(
    root: Path, description: dict[str, Any], mappings: list[dict[str, Any]], inputs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Подтвердить происхождение чистых текстов по точным строкам исходных JSONL."""

    inventory = _inventory(description.get("source_files"))

    for name in inventory:
        _checked_path(root, Path(name))

    selected: dict[str, dict[int, bytes]] = {}
    verified: dict[str, dict[str, Any]] = {}

    for kind, basename in (("candidate", "candidates.jsonl"), ("cleaned", "cleaned_candidates.jsonl")):
        names = [name for name in inventory if Path(name).name == basename]

        if len(names) != 1:
            raise ValueError(f"В source_files требуется единственный {basename}")

        positions: set[int] = set()

        for mapping in mappings:
            position = mapping.get(f"source_{kind}_line")

            if type(position) is not int or position < 1:
                raise ValueError(f"Нужен положительный номер source_{kind}_line")

            positions.add(position)
            _digest(mapping.get(f"source_{kind}_line_sha256"))

        if len(positions) != len(mappings):
            raise ValueError("Разные карточки пробы ссылаются на одну исходную строку")

        name = names[0]
        selected[kind] = _selected_source_lines(root, name, inventory[name], positions)
        verified[name] = copy.deepcopy(inventory[name])

    by_input = {record["annotation_item_id"]: record for record in inputs}

    for mapping in mappings:
        records: dict[str, dict[str, Any]] = {}

        for kind in ("candidate", "cleaned"):
            line = selected[kind][mapping[f"source_{kind}_line"]]

            if _sha256(line) != mapping[f"source_{kind}_line_sha256"]:
                raise ValueError(f"SHA-256 строки source_{kind}_line не совпадает с mapping")

            record = _read_object(line)

            if any(record.get(field) != mapping[field] for field in ("candidate_id", "item_uuid")):
                raise ValueError("Исходная строка относится к другой карточке")

            records[kind] = record

        if records["candidate"].get("source") != mapping["source"]:
            raise ValueError("Происхождение исходного кандидата не совпадает с mapping")

        if records["cleaned"].get("source_candidate_line_sha256") != mapping["source_candidate_line_sha256"]:
            raise ValueError("Очищенный текст не связан с точной строкой исходного кандидата")

        item = by_input[mapping["annotation_item_id"]]

        for field in ("title", "abstract"):
            if records["cleaned"].get(f"{field}_original") != records["candidate"].get(field):
                raise ValueError("Оригинал очищенного текста не совпадает с исходным кандидатом")

            if records["cleaned"].get(f"{field}_clean") != item[field]:
                raise ValueError("Вход пробы не совпадает с проверенным очищенным текстом")

    return verified


def read_practice_holds(
    root: Path, packet_dir: Path, snapshot: CandidateSnapshot,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Прочитать подтверждённые резервы пробы без Excel, сети и изменения источников."""

    root = Path(root).resolve()
    packet_dir = _checked_path(root, packet_dir)
    packet_path = packet_dir / "packet.json"
    packet_bytes = _file_bytes(root, packet_path)
    description = _read_object(packet_bytes)

    if description.get("format_version") != "h2-practice-draft-v1":
        raise ValueError("Нужен исходный пакет h2-practice-draft-v1")

    if description.get("status") != "awaiting_guide_approval" or any(
        description.get(field) is not False
        for field in ("guide_approved", "annotation_started", "formal_calibration", "formal_audit", "training_ready")
    ):
        raise ValueError("Нужен исходный черновой пакет пробы без допуска к обучению")

    count = description.get("item_count")

    if type(count) is not int or not 1 <= count <= 30:
        raise ValueError("В исходном пакете должно быть от 1 до 30 карточек")

    _text(description.get("packet_id"), "packet_id")
    _packet_snapshot(description, snapshot)
    packet_files = _packet_files(root, packet_dir, description)
    inputs = _records(packet_files[PACKET_FILES[0]], count, "annotation_item_id")
    mappings = _records(packet_files[PACKET_FILES[1]], count, "annotation_item_id")
    holds = _records(packet_files[PACKET_FILES[2]], count, "item_uuid")
    _check_links(inputs, mappings, holds, snapshot)
    registered_count = len({mapping["work_id"] for mapping in mappings if mapping["work_id"] is not None})

    if type(description.get("registered_work_count")) is not int or description["registered_work_count"] != registered_count:
        raise ValueError("Количество зарегистрированных работ не совпадает с mapping")

    verified_sources = _source_inputs(root, description, mappings, inputs)
    provenance = {
        "packet_id": description["packet_id"],
        "source_packet_path": packet_path.relative_to(root).as_posix(),
        "source_packet_sha256": _sha256(packet_bytes),
        "source_packet_bytes": len(packet_bytes),
        "snapshot": copy.deepcopy(description["snapshot"]),
        "output_files": copy.deepcopy(description["output_files"]),
        "verified_source_files": verified_sources,
        "source_files_as_reported": copy.deepcopy(description["source_files"]),
        "verified_input_count": count,
        "excel_read": False,
    }

    return holds, provenance


def _bundle_bytes(review: dict[str, Any]) -> dict[str, bytes]:
    """Сериализовать пять файлов и их контрольные суммы до первой записи."""

    summary = review.get("summary")

    if not isinstance(summary, dict) or "export" in summary:
        raise ValueError("Нужен объект summary без зарезервированного поля export")

    if (
        summary.get("proposals_only") is not True
        or summary.get("automatic_split_enforcement") is not False
        or summary.get("splits_checked") is not False
        or summary.get("registries_changed") is not False
        or summary.get("training_ready") is not False
    ):
        raise ValueError("Допустим только предварительный пакет без готового разбиения, изменений реестров и обучения")

    payloads: dict[str, bytes] = {}

    for name in RECORD_FILES:
        records = review.get(name)

        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError(f"Поле {name} должно содержать список JSON-объектов")

        payloads[f"{name}.jsonl"] = b"".join(_json_bytes(record) for record in records)

    payloads["START_HERE.txt"] = START_HERE.encode("utf-8")
    report = {
        **summary,
        "export": {
            "format_version": EXPORT_VERSION,
            "automatic_split_enforcement": False,
            "registry_changes": False,
            "files": {name: {"sha256": _sha256(data), "bytes": len(data)} for name, data in payloads.items()},
        },
    }
    payloads["summary.json"] = _json_bytes(report, indent=2)

    return payloads


def export_leakage_review(
    project_root: Path,
    output_dir: Path,
    review: dict[str, Any],
    *,
    snapshot_path: Path,
    practice_dir: Path,
    dry_run: bool = False,
) -> CandidateExportResult:
    """Создать независимый результат или проверить точный повтор без перезаписи."""

    root = Path(project_root).resolve()
    output = _output_path(root, output_dir, snapshot_path)
    practice = _checked_path(root, practice_dir)

    if output.is_relative_to(practice) or practice.is_relative_to(output):
        raise ValueError("Каталог результата не должен пересекаться с исходным пакетом пробы")

    for parent in output.parents:
        if parent == root:
            break

        if parent.exists() and not parent.is_dir():
            raise ValueError(f"Родительский путь результата занят файлом: {parent}")

    payloads = _bundle_bytes(review)
    hashes = {name: _sha256(data) for name, data in payloads.items()}

    if _check_existing(output, payloads):
        return CandidateExportResult(output, 0, len(payloads), hashes)

    if dry_run:
        return CandidateExportResult(output, 0, 0, hashes)

    output.parent.mkdir(parents=True, exist_ok=True)

    # Эксклюзивное создание защищает ручные результаты при появлении новых файлов.
    output.mkdir(exist_ok=False)

    for name, data in payloads.items():
        with (output / name).open("xb") as stream:
            stream.write(data)

    return CandidateExportResult(output, len(payloads), 0, hashes)
