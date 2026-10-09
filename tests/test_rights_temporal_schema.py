"""Проверки совместимых схем для позднего учёта оснований и решений по операциям."""

from __future__ import annotations

import json
import unittest

from pathlib import Path
from typing import Any

from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EFFECTIVE_FROM = "2026-08-18T10:00:00+03:00"
EVIDENCE_SHA256 = "a" * 64


class RightsTemporalSchemaTests(unittest.TestCase):
    """Проверки дат действия, подтверждающих свидетельств и обратной совместимости."""

    def setUp(self) -> None:
        """Загрузить действующие схемы и прежние синтетические примеры записей."""

        self.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")
        self.rights = self._template_record("rights")
        self.decision = self._template_record("operation_decisions")

    def _template_record(self, kind: str) -> dict[str, Any]:
        """Прочитать первую запись из неизменённого шаблона выбранного реестра."""

        template_path = (
            PROJECT_ROOT / "manifests" / "templates" / f"{kind}.example.jsonl"
        )

        return json.loads(template_path.read_text(encoding="utf-8").splitlines()[0])

    def _dated_rights(self) -> dict[str, Any]:
        """Дополнить прежнюю запись отдельно подтверждённой датой действия основания."""

        return {
            **self.rights,
            "effective_from": EFFECTIVE_FROM,
            "effective_from_evidence_sha256": EVIDENCE_SHA256,
        }

    def test_legacy_records_remain_valid_without_new_fields(self) -> None:
        """Прежние права и решения принимаются без миграции полей времени."""

        self.assertNotIn("effective_from", self.rights)
        self.assertNotIn("effective_from_evidence_sha256", self.rights)
        self.assertNotIn("knowledge_cutoff_at", self.decision)

        self.catalog.validate("rights", self.rights)
        self.catalog.validate("operation_decisions", self.decision)

    def test_effective_date_accepts_its_separate_evidence(self) -> None:
        """Дата действия и контрольная сумма подтверждения образуют допустимую пару."""

        self.catalog.validate("rights", self._dated_rights())

    def test_effective_date_and_evidence_require_each_other(self) -> None:
        """Нельзя хранить отдельно дату действия или свидетельство её начала."""

        for missing_field in ("effective_from", "effective_from_evidence_sha256"):
            with self.subTest(missing_field=missing_field):
                record = self._dated_rights()
                del record[missing_field]

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("rights", record)

    def test_effective_date_requires_rights_evidence_for_unknown_status(self) -> None:
        """Даже неопределённое основание с датой действия требует своего свидетельства."""

        record = {
            **self._dated_rights(),
            "status": "unknown",
            "basis_type": "unknown",
            "rights_evidence_sha256": None,
        }

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("rights", record)

        record["rights_evidence_sha256"] = EVIDENCE_SHA256
        self.catalog.validate("rights", record)

    def test_effective_date_rejects_invalid_timestamps(self) -> None:
        """Дата без времени, неверный день и значения другого типа отклоняются."""

        for timestamp in ("2026-08-18", "2026-02-30T10:00:00Z", "", None, 42):
            with self.subTest(timestamp=timestamp):
                record = {**self._dated_rights(), "effective_from": timestamp}

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("rights", record)

    def test_effective_date_rejects_invalid_evidence_hashes(self) -> None:
        """Свидетельство даты требует полной SHA-256 в нижнем регистре."""

        for digest in ("", "a" * 63, "A" * 64, "g" * 64, None, 42):
            with self.subTest(digest=digest):
                record = {
                    **self._dated_rights(),
                    "effective_from_evidence_sha256": digest,
                }

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("rights", record)

    def test_effective_date_forbids_materialized_condition_confirmation(self) -> None:
        """Поздний учёт использует отдельный журнал выполнения условий, а не старые поля."""

        conditional_record = {
            **self._dated_rights(),
            "status": "conditional",
            "rights_conditions": ["Закрытое хранение материалов"],
        }
        self.catalog.validate("rights", conditional_record)

        invalid_confirmations = (
            {"conditions_satisfied_at": EFFECTIVE_FROM},
            {"conditions_evidence_sha256": EVIDENCE_SHA256},
            {
                "conditions_satisfied_at": EFFECTIVE_FROM,
                "conditions_evidence_sha256": EVIDENCE_SHA256,
            },
        )

        for confirmation in invalid_confirmations:
            with (
                self.subTest(confirmation=confirmation),
                self.assertRaises(SchemaValidationError),
            ):
                self.catalog.validate("rights", {**conditional_record, **confirmation})

    def test_legacy_materialized_condition_confirmation_remains_valid(self) -> None:
        """Прежняя фиксация выполнения условий допустима без новой даты действия."""

        record = {
            **self.rights,
            "status": "conditional",
            "rights_conditions": ["Закрытое хранение материалов"],
            "conditions_satisfied_at": EFFECTIVE_FROM,
            "conditions_evidence_sha256": EVIDENCE_SHA256,
        }
        self.catalog.validate("rights", record)

    def test_decision_accepts_optional_knowledge_cutoff(self) -> None:
        """Решение может отдельно указывать момент доступности учтённых свидетельств."""

        record = {
            **self.decision,
            "knowledge_cutoff_at": "2026-08-30T12:06:00+03:00",
        }
        self.catalog.validate("operation_decisions", record)

    def test_decision_rejects_invalid_cutoff_and_unknown_fields(self) -> None:
        """Момент учёта должен быть временем с зоной, а лишние поля запрещены."""

        for timestamp in ("2026-08-30", "2026-08-30T12:06:00", "", None, 42):
            with self.subTest(timestamp=timestamp):
                record = {**self.decision, "knowledge_cutoff_at": timestamp}

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("operation_decisions", record)

        records = (
            ("rights", self._dated_rights()),
            ("operation_decisions", self.decision),
        )

        for kind, record in records:
            with self.subTest(kind=kind), self.assertRaises(SchemaValidationError):
                self.catalog.validate(kind, {**record, "unknown_temporal_field": True})


if __name__ == "__main__":
    unittest.main()
