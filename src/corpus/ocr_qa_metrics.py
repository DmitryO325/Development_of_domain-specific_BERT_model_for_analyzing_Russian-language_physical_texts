"""Расчёт точечных метрик и доверительных границ OCR QA."""

from __future__ import annotations

import math
import random

from collections import defaultdict
from typing import Any

UPPER_BOUND_METRICS = {
    "cer_ci_upper": "cer",
    "wer_ci_upper": "wer",
    "critical_reading_order_rate_ci_upper": "critical_reading_order_rate",
    "critical_formula_damage_rate_ci_upper": "critical_formula_damage_rate",
}
LOWER_BOUND_METRICS = {
    "formula_detection_f1_ci_lower": "formula_detection_f1",
}


def derive_aggregate(
    pages: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
) -> dict[str, int | float | None]:
    """Рассчитать точечные оценки и счётчики по одобренным актуальным записям."""

    prose_characters = sum(page.get("reference_characters", 0) for page in pages)
    character_errors = sum(
        page.get("character_substitutions", 0)
        + page.get("character_deletions", 0)
        + page.get("character_insertions", 0)
        for page in pages
    )
    reference_words = sum(page.get("reference_words", 0) for page in pages)
    word_errors = sum(
        page.get("word_substitutions", 0)
        + page.get("word_deletions", 0)
        + page.get("word_insertions", 0)
        for page in pages
    )
    page_count = len(pages)
    successful_pages = sum(
        page.get("extraction_outcome") == "succeeded" for page in pages
    )
    critical_pages = sum(
        page.get("reading_order_status") == "critical_error" for page in pages
    )
    true_positives = sum(
        formula.get("match_status") == "true_positive" for formula in formulas
    )
    false_negatives = sum(
        formula.get("match_status") == "false_negative" for formula in formulas
    )
    false_positives = sum(
        formula.get("match_status") == "false_positive" for formula in formulas
    )
    reference_formulas = [
        formula
        for formula in formulas
        if formula.get("match_status") in {"true_positive", "false_negative"}
    ]
    reference_count = len(reference_formulas)
    critical_formula_count = sum(
        formula.get("critical_damage") is True for formula in reference_formulas
    )
    f1_denominator = 2 * true_positives + false_positives + false_negatives

    return {
        "work_count": len({page.get("work_id") for page in pages}),
        "page_count": page_count,
        "attempted_pages": page_count,
        "successful_extraction_pages": successful_pages,
        "extraction_success_rate": _safe_ratio(successful_pages, page_count),
        "prose_characters": prose_characters,
        "character_errors": character_errors,
        "cer": _safe_ratio(character_errors, prose_characters),
        "reference_words": reference_words,
        "word_errors": word_errors,
        "wer": _safe_ratio(word_errors, reference_words),
        "critical_reading_order_pages": critical_pages,
        "critical_reading_order_rate": _safe_ratio(critical_pages, page_count),
        "formula_reference_count": reference_count,
        "formula_work_count": len(
            {formula.get("work_id") for formula in reference_formulas}
        ),
        "formula_true_positives": true_positives,
        "formula_false_negatives": false_negatives,
        "formula_false_positives": false_positives,
        "formula_detection_f1": (
            _safe_ratio(2 * true_positives, f1_denominator)
            if f1_denominator
            else None
        ),
        "critical_formula_damage_count": critical_formula_count,
        "critical_formula_damage_rate": _safe_ratio(
            critical_formula_count,
            reference_count,
        ),
    }


