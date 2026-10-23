"""Узкая очистка пробелов метаданных H2 без восстановления научного текста."""

from __future__ import annotations

import re
import unicodedata

from dataclasses import dataclass

CLEANING_VERSION = "h2-metadata-clean-v1"
_RUSSIAN_LETTERS = frozenset("АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя")
_WORD_BOUNDARIES = frozenset(".,;:!?«»\"'“”„")
_VOID_HTML_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})
_HTML_TAG = re.compile(
    r"<(?P<closing>/)?(?P<name>[A-Za-z][A-Za-z0-9:_-]*)"
    r"(?P<attributes>(?:\s+[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*)"
    r"\s*(?P<self_closing>/)?>"
)
_ENVIRONMENT = re.compile(r"\\(?P<kind>begin|end)\{(?P<name>[A-Za-z][A-Za-z0-9*_-]*)\}")
_GAP = re.compile(r"(?:[ \t\u00a0\u202f\r\n]|\\r\\n)+")
_LINE_BREAK = re.compile(r"\r\n|\r|\n|\\r\\n")
_HYPHENATED_WORD = re.compile(r"[А-Яа-яЁё]+[-\u00ad][ \t\r\n]+[А-Яа-яЁё]+")
_PUNCTUATION_GLUE = re.compile(r"[А-Яа-яЁё]{2,}[.,;:!?](?=[А-Яа-яЁё]{2,})")
_BROKEN_GAMMA = re.compile(r"(?<![&A-Za-z])gamma;")


@dataclass(frozen=True, slots=True)
class MetadataChange:
    """Замена полуинтервала исходной строки без сдвига последующих координат."""

    start: int
    end: int
    before: str
    after: str
    code: str


@dataclass(frozen=True, slots=True)
class MetadataIssue:
    """Наблюдение для проверки человеком с координатами исходной строки."""

    start: int
    end: int
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class MetadataCleaningResult:
    """Сохранённый или очищенный текст и неизменяемый журнал обработки."""

    text: str
    changes: tuple[MetadataChange, ...]
    issues: tuple[MetadataIssue, ...]


def _markup_issue(start: int, end: int, detail: str) -> MetadataIssue:
    """Объяснить отказ от очистки всего поля из-за неоднозначной разметки."""

    return MetadataIssue(start, end, "ambiguous_markup", detail)


def _html_region(text: str, start: int) -> tuple[int, MetadataIssue | None]:
    """Найти конец полностью закрытого HTML-элемента без разбора его смысла."""

    position = start
    names: list[str] = []

    while position < len(text):
        if text.startswith("<!--", position):
            comment_end = text.find("-->", position + 4)

            if comment_end < 0:
                return len(text), _markup_issue(position, len(text), "Не закрыт HTML-комментарий; поле сохранено целиком.")

            position = comment_end + 3

            if not names:
                return position, None

            continue

        if text[position] != "<":
            position += 1
            continue

        tag = _HTML_TAG.match(text, position)

        if tag is None:
            return len(text), _markup_issue(position, min(position + 1, len(text)), "Неоднозначный фрагмент с '<': разметка или сравнение; поле сохранено целиком.")

        name = tag.group("name").lower()
        closing = bool(tag.group("closing"))
        self_closing = bool(tag.group("self_closing"))

        if closing:
            if tag.group("attributes") or self_closing or not names or names[-1] != name:
                return tag.end(), _markup_issue(position, tag.end(), "Нарушена вложенность HTML-элементов; поле сохранено целиком.")

            names.pop()

        elif not self_closing and name not in _VOID_HTML_TAGS:
            names.append(name)

        position = tag.end()

        if not names:
            return position, None

    return len(text), _markup_issue(start, len(text), "Не закрыт HTML-элемент; поле сохранено целиком.")


