"""Семантическая проверка связей и арифметики контроля качества OCR."""

from __future__ import annotations

import math

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .ocr_qa_io import (
    _OcrQaBundle,
    _OcrQaIoMixin,
)
from .ocr_qa_summary_validation import _OcrQaSummaryMixin

FLOAT_ABSOLUTE_TOLERANCE = 1e-9
PAGE_FRAME_FIELDS = (
    "qa_run_id",
    "page_sample_id",
    "work_id",
    "source_id",
    "source_group_id",
    "source_pdf_artifact_id",
    "publication_year",
    "page_index",
    "page_number",
    "printed_page_label",
    "page_render_path",
    "page_render_sha256",
    "page_render_dpi",
    "layout",
    "scan_quality",
    "selection_tags",
    "selection_role",
)
FORMULA_PAGE_FIELDS = (
    "qa_run_id",
    "page_sample_id",
    "variant_id",
    "work_id",
    "source_id",
    "source_group_id",
    "source_pdf_artifact_id",
    "publication_year",
    "selection_role",
    "page_index",
    "page_number",
    "page_render_path",
    "page_render_sha256",
    "page_render_dpi",
    "candidate_page_text_path",
    "candidate_page_text_sha256",
)


@dataclass(frozen=True, slots=True)
class OcrQaValidationReport:
    """Результат полной проверки OCR QA."""

    counts: dict[str, int]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Показать, завершилась ли проверка без ошибок."""

        return not self.errors


class OcrQaValidator(_OcrQaIoMixin, _OcrQaSummaryMixin):
    """Проверка схем, связей, файлов и метрик запуска OCR QA."""

    def __init__(
        self,
        project_root: Path,
        *,
        schema_dir: Path | None = None,
        check_files: bool = True,
    ) -> None:
        """Создать валидатор с корнем в каталоге проекта."""

        self.project_root = Path(project_root).resolve()
        self.schema_dir = (
            Path(schema_dir).resolve()
            if schema_dir is not None
            else self.project_root / "manifests" / "schemas"
        )
        self.check_files = check_files
        self._file_hashes: dict[Path, str] = {}

    def validate(
        self,
        *,
        run_path: Path,
        summary_path: Path,
        frame_path: Path | None = None,
        page_results_path: Path | None = None,
        formula_results_path: Path | None = None,
    ) -> OcrQaValidationReport:
        """Проверить один полный запуск и вернуть все найденные проблемы."""

        self._file_hashes.clear()
        errors: list[str] = []
        warnings: list[str] = []

        bundle = self._load_bundle(
            run_path=run_path,
            summary_path=summary_path,
            frame_path=frame_path,
            page_results_path=page_results_path,
            formula_results_path=formula_results_path,
            errors=errors,
        )

        if bundle is None:
            return OcrQaValidationReport({}, tuple(errors), tuple(warnings))

        schema_error_count = len(errors)
        self._validate_schemas(bundle, errors)

        if len(errors) > schema_error_count:
            counts = {
                "frame_pages": len(bundle.frame),
                "page_records": len(bundle.pages),
                "formula_records": len(bundle.formulas),
                "variants": len(bundle.run.get("variants", []))
                if isinstance(bundle.run.get("variants"), list)
                else 0,
            }

            return OcrQaValidationReport(counts, tuple(errors), tuple(warnings))

        active_pages = self._active_records(
            bundle.pages,
            id_field="page_qa_record_id",
            supersedes_field="supersedes_page_qa_record_id",
            identity_fields=("qa_run_id", "page_sample_id", "variant_id"),
            label="результатах страниц",
            errors=errors,
        )
        active_formulas = self._active_records(
            bundle.formulas,
            id_field="formula_qa_record_id",
            supersedes_field="supersedes_formula_qa_record_id",
            identity_fields=(
                "qa_run_id",
                "page_sample_id",
                "variant_id",
                "reference_formula_id",
                "prediction_formula_id",
            ),
            label="результатах формул",
            errors=errors,
        )
        approved_pages = [
            record
            for record in active_pages
            if record.get("review_status") == "approved"
        ]
        approved_formulas = [
            record
            for record in active_formulas
            if record.get("review_status") == "approved"
        ]

        self._validate_run_and_frame(bundle, errors)
        self._validate_page_relations(
            bundle,
            active_pages,
            approved_pages,
            errors,
        )
        self._validate_formula_relations(
            bundle,
            active_formulas,
            approved_formulas,
            approved_pages,
            errors,
        )
        self._validate_geometry_and_metrics(
            approved_pages,
            approved_formulas,
            errors,
        )
        self._validate_summary(
            bundle,
            approved_pages,
            approved_formulas,
            errors,
            warnings,
        )

        if self.check_files:
            self._validate_files(bundle, approved_pages, approved_formulas, errors)
        else:
            warnings.append(
                "Проверка наличия и SHA-256 ссылочных файлов "
                "явно отключена."
            )

        counts = {
            "frame_pages": len(bundle.frame),
            "page_records": len(bundle.pages),
            "active_approved_pages": len(approved_pages),
            "formula_records": len(bundle.formulas),
            "active_approved_formulas": len(approved_formulas),
            "variants": len(bundle.run.get("variants", [])),
        }

        return OcrQaValidationReport(counts, tuple(errors), tuple(warnings))

    def _active_records(
        self,
        records: list[dict[str, Any]],
        *,
        id_field: str,
        supersedes_field: str,
        identity_fields: tuple[str, ...],
        label: str,
        errors: list[str],
    ) -> list[dict[str, Any]]:
        """Вернуть незаменённые записи после проверки цепочек ревизий."""

        records_by_id: dict[str, dict[str, Any]] = {}

        for record in records:
            record_id = record.get(id_field)

            if not isinstance(record_id, str):
                continue

            if record_id in records_by_id:
                errors.append(f"Повтор {id_field}={record_id!r} в {label}.")
                continue

            records_by_id[record_id] = record

        successor_counts: Counter[str] = Counter()

        for record_id, record in records_by_id.items():
            predecessor_id = record.get(supersedes_field)

            if predecessor_id is None:
                continue

            if predecessor_id == record_id:
                errors.append(f"Запись {record_id!r} не может заменять саму себя.")
                continue

            predecessor = records_by_id.get(predecessor_id)

            if predecessor is None:
                errors.append(
                    f"Запись {record_id!r} ссылается на неизвестную заменённую запись "
                    f"{predecessor_id!r}."
                )
                continue

            successor_counts[predecessor_id] += 1

            for field_name in identity_fields:
                if record.get(field_name) != predecessor.get(field_name):
                    errors.append(
                        f"Запись {record_id!r} меняет {field_name} при замене "
                        f"{predecessor_id!r}."
                    )

            current_time = _parse_datetime(record.get("completed_at"))
            predecessor_time = _parse_datetime(predecessor.get("completed_at"))

            if (
                current_time is not None
                and predecessor_time is not None
                and current_time <= predecessor_time
            ):
                errors.append(
                    f"Запись {record_id!r} должна быть новее {predecessor_id!r}."
                )

        for predecessor_id, count in successor_counts.items():
            if count > 1:
                errors.append(
                    f"У записи {predecessor_id!r} найдено прямых преемников: {count}."
                )

        self._check_revision_cycles(
            records_by_id,
            supersedes_field,
            label,
            errors,
        )
        superseded_ids = set(successor_counts)

        return [
            record
            for record_id, record in records_by_id.items()
            if record_id not in superseded_ids
        ]

    def _check_revision_cycles(
        self,
        records_by_id: dict[str, dict[str, Any]],
        supersedes_field: str,
        label: str,
        errors: list[str],
    ) -> None:
        """Найти циклы в явных цепочках замены записей."""

        for start_id in records_by_id:
            visited: set[str] = set()
            current_id: str | None = start_id

            while current_id is not None and current_id in records_by_id:
                if current_id in visited:
                    errors.append(
                        f"В {label} найден цикл замены около {current_id!r}."
                    )
                    break

                visited.add(current_id)
                predecessor = records_by_id[current_id].get(supersedes_field)
                current_id = predecessor if isinstance(predecessor, str) else None

    def _validate_run_and_frame(
        self,
        bundle: _OcrQaBundle,
        errors: list[str],
    ) -> None:
        """Проверить идентификаторы, квоты и страты подготовленной выборки."""

        run = bundle.run
        run_id = run.get("qa_run_id")
        variants = run.get("variants", [])
        strata = run.get("selection_plan", {}).get("strata", [])
        self._check_unique_values(variants, "variant_id", "вариантах запуска", errors)
        self._check_unique_values(strata, "stratum_id", "стратах выборки", errors)
        self._check_unique_values(
            bundle.frame,
            "frame_record_id",
            "кадре выборки",
            errors,
        )
        self._check_unique_values(
            bundle.frame,
            "page_sample_id",
            "кадре выборки",
            errors,
        )

        strata_by_id = {
            stratum.get("stratum_id"): stratum
            for stratum in strata
            if isinstance(stratum.get("stratum_id"), str)
        }
        random_stratum_counts: Counter[str] = Counter()

        for frame_record in bundle.frame:
            sample_id = frame_record.get("page_sample_id")

            if frame_record.get("qa_run_id") != run_id:
                errors.append(
                    f"Страница выборки {sample_id!r} относится к другому qa_run_id."
                )

            if frame_record.get("page_number") != frame_record.get("page_index", -1) + 1:
                errors.append(
                    f"Страница выборки {sample_id!r}: page_number должен равняться page_index + 1."
                )

            if frame_record.get("selection_role") != "stratified_random":
                continue

            stratum_id = frame_record.get("stratum_id")
            stratum = strata_by_id.get(stratum_id)

            if stratum is None:
                errors.append(
                    f"Страница выборки {sample_id!r} ссылается на неизвестную страту {stratum_id!r}."
                )
                continue

            random_stratum_counts[stratum_id] += 1
            self._check_frame_stratum(frame_record, stratum, errors)

        for stratum_id, stratum in strata_by_id.items():
            actual_count = random_stratum_counts[stratum_id]
            expected_count = stratum.get("target_pages")

            if actual_count != expected_count:
                errors.append(
                    f"Страта {stratum_id!r}: выбрано страниц {actual_count} "
                    f"вместо {expected_count}."
                )

        selection_plan = run.get("selection_plan", {})
        random_frames = [
            record
            for record in bundle.frame
            if record.get("selection_role") == "stratified_random"
        ]
        challenge_frames = [
            record
            for record in bundle.frame
            if record.get("selection_role") == "manual_challenge"
        ]
        self._compare_integer(
            len(random_frames),
            selection_plan.get("target_page_count"),
            "Число страниц stratified_random в выборке",
            errors,
        )
        self._compare_integer(
            len(challenge_frames),
            selection_plan.get("target_manual_challenge_page_count"),
            "Число страниц manual_challenge в выборке",
            errors,
        )
        pdf_count = len(
            {
                record.get("source_pdf_artifact_id")
                for record in random_frames
                if record.get("source_pdf_artifact_id") is not None
            }
        )
        self._compare_integer(
            pdf_count,
            selection_plan.get("target_pdf_count"),
            "Число PDF-файлов в выборке",
            errors,
        )

    def _check_frame_stratum(
        self,
        frame_record: dict[str, Any],
        stratum: dict[str, Any],
        errors: list[str],
    ) -> None:
        """Проверить соответствие случайной страницы её объявленной страте."""

        sample_id = frame_record.get("page_sample_id")

        for field_name in ("source_id", "source_group_id", "scan_quality", "layout"):
            if frame_record.get(field_name) != stratum.get(field_name):
                errors.append(
                    f"Страница выборки {sample_id!r} не соответствует полю страты "
                    f"{field_name}."
                )

        year = frame_record.get("publication_year")
        year_from = stratum.get("year_from")
        year_to = stratum.get("year_to")

        if (
            isinstance(year, int)
            and isinstance(year_from, int)
            and isinstance(year_to, int)
            and not year_from <= year <= year_to
        ):
            errors.append(
                f"Страница выборки {sample_id!r}: год публикации вне страты."
            )

    def _validate_page_relations(
        self,
        bundle: _OcrQaBundle,
        active_pages: list[dict[str, Any]],
        approved_pages: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        """Сверить актуальные результаты со страницами выборки и вариантами запуска."""

        run_id = bundle.run.get("qa_run_id")
        variants = {
            variant.get("variant_id"): variant
            for variant in bundle.run.get("variants", [])
        }
        frames = {
            frame.get("page_sample_id"): frame
            for frame in bundle.frame
            if isinstance(frame.get("page_sample_id"), str)
        }
        active_by_key: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)

        for page in active_pages:
            page_id = page.get("page_qa_record_id")
            key = (page.get("page_sample_id"), page.get("variant_id"))
            active_by_key[key].append(page)

            if page.get("review_status") != "approved":
                errors.append(
                    f"Актуальный результат страницы {page_id!r} не одобрен."
                )

            if page.get("qa_run_id") != run_id:
                errors.append(f"Результат страницы {page_id!r} относится к другому запуску.")

            variant = variants.get(page.get("variant_id"))

            if variant is None:
                errors.append(
                    f"Результат страницы {page_id!r} ссылается на неизвестный вариант."
                )

            frame = frames.get(page.get("page_sample_id"))

            if frame is None:
                errors.append(
                    f"Результат страницы {page_id!r} ссылается на неизвестную страницу выборки."
                )
                continue

            self._compare_fields(
                page,
                frame,
                PAGE_FRAME_FIELDS,
                f"Результат страницы {page_id!r}",
                errors,
            )

            comparison_plan = bundle.run.get("comparison_plan", {})

            if page.get("normalization_version") != comparison_plan.get(
                "prose_normalization_version"
            ):
                errors.append(
                    f"Результат страницы {page_id!r} использует другую версию нормализации."
                )

            if page.get("metric_version") != comparison_plan.get("metric_version"):
                errors.append(
                    f"Результат страницы {page_id!r} использует другую версию метрик."
                )

            reviewer_ids = bundle.run.get("review_plan", {}).get("reviewer_ids", [])

            if page.get("reviewer_id") not in reviewer_ids:
                errors.append(
                    f"Результат страницы {page_id!r} одобрен необъявленным проверяющим."
                )

            if (
                page.get("review_mode") == "independent"
                and page.get("gold_preparer_id") == page.get("gold_verifier_id")
            ):
                errors.append(
                    f"Результат страницы {page_id!r}: независимая проверка требует двух людей."
                )

        for key, records in active_by_key.items():
            if len(records) > 1:
                errors.append(
                    f"Для ключа {key!r} найдено актуальных результатов страницы: {len(records)}."
                )

        expected_keys = {
            (page_sample_id, variant_id)
            for page_sample_id in frames
            for variant_id in variants
        }
        approved_keys = {
            (page.get("page_sample_id"), page.get("variant_id"))
            for page in approved_pages
        }

        for missing_key in sorted(expected_keys - approved_keys):
            errors.append(
                f"Нет актуального одобренного результата страницы для ключа {missing_key!r}."
            )

        for extra_key in sorted(approved_keys - expected_keys):
            errors.append(
                f"Найден неожиданный одобренный результат страницы для ключа {extra_key!r}."
            )

    def _validate_formula_relations(
        self,
        bundle: _OcrQaBundle,
        active_formulas: list[dict[str, Any]],
        approved_formulas: list[dict[str, Any]],
        approved_pages: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        """Сверить записи формул с их актуальными результатами страниц."""

        run_id = bundle.run.get("qa_run_id")
        pages_by_key = {
            (page.get("page_sample_id"), page.get("variant_id")): page
            for page in approved_pages
        }
        formulas_by_page: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
        reference_ids: dict[tuple[Any, Any], set[str]] = defaultdict(set)
        prediction_ids: dict[tuple[Any, Any], set[str]] = defaultdict(set)
        reviewer_ids = bundle.run.get("review_plan", {}).get("reviewer_ids", [])

        for formula in active_formulas:
            record_id = formula.get("formula_qa_record_id")
            key = (formula.get("page_sample_id"), formula.get("variant_id"))

            if formula.get("review_status") != "approved":
                errors.append(
                    f"Актуальный результат формулы {record_id!r} не одобрен."
                )

            if formula.get("qa_run_id") != run_id:
                errors.append(f"Результат формулы {record_id!r} относится к другому запуску.")

            page = pages_by_key.get(key)

            if page is None:
                errors.append(
                    f"Для результата формулы {record_id!r} нет актуального одобренного результата страницы."
                )
                continue

            self._compare_fields(
                formula,
                page,
                FORMULA_PAGE_FIELDS,
                f"Результат формулы {record_id!r}",
                errors,
            )

            if page.get("formula_annotation_status") != "completed":
                errors.append(
                    f"Результат формулы {record_id!r} относится к странице "
                    "без завершённой разметки формул."
                )

            if formula.get("reviewer_id") not in reviewer_ids:
                errors.append(
                    f"Результат формулы {record_id!r} одобрен необъявленным проверяющим."
                )

        for formula in approved_formulas:
            key = (formula.get("page_sample_id"), formula.get("variant_id"))
            formulas_by_page[key].append(formula)
            reference_id = formula.get("reference_formula_id")
            prediction_id = formula.get("prediction_formula_id")

            if isinstance(reference_id, str):
                if reference_id in reference_ids[key]:
                    errors.append(
                        f"Эталонная формула {reference_id!r} повторяется для ключа страницы {key!r}."
                    )

                reference_ids[key].add(reference_id)

            if isinstance(prediction_id, str):
                if prediction_id in prediction_ids[key]:
                    errors.append(
                        f"Предсказанная формула {prediction_id!r} повторяется для ключа страницы {key!r}."
                    )

                prediction_ids[key].add(prediction_id)

        for key, page in pages_by_key.items():
            formulas = formulas_by_page.get(key, [])
            reference_count = sum(
                formula.get("match_status") in {"true_positive", "false_negative"}
                for formula in formulas
            )
            self._compare_integer(
                len(formulas),
                page.get("formula_record_count"),
                f"Страница {key!r}, formula_record_count",
                errors,
            )
            self._compare_integer(
                reference_count,
                page.get("formula_reference_count"),
                f"Страница {key!r}, formula_reference_count",
                errors,
            )

    def _validate_geometry_and_metrics(
        self,
        pages: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        """Проверить прямоугольники, интервалы и формулы постраничных метрик."""

        candidate_lengths: dict[tuple[str, str], int] = {}

        if self.check_files:
            for page in pages:
                path_value = page.get("candidate_page_text_path")

                if not isinstance(path_value, str):
                    continue

                path = self._resolve_embedded_path(
                    path_value,
                    "candidate_page_text_path",
                    errors,
                )

                if path is None or not path.is_file():
                    continue

                try:
                    text = path.read_text(encoding="utf-8")

                except (OSError, UnicodeDecodeError) as exception:
                    errors.append(f"Не удалось прочитать текст-кандидат {path}: {exception}")
                    continue

                key = (str(page.get("page_sample_id")), str(page.get("variant_id")))
                candidate_lengths[key] = len(text)

        for page in pages:
            record_id = page.get("page_qa_record_id")
            self._check_page_metric(page, "character", "cer", errors)
            self._check_page_metric(page, "word", "wer", errors)
            self._check_regions(page, errors)

            if page.get("page_number") != page.get("page_index", -1) + 1:
                errors.append(
                    f"Результат страницы {record_id!r}: page_number должен равняться page_index + 1."
                )

            if page.get("extraction_outcome") == "failed":
                self._check_failed_extraction(page, errors)

        for formula in formulas:
            record_id = formula.get("formula_qa_record_id")
            bounding_box = formula.get("reference_bounding_box")

            if isinstance(bounding_box, dict):
                self._check_bounding_box(
                    bounding_box,
                    f"Результат формулы {record_id!r}",
                    errors,
                )

            key = (
                str(formula.get("page_sample_id")),
                str(formula.get("variant_id")),
            )
            text_length = candidate_lengths.get(key)
            reference_span = formula.get("reference_span")
            prediction_span = formula.get("prediction_span")
            self._check_span(
                reference_span,
                text_length,
                f"Результат формулы {record_id!r}, reference_span",
                errors,
            )
            self._check_span(
                prediction_span,
                text_length,
                f"Результат формулы {record_id!r}, prediction_span",
                errors,
            )

            if formula.get("match_status") == "true_positive":
                expected_iou = _interval_iou(reference_span, prediction_span)
                self._compare_float(
                    expected_iou,
                    formula.get("intersection_over_union"),
                    f"Результат формулы {record_id!r}, intersection_over_union",
                    errors,
                )

    def _check_page_metric(
        self,
        page: dict[str, Any],
        prefix: str,
        metric_name: str,
        errors: list[str],
    ) -> None:
        """Пересчитать CER- или WER-подобную метрику из её счётчиков."""

        reference_field = "reference_characters" if prefix == "character" else "reference_words"
        reference_count = page.get(reference_field)
        substitutions = page.get(f"{prefix}_substitutions")
        deletions = page.get(f"{prefix}_deletions")
        insertions = page.get(f"{prefix}_insertions")

        if not all(
            isinstance(value, int)
            for value in (reference_count, substitutions, deletions, insertions)
        ) or not reference_count:
            return

        expected = (substitutions + deletions + insertions) / reference_count
        record_id = page.get("page_qa_record_id")
        self._compare_float(
            expected,
            page.get(metric_name),
            f"Результат страницы {record_id!r}, {metric_name}",
            errors,
        )

    def _check_regions(
        self,
        page: dict[str, Any],
        errors: list[str],
    ) -> None:
        """Проверить идентификаторы, порядок и нормированные прямоугольники областей."""

        record_id = page.get("page_qa_record_id")
        regions = page.get("prose_regions", [])
        region_ids: list[Any] = []
        expected_orders: list[Any] = []

        for region in regions:
            if not isinstance(region, dict):
                continue

            region_ids.append(region.get("region_id"))
            expected_orders.append(region.get("expected_order"))
            bounding_box = region.get("bounding_box")

            if isinstance(bounding_box, dict):
                self._check_bounding_box(
                    bounding_box,
                    f"Результат страницы {record_id!r}, область {region.get('region_id')!r}",
                    errors,
                )

        if len(region_ids) != len(set(region_ids)):
            errors.append(f"У результата страницы {record_id!r} повторяются region_id.")

        if len(expected_orders) != len(set(expected_orders)):
            errors.append(
                f"У результата страницы {record_id!r} повторяются expected_order."
            )

        ordered_region_ids = [
            region.get("region_id")
            for region in sorted(
                (item for item in regions if isinstance(item, dict)),
                key=lambda item: item.get("expected_order", 0),
            )
        ]

        if page.get("expected_block_order") != ordered_region_ids:
            errors.append(
                f"Результат страницы {record_id!r}: expected_block_order не "
                "совпадает с порядком prose_regions."
            )

    def _check_failed_extraction(
        self,
        page: dict[str, Any],
        errors: list[str],
    ) -> None:
        """Проверить учёт неудачного извлечения как пустого кандидата."""

        record_id = page.get("page_qa_record_id")
        expected_values = {
            "character_substitutions": 0,
            "character_deletions": page.get("reference_characters"),
            "character_insertions": 0,
            "cer": 1.0,
            "word_substitutions": 0,
            "word_deletions": page.get("reference_words"),
            "word_insertions": 0,
            "wer": 1.0,
        }

        for field_name, expected in expected_values.items():
            actual = page.get(field_name)

            if isinstance(expected, float):
                self._compare_float(
                    expected,
                    actual,
                    f"Неудачная страница {record_id!r}, {field_name}",
                    errors,
                )
            elif actual != expected:
                errors.append(
                    f"Неудачная страница {record_id!r}: {field_name}={actual!r}, "
                    f"ожидалось {expected!r}."
                )

    def _check_bounding_box(
        self,
        bounding_box: dict[str, Any],
        label: str,
        errors: list[str],
    ) -> None:
        """Проверить положительные ширину и высоту нормированного прямоугольника."""

        x_min = bounding_box.get("x_min")
        x_max = bounding_box.get("x_max")
        y_min = bounding_box.get("y_min")
        y_max = bounding_box.get("y_max")

        if isinstance(x_min, (int, float)) and isinstance(x_max, (int, float)):
            if x_min >= x_max:
                errors.append(f"{label}: x_min должен быть меньше x_max.")

        if isinstance(y_min, (int, float)) and isinstance(y_max, (int, float)):
            if y_min >= y_max:
                errors.append(f"{label}: y_min должен быть меньше y_max.")

    def _check_span(
        self,
        span: Any,
        text_length: int | None,
        label: str,
        errors: list[str],
    ) -> None:
        """Проверить полуоткрытый интервал и при возможности границу текста."""

        if not isinstance(span, dict):
            return

        start = span.get("start")
        end = span.get("end")

        if not isinstance(start, int) or not isinstance(end, int):
            return

        if start >= end:
            errors.append(f"{label}: start должен быть меньше end.")

        if text_length is not None and end > text_length:
            errors.append(
                f"{label}: end={end} превышает длину текста-кандида {text_length}."
            )

    def _check_unique_values(
        self,
        records: list[dict[str, Any]],
        field_name: str,
        label: str,
        errors: list[str],
    ) -> None:
        """Сообщить о повторах непустых идентификаторов в последовательности записей."""

        counts = Counter(
            record.get(field_name)
            for record in records
            if record.get(field_name) is not None
        )

        for value, count in counts.items():
            if count > 1:
                errors.append(
                    f"Повтор {field_name}={value!r} в {label}: записей {count}."
                )

    def _compare_fields(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        fields: tuple[str, ...],
        label: str,
        errors: list[str],
    ) -> None:
        """Сверить наследуемые поля идентичности и происхождения."""

        for field_name in fields:
            if left.get(field_name) != right.get(field_name):
                errors.append(f"{label}: поле {field_name} не совпадает с родительской записью.")

    def _compare_integer(
        self,
        expected: Any,
        actual: Any,
        label: str,
        errors: list[str],
    ) -> None:
        """Сверить целочисленный счётчик без приведения bool к int."""

        if (
            not isinstance(expected, int)
            or isinstance(expected, bool)
            or not isinstance(actual, int)
            or isinstance(actual, bool)
        ):
            return

        if expected != actual:
            errors.append(f"{label}={actual}, ожидалось {expected}.")

    def _compare_float(
        self,
        expected: Any,
        actual: Any,
        label: str,
        errors: list[str],
    ) -> None:
        """Сверить сохранённую вещественную метрику с пересчитанным значением."""

        if expected is None and actual is None:
            return

        if expected is None or actual is None:
            errors.append(f"{label}={actual!r}, ожидалось {expected!r}.")
            return

        if not isinstance(expected, (int, float)) or not isinstance(
            actual,
            (int, float),
        ):
            return

        if not math.isclose(
            float(expected),
            float(actual),
            rel_tol=0,
            abs_tol=FLOAT_ABSOLUTE_TOLERANCE,
        ):
            errors.append(f"{label}={actual!r}, ожидалось {expected!r}.")


def _parse_datetime(value: Any) -> datetime | None:
    """Разобрать дату и время ISO для упорядочивания ревизий."""

    if not isinstance(value, str):
        return

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    except ValueError:
        return


def _interval_iou(reference_span: Any, prediction_span: Any) -> float | None:
    """Вычислить IoU двух полуоткрытых текстовых интервалов."""

    if not isinstance(reference_span, dict) or not isinstance(prediction_span, dict):
        return

    reference_start = reference_span.get("start")
    reference_end = reference_span.get("end")
    prediction_start = prediction_span.get("start")
    prediction_end = prediction_span.get("end")

    if not all(
        isinstance(value, int)
        for value in (
            reference_start,
            reference_end,
            prediction_start,
            prediction_end,
        )
    ):
        return

    intersection = max(
        0,
        min(reference_end, prediction_end)
        - max(reference_start, prediction_start),
    )
    union = max(reference_end, prediction_end) - min(
        reference_start,
        prediction_start,
    )

    if union <= 0:
        return

    return intersection / union
