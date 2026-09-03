"""Проверки точечных метрик и bootstrap для контроля качества OCR."""

from __future__ import annotations

import unittest

from typing import Any

from src.corpus.ocr_qa_metrics import (
    nested_work_page_bootstrap,
    percentile,
)


class OcrQaMetricTests(unittest.TestCase):
    """Проверки воспроизводимого расчёта доверительных границ."""

    def test_percentile_uses_linear_type_seven_interpolation(self) -> None:
        """Перцентиль должен соответствовать зафиксированной интерполяции."""

        self.assertEqual(percentile([0.0, 1.0], 0.95), 0.95)
        self.assertEqual(percentile([0.0, 1.0], 0.05), 0.05)

    def test_bootstrap_is_independent_of_jsonl_record_order(self) -> None:
        """Перестановка тех же строк не должна менять доверительные границы."""

        pages = [
            self._page("work-b", "page-b2", errors=4),
            self._page("work-a", "page-a1", errors=0),
            self._page("work-b", "page-b1", errors=2),
            self._page("work-a", "page-a2", errors=1),
        ]
        formulas = [
            self._formula("work-a", "page-a1", "true_positive"),
            self._formula("work-b", "page-b1", "false_negative"),
            self._formula("work-b", "page-b2", "false_positive"),
        ]

        direct = nested_work_page_bootstrap(
            pages,
            formulas,
            replicates=2000,
            seed=20260901,
        )
        reordered = nested_work_page_bootstrap(
            list(reversed(pages)),
            list(reversed(formulas)),
            replicates=2000,
            seed=20260901,
        )

        self.assertEqual(reordered, direct)

    def test_false_positive_only_bootstrap_has_zero_f1_lower_bound(self) -> None:
        """Реплики с одними ложными срабатываниями не должны выпадать из F1."""

        pages = [self._page("work-a", "page-a1", errors=0)]
        formulas = [
            self._formula("work-a", "page-a1", "false_positive")
        ]

        bounds = nested_work_page_bootstrap(
            pages,
            formulas,
            replicates=100,
            seed=20260901,
        )

        self.assertEqual(bounds["formula_detection_f1_ci_lower"], 0.0)

    def _page(
        self,
        work_id: str,
        page_sample_id: str,
        *,
        errors: int,
    ) -> dict[str, Any]:
        """Построить минимальную страницу для расчёта агрегатов."""

        return {
            "page_qa_record_id": f"record-{page_sample_id}",
            "page_sample_id": page_sample_id,
            "variant_id": "variant-a",
            "work_id": work_id,
            "extraction_outcome": "succeeded",
            "reference_characters": 100,
            "character_substitutions": errors,
            "character_deletions": 0,
            "character_insertions": 0,
            "reference_words": 20,
            "word_substitutions": errors,
            "word_deletions": 0,
            "word_insertions": 0,
            "reading_order_status": "correct",
        }

    def _formula(
        self,
        work_id: str,
        page_sample_id: str,
        match_status: str,
    ) -> dict[str, Any]:
        """Построить минимальную формульную запись для bootstrap."""

        return {
            "page_sample_id": page_sample_id,
            "variant_id": "variant-a",
            "work_id": work_id,
            "match_status": match_status,
            "critical_damage": match_status == "false_negative",
        }


if __name__ == "__main__":
    unittest.main()
