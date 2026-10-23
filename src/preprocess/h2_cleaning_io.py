"""Происхождение и безопасная запись производной очереди очистки метаданных H2."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import tempfile
import unicodedata

from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from src.corpus.h2_candidate_export import EXPORT_VERSION, REVIEW_FLAGS
from src.preprocess.h2_text_cleaning import CLEANING_VERSION, clean_metadata_text

OUTPUT_BASE = Path("manifests/results")
SOURCE_NAMES = ("summary.json", "candidates.jsonl", "review.csv")
OUTPUT_NAMES = frozenset({"summary.json", "cleaned_candidates.jsonl", "changes.jsonl", "review.csv"})
CODE_NAMES = ("src/preprocess/h2_text_cleaning.py", "src/preprocess/h2_cleaning_io.py")
CSV_COLUMNS = (
    "Позиция", "ID кандидата", "ID карточки ОИЯИ", "Ссылка на карточку",
    "Название — оригинал", "Название — очищенное", "Аннотация — оригинал",
    "Аннотация — очищенная", "Число замен", "Причины проверки",
    "Решение при просмотре", "Комментарий",
)


@dataclass(frozen=True)
class PreparedH2Cleaning:
    """Проверенный пакет в памяти; исходная очередь и реестры остаются неизменными."""

    project_root: Path
    queue_dir: Path
    output_dir: Path
    files: dict[str, bytes]
    report: dict[str, Any]
    source_hashes: dict[str, str]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Не выбирать молча последнее значение повторяющегося ключа JSON."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Повторный ключ JSON: {key}")

        result[key] = value

    return result


def _reject_constant(value: str) -> None:
    """Отклонить не предусмотренные JSON числовые константы."""

    raise ValueError(f"Недопустимая константа JSON: {value}")


def _finite_float(value: str) -> float:
    """Отклонить переполнение числа, которое иначе превращается в Infinity."""

    number = float(value)

    if not math.isfinite(number):
        raise ValueError(f"Нечисловое или бесконечное значение JSON: {value}")

    return number


def _read_json(data: bytes) -> Any:
    """Прочитать строгий UTF-8 JSON без дубликатов ключей и бесконечностей."""

    return json.loads(
        data.decode("utf-8"), object_pairs_hook=_unique_object,
        parse_constant=_reject_constant, parse_float=_finite_float,
    )


def _json_bytes(value: Any, *, indent: int | None = None) -> bytes:
    """Получить стабильные байты JSON с завершающим переводом строки."""

    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=indent,
    ) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    """Вычислить контрольную сумму точных байтов, включая переводы строк."""

    return hashlib.sha256(data).hexdigest()


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


def _paths(project_root: Path, queue_dir: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    """Проверить независимые входной и выходной каталоги в пределах проекта."""

    if ".." in Path(project_root).parts or Path(project_root).is_symlink():
        raise ValueError("Корень проекта не должен содержать переход вверх или быть ссылкой")

    root = Path(project_root).resolve()

    if not root.is_dir():
        raise ValueError("Корень проекта должен быть существующим каталогом")

    queue = _checked_path(root, queue_dir)
    output = _checked_path(root, output_dir)
    output_base = root / OUTPUT_BASE

    if not queue.is_dir():
        raise ValueError(f"Каталог исходной очереди не найден: {queue}")

    if output == output_base or not output.is_relative_to(output_base):
        raise ValueError("Нужна отдельная подпапка результата внутри manifests/results")

    if output.is_relative_to(queue) or queue.is_relative_to(output):
        raise ValueError("Каталоги исходной очереди и результата не должны пересекаться")

    return root, queue, output


def _source_bytes(root: Path, queue: Path) -> dict[str, bytes]:
    """Прочитать три исходных файла без изменения заметок человека в CSV."""

    result: dict[str, bytes] = {}

    for name in SOURCE_NAMES:
        path = _checked_path(root, queue / name)

        if not path.is_file():
            raise ValueError(f"В исходной очереди отсутствует обычный файл: {path}")

        result[name] = path.read_bytes()

    return result


def _queue_count(summary: Any, candidates: bytes) -> int:
    """Сверить неизменяемый JSONL с исходным отчётом экспорта."""

    if not isinstance(summary, dict) or not isinstance(summary.get("export"), dict):
        raise ValueError("Отчёт очереди должен содержать объект export")

    export = summary["export"]

    if export.get("format_version") != EXPORT_VERSION:
        raise ValueError("Неизвестная версия экспорта очереди H2")

    count = export.get("candidate_count")

    if type(count) is not int or count < 0 or type(summary.get("candidate_count")) is not int:
        raise ValueError("Число кандидатов должно быть неотрицательным целым числом")

    if summary["candidate_count"] != count:
        raise ValueError("Число кандидатов не совпадает между отчётами очереди и экспорта")

    if (
        summary.get("training_ready") is not False
        or type(summary.get("labelled_count")) is not int
        or summary["labelled_count"] != 0
    ):
        raise ValueError("Нужна очередь кандидатов без допуска к обучению и готовых меток")

    exported_files = export.get("files")
    expected = exported_files.get("candidates.jsonl") if isinstance(exported_files, dict) else None

    if not isinstance(expected, dict):
        raise ValueError("В отчёте отсутствуют контрольные данные candidates.jsonl")

    if (
        type(expected.get("bytes")) is not int
        or expected["bytes"] != len(candidates)
        or expected.get("sha256") != _sha256(candidates)
    ):
        raise ValueError("SHA-256 или размер candidates.jsonl не совпадает с исходным отчётом")

    return count


def _candidate(data: bytes, position: int, seen: dict[str, set[str]]) -> dict[str, Any]:
    """Проверить идентификаторы и поля строки, не анализируя рубрики как метки."""

    row = _read_json(data)

    if not isinstance(row, dict):
        raise ValueError(f"Кандидат {position}: ожидается объект JSON")

    for field in ("candidate_id", "item_uuid", "source_url", "title", "abstract"):
        if not isinstance(row.get(field), str):
            raise ValueError(f"Кандидат {position}: поле {field} должно быть строкой")

    for field in ("candidate_id", "item_uuid"):
        value = row[field]

        if not value or value in seen[field]:
            raise ValueError(f"Кандидат {position}: пустое или повторное поле {field}")

        seen[field].add(value)

    try:
        identifier = str(UUID(row["item_uuid"]))
        url = urlsplit(row["source_url"])

    except ValueError as exception:
        raise ValueError(f"Кандидат {position}: неверный UUID или URL") from exception

    if identifier != row["item_uuid"] or row["candidate_id"] != f"jinr:{identifier}":
        raise ValueError(f"Кандидат {position}: идентификаторы карточки не согласованы")

    if (
        url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
        or any(character.isspace() for character in row["source_url"])
    ):
        raise ValueError(f"Кандидат {position}: нужна обычная HTTP(S)-ссылка на карточку")

    for field in ("label", "split"):
        if field not in row or row[field] is not None:
            raise ValueError(f"Кандидат {position}: поле {field} должно явно содержать null")

    flags = row.get("review_flags")

    if not isinstance(flags, list) or any(not isinstance(flag, str) or not flag for flag in flags):
        raise ValueError(f"Кандидат {position}: review_flags должен быть списком строк")

    return row


def _clean_candidate(row: dict[str, Any], position: int, line: bytes) -> dict[str, Any]:
    """Создать производную строку, сохранив оригиналы и неоднозначные варианты."""

    cleaned: dict[str, Any] = {
        "position": position,
        "candidate_id": row["candidate_id"],
        "item_uuid": row["item_uuid"],
        "source_url": row["source_url"],
        "source_candidate_line_sha256": _sha256(line),
        "cleaning_version": CLEANING_VERSION,
        "source_review_flags": list(row["review_flags"]),
        "changes": [], "issues": [], "label": None, "split": None,
        "training_ready": False,
    }

    for field in ("title", "abstract"):
        original = row[field]
        cleaned[f"{field}_original"] = original

        if f"multiple_{field}_variants" in row["review_flags"]:
            cleaned[f"{field}_clean"] = original
            cleaned["issues"].append({
                "field": field, "start": 0, "end": len(original),
                "code": f"multiple_{field}_variants",
                "detail": "Несколько исходных вариантов; поле сохранено целиком без очистки и выбора варианта",
            })
            continue

        result = clean_metadata_text(original)
        cleaned[f"{field}_clean"] = result.text
        cleaned["changes"].extend({"field": field, **asdict(change)} for change in result.changes)
        cleaned["issues"].extend({"field": field, **asdict(issue)} for issue in result.issues)

    return cleaned


def _csv_safe(value: str) -> str:
    """Защитить от формул только представление CSV, не меняя исходные тексты JSONL."""

    offset = 0

    for character in value:
        if not character.isspace() and not unicodedata.category(character).startswith("C"):
            break

        offset += 1

    return "'" + value if value[offset:offset + 1] in {"=", "+", "-", "@"} else value


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    """Собрать отдельную очередь сравнения без переноса решений человека в метки."""

    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)

    for row in rows:
        reasons = [REVIEW_FLAGS.get(flag, f"Флаг источника: {flag}") for flag in row["source_review_flags"]]
        field_names = {"title": "Название", "abstract": "Аннотация"}
        reasons.extend(f"{field_names[issue['field']]}: {issue['detail']}" for issue in row["issues"])
        values = [
            str(row["position"]), row["candidate_id"], row["item_uuid"], row["source_url"],
            row["title_original"], row["title_clean"], row["abstract_original"], row["abstract_clean"],
            str(len(row["changes"])), "\n".join(dict.fromkeys(reasons)), "", "",
        ]
        writer.writerow([_csv_safe(value) for value in values])

    return output.getvalue().encode("utf-8-sig")


def _counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Посчитать все карточки, изменения и проблемы без отбрасывания пустых полей."""

    return {
        "candidates": len(rows),
        "changed_candidates": sum(bool(row["changes"]) for row in rows),
        "changes": sum(len(row["changes"]) for row in rows),
        "issues": sum(len(row["issues"]) for row in rows),
        "candidates_with_issues": sum(bool(row["issues"]) for row in rows),
        "missing_title": sum(not row["title_original"].strip() for row in rows),
        "missing_abstract": sum(not row["abstract_original"].strip() for row in rows),
    }


