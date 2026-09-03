"""Модульные тесты связей и арифметики OCR QA."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest

from pathlib import Path
from typing import Any

from src.corpus.ocr_qa_validation import OcrQaValidationReport, OcrQaValidator
from src.corpus.ocr_qa_metrics import derive_aggregate

ROOT = Path(__file__).resolve().parents[1]


class OcrQaValidatorTests(unittest.TestCase):
    """Проверки целостного синтетического запуска OCR QA."""

    def setUp(self) -> None:
        """Создать изолированный каталог для машинных форм."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary_directory.name)
        (self.project_root / "manifests").mkdir()
        self.records = self._base_records()

    def tearDown(self) -> None:
        """Удалить временный каталог после теста."""

        self.temporary_directory.cleanup()

    def test_coherent_synthetic_run_passes_without_file_checks(self) -> None:
        """Согласованные формы должны проходить семантическую проверку."""

        report = self._validate()
        self.assertTrue(report.ok, "\n".join(report.errors))
        self.assertEqual(report.counts["active_approved_pages"], 2)
        self.assertEqual(report.counts["active_approved_formulas"], 3)
        self.assertTrue(any("явно отключена" in item for item in report.warnings))

    def test_recalculates_page_and_aggregate_cer(self) -> None:
        """Изменённый CER должен расходиться со счётчиками."""

        self.records["pages"][1]["cer"] = 0.5
        self.records["summary"]["variant_results"][1]["overall"]["cer"] = 0.5
        report = self._validate()
        self.assertFalse(report.ok)
        self.assertTrue(any("cer" in error and "0.01" in error for error in report.errors))

    def test_rejects_page_that_changes_frame_identity(self) -> None:
        """Страница не должна менять work_id из зафиксированной выборки."""

        self.records["pages"][0]["work_id"] = "other-work"
        report = self._validate()
        self.assertTrue(
            any("work_id не совпадает" in error for error in report.errors)
        )

    def test_rejects_invalid_span_and_iou(self) -> None:
        """Текстовый интервал должен быть непустым и согласованным с IoU."""

        formula = self.records["formulas"][0]
        formula["prediction_span"] = {"start": 121, "end": 121}
        report = self._validate()
        self.assertTrue(any("start должен" in error for error in report.errors))
        self.assertTrue(any("intersection_over_union" in error for error in report.errors))

    def test_only_current_approved_revision_contributes_to_metrics(self) -> None:
        """Заменённая ревизия не должна попадать в метрики."""

        current = self.records["pages"][1]
        previous = copy.deepcopy(current)
        previous["page_qa_record_id"] = "page_qa_previous"
        previous["cer"] = 0.9
        previous["completed_at"] = "2026-09-02T11:00:00+03:00"
        current["supersedes_page_qa_record_id"] = "page_qa_previous"
        self.records["pages"].append(previous)
        report = self._validate()
        self.assertTrue(report.ok, "\n".join(report.errors))
        self.assertEqual(report.counts["active_approved_pages"], 2)

    def test_current_unapproved_revision_is_not_accepted(self) -> None:
        """Актуальная неодобренная запись не может заменять результат."""

        self.records["pages"][0]["review_status"] = "pending"
        report = self._validate()
        self.assertTrue(any("не одобрен" in error for error in report.errors))
        self.assertTrue(any("Нет актуального" in error for error in report.errors))

    def test_rejects_selected_variant_without_confirming_decision(self) -> None:
        """Вариант нельзя выбирать без подтверждающего решения."""

        self.records["summary"]["variant_results"][1][
            "selected_for_adoption"
        ] = True
        report = self._validate()
        self.assertFalse(report.ok)
        self.assertTrue(any("selected_for_adoption" in error for error in report.errors))

    def test_file_checks_are_enabled_by_default(self) -> None:
        """Обычный режим должен отклонять отсутствующие ссылочные файлы."""

        report = self._validate(check_files=True)
        self.assertTrue(any("Ссылочный файл отсутствует" in error for error in report.errors))

    def test_recalculates_bootstrap_confidence_bound(self) -> None:
        """Подменённая bootstrap-граница должна быть обнаружена."""

        self.records["summary"]["variant_results"][0]["overall"][
            "cer_ci_upper"
        ] = 0.2
        report = self._validate()
        self.assertTrue(
            any("cer_ci_upper=0.2" in error and "0.02" in error for error in report.errors)
        )

    def test_manual_challenge_pdf_does_not_change_random_pdf_quota(self) -> None:
        """Дополнительный сложный PDF не должен входить в target_pdf_count."""

        self._add_manual_challenge_page()
        report = self._validate()
        self.assertTrue(report.ok, "\n".join(report.errors))

    def test_schema_error_does_not_crash_semantic_validation(self) -> None:
        """Неверный тип в паспорте должен давать отчёт, а не traceback."""

        self.records["run"]["selection_plan"] = []
        report = self._validate()
        self.assertFalse(report.ok)
        self.assertTrue(any("selection_plan" in error for error in report.errors))

    def test_formula_revision_cannot_change_occurrence_ids(self) -> None:
        """Ревизия формулы не должна подменять эталонное вхождение."""

        current = self.records["formulas"][0]
        previous = copy.deepcopy(current)
        previous["formula_qa_record_id"] = "formula_qa_previous"
        previous["completed_at"] = "2026-09-02T12:30:00+03:00"
        current["supersedes_formula_qa_record_id"] = "formula_qa_previous"
        current["reference_formula_id"] = "other-reference"
        self.records["formulas"].append(previous)
        report = self._validate()
        self.assertTrue(
            any("reference_formula_id" in error and "меняет" in error for error in report.errors)
        )

    def test_f1_is_zero_for_false_positive_only(self) -> None:
        """Одни ложные срабатывания должны давать нулевой F1."""

        false_positive = copy.deepcopy(self.records["formulas"][2])
        aggregate = derive_aggregate([self.records["pages"][0]], [false_positive])
        self.assertEqual(aggregate["formula_false_positives"], 1)
        self.assertEqual(aggregate["formula_detection_f1"], 0.0)

    def test_f1_is_undefined_without_reference_or_prediction(self) -> None:
        """Без эталонных и предсказанных формул F1 не определён."""

        aggregate = derive_aggregate([self.records["pages"][0]], [])
        self.assertIsNone(aggregate["formula_detection_f1"])

    def test_source_artifact_manifest_is_schema_validated(self) -> None:
        """Внешний реестр артефактов нельзя использовать без проверки схемы."""

        path = self.project_root / "manifests" / "source_artifacts.jsonl"
        data = b'{"artifact_id":"broken"}\n'
        path.write_bytes(data)
        self.records["run"]["source_artifacts_manifest_path"] = (
            "manifests/source_artifacts.jsonl"
        )
        self.records["run"]["source_artifacts_manifest_sha256"] = hashlib.sha256(
            data
        ).hexdigest()
        report = self._validate(check_files=True)
        self.assertTrue(
            any("ошибка схемы artifacts" in error for error in report.errors)
        )

    def test_repeated_validation_does_not_reuse_stale_file_hash(self) -> None:
        """Один валидатор должен заметить изменение файла между запусками."""

        paths = self._write_records()
        run_sha256 = hashlib.sha256(paths["run"].read_bytes()).hexdigest()
        self.records["summary"]["run_manifest_sha256"] = run_sha256
        paths = self._write_records()
        validator = OcrQaValidator(
            self.project_root,
            schema_dir=ROOT / "manifests" / "schemas",
            check_files=True,
        )
        first_report = validator.validate(
            run_path=paths["run"],
            summary_path=paths["summary"],
        )

        self.assertFalse(
            any(
                "Не совпадает SHA-256: паспорт запуска" in error
                for error in first_report.errors
            )
        )

        changed_run = copy.deepcopy(self.records["run"])
        changed_run["created_at"] = "2026-09-01T13:00:00+03:00"
        paths["run"].write_text(
            json.dumps(changed_run, ensure_ascii=False),
            encoding="utf-8",
        )
        second_report = validator.validate(
            run_path=paths["run"],
            summary_path=paths["summary"],
        )

        self.assertTrue(
            any(
                "Не совпадает SHA-256: паспорт запуска" in error
                for error in second_report.errors
            ),
            "\n".join(second_report.errors),
        )

    def _validate(self, *, check_files: bool = False) -> OcrQaValidationReport:
        """Записать текущие формы и запустить валидатор."""

        paths = self._write_records()
        validator = OcrQaValidator(
            self.project_root,
            schema_dir=ROOT / "manifests" / "schemas",
            check_files=check_files,
        )

        return validator.validate(
            run_path=paths["run"],
            summary_path=paths["summary"],
        )

    def _write_records(self) -> dict[str, Path]:
        """Сериализовать формы во временный каталог."""

        paths = {
            "run": self.project_root / "manifests" / "run.json",
            "frame": self.project_root / "manifests" / "frame.jsonl",
            "pages": self.project_root / "manifests" / "pages.jsonl",
            "formulas": self.project_root / "manifests" / "formulas.jsonl",
            "summary": self.project_root / "manifests" / "summary.json",
        }
        paths["run"].write_text(
            json.dumps(self.records["run"], ensure_ascii=False),
            encoding="utf-8",
        )
        paths["summary"].write_text(
            json.dumps(self.records["summary"], ensure_ascii=False),
            encoding="utf-8",
        )

        for kind in ("frame", "pages", "formulas"):
            paths[kind].write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False) + "\n"
                    for record in self.records[kind]
                ),
                encoding="utf-8",
            )

        return paths

    def _base_records(self) -> dict[str, Any]:
        """Построить минимальный согласованный набор пяти форм."""

        run = json.loads(
            (ROOT / "manifests" / "templates" / "ocr_qa_run.example.json").read_text(
                encoding="utf-8"
            )
        )
        frame = self._read_example_jsonl("ocr_qa_frame.example.jsonl")
        pages = self._read_example_jsonl("ocr_qa_page.example.jsonl")
        formulas = self._read_example_jsonl("ocr_qa_formula.example.jsonl")
        run["run_kind"] = "engineering_pilot"
        run["sample_frame_path"] = "manifests/frame.jsonl"
        run["selection_plan"]["target_pdf_count"] = 1
        run["selection_plan"]["target_page_count"] = 1
        run["selection_plan"]["minimum_prose_characters"] = 1000
        run["selection_plan"]["strata"][0]["target_pages"] = 1
        run["selection_plan"]["strata"][0]["minimum_prose_characters"] = 1000
        summary = self._summary(run, pages, formulas)

        return {
            "run": run,
            "frame": frame,
            "pages": pages,
            "formulas": formulas,
            "summary": summary,
        }

    def _add_manual_challenge_page(self) -> None:
        """Добавить сложную страницу из PDF вне случайной части выборки."""

        frame = copy.deepcopy(self.records["frame"][0])
        frame.update(
            {
                "frame_record_id": "ocr_frame_manual_0001",
                "page_sample_id": "page_sample_manual_0001",
                "work_id": "work_demo_manual_0001",
                "source_pdf_artifact_id": f"sha256:{'d' * 64}",
                "source_pdf_path": "data/raw/demo/manual_challenge.pdf",
                "page_index": 0,
                "page_number": 1,
                "page_render_path": (
                    "data/qa/ocr/ocr_qa_demo_v1/renders/"
                    "page_sample_manual_0001.png"
                ),
                "page_render_sha256": "e" * 64,
                "selection_reason": "Дополнительная сложная страница.",
                "selection_role": "manual_challenge",
            }
        )
        self.records["frame"].append(frame)
        self.records["run"]["selection_plan"][
            "target_manual_challenge_page_count"
        ] = 1
        self.records["summary"]["sample_counts"][
            "manual_challenge_page_count"
        ] = 1
        new_pages: list[dict[str, Any]] = []

        for page_index, original_page in enumerate(self.records["pages"]):
            page = copy.deepcopy(original_page)
            page.update(
                {
                    "page_qa_record_id": (
                        f"{original_page['page_qa_record_id']}_manual"
                    ),
                    "page_sample_id": frame["page_sample_id"],
                    "work_id": frame["work_id"],
                    "source_pdf_artifact_id": frame[
                        "source_pdf_artifact_id"
                    ],
                    "candidate_artifact_id": (
                        f"sha256:{str(page_index + 1) * 64}"
                    ),
                    "page_index": frame["page_index"],
                    "page_number": frame["page_number"],
                    "page_render_path": frame["page_render_path"],
                    "page_render_sha256": frame["page_render_sha256"],
                    "selection_role": "manual_challenge",
                    "formula_annotation_status": "not_selected",
                    "formula_reference_count": 0,
                    "formula_record_count": 0,
                }
            )
            new_pages.append(page)
            aggregate = self._aggregate(page, [])
            aggregate.update(
                {
                    "group_id": "manual_challenge_extra_pdf",
                    "source_id": page["source_id"],
                    "layout": page["layout"],
                }
            )
            variant_result = next(
                result
                for result in self.records["summary"]["variant_results"]
                if result["variant_id"] == page["variant_id"]
            )
            variant_result["manual_challenge_results"] = [aggregate]

        self.records["pages"].extend(new_pages)

    def _summary(
        self,
        run: dict[str, Any],
        pages: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Построить сводку, арифметически согласованную с двумя вариантами."""

        variant_results: list[dict[str, Any]] = []

        for variant in run["variants"]:
            variant_id = variant["variant_id"]
            page = next(item for item in pages if item["variant_id"] == variant_id)
            variant_formulas = [
                item for item in formulas if item["variant_id"] == variant_id
            ]
            aggregate = self._aggregate(page, variant_formulas)
            group = copy.deepcopy(aggregate)
            group.update(
                {
                    "group_id": "source_demo_journal_site__two_column",
                    "source_id": "source_demo_journal_site",
                    "layout": "two_column",
                }
            )
            variant_results.append(
                {
                    "variant_id": variant_id,
                    "overall": aggregate,
                    "source_layout_groups": [group],
                    "manual_challenge_results": [],
                    "meets_core_criteria": False,
                    "h3_allowed": False,
                    "selected_for_adoption": False,
                }
            )

        return {
            "schema_version": "ocr-qa-summary-v1",
            "summary_version": "test-v1",
            "qa_run_id": run["qa_run_id"],
            "run_manifest_path": "manifests/run.json",
            "run_manifest_sha256": "a" * 64,
            "page_results_path": "manifests/pages.jsonl",
            "page_results_sha256": "b" * 64,
            "formula_results_path": "manifests/formulas.jsonl",
            "formula_results_sha256": "c" * 64,
            "sample_counts": {
                "selection_role": "stratified_random",
                "pdf_count": 1,
                "work_count": 1,
                "page_count": 1,
                "prose_characters": 1000,
                "formula_occurrences": 1,
                "formula_work_ids": 1,
                "manual_challenge_page_count": 0,
            },
            "variant_results": variant_results,
            "decision_status": "insufficient",
            "recommendation": "revise_and_repeat",
            "decision_reason": "Синтетическая выборка мала для допуска.",
            "unresolved_issues": ["Нужно увеличить выборку."],
            "format_and_relation_checks": "not_run",
            "created_at": "2026-09-03T12:00:00+03:00",
        }

    def _aggregate(
        self,
        page: dict[str, Any],
        formulas: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Рассчитать одностраничный агрегат для теста."""

        character_errors = sum(
            page[field]
            for field in (
                "character_substitutions",
                "character_deletions",
                "character_insertions",
            )
        )
        word_errors = sum(
            page[field]
            for field in (
                "word_substitutions",
                "word_deletions",
                "word_insertions",
            )
        )
        true_positives = sum(
            item["match_status"] == "true_positive" for item in formulas
        )
        false_negatives = sum(
            item["match_status"] == "false_negative" for item in formulas
        )
        false_positives = sum(
            item["match_status"] == "false_positive" for item in formulas
        )
        reference_count = true_positives + false_negatives
        critical_count = sum(
            item["critical_damage"]
            for item in formulas
            if item["match_status"] != "false_positive"
        )
        f1_denominator = 2 * true_positives + false_negatives + false_positives
        f1 = (
            2 * true_positives / f1_denominator
            if f1_denominator
            else None
        )
        damage_rate = (
            critical_count / reference_count
            if reference_count
            else None
        )

        return {
            "selection_role": page["selection_role"],
            "work_count": 1,
            "page_count": 1,
            "attempted_pages": 1,
            "successful_extraction_pages": 1,
            "extraction_success_rate": 1.0,
            "prose_characters": page["reference_characters"],
            "character_errors": character_errors,
            "cer": page["cer"],
            "cer_ci_upper": page["cer"],
            "reference_words": page["reference_words"],
            "word_errors": word_errors,
            "wer": page["wer"],
            "wer_ci_upper": page["wer"],
            "critical_reading_order_pages": 0,
            "critical_reading_order_rate": 0.0,
            "critical_reading_order_rate_ci_upper": 0.0,
            "formula_reference_count": reference_count,
            "formula_work_count": 1 if reference_count else 0,
            "formula_true_positives": true_positives,
            "formula_false_negatives": false_negatives,
            "formula_false_positives": false_positives,
            "formula_detection_f1": f1,
            "formula_detection_f1_ci_lower": f1,
            "critical_formula_damage_count": critical_count,
            "critical_formula_damage_rate": damage_rate,
            "critical_formula_damage_rate_ci_upper": damage_rate,
            "prose_criteria_status": "insufficient",
            "formula_criteria_status": "insufficient",
        }

    def _read_example_jsonl(self, file_name: str) -> list[dict[str, Any]]:
        """Прочитать синтетические JSONL-примеры из проекта."""

        path = ROOT / "manifests" / "templates" / file_name

        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
