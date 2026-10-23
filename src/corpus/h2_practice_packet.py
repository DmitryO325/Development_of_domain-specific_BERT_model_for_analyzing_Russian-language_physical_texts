"""Проверка неизменяемых входов и исходных Excel-бланков пробной разметки H2."""

from __future__ import annotations

import hashlib
import json

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.corpus.h2_practice_xlsx import PracticeWorkbook, read_practice_workbook

CARD_CELLS = {
    "annotation_item_id": "A3", "input_sha256": "A4", "title": "A7", "abstract": "A10",
    "familiarity": "B13", "decision": "B14", "confidence": "B15",
    "rationale": "B16", "completed_at": "B17",
}
ANSWER_FIELDS = ("familiarity", "decision", "confidence", "rationale", "completed_at")
TEMPLATE_NAMES = ("Разметка_Дмитрий.xlsx", "Разметка_научрук.xlsx")
MAX_SOURCE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class PracticePacket:
    """Проверенные исходные тексты, пустой бланк и паспорт черновой пробы."""

    description: dict[str, Any]
    items: tuple[dict[str, str], ...]
    template: PracticeWorkbook


def sha256(data: bytes) -> str:
    """Вычислить контрольную сумму точных байтов источника."""

    return hashlib.sha256(data).hexdigest()


def checked_path(root: Path, value: Path) -> Path:
    """Запретить выход из проекта и символические ссылки во всех компонентах."""

    path = Path(value)
    candidate = path if path.is_absolute() else root / path

    if ".." in path.parts or not candidate.is_relative_to(root):
        raise ValueError(f"Путь должен оставаться внутри проекта без перехода вверх: {path}")

    current = root

    for part in candidate.relative_to(root).parts:
        current = current / part

        if current.is_symlink():
            raise ValueError(f"Символическая ссылка недопустима: {current}")

    return candidate


def read_source(root: Path, path: Path, sources: dict[str, bytes]) -> bytes:
    """Прочитать ограниченный обычный файл и сохранить точные байты для контроля."""

    checked = checked_path(root, path)

    if not checked.is_file() or checked.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"Нужен обычный файл размером не более 16 МиБ: {checked}")

    with checked.open("rb") as stream:
        data = stream.read(MAX_SOURCE_BYTES + 1)

    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError(f"Файл превышает допустимый размер: {checked}")

    name = checked.relative_to(root).as_posix()

    if name in sources and sources[name] != data:
        raise ValueError(f"Источник изменился во время чтения: {checked}")

    sources[name] = data
    return data


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Не выбирать последнее значение дублирующегося ключа JSON."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторный ключ JSON: {key}")

        result[key] = value

    return result


def _reject_constant(value: str) -> None:
    """Отклонить нечисловые константы, которых нет в формате JSON."""

    raise ValueError(f"Недопустимая константа JSON: {value}")


def read_object(data: bytes) -> dict[str, Any]:
    """Прочитать объект JSON, сохранив строгую проверку повторных ключей."""

    value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)

    if not isinstance(value, dict):
        raise ValueError("Ожидается объект JSON")

    return value


