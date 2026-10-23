"""Очередь просмотра метаданных ОИЯИ без назначения классов H2 и удаления дублей."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata

from collections import Counter, defaultdict
from typing import Any

from .h2_candidate_input import CandidateSnapshot
from .identity import normalize_doi

QUEUE_VERSION = "jinr-h2-candidates-v1"

# Совпадение рубрики меняет только порядок просмотра, а не класс или допуск.
PHYSICS_RUBRICS = frozenset({
    "физика",
    "физика элементарных частиц. теория полей. физика высоких энергий",
    "ядерная физика",
    "физика твердых тел",
    "физика твёрдых тел",
    "физика плазмы",
    "лазерная физика",
})


def _text_key(value: str) -> str:
    """Сравнить текст по регистру и пробелам, не объединяя смысловые варианты."""

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _values(metadata: dict[str, Any], field: str) -> list[str]:
    """Получить непустые варианты для просмотра, сохранив исходное поле отдельно."""

    entries = metadata.get(field, [])

    if not isinstance(entries, list):
        raise ValueError(f"Поле {field} должно быть списком")

    values: list[str] = []

    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
            raise ValueError(f"Поле {field} должно содержать строковые value")

        value = entry["value"].strip()

        if value and value not in values:
            values.append(value)

    return values


def _single(values: list[Any]) -> Any:
    """Выбрать значение только при отсутствии неоднозначности."""

    if len(values) == 1:
        return values[0]


def _russian_letter_share(text: str) -> float | None:
    """Измерить долю русских букв для диагностики, а не определения языка."""

    letters = [character for character in text if character.isalpha()]

    if not letters:
        return

    russian_count = sum(bool(re.fullmatch(r"[А-Яа-яЁё]", character)) for character in letters)

    return round(russian_count / len(letters), 6)


def _identifiers(values: list[str], kind: str, flags: list[str]) -> list[str]:
    """Нормализовать идентификаторы только для поиска совпадений, не сливая работы."""

    normalized: set[str] = set()

    for value in values:
        if kind == "doi":
            try:
                identifier = normalize_doi(value)

            except ValueError:
                identifier = None

        else:
            identifier = value.strip().upper()

            if re.fullmatch(r"[A-Z]{6}", identifier) is None:
                identifier = None

        if identifier is None:
            flags.append(f"invalid_{kind}")

        else:
            normalized.add(identifier)

    if len(normalized) > 1:
        flags.append(f"ambiguous_{kind}")

    return sorted(normalized)


def _candidate(item: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Собрать карточку просмотра, не выбирая молча один из исходных вариантов."""

    metadata = item["metadata"]
    flags: list[str] = []
    fields = {
        "title": "dc.title",
        "abstract": "dc.description.abstract",
        "year": "dc.date.issued",
        "journal": "dc.relation.ispartof",
    }
    variants = {name: _values(metadata, field) for name, field in fields.items()}

    for name, values in variants.items():
        if not values:
            flags.append(f"missing_{name}")

        if len(values) > 1:
            flags.append(f"multiple_{name}_variants")

    title = "\n\n".join(variants["title"])
    abstract = "\n\n".join(variants["abstract"])
    year_text = _single(variants["year"])
    year = int(year_text) if year_text and re.fullmatch(r"[0-9]{4}", year_text) else None

    if variants["year"] and year is None:
        flags.append("unresolved_year")

    shares = {
        "title": _russian_letter_share(title),
        "abstract": _russian_letter_share(abstract),
    }

    for name, share in shares.items():
        if variants[name] and (share is None or share < 0.5):
            flags.append(f"check_{name}_language")

    languages = _values(metadata, "dc.language.iso")

    if {_text_key(value) for value in languages} != {"ru"}:
        flags.append("check_reported_language")

    doi_values = _identifiers(_values(metadata, "dc.identifier.doi"), "doi", flags)
    edn_values = _identifiers(_values(metadata, "local.EDN"), "edn", flags)
    rubrics = _values(metadata, "local.Rubric")
    physics_signal = "other_rubric" if rubrics else "missing_rubric"

    if any(_text_key(value) in PHYSICS_RUBRICS for value in rubrics):
        physics_signal = "physics_rubric"

    if set(_values(metadata, "dc.type")) != {"Article"}:
        flags.append("mixed_document_types")

    for field, expected in (("withdrawn", False), ("inArchive", True), ("discoverable", True)):
        if field in item and item[field] is not expected:
            flags.append(f"check_{field}")

    # Код внутри текста требует проверки утечки, но автоматически не удаляется.
    if re.search(r"\b(?:ГРНТИ|GRNTI|PACS)\b", title + "\n" + abstract, re.IGNORECASE):
        flags.append("possible_label_hint_in_text")

    item_uuid = item["uuid"]
    journal_name = _single(variants["journal"])

    return {
        "candidate_id": f"jinr:{item_uuid}",
        "item_uuid": item_uuid,
        "source_url": f"https://pubrepo.jinr.ru/items/{item_uuid}",
        "title": title,
        "abstract": abstract,
        "year": year,
        "journal": "\n\n".join(variants["journal"]),
        "journal_key": _text_key(journal_name) if journal_name else None,
        "doi": _single(doi_values),
        "edn": _single(edn_values),
        "doi_values": doi_values,
        "edn_values": edn_values,
        "rubrics_as_reported": rubrics,
        "udc_as_reported": _values(metadata, "local.udc"),
        "has_title_abstract": bool(title and abstract),
        "russian_letter_share": shares,
        "physics_signal": physics_signal,
        "review_flags": sorted(set(flags)),
        "duplicate_groups": [],
        "review_status": "not_reviewed",
        "label": None,
        "split": None,
        "source": copy.deepcopy(source),
        "metadata_as_reported": copy.deepcopy(metadata),
    }


