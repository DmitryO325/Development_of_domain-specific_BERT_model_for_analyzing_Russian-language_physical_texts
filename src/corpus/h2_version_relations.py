"""Связи версий из локальных метаданных ОИЯИ без объединения публикаций."""

from __future__ import annotations

import copy
import hashlib
import json
import re

from typing import Any
from urllib.parse import urlsplit

from .h2_candidate_input import CandidateSnapshot, _validate_metadata
from .jinr_registration import _uuid


RELATION_KINDS = {
    "local.relation.has-preprint": "has_preprint",
    "local.relation.is-preprint-of": "is_preprint_of",
    "local.relation.has-translated-version": "has_translated_version",
    "local.relation.is-translated-version-of": "is_translated_version_of",
}
INVERSE_KINDS = {
    "has_preprint": "is_preprint_of",
    "is_preprint_of": "has_preprint",
    "has_translated_version": "is_translated_version_of",
    "is_translated_version_of": "has_translated_version",
}
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
URL_PATHS = {
    "pubrepo.jinr.ru": (r"/items/", r"/entities/publication/"),
    "pubrepo-api.jinr.ru": (r"/server/api/core/items/",),
}


def _snapshot_items(snapshot: CandidateSnapshot) -> dict[str, dict[str, Any]]:
    """Проверить все карточки и происхождение до извлечения первых связей."""

    if not isinstance(snapshot, CandidateSnapshot) or not isinstance(snapshot.sources, dict):
        raise ValueError("Нужен снимок CandidateSnapshot с происхождением карточек")

    items: dict[str, dict[str, Any]] = {}

    for item in snapshot.items:
        if not isinstance(item, dict):
            raise ValueError("Карточка снимка должна быть JSON-объектом")

        item_uuid = _uuid(item.get("uuid"))

        if item_uuid in items:
            raise ValueError(f"Снимок повторяет UUID карточки: {item_uuid}")

        if not isinstance(snapshot.sources.get(item_uuid), dict):
            raise ValueError(f"Нет проверенного происхождения карточки: {item_uuid}")

        _validate_metadata(item)
        items[item_uuid] = item

    return items


def _target_uuid(raw_value: str) -> str | None:
    """Распознать UUID или строгий адрес карточки ОИЯИ, не обращаясь к сети."""

    # urlsplit удаляет некоторые управляющие символы: не принимаем такой адрес молча.
    if re.search(r"[\x00-\x1f\x7f]", raw_value):
        return

    value = raw_value.strip()

    if re.fullmatch(UUID_PATTERN, value):
        return value.lower()

    try:
        parsed = urlsplit(value)
        port = parsed.port

    except ValueError:
        return

    if parsed.scheme not in ("http", "https") or parsed.hostname not in URL_PATHS:
        return

    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        return

    default_port = 443 if parsed.scheme == "https" else 80

    if port not in (None, default_port):
        return

    for prefix in URL_PATHS[parsed.hostname]:
        match = re.fullmatch(f"{prefix}({UUID_PATTERN})/?", parsed.path)

        if match:
            return match[1].lower()


def _relation_record(
    item_uuid: str,
    field: str,
    value_index: int,
    raw_value: str,
    item_uuids: set[str],
    source: dict[str, Any],
) -> dict[str, Any]:
    """Сохранить отдельное исходное значение, включая повторы и ошибочные ссылки."""

    kind = RELATION_KINDS.get(field, "unsupported")
    target_uuid = _target_uuid(raw_value) if kind != "unsupported" else None

    if kind == "unsupported":
        status = "unsupported_relation"

    elif target_uuid is None:
        status = "invalid_target"

    elif target_uuid == item_uuid:
        status = "self_link"

    elif target_uuid in item_uuids:
        status = "in_snapshot"

    else:
        status = "outside_snapshot"

    identity = json.dumps(
        [item_uuid, field, value_index, raw_value],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    return {
        "relation_id": f"jinr-relation:{hashlib.sha256(identity).hexdigest()}",
        "source_item_uuid": item_uuid,
        "target_item_uuid": target_uuid,
        "field": field,
        "value_index": value_index,
        "raw_value": raw_value,
        "kind": kind,
        "target_status": status,
        "reciprocal": False,
        "source": copy.deepcopy(source),
    }


def extract_version_relations(snapshot: CandidateSnapshot) -> list[dict[str, Any]]:
    """Извлечь все local.relation.* и отметить обратные связи без слияния работ."""

    items = _snapshot_items(snapshot)
    item_uuids = set(items)
    records: list[dict[str, Any]] = []

    for item_uuid, item in sorted(items.items()):
        for field, entries in sorted(item["metadata"].items()):
            if not field.startswith("local.relation."):
                continue

            for value_index, entry in enumerate(entries):
                records.append(_relation_record(
                    item_uuid,
                    field,
                    value_index,
                    entry["value"],
                    item_uuids,
                    snapshot.sources[item_uuid],
                ))

    directed_links = {
        (record["source_item_uuid"], record["target_item_uuid"], record["kind"])
        for record in records
        if record["target_status"] == "in_snapshot"
    }

    for record in records:
        if record["target_status"] != "in_snapshot":
            continue

        inverse = (
            record["target_item_uuid"],
            record["source_item_uuid"],
            INVERSE_KINDS[record["kind"]],
        )
        record["reciprocal"] = inverse in directed_links

    return records