def _environment_region(text: str, start: int) -> tuple[int, MetadataIssue | None]:
    """Проверить вложенность окружений LaTeX и сохранить их содержимое буквально."""

    names: list[str] = []
    position = start
    brace_depth = 0

    while position < len(text):
        token = _ENVIRONMENT.match(text, position)

        if token is not None:
            name = token.group("name")

            if token.group("kind") == "begin":
                names.append(name)

            elif not names or names[-1] != name:
                return token.end(), _markup_issue(position, token.end(), "Нарушена вложенность окружений LaTeX; поле сохранено целиком.")

            else:
                names.pop()

                if not names:
                    if brace_depth:
                        return token.end(), _markup_issue(start, token.end(), "Не закрыта группа LaTeX; поле сохранено целиком.")

                    return token.end(), None

            position = token.end()
            continue

        if text.startswith((r"\begin", r"\end"), position):
            return len(text), _markup_issue(position, len(text), "Неоднозначная граница окружения LaTeX; поле сохранено целиком.")

        if text[position] == "\\":
            position += 2
            continue

        if text[position] == "%":
            return len(text), _markup_issue(position, position + 1, "Комментарий LaTeX может скрывать границы окружения; поле сохранено целиком.")

        if text[position] == "{":
            brace_depth += 1

        elif text[position] == "}":
            brace_depth -= 1

            if brace_depth < 0:
                return position + 1, _markup_issue(position, position + 1, "Лишняя закрывающая группа LaTeX; поле сохранено целиком.")

        position += 1

    return len(text), _markup_issue(start, len(text), "Не закрыто окружение LaTeX; поле сохранено целиком.")


def _math_region(text: str, start: int, opening: str, closing: str) -> tuple[int, MetadataIssue | None]:
    """Найти парную границу формулы, не преобразуя команды и символы внутри."""

    position = start + len(opening)
    brace_depth = 0

    while position < len(text):
        if text.startswith(closing, position):
            if brace_depth:
                return position + len(closing), _markup_issue(start, position + len(closing), "Не закрыта группа в формуле; поле сохранено целиком.")

            if closing == "$" and text.startswith("$$", position):
                return position + 2, _markup_issue(start, position + 2, "Смешаны границы формулы $ и $$; поле сохранено целиком.")

            return position + len(closing), None

        if text[position] == "\\":
            if text.startswith((r"\(", r"\)", r"\[", r"\]"), position):
                return position + 2, _markup_issue(start, position + 2, "Неоднозначная вложенность границ формул; поле сохранено целиком.")

            position += 2
            continue

        if text[position] == "$":
            return position + 1, _markup_issue(start, position + 1, "Смешаны границы формул; поле сохранено целиком.")

        if text[position] == "%":
            return position + 1, _markup_issue(position, position + 1, "Комментарий LaTeX может скрывать границы формулы; поле сохранено целиком.")

        if text[position] == "{":
            brace_depth += 1

        elif text[position] == "}":
            brace_depth -= 1

            if brace_depth < 0:
                return position + 1, _markup_issue(position, position + 1, "Лишняя закрывающая группа в формуле; поле сохранено целиком.")

        position += 1

    return len(text), _markup_issue(start, len(text), "Не закрыта формула; поле сохранено целиком.")


def _protected_regions(text: str) -> tuple[list[tuple[int, int]], MetadataIssue | None]:
    """Защитить явную разметку и запретить догадки о неизвестных экранированиях."""

    regions: list[tuple[int, int]] = []
    position = 0

    while position < len(text):
        if text[position] == "<":
            end, issue = _html_region(text, position)

        elif text.startswith(r"\begin{", position):
            if _ENVIRONMENT.match(text, position) is None:
                return regions, _markup_issue(position, len(text), "Неоднозначное начало окружения LaTeX; поле сохранено целиком.")

            end, issue = _environment_region(text, position)

        elif text.startswith("$$", position):
            end, issue = _math_region(text, position, "$$", "$$")

        elif text[position] == "$":
            end, issue = _math_region(text, position, "$", "$")

        elif text.startswith(r"\(", position):
            end, issue = _math_region(text, position, r"\(", r"\)")

        elif text.startswith(r"\[", position):
            end, issue = _math_region(text, position, r"\[", r"\]")

        elif text.startswith(r"\r\n", position):
            position += 4
            continue

        elif text[position] == "\\":
            return regions, MetadataIssue(
                position, min(position + 2, len(text)), "unresolved_escape",
                "Неизвестное экранирование или команда вне явной формулы; поле сохранено целиком.",
            )

        else:
            position += 1
            continue

        if issue is not None:
            return regions, issue

        regions.append((position, end))
        position = end

    return regions, None


