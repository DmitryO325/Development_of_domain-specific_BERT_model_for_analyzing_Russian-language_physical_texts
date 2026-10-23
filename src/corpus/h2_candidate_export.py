"""Безопасный офлайн-экспорт очереди просмотра кандидатов для классификации H2."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata

from dataclasses import dataclass
from pathlib import Path
from typing import Any

OUTPUT_BASE = Path("manifests/results")
EXPORT_VERSION = "h2-candidate-export-v1"
CSV_FIELDS = (
    ("candidate_id", "ID кандидата"),
    ("item_uuid", "ID карточки ОИЯИ"),
    ("title", "Название"),
    ("abstract", "Аннотация"),
    ("journal", "Журнал"),
    ("year", "Год"),
    ("source_url", "Ссылка на карточку"),
    ("doi", "DOI"),
    ("edn", "EDN"),
    ("doi_values", "Варианты DOI (нормализованные)"),
    ("edn_values", "Варианты EDN (нормализованные)"),
    ("rubrics_as_reported", "Рубрики в источнике — не метки H2"),
    ("udc_as_reported", "УДК в источнике"),
    ("has_title_abstract", "Есть название и аннотация"),
    ("physics_signal", "Признак физической тематики по рубрике"),
    ("review_flags", "Причины ручной проверки"),
    ("duplicate_groups", "Группы возможных дублей"),
    ("review_status", "Статус просмотра"),
)
REVIEW_COLUMNS = ("Решение при просмотре", "Комментарий")
PHYSICS_SIGNALS = {
    "physics_rubric": "Есть рубрика физики; требуется проверка",
    "other_rubric": "Есть другая рубрика; требуется проверка",
    "missing_rubric": "Рубрика отсутствует",
}
REVIEW_FLAGS = {
    "missing_title": "Нет названия",
    "missing_abstract": "Нет аннотации",
    "missing_year": "Нет года",
    "missing_journal": "Нет журнала",
    "multiple_title_variants": "Несколько вариантов названия",
    "multiple_abstract_variants": "Несколько вариантов аннотации",
    "multiple_year_variants": "Несколько вариантов даты публикации",
    "multiple_journal_variants": "Несколько вариантов журнала",
    "unresolved_year": "Год не определён однозначно",
    "invalid_doi": "Неверный формат DOI",
    "invalid_edn": "Неверный формат EDN",
    "ambiguous_doi": "Несколько различных DOI",
    "ambiguous_edn": "Несколько различных EDN",
    "check_title_language": "Проверить язык названия",
    "check_abstract_language": "Проверить язык аннотации",
    "check_reported_language": "Проверить язык, указанный источником",
    "mixed_document_types": "В источнике указано несколько типов документа",
    "check_withdrawn": "Проверить признак отзыва карточки",
    "check_inArchive": "Проверить включение карточки в архив",
    "check_discoverable": "Проверить доступность карточки в поиске",
    "possible_label_hint_in_text": "В тексте возможна подсказка классификационной метки",
    "possible_duplicate_doi": "Возможный дубль по DOI",
    "possible_duplicate_edn": "Возможный дубль по EDN",
    "possible_duplicate_title_year": "Возможный дубль по названию и году",
    "possible_duplicate_abstract": "Возможный дубль по аннотации",
}


@dataclass(frozen=True)
class CandidateExportResult:
    """Результат записи нового пакета либо проверки неизменившегося повтора."""

    output_dir: Path
    created_files: int
    unchanged_files: int
    hashes: dict[str, str]


def _checked_path(root: Path, value: Path) -> Path:
    """Оставить путь внутри проекта без переходов вверх и символических ссылок."""

    path = Path(value)

    if ".." in path.parts:
        raise ValueError(f"Переход к родительскому каталогу недопустим: {path}")

    candidate = path if path.is_absolute() else root / path

    if not candidate.is_relative_to(root):
        raise ValueError(f"Путь находится вне проекта: {path}")

    current = root

    for part in candidate.relative_to(root).parts:
        current = current / part

        if current.is_symlink():
            raise ValueError(f"Символическая ссылка недопустима: {current}")

    if not candidate.resolve().is_relative_to(root):
        raise ValueError(f"Путь находится вне проекта: {path}")

    return candidate


def _output_path(root: Path, output_dir: Path, snapshot_path: Path) -> Path:
    """Проверить отдельный каталог результатов, не пересекающийся со снимком."""

    output = _checked_path(root, output_dir)
    snapshot = _checked_path(root, snapshot_path)
    output_base = root / OUTPUT_BASE

    if output == output_base or not output.is_relative_to(output_base):
        raise ValueError("Для результата нужен отдельный каталог внутри manifests/results")

    if output.is_relative_to(snapshot.parent) or snapshot.parent.is_relative_to(output):
        raise ValueError("Каталог результата не должен пересекаться с каталогом снимка")

    return output


def _csv_safe(value: str) -> str:
    """Защитить ячейку от формулы, сохранив исходные строки отдельно в JSONL."""

    prefix_length = 0

    for character in value:
        if not character.isspace() and not unicodedata.category(character).startswith("C"):
            break

        prefix_length += 1

    if value[prefix_length:prefix_length + 1] in {"=", "+", "-", "@"}:
        return "'" + value

    return value


def _csv_value(key: str, value: Any) -> str:
    """Представить значение для ручного просмотра без изменения исходной записи."""

    if value is None:
        return ""

    if key == "has_title_abstract":
        return "Да" if value else "Нет"

    if key == "physics_signal":
        return PHYSICS_SIGNALS.get(value, str(value))

    if key == "review_status" and value == "not_reviewed":
        return "Не просмотрено"

    if key == "review_flags":
        return _csv_safe("\n".join(REVIEW_FLAGS.get(flag, flag) for flag in value))

    if isinstance(value, list):
        return _csv_safe("\n".join(str(item) for item in value))

    return _csv_safe(str(value))


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    """Собрать CSV с русскими заголовками, пустыми решениями и поддержкой переносов."""

    output = io.StringIO(newline="")
    fieldnames = [title for _, title in CSV_FIELDS] + list(REVIEW_COLUMNS)
    writer = csv.DictWriter(
        output,
        fieldnames=fieldnames,
        delimiter=";",
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )
    writer.writeheader()

    for row in rows:
        record = {title: _csv_value(key, row[key]) for key, title in CSV_FIELDS}
        record.update({title: "" for title in REVIEW_COLUMNS})
        writer.writerow(record)

    return output.getvalue().encode("utf-8-sig")


def _json_bytes(value: Any, *, indent: int | None = None) -> bytes:
    """Сохранить Unicode и стабильный порядок ключей, запретив NaN и Infinity."""

    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=indent,
    )

    return (text + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    """Вычислить SHA-256 точного содержимого файла."""

    return hashlib.sha256(data).hexdigest()


def _bundle_bytes(
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, bytes]:
    """Подготовить все файлы в памяти до проверки и создания каталога результата."""

    if "export" in summary:
        raise ValueError("Поле export зарезервировано для отчёта о файлах результата")

    payloads = {
        "candidates.jsonl": b"".join(_json_bytes(row) for row in rows),
        "review.csv": _csv_bytes(rows),
    }
    report = {
        **summary,
        "export": {
            "format_version": EXPORT_VERSION,
            "candidate_count": len(rows),
            "review_is_blind_labeling": False,
            "csv_encoding": "utf-8-sig",
            "csv_delimiter": ";",
            "csv_formula_protection": "leading_apostrophe_only_in_csv",
            "files": {
                name: {"sha256": _sha256(data), "bytes": len(data)}
                for name, data in payloads.items()
            },
        },
    }
    payloads["summary.json"] = _json_bytes(report, indent=2)

    return payloads


def _check_existing(output: Path, payloads: dict[str, bytes]) -> bool:
    """Разрешить только полный побайтово совпадающий повтор без какой-либо записи."""

    if not output.exists():
        return False

    if not output.is_dir():
        raise ValueError(f"Путь результата уже занят файлом: {output}")

    if {path.name for path in output.iterdir()} != set(payloads):
        raise ValueError(
            "Каталог результата уже существует и не является полным пакетом; "
            "выберите новый каталог версии"
        )

    for name, expected in payloads.items():
        path = output / name

        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise ValueError(
                f"Существующий файл отличается или небезопасен: {path}; "
                "правки просмотра не перезаписываются, выберите новый каталог версии"
            )

    return True


def export_candidate_queue(
    project_root: Path,
    output_dir: Path,
    rows: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    snapshot_path: Path,
) -> CandidateExportResult:
    """Создать новый пакет либо проверить точный повтор, не меняя прежние файлы."""

    root = Path(project_root).resolve()
    output = _output_path(root, output_dir, snapshot_path)
    payloads = _bundle_bytes(rows, summary)
    hashes = {name: _sha256(data) for name, data in payloads.items()}

    if _check_existing(output, payloads):
        return CandidateExportResult(output, 0, len(payloads), hashes)

    output.parent.mkdir(parents=True, exist_ok=True)

    # mkdir и xb не допускают перезапись при появлении чужого результата после проверки.
    output.mkdir(exist_ok=False)

    for name, data in payloads.items():
        with (output / name).open("xb") as stream:
            stream.write(data)

    return CandidateExportResult(output, len(payloads), 0, hashes)
