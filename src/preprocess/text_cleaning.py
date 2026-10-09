"""Консервативная очистка страниц с воспроизводимым журналом изменений."""

from __future__ import annotations

import re
import unicodedata

from dataclasses import dataclass

CLEANING_VERSION = "text-clean-conservative-v3"
SOFT_HYPHEN = "\u00ad"


@dataclass(frozen=True, slots=True)
class LineExclusion:
    """Явное исключение строки с проверкой её исходного текста и причиной."""

    page_number: int
    line_number: int
    expected_text: str
    reason: str


@dataclass(frozen=True, slots=True)
class CleanedPage:
    """Очищенный текст с исходным физическим номером страницы."""

    page_number: int
    text: str


@dataclass(frozen=True, slots=True)
class CleaningIssue:
    """Место исходной страницы, требующее проверки человеком."""

    page_number: int
    line_number: int
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class CleaningChange:
    """Явное преобразование с номером строки исходной страницы."""

    page_number: int
    line_number: int
    code: str
    before: str
    after: str


@dataclass(frozen=True, slots=True)
class CleaningResult:
    """Текст, неизменяемые страницы и полный журнал обработки."""

    text: str
    pages: tuple[CleanedPage, ...]
    issues: tuple[CleaningIssue, ...]
    changes: tuple[CleaningChange, ...]


def _validate_arguments(
    pages: list[tuple[int, str]],
    join_words: frozenset[str],
) -> frozenset[str]:
    """Проверить страницы и нормализовать явно разрешённые слова."""

    if not isinstance(pages, list):
        raise ValueError("pages должен быть списком пар (номер, текст)")

    previous_page_number = 0

    for page in pages:
        if not isinstance(page, tuple) or len(page) != 2:
            raise ValueError("Каждая страница должна быть парой (номер, текст)")

        page_number, text = page

        if (
            not isinstance(page_number, int)
            or isinstance(page_number, bool)
            or page_number <= previous_page_number
        ):
            raise ValueError(
                "Номера страниц должны быть положительными целыми числами "
                "в строго возрастающем порядке"
            )

        if not isinstance(text, str):
            raise ValueError("Текст каждой страницы должен быть строкой")

        previous_page_number = page_number

    if not isinstance(join_words, frozenset):
        raise ValueError("join_words должен быть неизменяемым множеством слов")

    normalized_words: set[str] = set()

    for word in join_words:
        if not isinstance(word, str):
            raise ValueError("Разрешённое слово должно быть строкой из букв")

        normalized_word = unicodedata.normalize("NFC", word)

        if not normalized_word.isalpha():
            raise ValueError("Разрешённое слово должно быть непустой строкой из букв")

        normalized_words.add(normalized_word.casefold())

    return frozenset(normalized_words)


def _validate_line_exclusion(exclusion: LineExclusion) -> None:
    """Проверить типы и обязательные поля одного исключения строки."""

    if not isinstance(exclusion, LineExclusion):
        raise ValueError("Каждое исключение строки должно быть LineExclusion")

    for field_name, number in (
        ("страницы", exclusion.page_number),
        ("строки", exclusion.line_number),
    ):
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise ValueError(
                f"Номер {field_name} исключения должен быть положительным целым числом"
            )

    if not isinstance(exclusion.expected_text, str) or not exclusion.expected_text.strip():
        raise ValueError("expected_text исключения должен содержать непустой текст")

    if "\r" in exclusion.expected_text or "\n" in exclusion.expected_text:
        raise ValueError("expected_text исключения не должен содержать перевод строки")

    if not isinstance(exclusion.reason, str) or not exclusion.reason.strip():
        raise ValueError("Причина исключения строки должна содержать непустой текст")


def _validate_line_exclusions(
    pages: list[tuple[int, str]],
    exclude_lines: tuple[LineExclusion, ...],
) -> dict[int, frozenset[int]]:
    """Проверить исключения по точным исходным строкам до любой нормализации."""

    if not isinstance(exclude_lines, tuple):
        raise ValueError("exclude_lines должен быть кортежем исключений строк")

    if not exclude_lines:
        return {}

    page_lines = {
        page_number: re.split(r"\r\n|\r|\n", text)
        for page_number, text in pages
    }
    excluded_numbers: dict[int, set[int]] = {}

    for exclusion in exclude_lines:
        _validate_line_exclusion(exclusion)
        page_number = exclusion.page_number
        line_number = exclusion.line_number

        if page_number not in page_lines:
            raise ValueError(f"Исключение ссылается на неизвестную страницу {page_number}")

        if line_number > len(page_lines[page_number]):
            raise ValueError(
                f"На странице {page_number} отсутствует строка {line_number}"
            )

        numbers = excluded_numbers.setdefault(page_number, set())

        if line_number in numbers:
            raise ValueError(
                f"Повторное исключение строки {line_number} страницы {page_number}"
            )

        if page_lines[page_number][line_number - 1] != exclusion.expected_text:
            raise ValueError(
                f"Исходный текст строки {line_number} страницы {page_number} "
                "не совпадает с expected_text"
            )

        numbers.add(line_number)

    for page_number, numbers in excluded_numbers.items():
        if not any(
            line.strip()
            for line_number, line in enumerate(page_lines[page_number], start=1)
            if line_number not in numbers
        ):
            raise ValueError(
                "Исключения строк не должны удалять всё содержимое "
                f"страницы {page_number}"
            )

    return {
        page_number: frozenset(numbers)
        for page_number, numbers in excluded_numbers.items()
    }


