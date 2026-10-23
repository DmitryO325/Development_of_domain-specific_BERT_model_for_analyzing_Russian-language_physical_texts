"""Проверка ответов двух участников пробы H2 без изменения реестров и меток."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.corpus.h2_practice_packet import (
    ANSWER_FIELDS,
    CARD_CELLS,
    PracticePacket,
    checked_path,
    read_practice_packet,
    read_source,
    sha256,
)
from src.corpus.h2_practice_xlsx import PracticeWorkbook, read_practice_workbook

CHECK_VERSION = "h2-practice-check-v1"
OUTPUT_BASE = Path("manifests/results")
DECISIONS = {"C1", "C2", "C3", "C4", "C5", "OTHER", "AMBIGUOUS", "INSUFFICIENT"}
CONFIDENCE = {"high", "medium", "low"}
FAMILIARITY = {"да", "нет", "не уверен"}
TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


@dataclass(frozen=True)
class PreparedH2Practice:
    """Диагностика и точные снимки ответов, подготовленные без записи на диск."""

    project_root: Path
    output_dir: Path
    files: dict[str, bytes]
    report: dict[str, Any]
    source_hashes: dict[str, str]
    payload_hashes: dict[str, str]


def _json_bytes(value: Any, *, indent: int | None = None) -> bytes:
    """Сериализовать данные воспроизводимо без автоматической правки ответов."""

    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=indent) + "\n").encode("utf-8")


def _issue(issues: list[dict[str, str]], role: str, sheet: str, cell: str, code: str, message: str) -> None:
    """Добавить ошибку с точным расположением для исправления участником."""

    issues.append({"role": role, "sheet": sheet, "cell": cell, "code": code, "message": message})


def _valid_timestamp(value: str) -> bool:
    """Принять реальную календарную дату ISO 8601 только с часовым поясом."""

    if not TIMESTAMP.fullmatch(value):
        return False

    try:
        parsed = datetime.fromisoformat(value)

    except ValueError:
        return False

    return parsed.utcoffset() is not None


def _check_fields(role: str, sheet: str, answers: dict[str, Any], issues: list[dict[str, str]]) -> None:
    """Проверить все обязательные ответы, не заменяя отсутствующее догадками."""

    choices = {"familiarity": FAMILIARITY, "decision": DECISIONS, "confidence": CONFIDENCE}

    for field in ANSWER_FIELDS:
        value = answers[field]
        cell = CARD_CELLS[field]

        if not isinstance(value, str) or not value.strip():
            _issue(issues, role, sheet, cell, "missing_answer", "Обязательное поле ответа не заполнено")
            continue

        if field in choices and value not in choices[field]:
            _issue(issues, role, sheet, cell, "invalid_choice", "Значение не входит в разрешённый список")

        if field == "completed_at" and not _valid_timestamp(value):
            _issue(issues, role, sheet, cell, "invalid_timestamp", "Нужна дата ISO 8601 с временем и часовым поясом")


def _check_workbook(
    role: str, workbook: PracticeWorkbook, packet: PracticePacket, issues: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Сверить неизменяемые ячейки и получить ответы строго по исходным ID."""

    if workbook.sheets != packet.template.sheets:
        _issue(issues, role, "", "", "sheet_order", "Состав или порядок листов отличается от исходного бланка")

    for sheet in packet.template.sheets:
        if sheet not in workbook.cells:
            continue

        allowed = {"B4"} if sheet == "Памятка" else {CARD_CELLS[field] for field in ANSWER_FIELDS}
        original = packet.template.cells[sheet]
        actual = workbook.cells[sheet]

        for coordinate in sorted((original.keys() | actual.keys()) - allowed):
            if original.get(coordinate) != actual.get(coordinate):
                _issue(issues, role, sheet, coordinate, "immutable_cell", "Исходный текст, ID, хеш или подпись изменены")

    participant = workbook.cells.get("Памятка", {}).get("B4")

    if not isinstance(participant, str) or not participant.strip():
        _issue(issues, role, "Памятка", "B4", "missing_participant", "Не заполнено имя участника")

    records: list[dict[str, Any]] = []

    for index, item in enumerate(packet.items, 1):
        sheet = f"{index:02d}"
        cells = workbook.cells.get(sheet, {})
        answers = {field: cells.get(CARD_CELLS[field]) for field in ANSWER_FIELDS}
        _check_fields(role, sheet, answers, issues)
        records.append({
            "schema_version": "h2-practice-answer-v1", "packet_id": packet.description["packet_id"],
            "guide_version": packet.description["guide_version"], "role": role,
            "sheet": sheet, "annotator_id": participant,
            "annotation_item_id": item["annotation_item_id"], "input_sha256": item["input_sha256"],
            **answers,
        })

    return records


