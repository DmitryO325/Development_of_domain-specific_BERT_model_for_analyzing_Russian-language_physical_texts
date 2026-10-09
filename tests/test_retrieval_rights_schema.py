"""Проверки узких оснований прав на получение и хранение одного ответа."""

from __future__ import annotations

import json
import unittest

from pathlib import Path
from typing import Any

from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RETRIEVAL_OPERATIONS = ("acquisition", "storage")
CONTENT_OPERATIONS = (
    "evaluation",
    "ml_training",
    "redistribution",
    "derivatives_release",
    "checkpoint_release",
)
RIGHTS_STATUSES = (
    "allowed",
    "conditional",
    "permission_required",
    "prohibited",
    "unknown",
)
LEGACY_SCOPES = ("source_group", "source", "journal", "work", "artifact")
EVIDENCE_SHA256 = "a" * 64
EFFECTIVE_FROM = "2026-08-18T10:00:00+03:00"


class RetrievalRightsSchemaTests(unittest.TestCase):
    """Проверки границ нового охвата без миграции прежних оснований."""

    def setUp(self) -> None:
        """Загрузить действующую схему и синтетический пример основания."""

        self.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")
        template = PROJECT_ROOT / "manifests" / "templates" / "rights.example.jsonl"
        self.examples: list[dict[str, Any]] = [
            json.loads(line)
            for line in template.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def _retrieval_rights(
        self,
        *,
        operation: str = "acquisition",
        status: str = "allowed",
    ) -> dict[str, Any]:
        """Составить основание только на ответ API, сохраняя общие требования."""

        record = {
            **self.examples[0],
            "scope_type": "retrieval",
            "scope_id": "retrieval:synthetic-api-response",
            "operation": operation,
            "status": status,
            "acquisition_method": "api" if operation == "acquisition" else None,
            "acquisition_scope": "sample" if operation == "acquisition" else None,
        }

        if status == "conditional":
            record["rights_conditions"] = ["Закрытое хранение ответа API"]

        if operation == "derivatives_release":
            record["derivative_scope"] = ["aggregate_metrics"]

        return record

    def test_retrieval_accepts_acquisition_and_storage_for_every_status(self) -> None:
        """Один ответ может иметь разрешение, условия, запрет или неясный статус."""

        for operation in RETRIEVAL_OPERATIONS:
            for status in RIGHTS_STATUSES:
                with self.subTest(operation=operation, status=status):
                    record = self._retrieval_rights(operation=operation, status=status)
                    self.catalog.validate("rights", record)

    def test_retrieval_rejects_content_operations_for_every_status(self) -> None:
        """Получение ответа не задаёт прав на обучение, оценку или публикацию."""

        for operation in CONTENT_OPERATIONS:
            for status in RIGHTS_STATUSES:
                with self.subTest(operation=operation, status=status):
                    record = self._retrieval_rights(operation=operation, status=status)

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("rights", record)

    def test_retrieval_id_does_not_require_hash_or_new_prefix(self) -> None:
        """Ссылка принимает прежний непрефиксный ID, как схема событий получения."""

        for retrieval_id in ("retrieval:foo", "retrieval_demo_0001", "b" * 64):
            with self.subTest(retrieval_id=retrieval_id):
                record = {**self._retrieval_rights(), "scope_id": retrieval_id}
                self.catalog.validate("rights", record)

    def test_retrieval_rejects_empty_or_non_string_identifier(self) -> None:
        """Пустая строка и значения другого типа не являются идентификатором."""

        for retrieval_id in ("", None, 42, False, [], {}):
            with self.subTest(retrieval_id=retrieval_id):
                record = {**self._retrieval_rights(), "scope_id": retrieval_id}

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("rights", record)

        record = self._retrieval_rights()
        del record["scope_id"]

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("rights", record)

    def test_retrieval_permission_requires_evidence(self) -> None:
        """Разрешения обоих видов сохраняют требование свидетельства основания."""

        for operation in RETRIEVAL_OPERATIONS:
            for status in ("allowed", "conditional"):
                with self.subTest(operation=operation, status=status):
                    record = self._retrieval_rights(operation=operation, status=status)
                    record["rights_evidence_sha256"] = None

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("rights", record)

    def test_retrieval_conditional_permission_requires_conditions(self) -> None:
        """Условное основание нельзя записать без списка обязательных условий."""

        for operation in RETRIEVAL_OPERATIONS:
            with self.subTest(operation=operation):
                record = self._retrieval_rights(operation=operation, status="conditional")
                record["rights_conditions"] = []

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("rights", record)

    def test_retrieval_condition_confirmation_requires_evidence(self) -> None:
        """Дата выполнения условий по-прежнему требует своей контрольной суммы."""

        record = self._retrieval_rights(status="conditional")
        record["conditions_satisfied_at"] = EFFECTIVE_FROM

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("rights", record)

        record["conditions_evidence_sha256"] = EVIDENCE_SHA256
        self.catalog.validate("rights", record)

    def test_retrieval_acquisition_requires_method_and_scope(self) -> None:
        """Точное событие не отменяет указания способа и масштаба получения."""

        for field_name in ("acquisition_method", "acquisition_scope"):
            with self.subTest(field_name=field_name):
                record = {**self._retrieval_rights(), field_name: None}

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("rights", record)

    def test_retrieval_accepts_separately_confirmed_effective_date(self) -> None:
        """Поздний учёт узкого основания сохраняет отдельное подтверждение даты."""

        record = {
            **self._retrieval_rights(status="conditional"),
            "effective_from": EFFECTIVE_FROM,
            "effective_from_evidence_sha256": EVIDENCE_SHA256,
        }
        self.catalog.validate("rights", record)

        record["conditions_satisfied_at"] = EFFECTIVE_FROM
        record["conditions_evidence_sha256"] = EVIDENCE_SHA256

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("rights", record)

    def test_legacy_scopes_keep_all_previously_supported_operations(self) -> None:
        """Ограничение двух операций действует только на новый охват события."""

        for scope_type in LEGACY_SCOPES:
            for operation in (*RETRIEVAL_OPERATIONS, *CONTENT_OPERATIONS):
                with self.subTest(scope_type=scope_type, operation=operation):
                    record = {
                        **self._retrieval_rights(operation=operation),
                        "scope_type": scope_type,
                        "scope_id": "synthetic_legacy_id",
                    }
                    self.catalog.validate("rights", record)

    def test_all_legacy_examples_remain_valid(self) -> None:
        """Прежние примеры прав не требуют изменения полей или версии схемы."""

        for record in self.examples:
            with self.subTest(rights_record_id=record["rights_record_id"]):
                self.catalog.validate("rights", record)


if __name__ == "__main__":
    unittest.main()
