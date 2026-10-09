"""Загрузка и строгая проверка записей по JSON Schema Draft 2020-12."""

from __future__ import annotations

import json
import re

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jsonschema.protocols import Validator


class SchemaValidationError(ValueError):
    """Запись или сама схема не прошла проверку."""


SCHEMA_FILES = {
    "works": "works.schema.json",
    "artifacts": "artifacts.schema.json",
    "rights": "rights.schema.json",
    "work_revisions": "work_revisions.schema.json",
    "artifact_revisions": "artifact_revisions.schema.json",
    "retrieval_events": "retrieval_events.schema.json",
    "work_aliases": "work_aliases.schema.json",
    "identity_conflicts": "identity_conflicts.schema.json",
    "operation_decisions": "operation_decisions.schema.json",
    "condition_fulfilments": "condition_fulfilments.schema.json",
    "frozen_manifest": "frozen_manifest.schema.json",
}
MAX_VALIDATION_ERRORS = 8
MANIFEST_DATETIME_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _is_manifest_datetime(value: object) -> bool:
    """Проверить поддерживаемую реестрами метку RFC 3339 с часовым поясом."""

    if not isinstance(value, str):
        return True

    # fromisoformat принимает также сокращённые и локальные даты, поэтому
    # сначала ограничиваем синтаксис полным временем с явным часовым поясом.
    # Строчные t/z и дополнительные секунды не поддерживаются кодом реестров.
    if MANIFEST_DATETIME_PATTERN.fullmatch(value) is None:
        return False

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

    except ValueError:
        return False

    return parsed.utcoffset() is not None


class SchemaCatalog:
    """Каталог лениво загружаемых валидаторов машинных реестров."""

    def __init__(self, schema_dir: Path) -> None:
        """Создать каталог для схем из указанного каталога."""

        self.schema_dir = Path(schema_dir)
        self._validators: dict[str, Validator] = {}

    def validator(self, kind: str) -> Validator:
        """Вернуть закешированный валидатор реестра выбранного вида."""

        if kind in self._validators:
            return self._validators[kind]

        try:
            from jsonschema import Draft202012Validator, FormatChecker

        except ImportError as exception:
            raise RuntimeError(
                "Для проверки реестров установите зависимости: "
                "python -m pip install -r requirements.txt"
            ) from exception

        try:
            schema_file = SCHEMA_FILES[kind]

        except KeyError as exception:
            raise KeyError(f"Неизвестный реестр: {kind}") from exception

        path = self.schema_dir / schema_file

        try:
            schema = json.loads(path.read_text(encoding="utf-8"))

        except (OSError, json.JSONDecodeError) as exception:
            raise SchemaValidationError(
                f"Не удалось прочитать схему {path}: {exception}"
            ) from exception

        try:
            Draft202012Validator.check_schema(schema)

        except Exception as exception:  # jsonschema имеет несколько типов SchemaError
            raise SchemaValidationError(
                f"Некорректная JSON Schema {path}: {exception}"
            ) from exception

        format_checker = FormatChecker()
        # В jsonschema проверка date-time без необязательной зависимости
        # может отсутствовать. Переопределяем только её, сохраняя остальные.
        format_checker.checks("date-time")(_is_manifest_datetime)
        validator = Draft202012Validator(schema, format_checker=format_checker)
        self._validators[kind] = validator

        return validator

    def validate(self, kind: str, record: dict[str, Any]) -> None:
        """Проверить запись и перечислить первые найденные ошибки схемы."""

        errors = sorted(
            self.validator(kind).iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )

        if not errors:
            return

        details: list[str] = []

        for error in errors[:MAX_VALIDATION_ERRORS]:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            details.append(f"{location}: {error.message}")

        if len(errors) > MAX_VALIDATION_ERRORS:
            hidden_error_count = len(errors) - MAX_VALIDATION_ERRORS
            details.append(f"… ещё ошибок: {hidden_error_count}")

        raise SchemaValidationError(f"{kind}: " + "; ".join(details))
