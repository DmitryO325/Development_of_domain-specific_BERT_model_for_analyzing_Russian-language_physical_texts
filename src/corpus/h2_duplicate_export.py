"""Безопасный офлайн-экспорт сигналов повторов и связей версий публикаций H2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .h2_candidate_export import (
    CandidateExportResult,
    _check_existing,
    _json_bytes,
    _output_path,
    _sha256,
)

EXPORT_VERSION = "h2-duplicate-review-export-v1"
RECORD_FILES = ("cards", "groups", "pairs", "relations")
START_HERE = """Проверка возможных повторов и связей версий публикаций

Это пакет для ручной проверки, а не список подтверждённых дублей.
Совпадение аннотации, названия, DOI или EDN само по себе не объединяет статьи.
Связь препринта, статьи или перевода также не означает тождества их файлов.
Источник может содержать неверные ссылки, включая ссылку карточки на себя.

Состав пакета:
  summary.json — происхождение данных, численности и SHA-256 файлов.
  groups.jsonl — группы совпадений по отдельному признаку.
  pairs.jsonl — пары карточек, требующие проверки; решения изначально пусты.
  relations.jsonl — связи, заявленные источником, и результат проверки их целей.
  cards.jsonl — исходные сведения о включённых в пакет карточках.

Как проверить пару:
  1. Откройте pairs.jsonl и выберите запись по pair_id.
  2. Найдите каждый item_uuid из item_uuids в cards.jsonl.
  3. Сравните названия, годы, идентификаторы и аннотации.
     metadata_as_reported хранит исходные метаданные, source — их происхождение.
  4. Посмотрите comparison и review_flags, затем связанные записи
     из groups.jsonl и relations.jsonl по указанным идентификаторам.
  5. При необходимости сверьте карточки и полные тексты вручную.
     Если данных недостаточно, оставьте вопрос нерешённым.

Большие группы в groups.jsonl разбирайте отдельно: программа не создаёт
все попарные сочетания внутри них. Одинаковая аннотация годовых отчётов
не делает отчёты за разные годы одной публикацией.

Связи relations.jsonl охватывают исходный снимок, а не только кандидатов H2.
Отсутствие цели в снимке не доказывает, что публикация не существует.
Для самоссылок, неверных целей и отсутствующих карточек вторая карточка
не выдумывается; такая запись может не иметь соответствующей пары.

Сохраняйте заметки с pair_id/group_id/relation_id и причиной решения.
Можно заполнить review_decision и comment в копии записи; автоматического
применения решений в этом пакете нет. При изменении файлов повторная
генерация откажется их перезаписывать: для неё нужен новый каталог версии.

Метки H2, разбиение выборки и рабочие реестры не изменены.
Данные не допущены автоматически к обучению. Ничего не скачивалось.
"""


def _bundle_bytes(review: dict[str, Any]) -> dict[str, bytes]:
    """Подготовить все точные байты и контрольные суммы до любых записей."""

    summary = review.get("summary")

    if not isinstance(summary, dict):
        raise ValueError("Пакет проверки должен содержать объект summary")

    if "export" in summary:
        raise ValueError("Поле export зарезервировано для отчёта о файлах результата")

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
            "automatic_merge": False,
            "registry_changes": False,
            "files": {
                name: {"sha256": _sha256(data), "bytes": len(data)}
                for name, data in payloads.items()
            },
        },
    }
    payloads["summary.json"] = _json_bytes(report, indent=2)

    return payloads


def _check_parent_directories(root: Path, output: Path) -> None:
    """Отказать при файле на месте родителя даже в пробном запуске без mkdir."""

    for parent in output.parents:
        if parent == root:
            break

        if parent.exists() and not parent.is_dir():
            raise ValueError(f"Родительский путь результата занят файлом: {parent}")


def export_duplicate_review(
    project_root: Path,
    output_dir: Path,
    review: dict[str, Any],
    *,
    snapshot_path: Path,
    dry_run: bool = False,
) -> CandidateExportResult:
    """Создать независимый пакет либо проверить запуск без перезаписи и сети."""

    root = Path(project_root).resolve()
    output = _output_path(root, output_dir, snapshot_path)
    _check_parent_directories(root, output)
    payloads = _bundle_bytes(review)
    hashes = {name: _sha256(data) for name, data in payloads.items()}

    if _check_existing(output, payloads):
        return CandidateExportResult(output, 0, len(payloads), hashes)

    if dry_run:
        return CandidateExportResult(output, 0, 0, hashes)

    output.parent.mkdir(parents=True, exist_ok=True)

    # mkdir и xb защищают от перезаписи при появлении чужих файлов после проверки.
    output.mkdir(exist_ok=False)

    for name, data in payloads.items():
        with (output / name).open("xb") as stream:
            stream.write(data)

    return CandidateExportResult(output, len(payloads), 0, hashes)