def _character_issues(
    page_number: int,
    line_number: int,
    line: str,
) -> list[CleaningIssue]:
    """Зафиксировать подозрительные символы, не удаляя их из текста."""

    issues: list[CleaningIssue] = []

    for column_number, character in enumerate(line, start=1):
        category = unicodedata.category(character)

        if character == "\t":
            code = "tab_character"

        elif category == "Cc":
            code = "control_character"

        elif category == "Co":
            code = "private_use_character"

        elif character == "\ufffd":
            code = "replacement_character"

        else:
            continue

        issues.append(
            CleaningIssue(
                page_number,
                line_number,
                code,
                f"Сохранён символ U+{ord(character):04X}, "
                f"позиция в исходной строке: {column_number}",
            )
        )

    return issues


def _normalize_page(
    page_number: int,
    text: str,
    excluded_line_numbers: frozenset[int] = frozenset(),
) -> tuple[list[str], list[CleaningIssue], list[CleaningChange]]:
    """Исключить проверенные строки и нормализовать сохранённый текст."""

    # splitlines разделил бы текст также по сохраняемым управляющим символам.
    fragments = re.split(r"(\r\n|\r|\n)", text)
    lines: list[str] = []
    issues: list[CleaningIssue] = []
    changes: list[CleaningChange] = []

    for fragment_index in range(0, len(fragments), 2):
        line_number = fragment_index // 2 + 1
        line = fragments[fragment_index]

        if line_number in excluded_line_numbers:
            # Пустая строка сохраняет исходную позицию и барьер для склейки.
            lines.append("")
            changes.append(
                CleaningChange(page_number, line_number, "remove_explicit_line", line, "")
            )

        else:
            issues.extend(_character_issues(page_number, line_number, line))

            normalized_line = unicodedata.normalize("NFC", line)
            spaced_line = normalized_line.replace("\u00a0", " ")
            trimmed_line = spaced_line.rstrip(" ")

            for code, before, after in (
                ("normalize_unicode", line, normalized_line),
                ("replace_nbsp", normalized_line, spaced_line),
                ("trim_trailing_spaces", spaced_line, trimmed_line),
            ):
                if after != before:
                    changes.append(
                        CleaningChange(page_number, line_number, code, before, after)
                    )

            lines.append(trimmed_line)

        if fragment_index + 1 < len(fragments):
            line_ending = fragments[fragment_index + 1]

            if line_ending != "\n":
                changes.append(
                    CleaningChange(
                        page_number,
                        line_number,
                        "normalize_line_endings",
                        line_ending,
                        "\n",
                    )
                )

    return lines, issues, changes


def _join_candidate(left_line: str, right_line: str) -> str | None:
    """Получить полное буквенное слово по обе стороны переноса."""

    if (
        len(left_line) < 2
        or left_line[-1] not in {"-", SOFT_HYPHEN}
        or not left_line[-2].isalpha()
        or not right_line
        or not right_line[0].isalpha()
    ):
        return

    left_start = len(left_line) - 2

    while left_start > 0 and left_line[left_start - 1].isalpha():
        left_start -= 1

    right_end = 1

    while right_end < len(right_line) and right_line[right_end].isalpha():
        right_end += 1

    return left_line[left_start:-1] + right_line[:right_end]