def nested_work_page_bootstrap(
    pages: list[dict[str, Any]],
    formulas: list[dict[str, Any]],
    *,
    replicates: int,
    seed: int,
) -> dict[str, float | None]:
    """
    Пересчитать односторонние границы вложенным bootstrap по работам и страницам.

    Формулы выбираются вместе с каждым экземпляром своей страницы.
    Пустой знаменатель даёт ``None`` и не участвует в перцентиле.
    """

    if replicates <= 0:
        raise ValueError("Число bootstrap-реплик должно быть положительным")

    pages_by_work: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for page in pages:
        work_id = page.get("work_id")

        if isinstance(work_id, str):
            pages_by_work[work_id].append(page)

    for work_pages in pages_by_work.values():
        work_pages.sort(
            key=lambda page: (
                str(page.get("page_sample_id")),
                str(page.get("variant_id")),
                str(page.get("page_qa_record_id")),
            )
        )

    work_ids = sorted(pages_by_work)
    values: dict[str, list[float]] = {
        metric_name: []
        for metric_name in set(UPPER_BOUND_METRICS.values())
        | set(LOWER_BOUND_METRICS.values())
    }

    if not work_ids:
        return {
            bound_name: None
            for bound_name in (*UPPER_BOUND_METRICS, *LOWER_BOUND_METRICS)
        }

    formulas_by_page: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)

    for formula in formulas:
        key = (formula.get("page_sample_id"), formula.get("variant_id"))
        formulas_by_page[key].append(formula)

    generator = random.Random(seed)

    for _ in range(replicates):
        sampled_pages: list[dict[str, Any]] = []
        sampled_formulas: list[dict[str, Any]] = []

        for _ in work_ids:
            selected_work_id = generator.choice(work_ids)
            work_pages = pages_by_work[selected_work_id]

            for _ in work_pages:
                selected_page = generator.choice(work_pages)
                sampled_pages.append(selected_page)
                page_key = (
                    selected_page.get("page_sample_id"),
                    selected_page.get("variant_id"),
                )
                sampled_formulas.extend(formulas_by_page.get(page_key, []))

        aggregate = derive_aggregate(sampled_pages, sampled_formulas)

        for metric_name, metric_values in values.items():
            value = aggregate[metric_name]

            if isinstance(value, (int, float)) and math.isfinite(value):
                metric_values.append(float(value))

    result: dict[str, float | None] = {}

    for bound_name, metric_name in UPPER_BOUND_METRICS.items():
        result[bound_name] = percentile(values[metric_name], 0.95)

    for bound_name, metric_name in LOWER_BOUND_METRICS.items():
        result[bound_name] = percentile(values[metric_name], 0.05)

    return result


def percentile(values: list[float], quantile: float) -> float | None:
    """Вычислить линейно интерполированный перцентиль типа 7."""

    if not values:
        return

    if not 0 <= quantile <= 1:
        raise ValueError("Квантиль должен лежать в диапазоне от 0 до 1")

    ordered_values = sorted(values)
    position = (len(ordered_values) - 1) * quantile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)

    if lower_index == upper_index:
        return ordered_values[lower_index]

    fraction = position - lower_index

    return (
        ordered_values[lower_index] * (1 - fraction)
        + ordered_values[upper_index] * fraction
    )


def expected_prose_status(
    aggregate: dict[str, Any],
    run: dict[str, Any],
    aggregate_kind: str,
) -> str:
    """Вывести статус критерия прозы из минимумов выборки и доверительных границ."""

    thresholds = run.get("acceptance_thresholds", {})

    if aggregate_kind == "overall":
        minimum_pages = max(
            run.get("selection_plan", {}).get("target_page_count", 0),
            100,
        )
        minimum_characters = max(
            run.get("selection_plan", {}).get("minimum_prose_characters", 0),
            20000,
        )
    elif aggregate_kind == "source_layout":
        minimum_pages = thresholds.get("minimum_group_pages")
        minimum_characters = thresholds.get("minimum_group_prose_characters")
    else:
        return "insufficient"

    if (
        not isinstance(minimum_pages, int)
        or not isinstance(minimum_characters, int)
        or aggregate.get("page_count", 0) < minimum_pages
        or aggregate.get("prose_characters", 0) < minimum_characters
    ):
        return "insufficient"

    passes = (
        aggregate.get("cer_ci_upper", math.inf)
        <= thresholds.get("maximum_cer_upper_bound", -math.inf)
        and aggregate.get("wer_ci_upper", math.inf)
        <= thresholds.get("maximum_wer_upper_bound", -math.inf)
        and aggregate.get("critical_reading_order_rate_ci_upper", math.inf)
        <= thresholds.get("maximum_critical_reading_order_rate_upper_bound", -math.inf)
    )

    return "pass" if passes else "fail"


def expected_formula_status(
    aggregate: dict[str, Any],
    run: dict[str, Any],
) -> str:
    """Вывести статус критерия формул из квот и доверительных границ."""

    thresholds = run.get("acceptance_thresholds", {})

    if (
        aggregate.get("formula_reference_count", 0)
        < thresholds.get("minimum_formula_occurrences", math.inf)
        or aggregate.get("formula_work_count", 0)
        < thresholds.get("minimum_formula_work_ids", math.inf)
    ):
        return "insufficient"

    lower_bound = aggregate.get("formula_detection_f1_ci_lower")
    damage_upper_bound = aggregate.get("critical_formula_damage_rate_ci_upper")
    passes = (
        isinstance(lower_bound, (int, float))
        and lower_bound >= thresholds.get("minimum_formula_detection_f1_lower_bound", math.inf)
        and isinstance(damage_upper_bound, (int, float))
        and damage_upper_bound
        <= thresholds.get("maximum_critical_formula_damage_rate_upper_bound", -math.inf)
    )

    return "pass" if passes else "fail"


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    """Вернуть отношение или ``None`` при нулевом знаменателе."""

    if denominator == 0:
        return

    return numerator / denominator
