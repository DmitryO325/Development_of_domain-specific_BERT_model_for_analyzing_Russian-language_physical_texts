"""Проверки нормализации и точного расчёта CER и WER."""

from __future__ import annotations

import tempfile
import unittest

from itertools import product
from pathlib import Path

from src.corpus.ocr_qa_distance import (
    OCR_METRIC_VERSION,
    OCR_PROSE_NORMALIZATION_VERSION,
    calculate_ocr_metrics,
    calculate_ocr_metrics_from_files,
    levenshtein_counts,
    normalize_ocr_prose,
    tokenize_wer_words,
)


class OcrProseNormalizationTests(unittest.TestCase):
    """Проверки ``ocr-prose-norm-v1`` и токенизации WER."""

    def test_normalization_applies_nfc_line_endings_and_whitespace(self) -> None:
        """Шаги протокола должны применяться по порядку."""

        text = " \r\nи\u0306он\u00adный\t  текст\rёж\n "

        self.assertEqual(normalize_ocr_prose(text), "йонный текст ёж")

    def test_normalization_does_not_guess_line_break_hyphens(self) -> None:
        """Ложный перенос должен обрабатывать человек."""

        self.assertEqual(normalize_ocr_prose("кван-\nтовая"), "кван- товая")

    def test_normalization_preserves_significant_spelling(self) -> None:
        """Значимые особенности написания должны сохраняться."""

        text = "Ё-мода, е-мода!"

        self.assertEqual(normalize_ocr_prose(text), text)

    def test_word_tokenization_uses_normalized_single_spaces(self) -> None:
        """Пунктуация должна оставаться частью слова."""

        self.assertEqual(
            tokenize_wer_words("  Теория,\nполя!  "),
            ("Теория,", "поля!"),
        )
        self.assertEqual(tokenize_wer_words(" \n\t "), ())


class LevenshteinCountTests(unittest.TestCase):
    """Проверки счётчиков ошибок Левенштейна."""

    def test_empty_sequences_have_no_errors(self) -> None:
        """Две пустые строки должны иметь нулевое расстояние."""

        result = levenshtein_counts("", "")

        self.assertEqual(result.distance, 0)
        self.assertEqual(result.substitutions, 0)
        self.assertEqual(result.deletions, 0)
        self.assertEqual(result.insertions, 0)
        self.assertIsNone(result.error_rate)

    def test_substitution_is_counted(self) -> None:
        """Отличающийся символ должен давать одну замену."""

        result = levenshtein_counts("кот", "кит")

        self.assertEqual(
            (result.substitutions, result.deletions, result.insertions),
            (1, 0, 0),
        )

    def test_deletion_is_counted(self) -> None:
        """Пропуск символа должен давать одно удаление."""

        result = levenshtein_counts("коты", "кот")

        self.assertEqual(
            (result.substitutions, result.deletions, result.insertions),
            (0, 1, 0),
        )

    def test_insertion_is_counted(self) -> None:
        """Лишний символ должен давать одну вставку."""

        result = levenshtein_counts("кот", "коты")

        self.assertEqual(
            (result.substitutions, result.deletions, result.insertions),
            (0, 0, 1),
        )

    def test_ambiguous_alignment_has_deterministic_counts(self) -> None:
        """Равные пути должны стабильно предпочитать замены."""

        expected = (2, 0, 0)

        for _ in range(20):
            result = levenshtein_counts("ab", "ba")

            self.assertEqual(
                (result.substitutions, result.deletions, result.insertions),
                expected,
            )

    def test_all_small_binary_strings_match_reference_distance(self) -> None:
        """Быстрый расчёт должен совпадать с простой матрицей."""

        values = [
            "".join(units)
            for length in range(5)
            for units in product("ab", repeat=length)
        ]

        for reference in values:
            for candidate in values:
                with self.subTest(reference=reference, candidate=candidate):
                    result = levenshtein_counts(reference, candidate)

                    self.assertEqual(
                        result.distance,
                        self._reference_distance(reference, candidate),
                    )
                    self.assertEqual(
                        (
                            result.substitutions,
                            result.deletions,
                            result.insertions,
                        ),
                        self._reference_counts(reference, candidate),
                    )

    def _reference_distance(self, reference: str, candidate: str) -> int:
        """Вычислить расстояние независимой матрицей."""

        previous = list(range(len(candidate) + 1))

        for reference_index, reference_unit in enumerate(reference, start=1):
            current = [reference_index]

            for candidate_index, candidate_unit in enumerate(candidate, start=1):
                current.append(
                    min(
                        previous[candidate_index] + 1,
                        current[candidate_index - 1] + 1,
                        previous[candidate_index - 1]
                        + (reference_unit != candidate_unit),
                    )
                )

            previous = current

        return previous[-1]

    def _reference_counts(
        self,
        reference: str,
        candidate: str,
    ) -> tuple[int, int, int]:
        """Посчитать ошибки независимой полной матрицей."""

        previous = [
            (0, 0, candidate_index)
            for candidate_index in range(len(candidate) + 1)
        ]

        for reference_index, reference_unit in enumerate(reference, start=1):
            current = [(0, reference_index, 0)]

            for candidate_index, candidate_unit in enumerate(candidate, start=1):
                diagonal = previous[candidate_index - 1]

                if reference_unit == candidate_unit:
                    current.append(diagonal)
                    continue

                substitution = (
                    diagonal[0] + 1,
                    diagonal[1],
                    diagonal[2],
                )
                deleted = previous[candidate_index]
                deletion = (deleted[0], deleted[1] + 1, deleted[2])
                inserted = current[candidate_index - 1]
                insertion = (inserted[0], inserted[1], inserted[2] + 1)
                current.append(
                    min(
                        (substitution, deletion, insertion),
                        key=self._count_rank,
                    )
                )

            previous = current

        return previous[-1]

    def _count_rank(self, counts: tuple[int, int, int]) -> tuple[int, int, int]:
        """Построить ключ того же порядка выбора пути."""

        substitutions, deletions, insertions = counts

        return (
            substitutions + deletions + insertions,
            deletions + insertions,
            deletions,
        )


