"""Загрузка и строгая проверка записей по JSON Schema Draft 2020-12."""

from __future__ import annotations

import json

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

        validator = Draft202012Validator(schema, format_checker=FormatChecker())
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