def prepare_h2_cleaning(
    project_root: Path,
    queue_dir: Path,
    output_dir: Path,
) -> PreparedH2Cleaning:
    """Проверить источник и построить воспроизводимый пакет в памяти без записи."""

    root, queue, output = _paths(project_root, queue_dir, output_dir)
    sources = _source_bytes(root, queue)
    summary = _read_json(sources["summary.json"])
    expected_count = _queue_count(summary, sources["candidates.jsonl"])
    seen: dict[str, set[str]] = {"candidate_id": set(), "item_uuid": set()}
    rows: list[dict[str, Any]] = []

    for position, line in enumerate(sources["candidates.jsonl"].splitlines(keepends=True), 1):
        if not line.endswith(b"\n") or not line.strip():
            raise ValueError(f"Кандидат {position}: нужна непустая строка JSONL с завершающим LF")

        rows.append(_clean_candidate(_candidate(line, position, seen), position, line))

    if len(rows) != expected_count:
        raise ValueError("Число строк candidates.jsonl не совпадает с исходным отчётом")

    flat_changes = [
        {"position": row["position"], "candidate_id": row["candidate_id"], "item_uuid": row["item_uuid"], **change}
        for row in rows for change in row["changes"]
    ]
    files = {
        "cleaned_candidates.jsonl": b"".join(_json_bytes(row) for row in rows),
        "changes.jsonl": b"".join(_json_bytes(change) for change in flat_changes),
        "review.csv": _csv_bytes(rows),
    }
    source_files = {
        (queue / name).relative_to(root).as_posix(): {"sha256": _sha256(data), "bytes": len(data)}
        for name, data in sources.items()
    }
    code_files: dict[str, dict[str, Any]] = {}

    for name in CODE_NAMES:
        path = _checked_path(root, Path(name))

        if path.exists():
            if not path.is_file():
                raise ValueError(f"Исходник очистки должен быть обычным файлом: {path}")

            data = path.read_bytes()
            code_files[name] = {"sha256": _sha256(data), "bytes": len(data)}

    report = {
        "schema_version": "h2-metadata-cleaning-report-v1",
        "status": "candidate_not_approved",
        "cleaning_version": CLEANING_VERSION,
        "training_ready": False,
        "labelled_count": 0,
        "queue_dir": queue.relative_to(root).as_posix(),
        "output_dir": output.relative_to(root).as_posix(),
        "source_files": source_files,
        "code_files": code_files,
        "counts": _counts(rows),
        "change_counts": dict(sorted(Counter(change["code"] for change in flat_changes).items())),
        "issue_counts": dict(sorted(Counter(issue["code"] for row in rows for issue in row["issues"]).items())),
        "limitations": [
            "Очистка метаданных не назначает метки H2, разбиения или допуск к обучению",
            "Исходные рубрики и предложения помощника не используются как эталон",
            "Карточки, пустые поля, дубли и исходные варианты не удаляются",
            "Заметки исходного CSV остаются в исходной очереди; фиксируется его актуальная контрольная сумма",
            "Заменами из журнала воспроизводится очищенный текст; смещения относятся к оригиналу",
        ],
        "export": {
            "csv_encoding": "utf-8-sig", "csv_delimiter": ";",
            "csv_formula_protection": "leading_apostrophe_only_in_csv",
            "review_is_blind_labeling": False,
            "files": {name: {"sha256": _sha256(data), "bytes": len(data)} for name, data in files.items()},
        },
    }
    files["summary.json"] = _json_bytes(report, indent=2)
    source_hashes = {name: item["sha256"] for name, item in {**source_files, **code_files}.items()}

    return PreparedH2Cleaning(root, queue, output, files, report, source_hashes)