def _person_key(value: str) -> str:
    """Сопоставить имена с учётом регистра и пробелов, не изменяя исходную запись."""

    return unicodedata.normalize("NFKC", " ".join(value.split())).casefold()


def _comparison(author: list[dict[str, Any]], supervisor: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Сопоставить решения, не назначая окончательных меток и не вычисляя κ."""

    result: list[dict[str, Any]] = []

    for first, second in zip(author, supervisor, strict=True):
        result.append({
            "annotation_item_id": first["annotation_item_id"], "input_sha256": first["input_sha256"],
            "author_decision": first["decision"], "supervisor_decision": second["decision"],
            "decision_equal": first["decision"] == second["decision"],
            "author_confidence": first["confidence"], "supervisor_confidence": second["confidence"],
            "author_familiarity": first["familiarity"], "supervisor_familiarity": second["familiarity"],
            "author_rationale": first["rationale"], "supervisor_rationale": second["rationale"],
        })

    return result


def _output_path(root: Path, output_dir: Path, sources: dict[str, bytes]) -> Path:
    """Ограничить результат новой подпапкой manifests/results без перезаписи входов."""

    output = checked_path(root, output_dir)
    base = root / OUTPUT_BASE

    if output == base or not output.is_relative_to(base):
        raise ValueError("Нужна отдельная подпапка внутри manifests/results")

    if output.exists():
        raise ValueError("Каталог результата уже существует; выберите новый, перезапись запрещена")

    if any((root / name).is_relative_to(output) for name in sources):
        raise ValueError("Результат не должен содержать исходные файлы")

    return output


def prepare_h2_practice(
    *, project_root: Path, packet_dir: Path, template_dir: Path,
    author_file: Path, supervisor_file: Path, output_dir: Path,
) -> PreparedH2Practice:
    """Проверить обе книги и собрать отдельный диагностический пакет в памяти."""

    if ".." in Path(project_root).parts or Path(project_root).is_symlink():
        raise ValueError("Корень проекта не должен быть символической ссылкой или содержать переход вверх")

    root = Path(project_root).resolve()

    if not root.is_dir():
        raise ValueError("Корень проекта должен быть существующим каталогом")

    packet_path = checked_path(root, packet_dir)
    template_path = checked_path(root, template_dir)
    paths = {"author": checked_path(root, author_file), "supervisor": checked_path(root, supervisor_file)}
    sources: dict[str, bytes] = {}
    output = _output_path(root, output_dir, sources)

    if any(output.is_relative_to(path) or path.is_relative_to(output) for path in (packet_path, template_path)):
        raise ValueError("Каталоги исходного пакета, бланков и результата не должны пересекаться")

    if paths["author"] == paths["supervisor"] or (
        paths["author"].is_file() and paths["supervisor"].is_file()
        and paths["author"].samefile(paths["supervisor"])
    ):
        raise ValueError("Нужны два отдельных файла участников, а не один и тот же файл")

    packet = read_practice_packet(root, packet_path, template_path, sources)
    submitted = {role: read_source(root, path, sources) for role, path in paths.items()}
    _output_path(root, output, sources)
    issues: list[dict[str, str]] = []
    answers = {
        role: _check_workbook(role, read_practice_workbook(data), packet, issues)
        for role, data in submitted.items()
    }
    first_name = answers["author"][0]["annotator_id"]
    second_name = answers["supervisor"][0]["annotator_id"]

    if (
        isinstance(first_name, str) and isinstance(second_name, str)
        and first_name.strip() and second_name.strip()
        and _person_key(first_name) == _person_key(second_name)
    ):
        _issue(issues, "both", "Памятка", "B4", "same_participant", "В обоих файлах указан один участник")

    comparison = [] if issues else _comparison(answers["author"], answers["supervisor"])
    disagreements = [row for row in comparison if not row["decision_equal"]]
    files = {f"{role}.xlsx": data for role, data in submitted.items()}
    files["issues.jsonl"] = b"".join(_json_bytes(issue) for issue in issues)

    if not issues:
        files["answers.jsonl"] = b"".join(_json_bytes(row) for rows in answers.values() for row in rows)
        files["comparison.jsonl"] = b"".join(_json_bytes(row) for row in comparison)
        files["disagreements.jsonl"] = b"".join(_json_bytes(row) for row in disagreements)

    report: dict[str, Any] = {
        "schema_version": CHECK_VERSION,
        "status": "invalid" if issues else "practice_answers_checked",
        "packet_id": packet.description["packet_id"], "source_packet_status": packet.description["status"],
        "guide_version": packet.description["guide_version"], "guide_approved": False,
        "formal_h2_ready": False, "training_ready": False, "formal_labels_created": 0,
        "output_dir": output.relative_to(root).as_posix(),
        "counts": {"items": len(packet.items), "errors": len(issues),
                   "complete_pairs": len(comparison), "disagreements": len(disagreements)},
        "issues": issues,
        "source_files": {name: {"sha256": sha256(data), "bytes": len(data)} for name, data in sources.items()},
        "submitted_files": {role: path.relative_to(root).as_posix() for role, path in paths.items()},
        "export_files": {name: {"sha256": sha256(data), "bytes": len(data)} for name, data in files.items()},
        "limitations": [
            "Техническая проверка не утверждает руководство, запуск разметки или независимость участников",
            "Сравнение выполняется только при полной корректности обоих бланков; иначе нулевые счётчики не означают согласие",
            "Знакомство со статьёй сохраняется, но не исключает её автоматически",
            "Это целевая проба, не формальная калибровка, аудит или эталон H2; κ и доверительные интервалы не рассчитываются",
            "Рабочие реестры, права, допуск, итоговые метки и разбиения не изменяются",
        ],
    }
    files["summary.json"] = _json_bytes(report, indent=2)

    return PreparedH2Practice(
        root, output, files, report, {name: sha256(data) for name, data in sources.items()},
        {name: sha256(data) for name, data in files.items()},
    )


def _verify_prepared(prepared: PreparedH2Practice) -> None:
    """Повторно сверить пути, источники и подготовленное содержимое перед записью."""

    root = prepared.project_root
    _output_path(root, prepared.output_dir, {name: b"" for name in prepared.source_hashes})

    if prepared.files.get("summary.json") != _json_bytes(prepared.report, indent=2):
        raise ValueError("Подготовленный отчёт был изменён")

    if prepared.payload_hashes != {name: sha256(data) for name, data in prepared.files.items()}:
        raise ValueError("Подготовленное содержимое было изменено")

    if prepared.source_hashes != {name: record["sha256"] for name, record in prepared.report["source_files"].items()}:
        raise ValueError("Контрольные суммы источников были изменены")

    for name, expected in prepared.source_hashes.items():
        if sha256(read_source(root, Path(name), {})) != expected:
            raise ValueError(f"Источник изменился после подготовки: {name}")


def publish_h2_practice(prepared: PreparedH2Practice, *, dry_run: bool = False) -> bool:
    """Записать новый локальный пакет без перезаписи исходников и прежних ответов."""

    _verify_prepared(prepared)

    if dry_run:
        return False

    output = prepared.output_dir
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".h2-practice-", dir=output.parent) as temporary:
        staging = Path(temporary)

        for name, data in prepared.files.items():
            if Path(name).name != name:
                raise ValueError("Недопустимое имя выходного файла")

            with (staging / name).open("xb") as stream:
                stream.write(data)

        _verify_prepared(prepared)
        output.mkdir(exist_ok=False)
        linked: list[str] = []

        try:
            for name in prepared.files:
                os.link(staging / name, output / name)
                linked.append(name)

        except OSError:
            # Откатываются только наши неизменённые ссылки; чужие файлы не удаляются.
            for name in linked:
                path = output / name

                if not path.is_symlink() and path.exists() and path.samefile(staging / name) and path.read_bytes() == prepared.files[name]:
                    path.unlink()

            if not any(output.iterdir()):
                output.rmdir()

            raise

    return True
