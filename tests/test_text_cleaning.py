"""Проверки сохранности научного текста при консервативной очистке."""

from __future__ import annotations

import unittest

from dataclasses import FrozenInstanceError
from typing import Any

from src.preprocess.text_cleaning import CLEANING_VERSION, LineExclusion, clean_pages


class TextCleaningTests(unittest.TestCase):
    """Проверки правил очистки и журнала решений без внешних данных."""

    def test_normalization_preserves_layout_and_final_line_break(self) -> None:
        """Нормализация сохраняет отступы, таблицы и исходный конечный LF."""

        result = clean_pages([(1, "  и\u0306\u00a0  значение  \r\nA    B\r\n\r")])

        self.assertEqual(result.text, "  й   значение\nA    B\n\n")
        self.assertEqual(
            {change.code for change in result.changes},
            {
                "normalize_unicode",
                "replace_nbsp",
                "trim_trailing_spaces",
                "normalize_line_endings",
            },
        )
        self.assertFalse(result.issues)

    def test_formulas_and_real_hyphens_remain_literal(self) -> None:
        """Знаки формул и научные дефисы не заменяются и не угадываются."""

        source = (
            "научно-технический, протон–протонный, α−β, x—y\n"
            "E = mc²; H₂O; 10⁻³; ∑ᵢ xᵢ; ℏω ≥ 0\n"
            "x -\ny = 3\n"
            "A    B    C"
        )

        result = clean_pages([(1, source)])

        self.assertEqual(result.text, source)
        self.assertFalse(result.changes)
        self.assertFalse(result.issues)

    def test_soft_hyphen_joins_letters_without_losing_case(self) -> None:
        """Явный мягкий перенос склеивает кириллицу и латиницу с регистром."""

        result = clean_pages([(2, "Экспери\u00ad\nмент и MICRO\u00ad\nSCOPE.")])

        self.assertEqual(result.text, "Эксперимент и MICROSCOPE.")
        self.assertEqual(
            [(change.line_number, change.code) for change in result.changes],
            [(1, "join_soft_hyphen"), (2, "join_soft_hyphen")],
        )
        self.assertFalse(result.issues)

    def test_soft_hyphen_outside_letter_boundary_is_preserved(self) -> None:
        """Внутренний мягкий перенос и сомнительные границы не удаляются."""

        source = "а\u00adb\n2\u00ad\nтекст\nтекст\u00ad\n  новый\nконец\u00ad"

        result = clean_pages([(1, source)])

        self.assertEqual(result.text, source)
        self.assertEqual(len(result.issues), 4)
        self.assertTrue(
            all(issue.code == "unresolved_soft_hyphen" for issue in result.issues)
        )

    def test_displaced_drop_cap_is_not_joined(self) -> None:
        """Буквица после строчного обрывка не становится продолжением слова."""

        source = "\n" * 24 + "крупнейший в мире уско\u00ad\nВ"
        result = clean_pages([(3, source)])

        self.assertEqual(result.text, source)
        self.assertFalse(result.changes)
        self.assertEqual(
            [(issue.page_number, issue.line_number, issue.code) for issue in result.issues],
            [(3, 25, "unresolved_soft_hyphen")],
        )

    def test_rejected_soft_join_keeps_later_source_line_numbers(self) -> None:
        """Отказ от подозрительной склейки не мешает последующим переносам."""

        result = clean_pages([(1, "уско\u00ad\nВ\nЭкспери\u00ad\nмент\n\ufffd")])

        self.assertEqual(result.text, "уско\u00ad\nВ\nЭксперимент\n\ufffd")
        self.assertEqual([(change.line_number, change.code) for change in result.changes], [(3, "join_soft_hyphen")])
        self.assertEqual(
            {(issue.line_number, issue.code) for issue in result.issues},
            {(1, "unresolved_soft_hyphen"), (5, "replacement_character")},
        )

    def test_ascii_hyphen_requires_explicit_full_word(self) -> None:
        """Без явного слова настоящий дефис и перенос остаются различимыми."""

        source = "научно-\nтехнический текст, экспери-\nмент."
        original = clean_pages([(1, source)])
        permitted = clean_pages(
            [(1, source)], join_words=frozenset({"ЭКСПЕРИМЕНТ"})
        )

        self.assertEqual(original.text, source)
        self.assertEqual(len(original.issues), 2)
        self.assertEqual(permitted.text, "научно-\nтехнический текст, эксперимент.")
        self.assertEqual(
            [issue.code for issue in permitted.issues], ["ambiguous_line_hyphen"]
        )
        self.assertEqual(permitted.changes[0].code, "join_allowlisted_word")
        self.assertEqual(permitted.changes[0].line_number, 2)

    def test_allowlist_does_not_match_partial_or_indented_words(self) -> None:
        """Разрешение относится к целому слову без догадок об отступах."""

        source = "предэкспери-\nмент\nэкспери-\nментальный\nэкспери-\n мент"

        result = clean_pages([(1, source)], join_words=frozenset({"эксперимент"}))

        self.assertEqual(result.text, source)
        self.assertEqual(len(result.issues), 3)
        self.assertFalse(result.changes)

    def test_allowlist_normalizes_unicode_and_keeps_source_case(self) -> None:
        """Список разрешений учитывает NFC и регистр без переписывания текста."""

        result = clean_pages(
            [(1, "ЁЛ-\nКА, маи\u0306-\nский")],
            join_words=frozenset({"ёлка", "МАИ\u0306СКИЙ"}),
        )

        self.assertEqual(result.text, "ЁЛКА, майский")

    def test_unreadable_characters_are_preserved_and_reported(self) -> None:
        """Символы потери данных сохраняются вместе с кодом и исходной строкой."""

        source = "обычно\r\n\x00\x0b\x0c\x1f\x7f\u0085\t\ue000\U000f0000\ufffd  "

        result = clean_pages([(5, source)])

        self.assertEqual(result.text, source.replace("\r\n", "\n").rstrip(" "))
        self.assertEqual(len(result.issues), 10)
        self.assertTrue(all(issue.line_number == 2 for issue in result.issues))
        self.assertTrue(all(issue.page_number == 5 for issue in result.issues))
        self.assertIn("U+0000", result.issues[0].detail)
        self.assertIn("U+F0000", result.issues[-2].detail)
        self.assertEqual(
            {issue.code for issue in result.issues},
            {
                "control_character",
                "tab_character",
                "private_use_character",
                "replacement_character",
            },
        )

    def test_issues_keep_source_lines_after_joining(self) -> None:
        """Номера строк диагностики не сдвигаются после склейки переноса."""

        result = clean_pages([(1, "пер\u00ad\nвая\n\ufffd")])

        self.assertEqual(result.text, "первая\n\ufffd")
        self.assertEqual(result.issues[0].line_number, 3)

    def test_page_boundaries_are_never_joined(self) -> None:
        """Разрешённое слово не даёт права склеить физические страницы."""

        for hyphen in ("-", "\u00ad"):
            with self.subTest(hyphen=hyphen):
                result = clean_pages(
                    [(3, f"экспери{hyphen}"), (4, "мент")],
                    join_words=frozenset({"эксперимент"}),
                )

                self.assertEqual(result.text, f"экспери{hyphen}\n\nмент")
                self.assertEqual([page.page_number for page in result.pages], [3, 4])
                self.assertIn(
                    "page_boundary_hyphen", [issue.code for issue in result.issues]
                )
                self.assertFalse(result.changes)

    def test_tabs_and_unrecognized_spaces_are_not_stripped(self) -> None:
        """Табы и специальные пробелы не исчезают при очистке хвоста строки."""

        source = "  таблица\t\nстрока\u202f\nпоследняя\t  "

        result = clean_pages([(1, source)])

        self.assertEqual(result.text, "  таблица\t\nстрока\u202f\nпоследняя\t")
        self.assertEqual([issue.code for issue in result.issues], ["tab_character"] * 2)

    def test_joining_one_soft_hyphen_does_not_hide_another(self) -> None:
        """Склейка конца строки не скрывает оставшийся внутренний маркер."""

        result = clean_pages([(1, "сло\u00adво\u00ad\nформа")])

        self.assertEqual(result.text, "сло\u00adвоформа")
        self.assertEqual([issue.code for issue in result.issues], ["unresolved_soft_hyphen"])

    def test_empty_pages_and_empty_input_are_preserved(self) -> None:
        """Пустые страницы не исчезают из постраничного результата."""

        result = clean_pages([(1, "первая"), (2, ""), (4, "третья")])

        self.assertEqual(result.text, "первая\n\n\n\nтретья")
        self.assertEqual(len(result.pages), 3)
        self.assertEqual(result.pages[1].text, "")
        self.assertEqual(clean_pages([]).text, "")
        self.assertEqual(clean_pages([]).pages, ())

    def test_excluded_line_is_checked_and_recorded_before_normalization(self) -> None:
        """Точное тело исключения не нормализуется и не создаёт замечаний."""

        header = "и\u0306\u00a0\t\x0b\x0c\u0085\ue000\ufffd  "
        source = f"Введение\n{header}\r\nСохранено\ufffd  \n"
        exclusion = LineExclusion(4, 2, header, "Проверенный колонтитул")

        result = clean_pages([(4, source)], exclude_lines=(exclusion,))

        self.assertEqual(result.text, "Введение\n\nСохранено\ufffd\n")
        self.assertEqual(
            [
                (change.line_number, change.code, change.before, change.after)
                for change in result.changes
            ],
            [
                (2, "remove_explicit_line", header, ""),
                (2, "normalize_line_endings", "\r\n", "\n"),
                (3, "trim_trailing_spaces", "Сохранено\ufffd  ", "Сохранено\ufffd"),
            ],
        )
        self.assertEqual(
            [(issue.page_number, issue.line_number, issue.code) for issue in result.issues],
            [(4, 3, "replacement_character")],
        )

    def test_line_exclusion_preserves_first_last_and_final_line_breaks(self) -> None:
        """Первые и последние колонтитулы оставляют пустые строки на своих местах."""

        exclusions = (
            LineExclusion(2, 1, "Шапка", "Верхний колонтитул"),
            LineExclusion(2, 3, "Подвал", "Нижний колонтитул"),
        )

        for line_ending in ("\n", "\r\n", "\r"):
            with self.subTest(line_ending=line_ending):
                source = line_ending.join(("Шапка", "Основной текст", "Подвал", ""))
                result = clean_pages([(2, source)], exclude_lines=exclusions)

                self.assertEqual(result.text, "\nОсновной текст\n\n")
                self.assertEqual(
                    [
                        change.line_number
                        for change in result.changes
                        if change.code == "remove_explicit_line"
                    ],
                    [1, 3],
                )
                self.assertEqual(
                    sum(change.code == "normalize_line_endings" for change in result.changes),
                    0 if line_ending == "\n" else 3,
                )
                self.assertEqual(
                    result,
                    clean_pages([(2, source)], exclude_lines=tuple(reversed(exclusions))),
                )
                self.assertFalse(result.issues)

    def test_excluded_line_blocks_ascii_and_soft_hyphen_joins(self) -> None:
        """Пустая заглушка не позволяет склеить текст через удалённый колонтитул."""

        for hyphen, issue_code in (
            ("-", "ambiguous_line_hyphen"),
            ("\u00ad", "unresolved_soft_hyphen"),
        ):
            with self.subTest(hyphen=hyphen):
                result = clean_pages(
                    [(1, f"экспери{hyphen}\nШапка\nмент\n\ufffd")],
                    join_words=frozenset({"эксперимент"}),
                    exclude_lines=(LineExclusion(1, 2, "Шапка", "Колонтитул"),),
                )

                self.assertEqual(result.text, f"экспери{hyphen}\n\nмент\n\ufffd")
                self.assertEqual(
                    [(change.line_number, change.code) for change in result.changes],
                    [(2, "remove_explicit_line")],
                )
                self.assertEqual(
                    [(issue.line_number, issue.code) for issue in result.issues],
                    [(1, issue_code), (4, "replacement_character")],
                )

    def test_excluded_last_line_without_final_separator_keeps_its_position(self) -> None:
        """Последняя строка без конечного перевода оставляет пустую заглушку."""

        result = clean_pages(
            [(1, "Основной текст\rПодвал")],
            exclude_lines=(LineExclusion(1, 2, "Подвал", "Нижний колонтитул"),),
        )

        self.assertEqual(result.text, "Основной текст\n")
        self.assertEqual(
            [(change.line_number, change.code) for change in result.changes],
            [(1, "normalize_line_endings"), (2, "remove_explicit_line")],
        )
        self.assertFalse(result.issues)

    def test_exclusion_keeps_source_numbers_after_neighboring_joins(self) -> None:
        """Удаление и склейки сохраняют исходные номера последующих изменений."""

        result = clean_pages(
            [(1, "пер\u00ad\nвая\nШапка\nэкспери-\nмент\n\ufffd  ")],
            join_words=frozenset({"эксперимент"}),
            exclude_lines=(LineExclusion(1, 3, "Шапка", "Колонтитул"),),
        )

        self.assertEqual(result.text, "первая\n\nэксперимент\n\ufffd")
        self.assertEqual(
            [(change.line_number, change.code) for change in result.changes],
            [
                (3, "remove_explicit_line"),
                (6, "trim_trailing_spaces"),
                (1, "join_soft_hyphen"),
                (4, "join_allowlisted_word"),
            ],
        )
        self.assertEqual(
            [(issue.line_number, issue.code) for issue in result.issues],
            [(6, "replacement_character")],
        )

    def test_exclusion_does_not_join_physical_pages(self) -> None:
        """Удаление колонтитулов на стыке страниц не разрешает межстраничную склейку."""

        result = clean_pages(
            [(3, "экспери-\nПодвал"), (5, "Шапка\nмент")],
            join_words=frozenset({"эксперимент"}),
            exclude_lines=(
                LineExclusion(3, 2, "Подвал", "Нижний колонтитул"),
                LineExclusion(5, 1, "Шапка", "Верхний колонтитул"),
            ),
        )

        self.assertEqual(result.text, "экспери-\n\n\n\nмент")
        self.assertEqual([page.page_number for page in result.pages], [3, 5])
        self.assertIn(
            (3, 1, "page_boundary_hyphen"),
            [(issue.page_number, issue.line_number, issue.code) for issue in result.issues],
        )
        self.assertTrue(all(change.code == "remove_explicit_line" for change in result.changes))

    def test_empty_exclusions_preserve_v2_text_and_journal(self) -> None:
        """Пустой список исключений сохраняет прежний текст и порядок журнала v2."""

        pages = [(2, "  и\u0306\u00a0  \r\nэкспери-\rмент\n\x0b\ufffd\t  "), (5, "конец")]
        result = clean_pages(
            pages, join_words=frozenset({"эксперимент"}), exclude_lines=()
        )

        self.assertEqual(result.text.encode("utf-8"), "  й\nэксперимент\n\x0b\ufffd\t\n\nконец".encode("utf-8"))
        self.assertEqual(
            [
                (change.page_number, change.line_number, change.code, change.before, change.after)
                for change in result.changes
            ],
            [
                (2, 1, "normalize_unicode", "  и\u0306\u00a0  ", "  й\u00a0  "),
                (2, 1, "replace_nbsp", "  й\u00a0  ", "  й   "),
                (2, 1, "trim_trailing_spaces", "  й   ", "  й"),
                (2, 1, "normalize_line_endings", "\r\n", "\n"),
                (2, 2, "normalize_line_endings", "\r", "\n"),
                (2, 4, "trim_trailing_spaces", "\x0b\ufffd\t  ", "\x0b\ufffd\t"),
                (2, 2, "join_allowlisted_word", "экспери-\nмент", "эксперимент"),
            ],
        )
        self.assertEqual(
            [(issue.page_number, issue.line_number, issue.code) for issue in result.issues],
            [(2, 4, "control_character"), (2, 4, "replacement_character"), (2, 4, "tab_character")],
        )
        self.assertEqual(result, clean_pages(pages, join_words=frozenset({"эксперимент"})))

    def test_exclusion_requires_exact_original_text(self) -> None:
        """Нормализованная копия не подменяет точное совпадение исходной строки."""

        source = "Основной текст\nи\u0306\u00a0  "

        for expected_text in ("й\u00a0  ", "и\u0306   ", "и\u0306\u00a0"):
            with self.subTest(expected_text=expected_text), self.assertRaisesRegex(
                ValueError, "не совпадает с expected_text"
            ):
                clean_pages(
                    [(1, source)],
                    exclude_lines=(LineExclusion(1, 2, expected_text, "Колонтитул"),),
                )

    def test_invalid_exclusion_container_and_records_are_rejected(self) -> None:
        """Исключения принимаются только как кортеж типизированных записей."""

        exclusion = LineExclusion(1, 1, "Шапка", "Колонтитул")
        invalid_exclusions: list[Any] = [
            None, [], [exclusion], {exclusion}, "строка", (None,), ({},), ((1, 1, "Шапка", "Причина"),),
        ]

        for exclusions in invalid_exclusions:
            with self.subTest(exclusions=exclusions), self.assertRaises(ValueError):
                clean_pages([(1, "Шапка\nТекст")], exclude_lines=exclusions)

    def test_invalid_exclusion_fields_are_rejected(self) -> None:
        """Номера без bool и непустые строки проверяются до обращения к страницам."""

        valid_fields: dict[str, Any] = {
            "page_number": 1,
            "line_number": 1,
            "expected_text": "Шапка",
            "reason": "Колонтитул",
        }
        invalid_fields: dict[str, list[Any]] = {
            "page_number": [True, False, 0, -1, 1.0, "1", None],
            "line_number": [True, False, 0, -1, 1.0, "1", None],
            "expected_text": [None, 1, "", " \t\u00a0", "Шапка\n", "Шапка\r", "Шапка\r\nТекст"],
            "reason": [None, 1, "", " \t\u00a0\n"],
        }

        for field_name, values in invalid_fields.items():
            for value in values:
                with self.subTest(field_name=field_name, value=value), self.assertRaises(ValueError):
                    exclusion = LineExclusion(**{**valid_fields, field_name: value})
                    clean_pages([(1, "Шапка\nТекст")], exclude_lines=(exclusion,))

    def test_unknown_and_duplicate_exclusion_positions_are_rejected(self) -> None:
        """Несуществующие страницы, строки и повторные координаты отклоняются."""

        exclusion = LineExclusion(2, 1, "Шапка", "Колонтитул")
        invalid_exclusions = (
            ((LineExclusion(1, 1, "Шапка", "Причина"),), "неизвестную страницу 1"),
            ((LineExclusion(2, 3, "Шапка", "Причина"),), "отсутствует строка 3"),
            ((exclusion, exclusion), "Повторное исключение строки 1 страницы 2"),
            ((exclusion, LineExclusion(2, 1, "Шапка", "Другая причина")), "Повторное исключение"),
        )

        for exclusions, message in invalid_exclusions:
            with self.subTest(exclusions=exclusions), self.assertRaisesRegex(ValueError, message):
                clean_pages([(2, "Шапка\nТекст")], exclude_lines=exclusions)

        with self.assertRaisesRegex(ValueError, "неизвестную страницу"):
            clean_pages([], exclude_lines=(exclusion,))

    def test_exclusions_cannot_empty_a_nonempty_page(self) -> None:
        """Нельзя оставить от содержательной страницы только пустые строки и пробелы."""

        for source in ("Шапка", "Шапка\n", "Шапка\n \t\u00a0\n"):
            with self.subTest(source=source), self.assertRaisesRegex(
                ValueError, "не должны удалять всё содержимое страницы 1"
            ):
                clean_pages(
                    [(1, source)],
                    exclude_lines=(LineExclusion(1, 1, "Шапка", "Колонтитул"),),
                )

        with self.assertRaisesRegex(ValueError, "не должны удалять всё содержимое"):
            clean_pages(
                [(1, "Шапка\nПодвал"), (2, "Другая страница")],
                exclude_lines=(
                    LineExclusion(1, 1, "Шапка", "Верхний колонтитул"),
                    LineExclusion(1, 2, "Подвал", "Нижний колонтитул"),
                ),
            )

    def test_line_exclusion_is_immutable_and_inputs_are_preserved(self) -> None:
        """Очистка не меняет страницы и неизменяемые записи исключений."""

        pages = [(1, "Шапка\nТекст")]
        original_pages = list(pages)
        exclusion = LineExclusion(1, 1, "Шапка", "Колонтитул")

        clean_pages(pages, exclude_lines=(exclusion,))

        self.assertEqual(pages, original_pages)
        self.assertEqual(exclusion, LineExclusion(1, 1, "Шапка", "Колонтитул"))
        self.assertFalse(hasattr(exclusion, "__dict__"))

        # Здесь намеренно нарушаем контракт только для проверки неизменяемости.
        exclusion_for_mutation: Any = exclusion

        with self.assertRaises(FrozenInstanceError):
            exclusion_for_mutation.expected_text = "Другой текст"

    def test_processing_is_reproducible_and_does_not_mutate_input(self) -> None:
        """Повторный запуск и порядок множества не меняют результат."""

        pages = [(1, "экспери-\nмент\r\nA\tB  "), (2, "")]
        original_pages = list(pages)
        first = clean_pages(pages, join_words=frozenset({"эксперимент", "другое"}))
        second = clean_pages(pages, join_words=frozenset({"другое", "эксперимент"}))

        self.assertEqual(first, second)
        self.assertEqual(pages, original_pages)
        self.assertEqual(CLEANING_VERSION, "text-clean-conservative-v3")

        # Здесь намеренно нарушаем контракт только для проверки защиты во время выполнения.
        page_for_mutation: Any = first.pages[0]

        with self.assertRaises(FrozenInstanceError):
            page_for_mutation.text = "изменение"

    def test_invalid_pages_are_rejected(self) -> None:
        """Типы, повторы, порядок и неположительные номера отклоняются."""

        invalid_pages: list[Any] = [
            None, (), "текст", [(True, "текст")], [(0, "текст")],
            [(-1, "текст")], [(1.0, "текст")], [("1", "текст")],
            [(1, None)], [(1, b"text")], [(1, "одна"), (1, "две")],
            [(2, "две"), (1, "одна")], [(1,)], [[1, "текст"]],
        ]

        for pages in invalid_pages:
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                clean_pages(pages)

    def test_invalid_allowlist_is_rejected(self) -> None:
        """Список склеек принимает только неизменяемое множество полных слов."""

        invalid_words: list[Any] = [
            {"слово"}, ["слово"], None, "слово", frozenset({""}),
            frozenset({"два слова"}), frozenset({"научно-технический"}),
            frozenset({1}), frozenset({"123"}),
        ]

        for words in invalid_words:
            with self.subTest(words=words), self.assertRaises(ValueError):
                clean_pages([(1, "текст")], join_words=words)


if __name__ == "__main__":
    unittest.main()
