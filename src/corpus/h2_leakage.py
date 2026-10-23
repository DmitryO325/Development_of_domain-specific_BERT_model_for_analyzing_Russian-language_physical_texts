"""Предварительные ограничения против утечки H2 без назначения меток и выборок."""

from __future__ import annotations

import copy
import hashlib
import json
import re

from collections import Counter, defaultdict
from typing import Any

from .h2_candidate_input import CandidateSnapshot
from .h2_candidates import _text_key, _values, build_candidate_queue
from .h2_duplicate_review import build_duplicate_review

REVIEW_VERSION = "h2-leakage-review-v1"


def _identifier(prefix: str, value: Any) -> str:
    """Получить устойчивый ID из канонического JSON без зависимости от порядка ключей."""

    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    return f"{prefix}:{hashlib.sha256(data).hexdigest()}"


def _issue(code: str, members: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    """Сохранить нерешённый вопрос отдельно от предложений по выборкам."""

    return {
        "issue_id": _identifier("issue", [code, sorted(members), evidence]),
        "code": code,
        "status": "review_required",
        "item_uuids": sorted(members),
        "evidence": copy.deepcopy(evidence),
    }


def _constraint(kind: str, members: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    """Предложить совместное размещение, не объявляя карточки одной работой."""

    members = sorted(members)

    return {
        "constraint_id": _identifier("constraint", [kind, members]),
        "kind": kind,
        "item_uuids": members,
        "evidence": copy.deepcopy(evidence),
        "proposed_action": "keep_together",
        "status": "proposed",
        "applied": False,
    }


def _text_constraints(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Сравнить весь вход title+abstract, включая группы больше двух карточек."""

    inputs: dict[str, tuple[str, str]] = {}
    sources: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, Any]] = []

    for candidate in candidates:
        fields = {
            "title": _values(candidate["metadata_as_reported"], "dc.title"),
            "abstract": _values(candidate["metadata_as_reported"], "dc.description.abstract"),
        }
        unavailable = sorted(name for name, values in fields.items() if len(values) != 1)
        item_uuid = candidate["item_uuid"]

        if unavailable:
            issues.append(_issue("unavailable_h2_input", [item_uuid], {
                "unavailable_fields": unavailable,
                "source": candidate["source"],
            }))
            continue

        inputs[item_uuid] = fields["title"][0], fields["abstract"][0]
        sources[item_uuid] = candidate["source"]

    constraints: list[dict[str, Any]] = []

    for mode in ("exact", "normalized"):
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)

        for item_uuid, (title, abstract) in inputs.items():
            key = (title, abstract) if mode == "exact" else (_text_key(title), _text_key(abstract))
            groups[key].append(item_uuid)

        for key, members in sorted(groups.items()):
            if len(members) < 2:
                continue

            # Если нормализация не добавила новых совпадений, достаточно точной группы.
            if mode == "normalized" and len({inputs[member] for member in members}) == 1:
                continue

            constraints.append(_constraint(f"same_h2_input_{mode}", members, {
                "input_mode": f"queue_{mode}",
                "input_sha256": _identifier("input", list(key)).split(":", 1)[1],
                "sources": {member: sources[member] for member in sorted(members)},
            }))

    return constraints, issues, len(inputs)


def _relation_constraints(
    relations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Предложить связь известных версий; внешние и неверные ссылки оставить вопросами."""

    by_pair: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    issues: list[dict[str, Any]] = []

    for relation in relations:
        if relation["target_status"] != "in_snapshot":
            issues.append(_issue(relation["target_status"], [relation["source_item_uuid"]], {
                "relation": relation,
            }))
            continue

        members = tuple(sorted((relation["source_item_uuid"], relation["target_item_uuid"])))
        by_pair[members].append(relation)

    constraints: list[dict[str, Any]] = []

    for members, evidence in sorted(by_pair.items()):
        ordered = sorted(evidence, key=lambda relation: relation["relation_id"])
        constraints.append(_constraint("source_version_relation", list(members), {
            "relation_ids": [relation["relation_id"] for relation in ordered],
            "relations": ordered,
        }))

    return constraints, issues


def _validated_holds(
    practice_holds: list[dict[str, Any]],
    item_uuids: set[str],
) -> dict[str, dict[str, Any]]:
    """Проверить явный резерв пробной разметки, не трактуя его как подтверждение чтения."""

    if not isinstance(practice_holds, list):
        raise TypeError("Резерв пробной разметки должен быть списком записей")

    result: dict[str, dict[str, Any]] = {}

    for hold in practice_holds:
        if not isinstance(hold, dict):
            raise TypeError("Запись резерва должна быть JSON-объектом")

        item_uuid = hold.get("item_uuid")

        if not isinstance(item_uuid, str) or item_uuid not in item_uuids:
            raise ValueError("Карточка пробной разметки отсутствует в проверенном снимке")

        if item_uuid in result or hold.get("candidate_id") != f"jinr:{item_uuid}":
            raise ValueError("Повтор карточки или несовпадение candidate_id в резерве")

        if not isinstance(hold.get("input_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", hold["input_sha256"]):
            raise ValueError("Резерв должен содержать SHA-256 точного входа пробной разметки")

        work_id = hold.get("work_id")

        if "work_id" not in hold or (work_id is not None and (not isinstance(work_id, str) or not work_id.strip())):
            raise ValueError("work_id в резерве должен быть непустой строкой или null")

        if "seen_by_annotators" not in hold or (
            hold["seen_by_annotators"] is not None and type(hold["seen_by_annotators"]) is not bool
        ):
            raise ValueError("Факт просмотра должен быть true, false или null")

        if (
            hold.get("status") != "reserved_for_practice"
            or hold.get("exclude_from_future_evaluation") is not True
            or hold.get("automatic_split_enforcement") is not False
            or hold.get("related_versions_require_check") is not True
        ):
            raise ValueError("Нужен предварительный резерв без применённого разбиения")

        if not isinstance(hold.get("reason"), str) or not hold["reason"].strip():
            raise ValueError("Резерв должен содержать непустое обоснование")

        result[item_uuid] = copy.deepcopy(hold)

    return result


def _component(
    anchor: str,
    constraints_by_item: dict[str, list[dict[str, Any]]],
) -> tuple[set[str], set[str]]:
    """Найти связность для резерва оценки, не создавая семейств научных работ."""

    members: set[str] = {anchor}
    constraint_ids: set[str] = set()
    pending = [anchor]

    while pending:
        item_uuid = pending.pop()

        for constraint in constraints_by_item.get(item_uuid, []):
            constraint_id = constraint["constraint_id"]

            if constraint_id in constraint_ids:
                continue

            constraint_ids.add(constraint_id)

            for neighbour in constraint["item_uuids"]:
                if neighbour not in members:
                    members.add(neighbour)
                    pending.append(neighbour)

    return members, constraint_ids


def _evaluation_holds(
    anchors: dict[str, dict[str, Any]],
    constraints: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Расширить только предварительный резерв на известные связанные входы и версии."""

    by_item: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unresolved: dict[str, list[str]] = defaultdict(list)

    for constraint in constraints:
        for item_uuid in constraint["item_uuids"]:
            by_item[item_uuid].append(constraint)

    for relation in relations:
        if relation["target_status"] != "in_snapshot":
            unresolved[relation["source_item_uuid"]].append(relation["relation_id"])

    results: dict[str, dict[str, Any]] = {}

    for anchor in sorted(anchors):
        if anchor in results:
            continue

        members, constraint_ids = _component(anchor, by_item)
        origins = sorted(members & anchors.keys())
        unresolved_ids = sorted({identifier for member in members for identifier in unresolved[member]})

        for member in sorted(members):
            original = anchors.get(member)
            results[member] = {
                "item_uuid": member,
                "candidate_id": f"jinr:{member}",
                "work_id": original["work_id"] if original is not None else None,
                "input_sha256": original["input_sha256"] if original is not None else None,
                "origin_item_uuids": origins,
                "reason": "reserved_for_practice" if original is not None else "related_to_practice",
                "seen_by_annotators": original["seen_by_annotators"] if original is not None else None,
                "constraint_ids": sorted(constraint_ids),
                "unresolved_relation_ids": unresolved_ids,
                "exclude_from_future_evaluation": True,
                "automatic_split_enforcement": False,
                "status": "proposed",
                "applied": False,
            }

    return [results[item_uuid] for item_uuid in sorted(results)]


def build_h2_leakage_review(
    snapshot: CandidateSnapshot,
    practice_holds: list[dict[str, Any]],
) -> dict[str, Any]:
    """Собрать предложения и нерешённые вопросы без проверки ещё не созданных выборок."""

    duplicate_review = build_duplicate_review(snapshot)
    candidates, _ = build_candidate_queue(snapshot)
    anchors = _validated_holds(practice_holds, {item["uuid"] for item in snapshot.items})
    constraints, issues, comparable_count = _text_constraints(candidates)
    version_constraints, relation_issues = _relation_constraints(duplicate_review["relations"])
    constraints.extend(version_constraints)
    issues.extend(relation_issues)

    # Сигнал по отдельному полю не доказывает ни тождество работ, ни равенство входа.
    for group in duplicate_review["groups"]:
        issues.append(_issue("duplicate_signal", group["item_uuids"], {"group": group}))

    constraints.sort(key=lambda record: record["constraint_id"])
    issues.sort(key=lambda record: record["issue_id"])
    evaluation_holds = _evaluation_holds(anchors, constraints, duplicate_review["relations"])

    return {
        "constraints": constraints,
        "evaluation_holds": evaluation_holds,
        "issues": issues,
        "summary": {
            "schema_version": REVIEW_VERSION,
            "status": "preliminary_review",
            "proposals_only": True,
            "training_ready": False,
            "splits_checked": False,
            "automatic_split_enforcement": False,
            "registries_changed": False,
            "automatic_merges": 0,
            "provenance": copy.deepcopy(snapshot.provenance),
            "counts": {
                "source_items": len(snapshot.items),
                "h2_candidates": len(candidates),
                "comparable_inputs": comparable_count,
                "unavailable_inputs": len(candidates) - comparable_count,
                "constraints": len(constraints),
                "constraint_kinds": dict(sorted(Counter(record["kind"] for record in constraints).items())),
                "practice_anchors": len(anchors),
                "proposed_evaluation_holds": len(evaluation_holds),
                "propagated_holds": len(evaluation_holds) - len(anchors),
                "issues": len(issues),
                "issue_codes": dict(sorted(Counter(record["code"] for record in issues).items())),
            },
            "limitations": [
                "Предложения не применены: ни одна выборка, метка, работа или её права не изменены.",
                "Сравниваются title+abstract очереди Article, не окончательный очищенный вход модели.",
                "Точное сравнение убирает края полей; нормализованное дополнительно учитывает NFKC, регистр и пробелы.",
                "Пустые или неоднозначные поля не сравниваются; смысловые повторы автоматически не определяются.",
                "Совпадения отдельных полей и связи источника не доказывают тождества публикаций или файлов.",
                "Связность расширяет только консервативный резерв оценки, не объединяет work_id или карточки.",
                "Резерв пробных примеров не подтверждает их чтения; фактический просмотр хранится отдельно.",
                "Внешние цели не загружаются; ограниченность снимка не позволяет доказать отсутствие всех утечек.",
                "Не проверены реальные разбиения H2, пересечения с MLM/H1/H3 и финальные очищенные тексты.",
            ],
        },
    }
