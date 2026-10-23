"""Поиск явно подписанных классификационных кодов в сохранённом тексте."""

from __future__ import annotations

import re

from typing import Any

MARKER_PATTERN = re.compile(
    r"(?<!\w)(?P<marker>УДК|UDC|ГРНТИ|GRNTI|МРНТИ|MRNTI|[PР][AА][CС][SЅ])"
    r"(?!\w)[ \t]*(?::[ \t]*)?",
    re.IGNORECASE,
)
MARKER_SYSTEMS = {
    "УДК": "udc",
    "UDC": "udc",
    "ГРНТИ": "grnti",
    "GRNTI": "grnti",
    "МРНТИ": "mrnti",
    "MRNTI": "mrnti",
}
PACS_PATTERN = re.compile(r"[0-9]{2}\.[0-9]{2}\.(?:[A-Za-z]{2}|[+-][A-Za-z])")
RUBRIC_PATTERN = re.compile(r"[0-9]{2}\.[0-9]{2}\.[0-9]{2}")
UDC_CHARACTERS_PATTERN = re.compile(r"[0-9 \t.:\[\]()/+=-]+")
UDC_NUMBER_PATTERN = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3})*")
LINE_ENDINGS = "\r\n\v\f\x85\u2028\u2029"


def _marker_system(marker: str) -> tuple[str, bool]:
    """Определить систему по явной подписи, отмечая похожие буквы PACS."""

    normalized_marker = marker.upper()

    if normalized_marker in MARKER_SYSTEMS:
        return MARKER_SYSTEMS[normalized_marker], False

    return "pacs", normalized_marker != "PACS"


def _balanced_brackets(value: str) -> bool:
    """Проверить парность скобок без изменения составного выражения УДК."""

    opened_brackets: list[str] = []
    closing_brackets = {")": "(", "]": "["}

    for character in value:
        if character in "([":
            opened_brackets.append(character)

        elif character in closing_brackets:
            if not opened_brackets or opened_brackets.pop() != closing_brackets[character]:
                return False

    return not opened_brackets


def _valid_udc(value: str) -> bool:
    """Проверить простую форму УДК, не подтверждая смысл по рубрикатору."""

    if not value or not UDC_CHARACTERS_PATTERN.fullmatch(value):
        return False

    # Числовые фрагменты проверяются отдельно: неоднозначное повторение
    # коротких групп цифр в общем шаблоне может вызвать взрывной перебор.
    if any(
        UDC_NUMBER_PATTERN.fullmatch(number_match.group()) is None
        for number_match in re.finditer(r"[0-9.]+", value)
    ):
        return False

    # Без разделителя две соседние группы цифр не становятся одним кодом.
    if re.search(r"[0-9][ \t]+[0-9]", value):
        return False

    if not re.search(r"[0-9]", value) or value[-1] not in "0123456789)]":
        return False

    if value[0] not in "0123456789[(+=":
        return False

    return _balanced_brackets(value)


def _parse_codes(system: str, raw_value: str) -> tuple[list[str], list[str]]:
    """Разобрать список, сохранив сомнительные части только в исходной строке."""

    if not raw_value:
        return [], ["missing_code"]

    codes: list[str] = []
    flags: set[str] = set()

    for raw_part in re.split(r"[,;]", raw_value):
        code = raw_part.strip()

        # Единственная конечная точка может быть пунктуацией строки,
        # но внутренние точки и дефисы никогда не восстанавливаются.
        if code.endswith("."):
            code = code[:-1].rstrip()

        if system == "udc":
            valid_code = _valid_udc(code)

        elif system == "pacs":
            valid_code = PACS_PATTERN.fullmatch(code) is not None

        else:
            valid_code = RUBRIC_PATTERN.fullmatch(code) is not None

        if valid_code:
            if code not in codes:
                codes.append(code)

            continue

        flags.add("invalid_code")

        if re.search(r"[А-Яа-яЁё]|[A-Za-z]{3,}", code):
            flags.add("unexpected_text")

        elif system != "pacs" and re.search(r"[A-Za-z]", code):
            flags.add("unexpected_text")

    return codes, sorted(flags)


def _code_only_line(system: str, value: str) -> bool:
    """Разрешить перенос только на строку, похожую целиком на список кодов."""

    if not value or not re.search(r"[0-9]", value):
        return False

    if system == "udc":
        return re.fullmatch(r"[0-9 \t.,;:\[\]()/+=-]+", value) is not None

    if system in {"grnti", "mrnti"}:
        return re.fullmatch(r"[0-9 \t.,;]+", value) is not None

    return all(
        re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.?[+-]?[A-Za-z]{1,2}\.?", part.strip())
        is not None
        for part in re.split(r"[,;]", value)
    )


def _following_code_line(
    lines: list[str],
    line_index: int,
    system: str,
) -> tuple[str, int]:
    """Найти код на следующей строке, допуская одну пустую строку между ними."""

    for following_index in range(line_index + 1, min(line_index + 3, len(lines))):
        value = lines[following_index].strip()

        if not value:
            continue

        if _code_only_line(system, value):
            return value, following_index

        break

    return "", line_index


def extract_classification_codes(text: str) -> list[dict[str, Any]]:
    """
    Найти группы УДК, PACS, ГРНТИ и МРНТИ без изменения исходного текста.

    Подпись должна начинать строку; следующие подписи допустимы в той же
    строке. Номера строк начинаются с единицы, CRLF считается одним переводом,
    а разрыв страницы — границей строки. ``raw_excerpt`` сохраняет исходные
    разделители строк. Список ``codes`` содержит только синтаксически понятные
    значения, а ``flags`` требует проверки сомнительных фрагментов. Результат
    не подтверждает основную рубрику и не назначает метку классификации.
    """

    if not isinstance(text, str):
        raise TypeError("Текст для поиска классификационных кодов должен быть строкой")

    lines = text.splitlines(keepends=True)
    findings: list[dict[str, Any]] = []

    for line_index, source_line in enumerate(lines):
        line = source_line.rstrip(LINE_ENDINGS)
        markers = list(MARKER_PATTERN.finditer(line))

        if not markers or line[:markers[0].start()].strip():
            continue

        for marker_index, marker_match in enumerate(markers):
            system, confusable_marker = _marker_system(marker_match.group("marker"))
            segment_end = (
                markers[marker_index + 1].start()
                if marker_index + 1 < len(markers)
                else len(line)
            )
            raw_value = line[marker_match.end():segment_end].strip()
            line_end_index = line_index

            if not raw_value and len(markers) == 1:
                raw_value, line_end_index = _following_code_line(lines, line_index, system)

            # Упоминание «PACS используется…» не является подписью кода.
            if raw_value and raw_value[0] not in "0123456789[(+=":
                break

            codes, flags = _parse_codes(system, raw_value)

            if confusable_marker:
                flags.append("marker_confusable")

            findings.append({
                "system": system,
                "field": f"{system}_codes_raw",
                "codes": codes,
                "raw_value": raw_value,
                "line_start": line_index + 1,
                "line_end": line_end_index + 1,
                "raw_excerpt": "".join(lines[line_index:line_end_index + 1]),
                "flags": flags,
            })

            if "unexpected_text" in flags:
                break

    return findings
