"""Проверки узкой очистки метаданных H2 без изменения научных обозначений."""

from __future__ import annotations

import unittest

from dataclasses import FrozenInstanceError
from itertools import product
from typing import Any

from src.preprocess.h2_text_cleaning import CLEANING_VERSION, clean_metadata_text


class MetadataTextCleaningTests(unittest.TestCase):
    """Проверить сохранность текста, защиту разметки и воспроизводимость замен."""

    def test_version_is_explicit(self) -> None:
        """Версия правил отделена от очистки полного текста PDF."""

        self.assertEqual(CLEANING_VERSION, "h2-metadata-clean-v1")

    def test_empty_and_ordinary_text(self) -> None:
        """Пустая строка и обычная проза не требуют изменений."""

        for source in ("", "Русский текст без ошибок.", "  Начало текста.  \n"):
            with self.subTest(source=source):
                result = clean_metadata_text(source)
                self.assertEqual(result.text, source)
                self.assertEqual(result.changes, ())
                self.assertEqual(result.issues, ())

    def test_russian_spacing_is_narrow(self) -> None:
        """Повторные пробелы и табуляции сводятся только между целыми словами."""

        source = "  Первый  второй\tтретий\u00a0 четвёртый\u202fпятый.  "
        result = clean_metadata_text(source)

        self.assertEqual(result.text, "  Первый второй третий четвёртый пятый.  ")
        self.assertEqual(len(result.changes), 4)
        self.assertEqual(result.issues, ())

    def test_literal_crlf_candidate_sixty(self) -> None:
        """Буквальный CRLF между словами из карточки 60 не требует unicode_escape."""

        source = r"внешним силовым\r\nцентром системы"
        result = clean_metadata_text(source)

        self.assertEqual(result.text, "внешним силовым центром системы")
        self.assertEqual(result.changes[0].before, r"\r\n")
        self.assertEqual(result.changes[0].code, "literal_crlf_between_words")

    def test_single_real_line_break(self) -> None:
        """Одиночные реальные разделители между словами становятся пробелом."""

        for separator in ("\n", "\r", "\r\n", " \r\n\t"):
            with self.subTest(separator=separator):
                result = clean_metadata_text("Научный" + separator + "текст")
                self.assertEqual(result.text, "Научный текст")

    def test_paragraphs_remain_byte_for_byte(self) -> None:
        """Пустые строки сохраняют границы абзацев и исходные окончания строк."""

        for separator in ("\n\n", "\r\n\r\n", "\r\r", "\n \t\n", "\r\n  \n", r"\r\n\r\n"):
            with self.subTest(separator=separator):
                source = "Первый" + separator + "второй"
                self.assertEqual(clean_metadata_text(source).text, source)

    def test_numbers_symbols_and_unicode_remain_literal(self) -> None:
        """Научные символы, числа, индексы и формы Unicode не нормализуются."""

        source = "KΩÅ и\u0306 α  β 10  20 10⁻³ см⁻² x  y H₂O E = mc²"
        result = clean_metadata_text(source)

        self.assertEqual(result.text, source)
        self.assertEqual(result.changes, ())

    def test_not_whole_russian_words(self) -> None:
        """Латиница, индексы, числа и знаки рядом со словом запрещают замену."""

        for source in (
            "2слова  рядом", "слова  рядом2", "xслова  рядом", "слова  рядомα",
            "αслова  рядом", "слова²  рядом", "слова  рядом_индекс",
            "слова-  рядом", "слова  рядом-потом", "=слова  рядом", "слова  рядом=",
            "слово  и", "я  слово", "слова  рядом\u0301", "(слова  рядом)",
        ):
            with self.subTest(source=source):
                self.assertEqual(clean_metadata_text(source).text, source)

    def test_words_and_hyphens_are_not_restored(self) -> None:
        """Склейки, дефисы и сомнительные числовые фрагменты не исправляются."""

        source = "Вмодели; чегоспециальноразработан; фер- ромагнетик; 2030; гамма-текст"
        result = clean_metadata_text(source)

        self.assertEqual(result.text, source)
        self.assertIn("ambiguous_word_break", {issue.code for issue in result.issues})

    def test_all_explicit_math_delimiters_protect_cyrillic(self) -> None:
        """Формулы защищены даже при наличии похожей на прозу кириллицы."""

        for formula in (
            "$русские  слова$", "$$русские\nслова$$",
            r"\(русские  слова\)", r"\[русские  слова\]",
            r"$\text{русские  слова} + \gamma$",
            r"\begin{equation}русские  слова\end{equation}",
        ):
            with self.subTest(formula=formula):
                source = "Начальный  текст " + formula + " конечный  текст"
                result = clean_metadata_text(source)
                self.assertEqual(result.text, "Начальный текст " + formula + " конечный текст")
                self.assertEqual(result.issues, ())

    def test_nested_environments_are_preserved(self) -> None:
        """Вложенные окружения LaTeX сохраняются вместе с командами внутри."""

        formula = r"\begin{align*}\begin{matrix}русские  слова\\a & b\end{matrix}\end{align*}"
        result = clean_metadata_text("Первый  текст " + formula)

        self.assertEqual(result.text, "Первый текст " + formula)
        self.assertEqual(result.issues, ())

    def test_html_nested_elements_are_preserved(self) -> None:
        """Парные HTML-элементы защищают содержимое и атрибуты целиком."""

        html_text = '<div title="a > b"><math><mi>русские  слова</mi></math><sup>два  слова</sup></div>'
        result = clean_metadata_text("Первый  текст " + html_text + " второй  текст")

        self.assertEqual(result.text, "Первый текст " + html_text + " второй текст")
        self.assertEqual(result.issues, ())

    def test_void_elements_and_comments(self) -> None:
        """Пустые элементы и комментарии не считаются повреждением текста."""

        for markup in ("<br>", "<BR />", '<img src="a.png">', "<!-- русские  слова -->", "<sub/>"):
            with self.subTest(markup=markup):
                source = "Первый  текст " + markup
                result = clean_metadata_text(source)
                self.assertEqual(result.text, "Первый текст " + markup)
                self.assertEqual(result.issues, ())

    def test_entities_are_not_unescaped(self) -> None:
        """Правильные HTML-сущности не декодируются и не считаются дефектом."""

        source = "&gamma; &nbsp; &lt; &#945;"
        result = clean_metadata_text(source)

        self.assertEqual(result.text, source)
        self.assertEqual(result.issues, ())

    def test_malformed_markup_blocks_the_entire_field(self) -> None:
        """Незакрытая или несовместимая разметка запрещает даже ранние замены."""

        for markup in (
            "<sup>текст", "</sup>", "<b><i>текст</b></i>", "<sup title='x>",
            "<!-- текст", "<sup broken=>текст</sup>", "$текст", "$$текст$",
            "$текст$$", r"\(текст", r"\[текст\)", r"\begin{a}текст\end{b}",
            r"\begin{a}текст", r"\begin{неизвестно}текст",
        ):
            with self.subTest(markup=markup):
                source = "Первый  текст " + markup
                result = clean_metadata_text(source)
                self.assertEqual(result.text, source)
                self.assertEqual(result.changes, ())
                self.assertIn("ambiguous_markup", {issue.code for issue in result.issues})

    def test_unknown_escapes_block_the_entire_field(self) -> None:
        """Голые команды и неизвестные экранирования не раскодируются."""

        for escaped in (r"\n", r"\r", r"\gamma", r"\text{русские  слова}", r"\\r\\n", r"\u03b3", r"\$", "\\"):
            with self.subTest(escaped=escaped):
                source = "Первый  текст " + escaped
                result = clean_metadata_text(source)
                self.assertEqual(result.text, source)
                self.assertEqual(result.changes, ())
                self.assertIn("unresolved_escape", {issue.code for issue in result.issues})

    def test_escaped_math_dollar_is_protected(self) -> None:
        """Экранированный доллар внутри формулы не завершает её раньше времени."""

        source = r"$\text{цена \$ два  слова}$"
        result = clean_metadata_text(source)

        self.assertEqual(result.text, source)
        self.assertEqual(result.issues, ())

    def test_latex_ambiguous_groups_and_comments_block_cleaning(self) -> None:
        """Комментарии и незакрытые группы не позволяют ошибочно завершить формулу."""

        for formula in (
            "$x_{a$", "$x} + y$", "$x % $ русский  текст",
            r"\begin{equation}x % \end{equation} русский  текст",
            r"\begin{a}\begin{плохо}x\end{a}",
            r"\begin{a}{x\end{a}", r"\begin{a}x}\end{a}",
        ):
            with self.subTest(formula=formula):
                source = "Первый  текст " + formula
                result = clean_metadata_text(source)
                self.assertEqual(result.text, source)
                self.assertEqual(result.changes, ())
                self.assertIn("ambiguous_markup", {issue.code for issue in result.issues})

    def test_escaped_braces_and_percent_remain_protected(self) -> None:
        """Экранированные скобки и проценты не трактуются как группы и комментарии."""

        for formula in (
            r"$\left\{x\right\} + 5\%$",
            r"\begin{a}\{x\} + 5\%\end{a}",
        ):
            with self.subTest(formula=formula):
                result = clean_metadata_text("Первый  текст " + formula)
                self.assertEqual(result.text, "Первый текст " + formula)
                self.assertEqual(result.issues, ())

    def test_replacement_controls_and_broken_entity_are_reported(self) -> None:
        """Конкретные признаки повреждений остаются в тексте с исходными координатами."""

        source = "Начальный  текст \ufffd\x00\u200b gamma; &gamma; пример,текста"
        result = clean_metadata_text(source)

        self.assertEqual(result.text, source.replace("Начальный  текст", "Начальный текст"))
        self.assertEqual(
            [issue.code for issue in result.issues],
            ["replacement_character", "control_character", "control_character", "possible_broken_entity", "possible_punctuation_glue"],
        )
        self.assertEqual(source[result.issues[0].start:result.issues[0].end], "\ufffd")
        self.assertEqual(source[result.issues[-2].start:result.issues[-2].end], "gamma;")

    def test_word_diagnostics_do_not_parse_formulas(self) -> None:
        """Диагностика склеек не интерпретирует содержимое защищённой формулы."""

        source = "$текст,другой + gamma; + фер- ромагнетик$"
        result = clean_metadata_text(source)

        self.assertEqual(result.text, source)
        self.assertEqual(result.issues, ())

    def test_changes_replay_original_offsets(self) -> None:
        """Непересекающиеся замены исходных полуинтервалов воспроизводят результат."""

        source = "Первый  второй\nтретий\tчетвёртый" + r"\r\nпятый"
        result = clean_metadata_text(source)
        replayed = source
        previous_end = 0

        for change in result.changes:
            self.assertGreaterEqual(change.start, previous_end)
            self.assertEqual(source[change.start:change.end], change.before)
            previous_end = change.end

        for change in reversed(result.changes):
            replayed = replayed[:change.start] + change.after + replayed[change.end:]

        self.assertEqual(replayed, result.text)
        self.assertEqual(clean_metadata_text(source), result)
        self.assertEqual(clean_metadata_text(result.text).text, result.text)
        self.assertEqual(clean_metadata_text(result.text).changes, ())

    def test_result_and_events_are_immutable(self) -> None:
        """Результат и записи журнала нельзя изменить после очистки."""

        result: Any = clean_metadata_text("Первый  текст \ufffd")

        with self.assertRaises(FrozenInstanceError):
            result.text = "другое"

        with self.assertRaises(FrozenInstanceError):
            result.changes[0].after = "другое"

        with self.assertRaises(FrozenInstanceError):
            result.issues[0].detail = "другое"

    def test_mixed_spacing_is_idempotent(self) -> None:
        """Комбинации пробелов и переносов не открывают замены при втором проходе."""

        separators = (" ", "  ", "\t", "\n", "\r\n", r"\r\n", "\n\n", r"\r\n" + "\n")

        for first, second, third in product(separators, repeat=3):
            source = "Первый" + first + "второй" + second + "третий" + third + "четвёртый"

            with self.subTest(source=source):
                result = clean_metadata_text(source)
                repeated = clean_metadata_text(result.text)
                self.assertEqual(repeated.text, result.text)
                self.assertEqual(repeated.changes, ())

    def test_invalid_input(self) -> None:
        """Вместо молчаливого преобразования тип входного поля проверяется явно."""

        for source in (None, 42, [], b"text"):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, "строкой"):
                invalid_source: Any = source
                clean_metadata_text(invalid_source)