def _join_page_lines(
    page_number: int,
    lines: list[str],
    join_words: frozenset[str],
) -> tuple[str, list[CleaningIssue], list[CleaningChange]]:
    """Склеить только подтверждённые переносы внутри одной страницы."""

    output_lines: list[str] = []
    issues: list[CleaningIssue] = []
    changes: list[CleaningChange] = []
    joined_soft_hyphens: set[int] = set()
    current_line = lines[0]

    for line_number, following_line in enumerate(lines[1:], start=1):
        candidate = _join_candidate(current_line, following_line)
        code: str | None = None

        if candidate is not None:
            if current_line.endswith(SOFT_HYPHEN):
                # Отдельная буквица может оказаться в конце выгрузки страницы.
                # Мягкий перенос не подтверждает такой переход регистра.
                suspicious_case = current_line[-2].islower() and following_line[0].isupper()

                if not suspicious_case:
                    code = "join_soft_hyphen"
                    joined_soft_hyphens.add(line_number)

            elif candidate.casefold() in join_words:
                code = "join_allowlisted_word"

        if code is not None:
            joined_line = current_line[:-1] + following_line
            changes.append(
                CleaningChange(
                    page_number,
                    line_number,
                    code,
                    current_line + "\n" + following_line,
                    joined_line,
                )
            )
            current_line = joined_line
            continue

        if (
            len(current_line) >= 2
            and current_line.endswith("-")
            and current_line[-2].isalpha()
        ):
            issues.append(
                CleaningIssue(
                    page_number,
                    line_number,
                    "ambiguous_line_hyphen",
                    "Сохранён дефис U+002D в конце строки: "
                    "склейка слова не подтверждена",
                )
            )

        output_lines.append(current_line)
        current_line = following_line

    output_lines.append(current_line)

    for line_number, line in enumerate(lines, start=1):
        for column_number, character in enumerate(line, start=1):
            if character != SOFT_HYPHEN:
                continue

            if line_number in joined_soft_hyphens and column_number == len(line):
                continue

            issues.append(
                CleaningIssue(
                    page_number,
                    line_number,
                    "unresolved_soft_hyphen",
                    "Сохранён мягкий перенос U+00AD: склейка не подтверждена; "
                    f"позиция в нормализованной строке: {column_number}",
                )
            )

    return "\n".join(output_lines), issues, changes


def _boundary_issue(
    page_number: int,
    lines: list[str],
    following_lines: list[str],
) -> CleaningIssue | None:
    """Отметить возможный перенос между физическими страницами."""

    nonempty_lines = [
        (line_number, line)
        for line_number, line in enumerate(lines, start=1)
        if line
    ]
    following_nonempty_lines = [line for line in following_lines if line]

    if not nonempty_lines or not following_nonempty_lines:
        return

    line_number, last_line = nonempty_lines[-1]
    first_line = following_nonempty_lines[0]

    if _join_candidate(last_line, first_line) is None:
        return

    return CleaningIssue(
        page_number,
        line_number,
        "page_boundary_hyphen",
        f"Сохранён перенос U+{ord(last_line[-1]):04X} на границе "
        "физических страниц; межстраничная склейка запрещена",
    )


def clean_pages(
    pages: list[tuple[int, str]],
    *,
    join_words: frozenset[str] = frozenset(),
    exclude_lines: tuple[LineExclusion, ...] = (),
) -> CleaningResult:
    """
    Очистить страницы без догадок о формулах, дефисах и структуре документа.

    Для обычного дефиса разрешены только явно перечисленные полные слова.
    Сопоставление не учитывает регистр, но сохраняет его в тексте. Номера
    строк журнала относятся к исходной странице; CRLF и CR считаются одним
    переносом. Пустые страницы сохраняются. Общий текст соединяет страницы
    ровно двумя LF, без добавления завершающего перевода строки.
    Явно исключённая строка должна точно совпадать с исходной и заменяется
    пустой строкой, не позволяющей склеить соседние фрагменты текста.
    """

    normalized_words = _validate_arguments(pages, join_words)
    excluded_line_numbers = _validate_line_exclusions(pages, exclude_lines)
    cleaned_pages: list[CleanedPage] = []
    issues: list[CleaningIssue] = []
    changes: list[CleaningChange] = []
    normalized_pages: list[tuple[int, list[str]]] = []

    for page_number, text in pages:
        lines, page_issues, page_changes = _normalize_page(
            page_number, text, excluded_line_numbers.get(page_number, frozenset())
        )
        normalized_pages.append((page_number, lines))
        issues.extend(page_issues)
        changes.extend(page_changes)
        cleaned_text, join_issues, join_changes = _join_page_lines(
            page_number, lines, normalized_words
        )
        cleaned_pages.append(CleanedPage(page_number, cleaned_text))
        issues.extend(join_issues)
        changes.extend(join_changes)

    for page_index in range(len(normalized_pages) - 1):
        page_number, lines = normalized_pages[page_index]
        following_lines = normalized_pages[page_index + 1][1]
        issue = _boundary_issue(page_number, lines, following_lines)

        if issue is not None:
            issues.append(issue)

    return CleaningResult(
        text="\n\n".join(page.text for page in cleaned_pages),
        pages=tuple(cleaned_pages),
        issues=tuple(
            sorted(issues, key=lambda issue: (issue.page_number, issue.line_number))
        ),
        changes=tuple(changes),
    )