class OcrMetricCalculationTests(unittest.TestCase):
    """Проверки связанного расчёта CER и WER."""

    def test_metrics_use_unicode_code_points_and_space_tokens(self) -> None:
        """CER и WER должны исходить из одной нормализованной пары."""

        result = calculate_ocr_metrics(
            "квантовая теория поля",
            "квантовая теория сильного поля",
        )

        self.assertEqual(
            result.normalization_version,
            OCR_PROSE_NORMALIZATION_VERSION,
        )
        self.assertEqual(result.metric_version, OCR_METRIC_VERSION)
        self.assertEqual(result.words.reference_length, 3)
        self.assertEqual(result.words.insertions, 1)
        self.assertEqual(result.wer, 1 / 3)
        self.assertEqual(
            result.cer,
            result.characters.distance / result.characters.reference_length,
        )

    def test_nfc_equivalent_texts_have_zero_error(self) -> None:
        """Unicode-строки в NFC не должны давать ошибку."""

        result = calculate_ocr_metrics("и\u0306он", "йон")

        self.assertEqual(result.cer, 0.0)
        self.assertEqual(result.wer, 0.0)

    def test_empty_reference_has_undefined_rates(self) -> None:
        """Нулевой знаменатель должен давать ``None``."""

        empty_pair = calculate_ocr_metrics("", "")
        insertion_pair = calculate_ocr_metrics("", "поле")

        self.assertIsNone(empty_pair.cer)
        self.assertIsNone(empty_pair.wer)
        self.assertEqual(insertion_pair.characters.insertions, 4)
        self.assertEqual(insertion_pair.words.insertions, 1)
        self.assertIsNone(insertion_pair.cer)
        self.assertIsNone(insertion_pair.wer)

    def test_empty_candidate_has_unit_error_rates(self) -> None:
        """Полный пропуск эталона должен давать CER=WER=1."""

        result = calculate_ocr_metrics("квантовое поле", "")

        self.assertEqual(result.cer, 1.0)
        self.assertEqual(result.wer, 1.0)
        self.assertEqual(
            result.characters.deletions,
            result.characters.reference_length,
        )
        self.assertEqual(result.words.deletions, 2)

    def test_file_interface_reads_utf8_texts(self) -> None:
        """Файловый и строковый интерфейсы должны совпадать."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reference_path = root / "reference.txt"
            candidate_path = root / "candidate.txt"
            reference_path.write_text("теория\r\nполя", encoding="utf-8")
            candidate_path.write_text("теория поля", encoding="utf-8")

            from_files = calculate_ocr_metrics_from_files(
                reference_path,
                candidate_path,
            )
            from_strings = calculate_ocr_metrics(
                "теория\r\nполя",
                "теория поля",
            )

        self.assertEqual(from_files, from_strings)


if __name__ == "__main__":
    unittest.main()
