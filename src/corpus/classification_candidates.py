"""Подготовка проверяемых исходных классификационных кодов без записи в реестры."""

from __future__ import annotations

import copy
import json
import re

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .classification_codes import extract_classification_codes
from .h2_candidate_export import _checked_path
from .h2_candidate_input import CandidateSnapshot, read_candidate_snapshot
from .jinr_registration import _json_object
from .manifests import canonical_json, sha256_bytes
from .schema_validation import SchemaCatalog

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "manifests" / "schemas"
PACKAGE_VERSION = "work-classification-candidates-v1"
SYSTEMS = ("udc", "pacs", "grnti", "mrnti")
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _records(
    root: Path, kind: str, catalog: SchemaCatalog,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Прочитать и проверить реестр, закрепив хеш его точных байтов."""

    path = _checked_path(root, Path("manifests") / f"{kind}.jsonl")
    content = path.read_bytes()
    records: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    key = "work_id" if kind == "works" else "artifact_record_id"

    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue

        record = _json_object(line, f"{path}:{line_number}")
        catalog.validate(kind, record)
        identifier = record[key]

        if identifier in identifiers:
            raise ValueError(f"Повторный {key} в {kind}: {identifier}")

        identifiers.add(identifier)
        records.append(record)

    return records, {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256_bytes(content),
    }


def _candidate(
    work: dict[str, Any], group: dict[str, Any], source: dict[str, Any],
) -> dict[str, Any]:
    """Связать находку с работой и точным свидетельством, не утверждая её меткой."""

    observations = source.get("retrievals") or [{
        "retrieved_url": source["source_url"],
        "retrieved_at": source["retrieved_at"],
    }]
    signals: list[dict[str, str]] = []

    for code in group["codes"]:
        for observation in observations:
            signal = {
                "code": code,
                "source_url": observation["retrieved_url"],
                "retrieved_at": observation["retrieved_at"],
                "evidence_sha256": source["sha256"],
            }

            if signal not in signals:
                signals.append(signal)

    identity = {
        "work_id": work["work_id"], "field": group["field"],
        "source": source, "raw_value": group["raw_value"],
        "line_start": group["line_start"], "line_end": group["line_end"],
    }

    return {
        **copy.deepcopy(group),
        "schema_version": PACKAGE_VERSION,
        "candidate_id": "classification:" + sha256_bytes(canonical_json(identity).encode("utf-8")),
        "work_id": work["work_id"],
        "title": work["title"],
        "review_status": "pending",
        "proposed_signals": signals,
        "source": copy.deepcopy(source),
    }


def _text_candidates(
    root: Path, work: dict[str, Any], artifact: dict[str, Any],
) -> list[dict[str, Any]]:
    """Извлечь явные коды из проверенного TXT с сохранением строк и происхождения."""

    if not isinstance(artifact["path"], str) or not artifact["retrievals"]:
        raise ValueError("Текстовый артефакт должен иметь путь и события получения")

    path = _checked_path(root, Path(artifact["path"]))

    if not path.is_relative_to(root / "data"):
        raise ValueError(f"Текстовый артефакт находится вне data/: {path}")

    content = path.read_bytes()
    digest = sha256_bytes(content)

    if (
        digest != artifact["sha256"] or len(content) != artifact["bytes"]
        or artifact["artifact_id"] != f"sha256:{digest}"
    ):
        raise ValueError(f"Байты или SHA-256 текста не совпадают с реестром: {path}")

    text = content.decode("utf-8")
    retrieval = artifact["retrievals"][0]
    source = {
        "kind": "registered_text",
        "path": path.relative_to(root).as_posix(),
        "sha256": digest,
        "source_url": retrieval["retrieved_url"],
        "retrieved_at": retrieval["retrieved_at"],
        "retrievals": artifact["retrievals"],
        "artifact_record_id": artifact["artifact_record_id"],
        "parent_artifact_id": artifact["parent_artifact_id"],
        "representation": artifact["representation"],
        "extraction_version": artifact["extraction_version"],
    }
    rows = [_candidate(work, group, source) for group in extract_classification_codes(text)]

    if artifact["representation"] == "ocr_text":
        for row in rows:
            row["flags"].append("ocr_source")

    return rows


def _metadata_candidates(
    works: list[dict[str, Any]], snapshot: CandidateSnapshot,
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Перенести local.udc только из точно связанной карточки ОИЯИ."""

    items = {item["uuid"]: item for item in snapshot.items}
    owners: dict[str, set[str]] = defaultdict(set)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    matched = 0

    for work in works:
        if work["source_id"].startswith("F04_JINR_"):
            prefix = f"source_native_id:{work['source_id']}:"

            for alias in work["work_aliases"]:
                if alias.startswith(prefix):
                    owners[alias.removeprefix(prefix)].add(work["work_id"])

    for work in works:
        if not work["source_id"].startswith("F04_JINR_"):
            continue

        prefix = f"source_native_id:{work['source_id']}:"
        identifiers = {
            alias.removeprefix(prefix)
            for alias in work["work_aliases"] if alias.startswith(prefix)
        }

        if len(identifiers) != 1 or not UUID_PATTERN.fullmatch(next(iter(identifiers))):
            warnings.append(f"{work['work_id']}: нет однозначного UUID ОИЯИ; метаданные пропущены")
            continue

        item_uuid = next(iter(identifiers))

        if len(owners[item_uuid]) != 1:
            warnings.append(f"{work['work_id']}: UUID {item_uuid} связан с несколькими работами")
            continue

        if item_uuid not in items:
            warnings.append(f"{work['work_id']}: карточки {item_uuid} нет в снимке")
            continue

        matched += 1
        source = snapshot.sources[item_uuid]
        entries = items[item_uuid]["metadata"].get("local.udc", [])

        for entry_index, entry in enumerate(entries):
            raw_value = entry["value"]

            if not raw_value.strip():
                continue

            groups = extract_classification_codes("УДК " + raw_value)

            # Поле API уже обозначает систему; нельзя превращать вложенный
            # маркер из испорченного значения в другую систему классификации.
            if len(groups) != 1 or groups[0]["system"] != "udc":
                group = {
                    "system": "udc", "field": "udc_codes_raw", "codes": [],
                    "flags": ["invalid_code"],
                }

            else:
                group = groups[0]

            group.update({
                "raw_value": raw_value, "raw_excerpt": raw_value,
                "line_start": None, "line_end": None,
            })
            rows.append(_candidate(work, group, {
                **source,
                "kind": "jinr_metadata",
                "path": source["page_path"],
                "sha256": source["page_sha256"],
                "source_url": source["requested_url"],
                "item_uuid": item_uuid,
                "metadata_field": "local.udc",
                "value_index": entry_index,
            }))

    return rows, matched, warnings


def _mark_variants(rows: list[dict[str, Any]]) -> None:
    """Отметить разные наборы кодов одной системы, не выбирая правильный вариант."""

    variants: dict[tuple[str, str], set[tuple[str, ...]]] = defaultdict(set)

    for row in rows:
        if row["codes"]:
            variants[(row["work_id"], row["system"])].add(tuple(sorted(set(row["codes"]))))

    for row in rows:
        if len(variants[(row["work_id"], row["system"])]) > 1:
            row["flags"].append("source_variants")


def build_classification_candidates(
    project_root: Path, *, snapshot_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Подготовить находки для зарегистрированных работ без сети и изменения данных."""

    root = Path(project_root).resolve()
    catalog = SchemaCatalog(SCHEMA_DIR)
    works, works_input = _records(root, "works", catalog)
    artifacts, artifacts_input = _records(root, "artifacts", catalog)
    work_index = {work["work_id"]: work for work in works}
    input_files = [works_input, artifacts_input]
    rows: list[dict[str, Any]] = []
    scanned = 0
    text_work_ids: set[str] = set()

    for artifact in artifacts:
        if artifact["work_id"] not in work_index:
            raise ValueError(f"Артефакт ссылается на неизвестную работу: {artifact['work_id']}")

        if (
            artifact["representation"] not in {"plain_text", "ocr_text"}
            or artifact["content_role"] != "full_text"
            or artifact["extraction_status"] != "succeeded"
        ):
            continue

        rows.extend(_text_candidates(root, work_index[artifact["work_id"]], artifact))
        input_files.append({"path": artifact["path"], "sha256": artifact["sha256"]})
        text_work_ids.add(artifact["work_id"])
        scanned += 1

    warnings = [
        f"{work_id}: нет зарегистрированного извлечённого полного текста"
        for work_id in sorted(set(work_index) - text_work_ids)
    ]
    metadata_matched = 0
    snapshot_provenance = None

    if snapshot_path is not None:
        path = _checked_path(root, snapshot_path)
        snapshot = read_candidate_snapshot(root, path)
        metadata_rows, metadata_matched, metadata_warnings = _metadata_candidates(works, snapshot)
        rows.extend(metadata_rows)
        warnings.extend(metadata_warnings)
        snapshot_provenance = snapshot.provenance
        input_files.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": snapshot.provenance["snapshot_sha256"],
        })

        for page in snapshot.provenance["source_pages"]:
            input_files.append({"path": page["path"], "sha256": page["sha256"]})

            if "http_evidence_path" in page:
                input_files.append({
                    "path": page["http_evidence_path"],
                    "sha256": page["http_evidence_sha256"],
                })

    _mark_variants(rows)
    rows.sort(key=lambda row: (row["work_id"], row["system"], row["candidate_id"]))
    summary = {
        "schema_version": PACKAGE_VERSION,
        "work_count": len(works), "text_artifacts_scanned": scanned,
        "metadata_items_matched": metadata_matched,
        "candidate_count": len(rows),
        "works_with_candidates": len({row["work_id"] for row in rows}),
        "by_system": {
            system: {
                "candidate_count": sum(row["system"] == system for row in rows),
                "code_count": sum(len(row["codes"]) for row in rows if row["system"] == system),
                "work_count": len({row["work_id"] for row in rows if row["system"] == system}),
            }
            for system in SYSTEMS
        },
        "by_source_kind": dict(Counter(row["source"]["kind"] for row in rows)),
        "flag_counts": dict(Counter(flag for row in rows for flag in row["flags"])),
        "warnings": warnings,
        "input_files": input_files,
        "metadata_snapshot": snapshot_provenance,
        "registry_changed": False, "labels_assigned": False,
        "review_status": "pending",
    }

    return rows, summary


def export_classification_candidates(
    project_root: Path, output_dir: Path,
    rows: list[dict[str, Any]], summary: dict[str, Any],
) -> Path:
    """Записать отдельный пакет кандидатов без перезаписи и изменения реестров."""

    root = Path(project_root).resolve()
    output = _checked_path(root, output_dir)
    base = root / "manifests" / "results"

    if output == base or not output.is_relative_to(base):
        raise ValueError("Нужен отдельный каталог внутри manifests/results")

    if output.exists():
        raise ValueError("Каталог результата уже существует; выберите новую версию")

    for source in summary["input_files"]:
        path = _checked_path(root, Path(source["path"]))

        if sha256_bytes(path.read_bytes()) != source["sha256"]:
            raise ValueError(f"Исходный файл изменился после извлечения: {path}")

    candidates = "".join(canonical_json(row) + "\n" for row in rows).encode("utf-8")
    report = {
        **summary,
        "files": {"candidates.jsonl": {"sha256": sha256_bytes(candidates), "bytes": len(candidates)}},
    }
    payloads = {
        "candidates.jsonl": candidates,
        "summary.json": (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(exist_ok=False)

    for name, content in payloads.items():
        with (output / name).open("xb") as stream:
            stream.write(content)

    return output
