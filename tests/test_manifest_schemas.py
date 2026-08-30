"""Проверки схем JSON и примеров реестров корпуса."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ManifestSchemaTests(unittest.TestCase):
    """Проверки корректности схем и ограничений записей реестров."""

    catalog: SchemaCatalog

    @classmethod
    def setUpClass(cls) -> None:
        """Подготовить общий каталог схем для набора тестов."""

        cls.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")

    def _example(self, kind: str, *, index: int = 0) -> dict[str, Any]:
        """Загрузить примерную запись указанного вида по её номеру."""

        path = PROJECT_ROOT / "manifests" / "templates" / f"{kind}.example.jsonl"
        lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

        return json.loads(lines[index])

    def test_core_schemas_and_all_core_examples(self) -> None:
        """Основные примеры должны соответствовать своим схемам."""

        for kind in ("works", "artifacts", "rights"):
            self.catalog.validator(kind)
            path = PROJECT_ROOT / "manifests" / "templates" / f"{kind}.example.jsonl"
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.catalog.validate(kind, json.loads(line))

    def test_every_json_schema_and_matching_example(self) -> None:
        """Все схемы и связанные с ними примеры должны быть корректными."""

        mappings = {
            "artifacts.example.jsonl": "artifacts.schema.json",
            "artifact_revisions.example.jsonl": (
                "artifact_revisions.schema.json"
            ),
            "condition_fulfilments.example.jsonl": (
                "condition_fulfilments.schema.json"
            ),
            "frozen_manifest.example.json": "frozen_manifest.schema.json",
            "identity_conflicts.example.jsonl": "identity_conflicts.schema.json",
            "operation_decisions.example.jsonl": "operation_decisions.schema.json",
            "retrieval_events.example.jsonl": "retrieval_events.schema.json",
            "rights.example.jsonl": "rights.schema.json",
            "work_aliases.example.jsonl": "work_aliases.schema.json",
            "work_revisions.example.jsonl": "work_revisions.schema.json",
            "works.example.jsonl": "works.schema.json",
            "h2_adjudication_form.example.jsonl": "h2_adjudication_form.schema.json",
            "h2_annotation_form.example.jsonl": "h2_annotation_form.schema.json",
            "h2_audit_frame.example.jsonl": "h2_audit_frame.schema.json",
            "h2_labels.example.jsonl": "h2_labels.schema.json",
            "h2_queries.example.jsonl": "h2_queries.schema.json",
            "h2_audit_run.example.json": "h2_audit_run.schema.json",
            "h2_calibration_run.example.json": "h2_audit_run.schema.json",
            "h2_calibration_plan.example.json": "h2_calibration_plan.schema.json",
            "h2_calibration_summary.example.json": "h2_calibration_summary.schema.json",
            "h2_grnti_excerpt.example.json": "h2_grnti_excerpt.schema.json",
        }
        schema_dir = PROJECT_ROOT / "manifests" / "schemas"
        template_dir = PROJECT_ROOT / "manifests" / "templates"
        for schema_path in schema_dir.glob("*.schema.json"):
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)

        for template_name, schema_name in mappings.items():
            schema = json.loads((schema_dir / schema_name).read_text(encoding="utf-8"))
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            path = template_dir / template_name
            if path.suffix == ".jsonl":
                lines = path.read_text(encoding="utf-8").splitlines()
                records = [json.loads(line) for line in lines if line]
            else:
                records = [json.loads(path.read_text(encoding="utf-8"))]
            for record in records:
                errors = sorted(
                    validator.iter_errors(record),
                    key=lambda item: str(item.path),
                )
                self.assertEqual(errors, [], msg=f"{template_name}: {errors}")

    def test_dec013_examples_are_available_through_catalog(self) -> None:
        """Каталог должен проверять все новые журналы жизненного цикла."""

        kinds = (
            "work_revisions",
            "artifact_revisions",
            "retrieval_events",
            "work_aliases",
            "identity_conflicts",
            "operation_decisions",
            "condition_fulfilments",
        )

        for kind in kinds:
            path = (
                PROJECT_ROOT
                / "manifests"
                / "templates"
                / f"{kind}.example.jsonl"
            )

            for line in path.read_text(encoding="utf-8").splitlines():
                if line:
                    self.catalog.validate(kind, json.loads(line))

        frozen_path = (
            PROJECT_ROOT / "manifests" / "templates" / "frozen_manifest.example.json"
        )

        self.catalog.validate(
            "frozen_manifest",
            json.loads(frozen_path.read_text(encoding="utf-8")),
        )

    def test_metadata_only_retrieval_cannot_invent_http_response(self) -> None:
        """Событие без снимка ответа не должно содержать фиктивные HTTP-поля."""

        record = self._example("retrieval_events", index=1)
        self.catalog.validate("retrieval_events", record)

        record["http_status"] = 200

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("retrieval_events", record)

    def test_derivatives_release_requires_one_derivative_scope(self) -> None:
        """Решение о выпуске должно относиться к одному виду производного объекта."""

        record = self._example("operation_decisions")
        record["operation"] = "derivatives_release"

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("operation_decisions", record)

    def test_pending_identity_conflict_has_no_resolution(self) -> None:
        """Неразрешённый конфликт не должен выглядеть как принятое решение."""

        record = self._example("identity_conflicts")
        record["resolution_reason"] = "Оставлен текущий заголовок."

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("identity_conflicts", record)

    def test_revoked_condition_fulfilment_requires_previous_record(self) -> None:
        """Отзыв выполнения условия должен явно заменять прежнюю запись."""

        record = self._example("condition_fulfilments")
        record["status"] = "revoked"

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("condition_fulfilments", record)

    def test_eligible_work_requires_abstract(self) -> None:
        """Допущенная к использованию работа должна иметь аннотацию."""

        record = self._example("works")
        record["eligibility_status"] = "eligible"
        record["abstract"] = None
        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("works", record)

    def test_retrieved_artifact_requires_hash(self) -> None:
        """Полученный артефакт должен иметь идентификатор и SHA-256."""

        record = self._example("artifacts")
        record["artifact_id"] = None
        record["sha256"] = None
        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("artifacts", record)

    def test_conditional_right_requires_conditions(self) -> None:
        """Условное право должно содержать перечень условий."""

        record = self._example("rights")
        record["status"] = "conditional"
        record["rights_conditions"] = []
        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("rights", record)

    def test_permitting_right_requires_basis_evidence(self) -> None:
        """Разрешающее право должно ссылаться на подтверждение основания."""

        record = self._example("rights")
        record["rights_evidence_sha256"] = None
        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("rights", record)

    def test_format_checker_rejects_impossible_date(self) -> None:
        """Проверка формата должна отклонять невозможную календарную дату."""

        record = copy.deepcopy(self._example("rights"))
        record["rights_checked_at"] = "2026-99-99"
        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("rights", record)


if __name__ == "__main__":
    unittest.main()
