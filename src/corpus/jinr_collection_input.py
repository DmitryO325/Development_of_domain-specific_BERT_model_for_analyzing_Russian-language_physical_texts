"""Строгое чтение офлайн-пакета полнотекстовых материалов научных сборников ОИЯИ."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .jinr_article_input import (
    ArticleBatch,
    VerifiedArticle,
    _BatchFormat,
    _expect,
    _manifest_path,
    _object,
    _read_batch,
    _unique,
)
from .jinr_registration import _json_object, _metadata_values, _record_list
from .manifests import sha256_bytes

COLLECTION_SELECTION_RULE = {
    "decision": "physics_candidate",
    "match_status": "document_identity_match",
    "metadata_document_type": "Book chapter",
    "material_kind": "full_collection_paper",
    "deduplicate_by": "pdf_sha256",
    "human_review_status": "pending",
}
COLLECTION_FORMAT = _BatchFormat(
    schema_version="jinr-collection-registration-preparation-v1",
    profile="jinr_collections",
    selection_version="jinr-collection-selection-v1",
    selection_prefix="jinr-collection-selection",
    selection_rule=COLLECTION_SELECTION_RULE,
    document_type="Book chapter",
    container_entry_field="collection_title_as_reported",
    container_selection_field="collection_title",
)


def read_collection_batch(project_root: Path, path: Path) -> ArticleBatch:
    """Проверить происхождение и отдельное свидетельство о полном материале сборника."""

    root = Path(project_root).resolve()
    batch = _read_batch(root, path, COLLECTION_FORMAT)

    for article in batch.entries:
        _expect(article.entry, {"genre": "collection_paper", "journal_id": None}, "жанр сборника")

        if article.entry.get("journal_title") is not None or "journal_title_as_reported" in article.entry:
            raise ValueError("Материал сборника не должен приписывать себе журнальное издание")

        _expect(_object(article.entry.get("review_status"), "review_status"), {
            "human_selection": "pending", "registration": "not_started",
        }, "статус проверки материала")
        _validate_material_review(root, article)

    return batch


def _read_review_report(root: Path, article: VerifiedArticle) -> dict[str, Any]:
    """Проверить хеш обзора и его привязку к тем же исходным описям."""

    reference = _object(article.entry.get("material_review"), "material_review")
    path = _manifest_path(root, reference.get("path"))
    body = path.read_bytes()
    _expect(reference, {"sha256": sha256_bytes(body)}, "material_review")
    report = _json_object(body, "проверка материалов сборников")
    _expect(report, {"report_version": "jinr-collection-materials-preparation-v1"}, "версия проверки")
    sources = _record_list(report.get("inputs"), "inputs проверки")
    _unique(sources, "path")
    expected_paths = {article.entry["identity_evidence"]["matches_path"]}
    expected_paths.update(row["inventory_path"] for row in article.entry["acquisition_evidence"])

    if {source["path"] for source in sources} != expected_paths:
        raise ValueError("Проверка материалов должна ссылаться на те же matches и inventory")

    for source in sources:
        source_path = _manifest_path(root, source["path"])
        _expect(source, {"sha256": sha256_bytes(source_path.read_bytes())}, "исходник проверки")

    return report


def _validate_material_review(root: Path, article: VerifiedArticle) -> None:
    """Отклонить тезисы, чужие фрагменты и неполные страницы даже при типе Book chapter."""

    report = _read_review_report(root, article)
    reference = article.entry["material_review"]
    review_id = f"jinr-collection-review:{article.pdf_sha256}"
    _expect(reference, {"review_id": review_id}, "идентификатор проверки")
    records = _record_list(report.get("records"), "records проверки")
    _unique(records, "review_id")
    selected = [record for record in records if record["review_id"] == review_id]
    proposals = report.get("proposed_next_batch")

    if not isinstance(proposals, list) or review_id not in proposals or len(selected) != 1:
        raise ValueError("Материал должен быть явно предложен к отдельной регистрации в проверке")

    review = selected[0]
    _expect(review, {
        "pdf_path": article.pdf_path,
        "pdf_sha256": article.pdf_sha256,
        "item_uuid": article.item["uuid"],
        "matches_line": article.entry["identity_evidence"]["matches_line"],
        "metadata_source": article.entry["metadata_source"],
        "metadata_document_type_as_reported": ["Book chapter"],
        "title_as_reported": article.entry["title"],
        "collection_title_as_reported": article.entry["collection_title_as_reported"],
        "issued_as_reported": _metadata_values(article.item, "dc.date.issued"),
        "doi_as_reported": _metadata_values(article.item, "dc.identifier.doi"),
        "reviewed_identity_status": "document_identity_match",
        "main_text_language": "ru",
        "material_kind": "full_collection_paper",
        "topic_decision": "physics_candidate",
        "next_batch_proposal": True,
        "completeness": "complete_paper",
        "contains_other_publications": False,
        "human_review_status": "pending",
        "original_bitstream_item_relation_verified": False,
        "new_server_relation_verified": False,
        "assigns_grnti_label": False,
        "assigns_training_eligibility": False,
        "registered": False,
    }, "проверка полного материала")
    probe = _object(review.get("extraction_probe"), "extraction_probe")
    _expect(probe, {"pdf_sha256": article.pdf_sha256}, "PDF пробного извлечения")
    page_count = probe.get("pages")

    if type(page_count) is not int or page_count <= 0:
        raise ValueError("Проверка должна содержать положительное число страниц PDF")

    pages = list(range(1, page_count + 1))

    for field in ("target_pdf_pages", "reviewed_pdf_pages"):
        value = review.get(field)

        if not isinstance(value, list) or any(type(page) is not int for page in value) or value != pages:
            raise ValueError(f"{field}: полный материал должен покрывать все страницы PDF без обрезки")