def _verify_sources(prepared: PreparedH2Cleaning) -> None:
    """Перед публикацией обнаружить изменение очереди, заметок CSV или исходников."""

    for name, expected in prepared.source_hashes.items():
        path = _checked_path(prepared.project_root, Path(name))

        if not path.is_file() or _sha256(path.read_bytes()) != expected:
            raise ValueError(f"Исходник изменился после подготовки: {path}")


def _existing_matches(prepared: PreparedH2Cleaning) -> bool:
    """Разрешить лишь точный полный повтор без перезаписи существующих результатов."""

    output = prepared.output_dir

    if not output.exists():
        return False

    if not output.is_dir() or {path.name for path in output.iterdir()} != OUTPUT_NAMES:
        raise ValueError("Существующий результат неполон или имеет другой состав; выберите новый каталог")

    for name, data in prepared.files.items():
        path = _checked_path(prepared.project_root, output / name)

        if not path.is_file() or path.read_bytes() != data:
            raise ValueError(f"Существующий результат отличается; перезапись запрещена: {path}")

    return True


def _verify_prepared(prepared: PreparedH2Cleaning) -> None:
    """Проверить целостность изменяемых словарей подготовленного пакета."""

    if set(prepared.files) != OUTPUT_NAMES:
        raise ValueError("Неверный состав подготовленного пакета очистки")

    if prepared.files["summary.json"] != _json_bytes(prepared.report, indent=2):
        raise ValueError("Подготовленный отчёт был изменён")

    if prepared.report.get("cleaning_version") != CLEANING_VERSION:
        raise ValueError("Версия подготовленного пакета не совпадает с версией очистки")

    if (
        prepared.report["queue_dir"] != prepared.queue_dir.relative_to(prepared.project_root).as_posix()
        or prepared.report["output_dir"] != prepared.output_dir.relative_to(prepared.project_root).as_posix()
    ):
        raise ValueError("Каталоги подготовленного пакета не совпадают с отчётом")

    expected_sources = {
        name: record["sha256"]
        for name, record in {**prepared.report["source_files"], **prepared.report["code_files"]}.items()
    }

    if prepared.source_hashes != expected_sources:
        raise ValueError("Контрольные суммы источников подготовленного пакета изменены")

    for name, expected in prepared.report["export"]["files"].items():
        data = prepared.files[name]

        if expected != {"sha256": _sha256(data), "bytes": len(data)}:
            raise ValueError(f"Подготовленное содержимое изменилось: {name}")


