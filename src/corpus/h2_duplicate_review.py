"""Пакет проверки совпадений H2 и связей версий без объединения научных работ."""

from __future__ import annotations

import copy
import hashlib

from collections import Counter
from typing import Any

from .h2_candidate_input import CandidateSnapshot
from .h2_candidates import _candidate, _text_key, _values, build_candidate_queue
from .h2_version_relations import extract_version_relations

REVIEW_VERSION = "h2-duplicate-review-v1"
BASIS_EXPLANATIONS = {
    "doi": "Совпадает нормализованный DOI; проверьте все исходные варианты.",
    "edn": "Совпадает нормализованный EDN; проверьте все исходные варианты.",
    "title_year": "Совпадают нормализованное название и год, но не обязательно публикации.",
    "abstract": "Совпадает нормализованная аннотация; она может быть шаблонной или ошибочной.",
}


def _review_groups(
    raw_groups: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Дополнить группы основаниями проверки, сохранив неоднозначные идентификаторы."""

    groups: list[dict[str, Any]] = []

    for raw_group in raw_groups:
        members = sorted(candidates[candidate_id]["item_uuid"] for candidate_id in raw_group["candidate_ids"])
        basis = raw_group["basis"]
        ambiguous_items: list[str] = []
        flags: list[str] = []

        if basis in {"doi", "edn"}:
            ambiguous_items = sorted(
                candidates[candidate_id]["item_uuid"]
                for candidate_id in raw_group["candidate_ids"]
                if len(candidates[candidate_id][f"{basis}_values"]) > 1
            )

            if ambiguous_items:
                flags.append(f"shared_ambiguous_{basis}")

        if len(members) > 2:
            flags.append("large_group_not_expanded")

        groups.append({
            **copy.deepcopy(raw_group),
            "item_uuids": members,
            "member_count": len(members),
            "basis_explanation": BASIS_EXPLANATIONS[basis],
            "ambiguous_identifier_items": ambiguous_items,
            "review_flags": flags,
            "review_decision": None,
            "comment": "",
        })

    return groups


def _pair_key(first: str, second: str) -> tuple[str, str]:
    """Получить единственный ключ неупорядоченной пары различных карточек."""

    if first == second:
        raise ValueError("Для пары нужны две разные карточки")

    return (first, second) if first < second else (second, first)


def _pair_identifier(key: tuple[str, str]) -> str:
    """Закрепить воспроизводимый ID пары независимо от числа оснований совпадения."""

    digest = hashlib.sha256("\n".join(key).encode("utf-8")).hexdigest()

    return f"pair:{digest}"


def _single_text(card: dict[str, Any], field: str) -> str | None:
    """Сравнивать вход H2 только при единственном непустом варианте поля."""

    metadata_field = {"title": "dc.title", "abstract": "dc.description.abstract"}[field]
    values = _values(card["metadata_as_reported"], metadata_field)

    if len(values) != 1 or not card[field].strip():
        return

    return card[field]


def _comparison(
    first: dict[str, Any] | None,
    second: dict[str, Any] | None,
) -> dict[str, bool | None]:
    """Различить точный и нормализованный текст; пропуск не считать совпадением."""

    comparison: dict[str, bool | None] = {
        "same_title_exact": None,
        "same_title_normalized": None,
        "same_abstract_exact": None,
        "same_abstract_normalized": None,
        "same_h2_input_exact": None,
        "same_h2_input_normalized": None,
        "differing_years": None,
    }

    if first is None or second is None:
        return comparison

    for field in ("title", "abstract"):
        first_text = _single_text(first, field)
        second_text = _single_text(second, field)

        if first_text is None or second_text is None:
            continue

        comparison[f"same_{field}_exact"] = first_text == second_text
        comparison[f"same_{field}_normalized"] = _text_key(first_text) == _text_key(second_text)

    for mode in ("exact", "normalized"):
        title_matches = comparison[f"same_title_{mode}"]
        abstract_matches = comparison[f"same_abstract_{mode}"]

        if title_matches is not None and abstract_matches is not None:
            comparison[f"same_h2_input_{mode}"] = title_matches and abstract_matches

    if first["year"] is not None and second["year"] is not None:
        comparison["differing_years"] = first["year"] != second["year"]

    return comparison


def _build_pairs(
    groups: list[dict[str, Any]],
    relations: list[dict[str, Any]],
    cards: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Свести повторяющиеся основания к парам без разворачивания больших групп."""

    pair_relations: dict[tuple[str, str], list[str]] = {}
    groups_by_item: dict[str, set[str]] = {}
    groups_by_id = {group["group_id"]: group for group in groups}

    for group in groups:
        members = group["item_uuids"]

        for item_uuid in members:
            groups_by_item.setdefault(item_uuid, set()).add(group["group_id"])

        if len(members) == 2:
            pair_relations.setdefault(_pair_key(*members), [])

    for relation in relations:
        if relation["target_status"] not in {"in_snapshot", "outside_snapshot"}:
            continue

        key = _pair_key(relation["source_item_uuid"], relation["target_item_uuid"])
        pair_relations.setdefault(key, []).append(relation["relation_id"])

    pairs: list[dict[str, Any]] = []

    for key, relation_ids in sorted(pair_relations.items()):
        group_ids = sorted(groups_by_item.get(key[0], set()) & groups_by_item.get(key[1], set()))
        comparison = _comparison(cards.get(key[0]), cards.get(key[1]))
        flags = {
            flag for group_id in group_ids for flag in groups_by_id[group_id]["review_flags"]
        }

        if any(item_uuid not in cards for item_uuid in key):
            flags.add("target_outside_snapshot")

        if relation_ids:
            flags.add("source_version_relation")

        if comparison["same_h2_input_exact"]:
            flags.add("same_h2_input_exact")

        elif comparison["same_h2_input_normalized"]:
            flags.add("same_h2_input_normalized")

        if comparison["same_abstract_normalized"] and comparison["same_title_normalized"] is False:
            flags.add("same_abstract_different_title")

        pairs.append({
            "pair_id": _pair_identifier(key),
            "item_uuids": list(key),
            "duplicate_group_ids": group_ids,
            "relation_ids": sorted(relation_ids),
            "comparison": comparison,
            "review_flags": sorted(flags),
            "review_decision": None,
            "comment": "",
        })

    return pairs


def _evidence_cards(
    snapshot: CandidateSnapshot,
    candidates: dict[str, dict[str, Any]],
    groups: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Сохранить известные карточки участников, не выдумывая отсутствующие цели ссылок."""

    needed = {item_uuid for group in groups for item_uuid in group["item_uuids"]}
    needed.update(relation["source_item_uuid"] for relation in relations)
    needed.update(
        relation["target_item_uuid"]
        for relation in relations
        if relation["target_status"] == "in_snapshot"
    )
    items = {item["uuid"]: item for item in snapshot.items}
    cards: dict[str, dict[str, Any]] = {}

    for item_uuid in sorted(needed):
        row = candidates.get(f"jinr:{item_uuid}")

        if row is None:
            # Связанный препринт остаётся свидетельством, а не кандидатом Article.
            card = _candidate(items[item_uuid], snapshot.sources[item_uuid])
            card["review_flags"] = [flag for flag in card["review_flags"] if flag != "mixed_document_types"]
            card["is_h2_candidate"] = False

        else:
            card = copy.deepcopy(row)
            card["is_h2_candidate"] = True

        cards[item_uuid] = card

    return cards


def build_duplicate_review(snapshot: CandidateSnapshot) -> dict[str, Any]:
    """Собрать локальные свидетельства для проверки без меток, разбиения и слияния."""

    # Извлечение сначала проверяет UUID и метаданные всех жанров, включая не-Article.
    relations = extract_version_relations(snapshot)
    rows, queue_summary = build_candidate_queue(snapshot)
    candidates = {row["candidate_id"]: row for row in rows}
    groups = _review_groups(queue_summary["duplicate_groups"], candidates)
    cards = _evidence_cards(snapshot, candidates, groups, relations)
    pairs = _build_pairs(groups, relations, cards)
    duplicate_pairs = {
        _pair_key(*group["item_uuids"]) for group in groups if group["member_count"] == 2
    }

    return {
        "cards": list(cards.values()),
        "groups": groups,
        "pairs": pairs,
        "relations": relations,
        "summary": {
            "schema_version": REVIEW_VERSION,
            "status": "review_required",
            "provenance": copy.deepcopy(snapshot.provenance),
            "source_item_count": len(snapshot.items),
            "candidate_count": len(rows),
            "duplicate_group_count": len(groups),
            "duplicate_card_count": queue_summary["rows_with_duplicate_signals"],
            "duplicate_two_card_pair_count": len(duplicate_pairs),
            "large_duplicate_group_count": sum(group["member_count"] > 2 for group in groups),
            "relation_record_count": len(relations),
            "relation_status_counts": dict(sorted(Counter(
                relation["target_status"] for relation in relations
            ).items())),
            "relation_kind_counts": dict(sorted(Counter(relation["kind"] for relation in relations).items())),
            "pair_count": len(pairs),
            "card_count": len(cards),
            "training_ready": False,
            "registries_changed": False,
            "automatic_merges": 0,
            "limitations": [
                "Совпадения ищутся среди Article; связи источника сохраняются для всех жанров снимка.",
                "Пары образованы группами из двух карточек и явными ссылками; большие группы не развёрнуты.",
                "Число пар не равно числу дубликатов; транзитивное объединение не выполнялось.",
                "Текст сравнивается по правилам очереди H2: края строк обрезаны, варианты сохранены отдельно.",
                "Нормализация сравнения: NFKC, регистр и пробелы; оригиналы остаются в metadata_as_reported.",
                "Одинаковая аннотация может быть шаблоном или ошибкой, а не признаком одной статьи.",
                "Связь версий — утверждение источника, а не подтверждённое тождество полных текстов.",
                "Отсутствие цели ссылки в русскоязычном снимке не означает отсутствия публикации.",
                "Неподдержанные связи и некорректные ссылки сохранены для проверки, но не образуют пары.",
                "Пакет не слепой: содержит подсказки источника; решения, метки, разбиения и права не изменены.",
            ],
        },
    }