def _duplicate_keys(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Выдать независимые сигналы совпадения без транзитивного объединения карточек."""

    keys = [("doi", value) for value in row["doi_values"]]
    keys.extend(("edn", value) for value in row["edn_values"])
    titles = _values(row["metadata_as_reported"], "dc.title")

    if len(titles) == 1 and row["year"] is not None:
        keys.append(("title_year", json.dumps([_text_key(titles[0]), row["year"]], ensure_ascii=False)))

    if row["abstract"]:
        keys.append(("abstract", _text_key(row["abstract"])))

    return keys


def _mark_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Пометить возможные повторы, сохранив все строки и основание каждого совпадения."""

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        for key in _duplicate_keys(row):
            groups[key].append(row)

    results: list[dict[str, Any]] = []

    for (basis, value), members in sorted(groups.items()):
        if len(members) < 2:
            continue

        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        group_id = f"{basis}:{digest}"

        for row in members:
            row["duplicate_groups"].append(group_id)
            row["review_flags"].append(f"possible_duplicate_{basis}")

        results.append({
            "group_id": group_id,
            "basis": basis,
            "value_sha256": digest,
            "candidate_ids": sorted(row["candidate_id"] for row in members),
        })

    return results


def build_candidate_queue(snapshot: CandidateSnapshot) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Подготовить все Article, включая неполные, и воспроизводимую сводку просмотра."""

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    other_types: Counter[str] = Counter()

    for item in snapshot.items:
        item_uuid = item.get("uuid")
        metadata = item.get("metadata")

        if not isinstance(item_uuid, str) or item_uuid in seen or item_uuid not in snapshot.sources:
            raise ValueError("Карточка должна иметь уникальный UUID и проверенный источник")

        if not isinstance(metadata, dict):
            raise ValueError("Карточка не содержит объекта metadata")

        seen.add(item_uuid)
        document_types = _values(metadata, "dc.type")

        if "Article" not in document_types:
            other_types[" + ".join(sorted(document_types)) or "не указан"] += 1
            continue

        rows.append(_candidate(item, snapshot.sources[item_uuid]))

    duplicate_groups = _mark_duplicates(rows)

    # Порядок просмотра не является выборкой: сохраняются и другие рубрики, и пропуски.
    rows.sort(key=lambda row: (
        not row["has_title_abstract"],
        row["physics_signal"] != "physics_rubric",
        row["item_uuid"],
    ))

    for row in rows:
        row["review_flags"] = sorted(set(row["review_flags"]))
        row["duplicate_groups"].sort()

    summary = {
        "schema_version": QUEUE_VERSION,
        "scope": "Очередь просмотра Article из полного локального снимка; не набор обучения H2.",
        "provenance": copy.deepcopy(snapshot.provenance),
        "source_item_count": len(snapshot.items),
        "candidate_count": len(rows),
        "with_title_abstract": sum(row["has_title_abstract"] for row in rows),
        "with_title_abstract_year_journal": sum(
            row["has_title_abstract"] and row["year"] is not None and bool(row["journal"])
            for row in rows
        ),
        "other_document_types": dict(sorted(other_types.items())),
        "physics_signals": dict(sorted(Counter(row["physics_signal"] for row in rows).items())),
        "review_flag_counts": dict(sorted(Counter(
            flag for row in rows for flag in row["review_flags"]
        ).items())),
        "journal_counts": dict(sorted(Counter(
            row["journal_key"] for row in rows if row["journal_key"] is not None
        ).items())),
        "duplicate_groups": duplicate_groups,
        "rows_with_duplicate_signals": sum(bool(row["duplicate_groups"]) for row in rows),
        "labelled_count": 0,
        "training_ready": False,
        "limitations": [
            "Article — исходный тип API, а не подтверждённый научный жанр.",
            "Заполненные title+abstract не доказывают русский язык или физическую тематику.",
            "local.Rubric и УДК не преобразуются в ГРНТИ или эталонные классы.",
            "Совпадение DOI, EDN, названия с годом или аннотации требует проверки; строки не удаляются.",
            "Для journal_key нормализованы только Unicode, регистр и пробелы; алиасы журналов не объединены.",
            "Очередь содержит подсказки источника и не предназначена для слепой независимой разметки.",
            "Права, рабочие реестры, разбиения и допуск к обучению не изменяются.",
        ],
    }

    return rows, summary
