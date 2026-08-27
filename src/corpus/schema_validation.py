"""Загрузка и строгая проверка записей по JSON Schema Draft 2020-12."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SchemaValidationError(ValueError):
    """Запись или сама схема не прошла проверку."""


SCHEMA_FILES = {
    "works": "works.schema.json",
    "artifacts": "artifacts.schema.json",
    "rights": "rights.schema.json",
}


class SchemaCatalog:
    def __init__(self, schema_dir: Path) -> None:
        self.schema_dir = Path(schema_dir)
        self._validators: dict[str, Any] = {}

    def validator(self, kind: str):
        if kind in self._validators:
            return self._validators[kind]
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError as exc:
            raise RuntimeError(
                "Для проверки реестров установите зависимости: "
                "python -m pip install -r requirements.txt"
            ) from exc

        try:
            schema_file = SCHEMA_FILES[kind]
        except KeyError as exc:
            raise KeyError(f"Неизвестный реестр: {kind}") from exc
        path = self.schema_dir / schema_file
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SchemaValidationError(f"Не удалось прочитать схему {path}: {exc}") from exc

        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # jsonschema имеет несколько типов SchemaError
            raise SchemaValidationError(f"Некорректная JSON Schema {path}: {exc}") from exc

        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self._validators[kind] = validator
        return validator

    def validate(self, kind: str, record: dict[str, Any]) -> None:
        errors = sorted(
            self.validator(kind).iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if not errors:
            return
        details = []
        for error in errors[:8]:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            details.append(f"{location}: {error.message}")
        if len(errors) > 8:
            details.append(f"… ещё ошибок: {len(errors) - 8}")
        raise SchemaValidationError(f"{kind}: " + "; ".join(details))
