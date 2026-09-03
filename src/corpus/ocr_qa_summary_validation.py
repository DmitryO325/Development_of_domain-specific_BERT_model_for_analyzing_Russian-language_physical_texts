"""Проверка сводных метрик и решений запуска OCR QA."""

from __future__ import annotations

import math

from typing import Any

from .ocr_qa_io import _OcrQaBundle
from .ocr_qa_metrics import (
    derive_aggregate,
    expected_formula_status as derive_formula_status,
    expected_prose_status as derive_prose_status,
    nested_work_page_bootstrap,
)

FLOAT_ABSOLUTE_TOLERANCE = 1e-9


class _OcrQaSummaryMixin:
    """Сверка агрегатов, доверительных границ и итогового решения."""

    def _check_unique_values(
        self,
        records: list[dict[str, Any]],
        field_name: str,
        label: str,
        errors: list[str],
    ) -> None:
        """Предоставить вспомогательную проверку из основного валидатора."""

        raise NotImplementedError

    def _compare_integer(
        self,
        expected: Any,
        actual: Any,
        label: str,
        errors: list[str],
    ) -> None:
        """Предоставить сравнение счётчиков из основного валидатора."""

        raise NotImplementedError

    def _compare_float(
        self,
        expected: Any,
        actual: Any,
        label: str,
        errors: list[str],
    ) -> None:
        """Предоставить сравнение метрик из основного валидатора."""

        raise NotImplementedError

    def _validate_summary(
        self,
        bundle: _OcrQaBundle,
        pages: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Пересчитать агрегаты сводки и проверить итоговое решение."""

        summary = bundle.summary
        run = bundle.run

        if summary.get("qa_run_id") != run.get("qa_run_id"):
            errors.append("Паспорт и сводка имеют разные qa_run_id.")

        variants = {
            variant.get("variant_id")
            for variant in run.get("variants", [])
            if isinstance(variant.get("variant_id"), str)
        }
        variant_results = summary.get("variant_results", [])
        self._check_unique_values(
            variant_results,
            "variant_id",
            "результатах вариантов сводки",
            errors,
        )
        result_variants = {
            result.get("variant_id")
            for result in variant_results
            if isinstance(result.get("variant_id"), str)
        }

        if result_variants != variants:
            errors.append(
                "Набор variant_id в сводке не совпадает с вариантами паспорта."
            )

        self._validate_sample_counts(bundle, pages, formulas, errors)

        for variant_result in variant_results:
            variant_id = variant_result.get("variant_id")
            variant_pages = [
                page for page in pages if page.get("variant_id") == variant_id
            ]
            variant_formulas = [
                formula
                for formula in formulas
                if formula.get("variant_id") == variant_id
            ]
            random_pages = [
                page
                for page in variant_pages
                if page.get("selection_role") == "stratified_random"
            ]
            random_formulas = [
                formula
                for formula in variant_formulas
                if formula.get("selection_role") == "stratified_random"
            ]
            self._check_aggregate(
                variant_result.get("overall"),
                random_pages,
                random_formulas,
                run,
                aggregate_kind="overall",
                label=f"Вариант {variant_id!r}, общий итог",
                errors=errors,
            )
            self._validate_source_layout_groups(
                variant_result,
                random_pages,
                random_formulas,
                run,
                errors,
            )
            self._validate_manual_challenge_groups(
                variant_result,
                variant_pages,
                variant_formulas,
                run,
                errors,
            )
            self._validate_variant_flags(variant_result, errors)

        self._validate_decision(summary, errors, warnings)

    def _validate_sample_counts(
        self,
        bundle: _OcrQaBundle,
        pages: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        """Сверить счётчики сводки с выборкой и одобренными записями."""

        counts = bundle.summary.get("sample_counts", {})
        random_frames = [
            record
            for record in bundle.frame
            if record.get("selection_role") == "stratified_random"
        ]
        challenge_count = sum(
            record.get("selection_role") == "manual_challenge"
            for record in bundle.frame
        )
        expected = {
            "pdf_count": len(
                {record.get("source_pdf_artifact_id") for record in random_frames}
            ),
            "work_count": len({record.get("work_id") for record in random_frames}),
            "page_count": len(random_frames),
            "manual_challenge_page_count": challenge_count,
        }

        for field_name, value in expected.items():
            self._compare_integer(
                value,
                counts.get(field_name),
                f"sample_counts.{field_name}",
                errors,
            )

        for variant_id in {
            page.get("variant_id")
            for page in pages
            if page.get("selection_role") == "stratified_random"
        }:
            variant_pages = [
                page
                for page in pages
                if page.get("variant_id") == variant_id
                and page.get("selection_role") == "stratified_random"
            ]
            variant_formulas = [
                formula
                for formula in formulas
                if formula.get("variant_id") == variant_id
                and formula.get("selection_role") == "stratified_random"
            ]
            prose_characters = sum(
                page.get("reference_characters", 0) for page in variant_pages
            )
            reference_formulas = [
                formula
                for formula in variant_formulas
                if formula.get("match_status") in {"true_positive", "false_negative"}
            ]
            formula_work_ids = len(
                {formula.get("work_id") for formula in reference_formulas}
            )
            self._compare_integer(
                prose_characters,
                counts.get("prose_characters"),
                f"sample_counts.prose_characters для варианта {variant_id!r}",
                errors,
            )
            self._compare_integer(
                len(reference_formulas),
                counts.get("formula_occurrences"),
                f"sample_counts.formula_occurrences для варианта {variant_id!r}",
                errors,
            )
            self._compare_integer(
                formula_work_ids,
                counts.get("formula_work_ids"),
                f"sample_counts.formula_work_ids для варианта {variant_id!r}",
                errors,
            )

    def _validate_source_layout_groups(
        self,
        variant_result: dict[str, Any],
        pages: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
        run: dict[str, Any],
        errors: list[str],
    ) -> None:
        """Проверить агрегаты и полное покрытие групп источник × макет."""

        variant_id = variant_result.get("variant_id")
        groups = variant_result.get("source_layout_groups", [])
        self._check_unique_values(
            groups,
            "group_id",
            f"группах источник × макет для варианта {variant_id!r}",
            errors,
        )
        expected_keys = {
            (page.get("source_id"), page.get("layout")) for page in pages
        }
        actual_keys = {
            (group.get("source_id"), group.get("layout"))
            for group in groups
            if isinstance(group, dict)
        }

        if actual_keys != expected_keys:
            errors.append(
                f"Вариант {variant_id!r}: source_layout_groups не покрывают "
                "точно все группы source_id × layout одобренных случайных страниц."
            )

        for group in groups:
            source_id = group.get("source_id")
            layout = group.get("layout")
            group_pages = [
                page
                for page in pages
                if page.get("source_id") == source_id and page.get("layout") == layout
            ]
            page_keys = {
                (page.get("page_sample_id"), page.get("variant_id"))
                for page in group_pages
            }
            group_formulas = [
                formula
                for formula in formulas
                if (formula.get("page_sample_id"), formula.get("variant_id"))
                in page_keys
            ]
            self._check_aggregate(
                group,
                group_pages,
                group_formulas,
                run,
                aggregate_kind="source_layout",
                label=(
                    f"Вариант {variant_id!r}, группа {group.get('group_id')!r}"
                ),
                errors=errors,
            )

    def _validate_manual_challenge_groups(
        self,
        variant_result: dict[str, Any],
        pages: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
        run: dict[str, Any],
        errors: list[str],
    ) -> None:
        """Проверить агрегаты сложных страниц без смешения со случайными."""

        variant_id = variant_result.get("variant_id")
        challenge_pages = [
            page for page in pages if page.get("selection_role") == "manual_challenge"
        ]
        challenge_formulas = [
            formula
            for formula in formulas
            if formula.get("selection_role") == "manual_challenge"
        ]
        groups = variant_result.get("manual_challenge_results", [])
        self._check_unique_values(
            groups,
            "group_id",
            f"группах manual_challenge для варианта {variant_id!r}",
            errors,
        )

        if bool(groups) != bool(challenge_pages):
            errors.append(
                f"Вариант {variant_id!r}: наличие manual_challenge_results не "
                "соответствует наличию сложных страниц."
            )

        for group in groups:
            group_pages = challenge_pages

            if group.get("source_id") is not None:
                group_pages = [
                    page
                    for page in group_pages
                    if page.get("source_id") == group.get("source_id")
                ]

            if group.get("layout") is not None:
                group_pages = [
                    page
                    for page in group_pages
                    if page.get("layout") == group.get("layout")
                ]

            page_keys = {
                (page.get("page_sample_id"), page.get("variant_id"))
                for page in group_pages
            }
            group_formulas = [
                formula
                for formula in challenge_formulas
                if (formula.get("page_sample_id"), formula.get("variant_id"))
                in page_keys
            ]
            self._check_aggregate(
                group,
                group_pages,
                group_formulas,
                run,
                aggregate_kind="manual_challenge",
                label=(
                    f"Вариант {variant_id!r}, группа сложных страниц "
                    f"{group.get('group_id')!r}"
                ),
                errors=errors,
            )

    def _check_aggregate(
        self,
        aggregate: Any,
        pages: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
        run: dict[str, Any],
        *,
        aggregate_kind: str,
        label: str,
        errors: list[str],
    ) -> None:
        """Сверить один сохранённый агрегат со счётчиками одобренных строк."""

        if not isinstance(aggregate, dict):
            errors.append(f"{label}: агрегат не является объектом.")
            return

        derived = derive_aggregate(pages, formulas)

        for field_name in (
            "work_count",
            "page_count",
            "attempted_pages",
            "successful_extraction_pages",
            "prose_characters",
            "character_errors",
            "reference_words",
            "word_errors",
            "critical_reading_order_pages",
            "formula_reference_count",
            "formula_work_count",
            "formula_true_positives",
            "formula_false_negatives",
            "formula_false_positives",
            "critical_formula_damage_count",
        ):
            self._compare_integer(
                derived[field_name],
                aggregate.get(field_name),
                f"{label}.{field_name}",
                errors,
            )

        for field_name in (
            "extraction_success_rate",
            "cer",
            "wer",
            "critical_reading_order_rate",
            "formula_detection_f1",
            "critical_formula_damage_rate",
        ):
            self._compare_float(
                derived[field_name],
                aggregate.get(field_name),
                f"{label}.{field_name}",
                errors,
            )

        confidence_plan = run.get("confidence_interval_plan", {})
        bootstrap_bounds = nested_work_page_bootstrap(
            pages,
            formulas,
            replicates=confidence_plan.get("replicates", 0),
            seed=confidence_plan.get("bootstrap_seed", 0),
        )

        for field_name, expected_bound in bootstrap_bounds.items():
            self._compare_float(
                expected_bound,
                aggregate.get(field_name),
                f"{label}.{field_name}",
                errors,
            )

        self._check_confidence_bound(
            aggregate.get("cer"),
            aggregate.get("cer_ci_upper"),
            upper=True,
            label=f"{label}.cer_ci_upper",
            errors=errors,
        )
        self._check_confidence_bound(
            aggregate.get("wer"),
            aggregate.get("wer_ci_upper"),
            upper=True,
            label=f"{label}.wer_ci_upper",
            errors=errors,
        )
        self._check_confidence_bound(
            aggregate.get("critical_reading_order_rate"),
            aggregate.get("critical_reading_order_rate_ci_upper"),
            upper=True,
            label=f"{label}.critical_reading_order_rate_ci_upper",
            errors=errors,
        )
        self._check_confidence_bound(
            aggregate.get("formula_detection_f1"),
            aggregate.get("formula_detection_f1_ci_lower"),
            upper=False,
            label=f"{label}.formula_detection_f1_ci_lower",
            errors=errors,
        )
        self._check_confidence_bound(
            aggregate.get("critical_formula_damage_rate"),
            aggregate.get("critical_formula_damage_rate_ci_upper"),
            upper=True,
            label=f"{label}.critical_formula_damage_rate_ci_upper",
            errors=errors,
        )
        if aggregate_kind != "manual_challenge":
            expected_prose_status = derive_prose_status(
                aggregate,
                run,
                aggregate_kind,
            )

            if aggregate.get("prose_criteria_status") != expected_prose_status:
                errors.append(
                    f"{label}.prose_criteria_status="
                    f"{aggregate.get('prose_criteria_status')!r}, "
                    f"ожидалось {expected_prose_status!r}."
                )

            expected_formula_status = derive_formula_status(aggregate, run)
            actual_formula_status = aggregate.get("formula_criteria_status")
            zero_reference_status = (
                aggregate.get("formula_reference_count") == 0
                and actual_formula_status in {"insufficient", "not_evaluated"}
            )

            if actual_formula_status != expected_formula_status and not zero_reference_status:
                errors.append(
                    f"{label}.formula_criteria_status={actual_formula_status!r}, "
                    f"ожидалось {expected_formula_status!r}."
                )

    def _check_confidence_bound(
        self,
        point: Any,
        bound: Any,
        *,
        upper: bool,
        label: str,
        errors: list[str],
    ) -> None:
        """Проверить, что односторонняя доверительная граница лежит с верной стороны."""

        if point is None and bound is None:
            return

        if not isinstance(point, (int, float)) or not isinstance(bound, (int, float)):
            return

        invalid = bound < point if upper else bound > point

        if invalid and not math.isclose(
            bound,
            point,
            rel_tol=0,
            abs_tol=FLOAT_ABSOLUTE_TOLERANCE,
        ):
            direction = "не меньше" if upper else "не больше"
            errors.append(f"{label} должна быть {direction} точечной оценки.")

    def _validate_variant_flags(
        self,
        variant_result: dict[str, Any],
        errors: list[str],
    ) -> None:
        """Проверить выводимые флаги основного критерия и H3 для одного варианта."""

        overall = variant_result.get("overall", {})
        groups = variant_result.get("source_layout_groups", [])
        expected_core = (
            overall.get("prose_criteria_status") == "pass"
            and bool(groups)
            and all(group.get("prose_criteria_status") == "pass" for group in groups)
        )
        expected_h3 = (
            expected_core and overall.get("formula_criteria_status") == "pass"
        )
        variant_id = variant_result.get("variant_id")

        if variant_result.get("meets_core_criteria") is not expected_core:
            errors.append(
                f"Вариант {variant_id!r}: meets_core_criteria должен быть {expected_core}."
            )

        if variant_result.get("h3_allowed") is not expected_h3:
            errors.append(f"Вариант {variant_id!r}: h3_allowed должен быть {expected_h3}.")

    def _validate_decision(
        self,
        summary: dict[str, Any],
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Проверить итоговое решение, рекомендацию и выбранный вариант."""

        variant_results = summary.get("variant_results", [])
        selected = [
            result
            for result in variant_results
            if result.get("selected_for_adoption") is True
        ]
        decision = summary.get("decision_status")
        recommendation = summary.get("recommendation")
        relation_status = summary.get("format_and_relation_checks")
        confirming_decision = decision == "pass" or recommendation == "adopt_variant"

        if confirming_decision:
            if len(selected) != 1:
                errors.append(
                    "Подтверждающее решение должно выбирать ровно один вариант."
                )

            if relation_status != "pass":
                errors.append(
                    "Подтверждающее решение требует format_and_relation_checks='pass'."
                )
        elif selected:
            errors.append(
                "Вариант выбран для принятия без подтверждающего решения."
            )

        if recommendation == "adopt_variant" and decision != "pass":
            errors.append("Рекомендация adopt_variant требует decision_status='pass'.")

        for result in selected:
            if result.get("meets_core_criteria") is not True:
                errors.append(
                    f"Выбранный вариант {result.get('variant_id')!r} не прошёл основной критерий."
                )

        if relation_status == "fail":
            errors.append("Сводка явно отмечает проверку формата и связей как неуспешную.")
        elif relation_status == "not_run":
            warnings.append("В сводке указано, что проверка формата и связей не запускалась.")
