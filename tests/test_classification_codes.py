"""Проверки поиска явно подписанных классификационных кодов."""

from __future__ import annotations

import unittest

from typing import Any

from src.corpus.classification_codes import extract_classification_codes


class ClassificationCodesTests(unittest.TestCase):
    """Проверки извлечения кодов без догадок и восстановления пунктуации."""

    def test_udc_composite_expression_is_not_split(self) -> None:
        """Связи внутри составного УДК должны остаться одним выражением."""

        for code in (
            "575.1:575.224.22:595.773.4:539.1.047",
            "538.9+539.123+539.17",
            "004.056:539.12.04",
            "[539.1+538.9](470)/539.12",
        ):
            with self.subTest(code=code):
                result = extract_classification_codes(f"УДК: {code}")

                self.assertEqual(result[0]["codes"], [code])
                self.assertEqual(result[0]["flags"], [])

    def test_udc_lists_preserve_individual_expressions(self) -> None:
        """Запятые и точки с запятой должны разделять самостоятельные УДК."""

        result = extract_classification_codes("UDC 512.6, 517.9; 519.6.")

        self.assertEqual(result[0]["codes"], ["512.6", "517.9", "519.6"])

    def test_pacs_and_udc_can_share_line(self) -> None:
        """Два явно подписанных классификатора должны извлекаться независимо."""

        line = "PACS: 29.30.Kv, 29.40.Wk, 25.40.Fq. УДК: 539.1.07"
        result = extract_classification_codes(line)

        self.assertEqual([entry["system"] for entry in result], ["pacs", "udc"])
        self.assertEqual(result[0]["codes"], ["29.30.Kv", "29.40.Wk", "25.40.Fq"])
        self.assertEqual(result[1]["codes"], ["539.1.07"])
        self.assertEqual(result[0]["raw_excerpt"], line)

    def test_pacs_signs_and_lowercase_suffix_are_supported(self) -> None:
        """Знаки и строчные суффиксы PACS должны сохраняться без изменения."""

        result = extract_classification_codes("PACS 21.30.-x; 27.50.+z; 61.05.cp.")

        self.assertEqual(result[0]["codes"], ["21.30.-x", "27.50.+z", "61.05.cp"])

    def test_malformed_pacs_is_not_repaired(self) -> None:
        """Пропущенные точки в PACS должны требовать проверки исходника."""

        value = "21.60-n; 21.10-k; 23.90+w."
        result = extract_classification_codes(f"PACS: {value}")

        self.assertEqual(result[0]["codes"], [])
        self.assertEqual(result[0]["raw_value"], value)
        self.assertIn("invalid_code", result[0]["flags"])

    def test_valid_codes_survive_beside_malformed_codes(self) -> None:
        """Понятный элемент списка не должен скрывать предупреждение о соседнем."""

        result = extract_classification_codes("PACS: 14.60.Pq, 21.60-n")

        self.assertEqual(result[0]["codes"], ["14.60.Pq"])
        self.assertEqual(result[0]["flags"], ["invalid_code"])

    def test_rubric_systems_are_kept_separate(self) -> None:
        """ГРНТИ и МРНТИ должны оставаться разными системами даже при общем коде."""

        result = extract_classification_codes(
            "ГРНТИ: 29.15.19, 29.19.29\nМРНТИ 29.15.19\n"
            "GRNTI 29.05.03\nMRNTI: 29.05.03"
        )

        self.assertEqual(
            [entry["field"] for entry in result],
            ["grnti_codes_raw", "mrnti_codes_raw", "grnti_codes_raw", "mrnti_codes_raw"],
        )
        self.assertEqual(result[0]["codes"], ["29.15.19", "29.19.29"])

    def test_unknown_irsti_alias_is_not_assumed(self) -> None:
        """Неутверждённое соответствие IRSTI системе МРНТИ не должно угадываться."""

        self.assertEqual(extract_classification_codes("IRSTI 29.15.19"), [])

    def test_bare_codes_prose_years_and_references_are_ignored(self) -> None:
        """Поиск должен отличать подпись кода от чисел и упоминаний в тексте."""

        text = (
            "29.15.19\n2024\n[1] PACS 14.60.Pq\n"
            "Обсуждается УДК 539.1\nБудкера\nпредрассудки\n"
            "PACS используется для классификации\nУДК рассматривается далее"
        )

        self.assertEqual(extract_classification_codes(text), [])

    def test_code_continuation_preserves_exact_excerpt(self) -> None:
        """Перенос через пустую строку должен сохранить исходный отрывок."""

        text = "Заголовок\r\nУДК:\r\n\r\n539.1\r\nАннотация"
        result = extract_classification_codes(text)

        self.assertEqual(result[0]["codes"], ["539.1"])
        self.assertEqual(result[0]["line_start"], 2)
        self.assertEqual(result[0]["line_end"], 4)
        self.assertEqual(result[0]["raw_excerpt"], "УДК:\r\n\r\n539.1\r\n")

    def test_form_feed_counts_as_line_boundary(self) -> None:
        """Разрыв страницы и CRLF должны давать воспроизводимые номера строк."""

        result = extract_classification_codes("Первая\r\nстраница\fМРНТИ: 29.15.19")

        self.assertEqual(result[0]["line_start"], 3)
        self.assertEqual(result[0]["line_end"], 3)

    def test_marker_without_code_does_not_consume_prose(self) -> None:
        """После пустой подписи обычный текст не должен превращаться в код."""

        result = extract_classification_codes("PACS:\n2024 год: результаты исследования")

        self.assertEqual(result[0]["codes"], [])
        self.assertEqual(result[0]["flags"], ["missing_code"])
        self.assertEqual(result[0]["line_end"], 1)

    def test_continuation_search_is_bounded(self) -> None:
        """Поиск после подписи не должен перескакивать через несколько пустых строк."""

        result = extract_classification_codes("ГРНТИ\n\n\n29.15.19")

        self.assertEqual(result[0]["codes"], [])
        self.assertEqual(result[0]["line_end"], 1)

    def test_unexpected_tail_is_not_silently_truncated(self) -> None:
        """Код вместе с посторонним текстом должен оставаться сомнительной находкой."""

        result = extract_classification_codes("УДК 539.1 дополнительное описание")

        self.assertEqual(result[0]["codes"], [])
        self.assertIn("unexpected_text", result[0]["flags"])
        self.assertEqual(result[0]["raw_value"], "539.1 дополнительное описание")

    def test_digits_are_not_truncated_to_match(self) -> None:
        """Нельзя извлекать правильный префикс из более длинной неверной записи."""

        for line in ("ГРНТИ 29.15.199", "УДК 539.171.4539.3", "PACS 14.60.Pq1"):
            with self.subTest(line=line):
                result = extract_classification_codes(line)

                self.assertEqual(result[0]["codes"], [])
                self.assertIn("invalid_code", result[0]["flags"])

    def test_unbalanced_udc_expression_is_flagged(self) -> None:
        """Незакрытая скобка УДК должна оставаться в исходнике с предупреждением."""

        result = extract_classification_codes("УДК [539.1+538.9")

        self.assertEqual(result[0]["codes"], [])
        self.assertIn("invalid_code", result[0]["flags"])

    def test_long_malformed_udc_does_not_require_exponential_search(self) -> None:
        """Длинные неверные УДК должны проверяться без неоднозначного перебора."""

        for value in (
            "1" * 200 + "X",
            "1" * 200,
            "1.1" * 100 + "X",
            "1." * 100 + ".",
        ):
            with self.subTest(value=value):
                result = extract_classification_codes(f"УДК {value}")

                self.assertEqual(result[0]["codes"], [])
                self.assertEqual(result[0]["raw_value"], value)
                self.assertIn("invalid_code", result[0]["flags"])

    def test_confusable_pacs_marker_preserves_raw_spelling(self) -> None:
        """Похожие кириллические буквы допускаются лишь в явной подписи PACS."""

        line = "РАСS: 14.60.Pq"
        result = extract_classification_codes(line)

        self.assertEqual(result[0]["system"], "pacs")
        self.assertEqual(result[0]["codes"], ["14.60.Pq"])
        self.assertEqual(result[0]["raw_excerpt"], line)
        self.assertIn("marker_confusable", result[0]["flags"])

    def test_confusable_code_suffix_is_not_transliterated(self) -> None:
        """Сами коды с похожими кириллическими буквами не должны исправляться."""

        result = extract_classification_codes("PACS 14.60.Рq")

        self.assertEqual(result[0]["codes"], [])
        self.assertEqual(result[0]["raw_value"], "14.60.Рq")

    def test_output_fields_are_stable(self) -> None:
        """Находка должна содержать только согласованные поля протокола."""

        result = extract_classification_codes("УДК 539.1")

        self.assertEqual(set(result[0]), {
            "system", "field", "codes", "raw_value", "line_start", "line_end",
            "raw_excerpt", "flags",
        })

    def test_repeated_codes_are_deduplicated_within_group(self) -> None:
        """Повтор кода внутри подписи не должен терять исходный текст списка."""

        result = extract_classification_codes("ГРНТИ 29.15.19, 29.15.19")

        self.assertEqual(result[0]["codes"], ["29.15.19"])
        self.assertEqual(result[0]["raw_value"], "29.15.19, 29.15.19")

    def test_empty_text_returns_no_findings(self) -> None:
        """Пустой текст не должен создавать фиктивные находки."""

        self.assertEqual(extract_classification_codes(""), [])

    def test_non_text_input_is_rejected(self) -> None:
        """Неверный тип аргумента должен приводить к понятной ошибке."""

        invalid_text: Any = None

        with self.assertRaisesRegex(TypeError, "должен быть строкой"):
            extract_classification_codes(invalid_text)


if __name__ == "__main__":
    unittest.main()