def _inputs(data: bytes, count: int) -> tuple[dict[str, str], ...]:
    """Проверить уникальные ID и заново вычислить хеш каждого точного входа."""

    items: list[dict[str, str]] = []
    identifiers: set[str] = set()

    for line in data.splitlines():
        item = read_object(line)

        if set(item) != {"annotation_item_id", "title", "abstract", "input_sha256"}:
            raise ValueError("Неожиданные поля в обезличенном inputs.jsonl")

        if any(not isinstance(value, str) or not value.strip() for value in item.values()):
            raise ValueError("ID, название, аннотация и хеш должны быть непустыми строками")

        identifier = item["annotation_item_id"]

        if identifier in identifiers:
            raise ValueError(f"Повторный ID карточки: {identifier}")

        identifiers.add(identifier)
        text = {"title": item["title"], "abstract": item["abstract"]}
        canonical = json.dumps(text, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

        if sha256(canonical) != item["input_sha256"]:
            raise ValueError(f"Не совпадает SHA-256 входа карточки: {identifier}")

        items.append(item)

    if len(items) != count:
        raise ValueError("Число входных карточек не совпадает с паспортом")

    return tuple(items)


def _packet_files(root: Path, packet_dir: Path, description: dict[str, Any], sources: dict[str, bytes]) -> dict[str, bytes]:
    """Проверить файлы исходного пакета по его описи, не раскрывая закрытые поля."""

    inventory = description.get("output_files")

    if not isinstance(inventory, dict) or not {"inputs.jsonl", "author/INSTRUCTIONS.txt"} <= inventory.keys():
        raise ValueError("В описи пакета отсутствуют inputs.jsonl или памятка")

    result: dict[str, bytes] = {}

    for name, expected in inventory.items():
        path = checked_path(root, packet_dir / name)

        if not path.is_relative_to(packet_dir) or not isinstance(expected, dict):
            raise ValueError("Опись должна ссылаться только на файлы внутри пакета")

        data = read_source(root, path, sources)

        if type(expected.get("bytes")) is not int or expected["bytes"] != len(data) or expected.get("sha256") != sha256(data):
            raise ValueError(f"SHA-256 или размер исходного файла изменён: {name}")

        result[name] = data

    return result


def read_practice_packet(root: Path, packet_dir: Path, template_dir: Path, sources: dict[str, bytes]) -> PracticePacket:
    """Связать паспорт, точные тексты и пустые книги по контрольным суммам."""

    packet_bytes = read_source(root, packet_dir / "packet.json", sources)
    description = read_object(packet_bytes)

    if description.get("format_version") != "h2-practice-draft-v1":
        raise ValueError("Поддерживается только отдельный черновой пакет h2-practice-draft-v1")

    if description.get("status") != "awaiting_guide_approval" or any(
        description.get(field) is not False
        for field in ("guide_approved", "annotation_started", "formal_calibration", "formal_audit", "training_ready")
    ):
        raise ValueError("Паспорт должен описывать исходную черновую пробу, не формальный аудит")

    count = description.get("item_count")

    if type(count) is not int or not 1 <= count <= 30:
        raise ValueError("В черновом пакете должно быть от 1 до 30 карточек")

    if any(not isinstance(description.get(field), str) or not description[field].strip() for field in ("packet_id", "guide_version")):
        raise ValueError("Не заданы ID пакета и версия памятки")

    packet_files = _packet_files(root, packet_dir, description, sources)
    items = _inputs(packet_files["inputs.jsonl"], count)
    verification = read_object(read_source(root, template_dir / "verification.json", sources))
    expected_values = {
        "format_version": "h2-practice-excel-v1",
        "source_packet_sha256": sha256(packet_bytes),
        "source_inputs_sha256": sha256(packet_files["inputs.jsonl"]),
        "source_guide_sha256": sha256(packet_files["author/INSTRUCTIONS.txt"]),
        "files": list(TEMPLATE_NAMES), "items": count, "card_cells": CARD_CELLS,
        "participant_cell": "Памятка!B4",
        "sheet_order": ["Памятка"] + [f"{index:02d}" for index in range(1, count + 1)],
    }

    if any(verification.get(key) != value for key, value in expected_values.items()):
        raise ValueError("Паспорт Excel не совпадает с исходным пакетом или известным расположением полей")

    template_data = [read_source(root, template_dir / name, sources) for name in TEMPLATE_NAMES]

    if template_data[0] != template_data[1] or sha256(template_data[0]) != verification.get("initial_workbook_sha256"):
        raise ValueError("SHA-256 исходных пустых Excel-файлов изменён или книги различаются")

    template = read_practice_workbook(template_data[0])

    if list(template.sheets) != expected_values["sheet_order"] or template.cells["Памятка"].get("B4"):
        raise ValueError("Неверные листы или заполненный участник в исходном бланке")

    for index, item in enumerate(items, 1):
        cells = template.cells[f"{index:02d}"]

        if any(cells.get(CARD_CELLS[field]) != value for field, value in item.items()):
            raise ValueError("Тексты и ID исходного бланка не совпадают с inputs.jsonl")

        if any(cells.get(CARD_CELLS[field]) for field in ANSWER_FIELDS):
            raise ValueError("Исходный Excel-бланк содержит готовые ответы")

    return PracticePacket(description, items, template)