def publish_h2_cleaning(prepared: PreparedH2Cleaning, *, dry_run: bool = False) -> bool:
    """Создать отдельный пакет либо подтвердить неизменный повтор без записи."""

    root, queue, output = _paths(prepared.project_root, prepared.queue_dir, prepared.output_dir)

    if (root, queue, output) != (prepared.project_root, prepared.queue_dir, prepared.output_dir):
        raise ValueError("Пути подготовленного пакета должны быть абсолютными и проверенными")

    _verify_prepared(prepared)
    _verify_sources(prepared)

    if _existing_matches(prepared) or dry_run:
        return False

    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".h2-cleaning-", dir=output.parent) as temporary:
        staging = Path(temporary)

        for name, data in prepared.files.items():
            with (staging / name).open("xb") as stream:
                stream.write(data)

        _paths(root, queue, output)
        _verify_sources(prepared)

        # mkdir и hard link не заменяют даже пустой чужой каталог при конкурентной записи.
        try:
            output.mkdir(exist_ok=False)

        except FileExistsError as exception:
            raise ValueError("Каталог результата появился во время записи; повторите проверку") from exception

        linked: list[str] = []

        try:
            for name in prepared.files:
                os.link(staging / name, output / name)
                linked.append(name)

        except OSError:
            # Удаляются только созданные нами ссылки на наши временные файлы, не чужие правки.
            for name in linked:
                path = output / name

                if (
                    not path.is_symlink() and path.exists() and path.samefile(staging / name)
                    and path.read_bytes() == prepared.files[name]
                ):
                    path.unlink()

            try:
                output.rmdir()

            except OSError:
                pass

            raise

    return True