def _overlaps(start: int, end: int, regions: list[tuple[int, int]]) -> bool:
    """Проверить пересечение полуинтервала с защищённой разметкой."""

    return any(start < region_end and end > region_start for region_start, region_end in regions)


def _is_word_boundary(character: str) -> bool:
    """Допустить только пробел или простую пунктуацию вне научных обозначений."""

    return character.isspace() or character in _WORD_BOUNDARIES


def _word_region(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Найти целые русские слова по обе стороны промежутка без угадывания языка."""

    left = start
    right = end

    while left > 0 and text[left - 1] in _RUSSIAN_LETTERS:
        left -= 1

    while right < len(text) and text[right] in _RUSSIAN_LETTERS:
        right += 1

    if start - left < 2 or right - end < 2:
        return

    # Буквальный CRLF уже разрешён сканером. Он служит границей слова и
    # для соседнего промежутка, иначе повторный проход создавал бы новые замены.
    if left > 0 and not _is_word_boundary(text[left - 1]) and text[max(0, left - 4):left] != r"\r\n":
        return

    if right < len(text) and not _is_word_boundary(text[right]) and not text.startswith(r"\r\n", right):
        return

    return left, right


def _text_issues(text: str, regions: list[tuple[int, int]]) -> list[MetadataIssue]:
    """Отметить конкретные видимые дефекты, не обещая найти все склейки слов."""

    issues: list[MetadataIssue] = []

    for position, character in enumerate(text):
        if character == "\ufffd":
            code = "replacement_character"
            detail = "Символ замены U+FFFD может означать потерю исходных данных."

        elif unicodedata.category(character) in {"Cc", "Cf"} and character not in "\r\n\t":
            code = "control_character"
            detail = f"Сохранён управляющий или невидимый символ U+{ord(character):04X}."

        else:
            continue

        issues.append(MetadataIssue(position, position + 1, code, detail))

    for pattern, code, detail in (
        (_BROKEN_GAMMA, "possible_broken_entity", "Фрагмент gamma; без амперсанда требует сверки с источником."),
        (_HYPHENATED_WORD, "ambiguous_word_break", "Дефис или мягкий перенос перед пробелом: слово не склеивалось."),
        (_PUNCTUATION_GLUE, "possible_punctuation_glue", "После пунктуации между буквенными фрагментами нет пробела; требуется проверка."),
    ):
        for match in pattern.finditer(text):
            if not _overlaps(match.start(), match.end(), regions):
                issues.append(MetadataIssue(match.start(), match.end(), code, detail))

    return issues


def clean_metadata_text(text: str) -> MetadataCleaningResult:
    """
    Свести только безопасные пробельные промежутки между целыми русскими словами.

    Все координаты относятся к исходной строке и измеряются в символах Python,
    конец интервала не включается. Разметка, слова, числа, формулы и абзацы
    не восстанавливаются. Текст не нормализуется по Unicode и не обрезается.
    Неоднозначная разметка или неизвестный escape запрещают очистку всего поля.
    """

    if not isinstance(text, str):
        raise ValueError("Текст метаданных должен быть строкой")

    regions, markup_issue = _protected_regions(text)
    issues = _text_issues(text, regions)

    if markup_issue is not None:
        issues.append(markup_issue)

    changes: list[MetadataChange] = []

    if markup_issue is None:
        for match in _GAP.finditer(text):
            before = match.group()

            if before == " " or len(_LINE_BREAK.findall(before)) >= 2:
                continue

            word_region = _word_region(text, match.start(), match.end())

            if word_region is None or _overlaps(*word_region, regions):
                continue

            if r"\r\n" in before:
                code = "literal_crlf_between_words"

            elif "\n" in before or "\r" in before:
                code = "line_break_between_words"

            else:
                code = "spacing_between_words"

            changes.append(MetadataChange(match.start(), match.end(), before, " ", code))

    fragments: list[str] = []
    previous_end = 0

    for change in changes:
        fragments.extend((text[previous_end:change.start], change.after))
        previous_end = change.end

    fragments.append(text[previous_end:])

    return MetadataCleaningResult(
        text="".join(fragments),
        changes=tuple(changes),
        issues=tuple(sorted(issues, key=lambda issue: (issue.start, issue.end, issue.code, issue.detail))),
    )
