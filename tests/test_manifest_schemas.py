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
            "ocr_qa_candidate.example.jsonl": "ocr_qa_candidate.schema.json",
            "ocr_qa_formula.example.jsonl": "ocr_qa_formula.schema.json",
            "ocr_qa_frame.example.jsonl": "ocr_qa_frame.schema.json",
            "ocr_qa_page.example.jsonl": "ocr_qa_page.schema.json",
            "ocr_qa_run.example.json": "ocr_qa_run.schema.json",
            "ocr_qa_summary.example.json": "ocr_qa_summary.schema.json",
            "pdf_page_export.example.jsonl": "pdf_page_export.schema.json",
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

    def test_ocr_engineering_pilot_accepts_manual_challenge_only(self) -> None:
        """Инженерный пилот может состоять только из стрессовых страниц."""

        schema_path = (
            PROJECT_ROOT
            / "manifests"
            / "schemas"
            / "ocr_qa_run.schema.json"
        )
        example_path = (
            PROJECT_ROOT
            / "manifests"
            / "templates"
            / "ocr_qa_run.example.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        run = json.loads(example_path.read_text(encoding="utf-8"))
        selection_plan = run["selection_plan"]

        run["run_kind"] = "engineering_pilot"
        selection_plan["method"] = "manual_challenge_only"
        selection_plan["target_pdf_count"] = 0
        selection_plan["target_page_count"] = 0
        selection_plan["target_manual_challenge_page_count"] = 29
        selection_plan["strata"] = []

        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
        self.assertEqual(list(validator.iter_errors(run)), [])

        invalid_run = copy.deepcopy(run)
        invalid_run["selection_plan"]["target_manual_challenge_page_count"] = 0
        self.assertTrue(list(validator.iter_errors(invalid_run)))

        invalid_run = copy.deepcopy(run)
        invalid_run["selection_plan"]["target_page_count"] = 1
        self.assertTrue(list(validator.iter_errors(invalid_run)))

        invalid_run = copy.deepcopy(run)
        invalid_run["selection_plan"]["strata"] = [
            copy.deepcopy(
                json.loads(example_path.read_text(encoding="utf-8"))[
                    "selection_plan"
                ]["strata"][0]
            )
        ]
        self.assertTrue(list(validator.iter_errors(invalid_run)))

        invalid_run = copy.deepcopy(run)
        invalid_run["run_kind"] = "corpus_gate"
        self.assertTrue(list(validator.iter_errors(invalid_run)))

        invalid_run = copy.deepcopy(run)
        invalid_run["selection_plan"]["method"] = (
            "stratified_random_with_manual_challenge_pages"
        )
        self.assertTrue(list(validator.iter_errors(invalid_run)))

    def test_ocr_candidate_outcome_controls_text_and_error_fields(self) -> None:
        """Исход извлечения должен соответствовать файлу текста и ошибке."""

        schema_path = (
            PROJECT_ROOT
            / "manifests"
            / "schemas"
            / "ocr_qa_candidate.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )

        succeeded = self._example("ocr_qa_candidate")
        failed = self._example("ocr_qa_candidate", index=1)
        self.assertEqual(list(validator.iter_errors(succeeded)), [])
        self.assertEqual(list(validator.iter_errors(failed)), [])

        succeeded["error_code"] = "unexpected_error"
        self.assertTrue(list(validator.iter_errors(succeeded)))

        failed["candidate_page_text_path"] = "demo/partial.txt"
        self.assertTrue(list(validator.iter_errors(failed)))

    def test_ocr_run_candidate_manifest_fields_form_a_pair(self) -> None:
        """Паспорт OCR ссылается на кандидаты только вместе с их хешем."""

        schema_path = (
            PROJECT_ROOT
            / "manifests"
            / "schemas"
            / "ocr_qa_run.schema.json"
        )
        example_path = (
            PROJECT_ROOT
            / "manifests"
            / "templates"
            / "ocr_qa_run.example.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        run = json.loads(example_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )

        self.assertEqual(list(validator.iter_errors(run)), [])

        run_without_candidates = copy.deepcopy(run)
        del run_without_candidates["candidate_manifest_path"]
        del run_without_candidates["candidate_manifest_sha256"]
        self.assertEqual(list(validator.iter_errors(run_without_candidates)), [])

        del run["candidate_manifest_sha256"]
        self.assertTrue(list(validator.iter_errors(run)))

    def test_ocr_page_plan_schema_is_valid(self) -> None:
        """Схема плана страниц OCR должна соответствовать Draft 2020-12."""

        schema_path = (
            PROJECT_ROOT
            / "manifests"
            / "schemas"
            / "ocr_qa_page_plan.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        Draft202012Validator.check_schema(schema)

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

    def test_ocr_summary_uses_zero_f1_for_false_positive_only(self) -> None:
        """Одни ложные формулы должны иметь нулевой, а не пустой F1."""

        schema_path = (
            PROJECT_ROOT
            / "manifests"
            / "schemas"
            / "ocr_qa_summary.schema.json"
        )
        example_path = (
            PROJECT_ROOT
            / "manifests"
            / "templates"
            / "ocr_qa_summary.example.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        summary = json.loads(example_path.read_text(encoding="utf-8"))

        summary["sample_counts"]["formula_occurrences"] = 0
        summary["sample_counts"]["formula_work_ids"] = 0

        for variant_result in summary["variant_results"]:
            aggregates = [
                variant_result["overall"],
                *variant_result["source_layout_groups"],
            ]

            for aggregate in aggregates:
                aggregate.update(
                    {
                        "formula_reference_count": 0,
                        "formula_work_count": 0,
                        "formula_true_positives": 0,
                        "formula_false_negatives": 0,
                        "formula_false_positives": 1,
                        "formula_detection_f1": 0,
                        "formula_detection_f1_ci_lower": 0,
                        "critical_formula_damage_count": 0,
                        "critical_formula_damage_rate": None,
                        "critical_formula_damage_rate_ci_upper": None,
                        "formula_criteria_status": "insufficient",
                    }
                )

        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
        errors = list(validator.iter_errors(summary))
        self.assertEqual(errors, [])

        summary["variant_results"][0]["overall"]["formula_detection_f1"] = None
        errors = list(validator.iter_errors(summary))
        self.assertTrue(errors)

    def test_ocr_summary_accepts_manual_challenge_only_pilot(self) -> None:
        """Сводка ручного инженерного пилота не имитирует случайную выборку."""

        schema_path = (
            PROJECT_ROOT
            / "manifests"
            / "schemas"
            / "ocr_qa_summary.schema.json"
        )
        example_path = (
            PROJECT_ROOT
            / "manifests"
            / "templates"
            / "ocr_qa_summary.example.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        summary = json.loads(example_path.read_text(encoding="utf-8"))
        summary["sample_counts"].update(
            {
                "pdf_count": 0,
                "work_count": 0,
                "page_count": 0,
                "prose_characters": 0,
                "formula_occurrences": 0,
                "formula_work_ids": 0,
                "manual_challenge_page_count": 29,
            }
        )

        for variant_result in summary["variant_results"]:
            manual_aggregate = copy.deepcopy(
                variant_result["source_layout_groups"][0]
            )
            manual_aggregate["selection_role"] = "manual_challenge"
            variant_result.update(
                {
                    "overall": None,
                    "source_layout_groups": [],
                    "manual_challenge_results": [manual_aggregate],
                    "meets_core_criteria": False,
                    "h3_allowed": False,
                    "selected_for_adoption": False,
                }
            )

        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )
        self.assertEqual(list(validator.iter_errors(summary)), [])

        for field_name in (
            "pdf_count",
            "work_count",
            "page_count",
            "prose_characters",
            "formula_occurrences",
            "formula_work_ids",
        ):
            with self.subTest(field_name=field_name):
                invalid_summary = copy.deepcopy(summary)
                invalid_summary["sample_counts"][field_name] = 1
                self.assertTrue(list(validator.iter_errors(invalid_summary)))

        invalid_summary = copy.deepcopy(summary)
        invalid_summary["sample_counts"]["manual_challenge_page_count"] = 0
        self.assertTrue(list(validator.iter_errors(invalid_summary)))

        invalid_summary = copy.deepcopy(summary)
        invalid_summary["variant_results"][0]["manual_challenge_results"] = []
        self.assertTrue(list(validator.iter_errors(invalid_summary)))

        invalid_summary = copy.deepcopy(summary)
        invalid_summary["variant_results"][0]["overall"] = copy.deepcopy(
            summary["variant_results"][0]["manual_challenge_results"][0]
        )
        self.assertTrue(list(validator.iter_errors(invalid_summary)))

        invalid_summary = copy.deepcopy(summary)
        invalid_summary["variant_results"][0]["source_layout_groups"] = copy.deepcopy(
            summary["variant_results"][0]["manual_challenge_results"]
        )
        self.assertTrue(list(validator.iter_errors(invalid_summary)))

        for field_name in (
            "meets_core_criteria",
            "h3_allowed",
            "selected_for_adoption",
        ):
            with self.subTest(field_name=field_name):
                invalid_summary = copy.deepcopy(summary)
                invalid_summary["variant_results"][0][field_name] = True
                self.assertTrue(list(validator.iter_errors(invalid_summary)))

        invalid_summary = copy.deepcopy(summary)
        invalid_summary["decision_status"] = "pass"
        self.assertTrue(list(validator.iter_errors(invalid_summary)))

        invalid_summary = copy.deepcopy(summary)
        invalid_summary["recommendation"] = "adopt_variant"
        self.assertTrue(list(validator.iter_errors(invalid_summary)))

    def test_ocr_summary_keeps_random_sample_requirements(self) -> None:
        """Корпусная сводка не может выдать пустую случайную часть за результат."""

        schema_path = (
            PROJECT_ROOT
            / "manifests"
            / "schemas"
            / "ocr_qa_summary.schema.json"
        )
        example_path = (
            PROJECT_ROOT
            / "manifests"
            / "templates"
            / "ocr_qa_summary.example.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        summary = json.loads(example_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )

        self.assertEqual(list(validator.iter_errors(summary)), [])

        for field_name in ("pdf_count", "work_count", "prose_characters"):
            with self.subTest(field_name=field_name):
                invalid_summary = copy.deepcopy(summary)
                invalid_summary["sample_counts"][field_name] = 0
                self.assertTrue(list(validator.iter_errors(invalid_summary)))

        invalid_summary = copy.deepcopy(summary)
        invalid_summary["variant_results"][0]["overall"] = None
        self.assertTrue(list(validator.iter_errors(invalid_summary)))

        invalid_summary = copy.deepcopy(summary)
        invalid_summary["variant_results"][0]["source_layout_groups"] = []
        self.assertTrue(list(validator.iter_errors(invalid_summary)))

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

    def test_eligible_work_accepts_dates_outside_previous_period(self) -> None:
        """Схема не должна ограничивать работу прежним диапазоном годов."""

        record = self._example("works")
        record["eligibility_status"] = "eligible"

        for published_at in ("1918-01-01", "2026-09-02"):
            with self.subTest(published_at=published_at):
                record["published_at"] = published_at
                self.catalog.validate("works", record)

    def test_eligible_work_requires_valid_publication_date(self) -> None:
        """Допущенная работа должна иметь корректную календарную дату."""

        for published_at in (None, "2024-02-30"):
            with self.subTest(published_at=published_at):
                record = self._example("works")
                record["eligibility_status"] = "eligible"
                record["published_at"] = published_at

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
