"""Строгое чтение офлайн-пакета тезисов ОИЯИ с отдельным согласием на жанр."""

from __future__ import annotations

from pathlib import Path

from .jinr_article_input import (
    ArticleBatch,
    VerifiedArticle,
    _BatchFormat,
    _expect,
    _manifest_path,
    _object,
    _read_batch,
    _text,
    _unique,
)
from .jinr_collection_input import COLLECTION_SELECTION_RULE, _read_review_report
from .jinr_registration import _json_object, _metadata_values, _record_list
from .manifests import sha256_bytes

ABSTRACT_SELECTION_RULE = {
    **COLLECTION_SELECTION_RULE,
    "material_kind": "conference_abstract",
}
ABSTRACT_FORMAT = _BatchFormat(
    schema_version="jinr-abstract-registration-preparation-v1",
    profile="jinr_abstracts",
    selection_version="jinr-abstract-selection-v1",
    selection_prefix="jinr-abstract-selection",
    selection_rule=ABSTRACT_SELECTION_RULE,
    document_type="Book chapter",
    container_entry_field="collection_title_as_reported",
    container_selection_field="collection_title",
)


def read_abstract_batch(project_root: Path, path: Path) -> ArticleBatch:
    """Проверить исходники, границы тезисов и отдельное согласие без записи и сети."""

    root = Path(project_root).resolve()
    batch = _read_batch(root, path, ABSTRACT_FORMAT)

    for article in batch.entries:
        _expect(article.entry, {
            "genre": "conference_abstract", "journal_id": None,
        }, "жанр тезисов")

        if article.entry.get("journal_title") is not None or "journal_title_as_reported" in article.entry:
            raise ValueError("Тезисы не должны приписывать себе журнальное издание")

        _expect(_object(article.entry.get("review_status"), "review_status"), {
            "human_selection": "pending", "registration": "not_started",
        }, "статус проверки тезисов")
        _validate_material_review(root, article)
        _validate_genre_approval(root, article)

    return batch


def _validate_material_review(root: Path, article: VerifiedArticle) -> None:
    """Сверить тезисы с историческим обзором, не превращая их в полный материал."""

    report = _read_review_report(root, article)
    reference = article.entry["material_review"]
    review_id = f"jinr-collection-review:{article.pdf_sha256}"
    _expect(reference, {"review_id": review_id}, "идентификатор проверки")
    records = _record_list(report.get("records"), "records проверки")
    _unique(records, "review_id")
    selected = [record for record in records if record["review_id"] == review_id]

    if len(selected) != 1:
        raise ValueError("Историческая проверка должна содержать ровно одну запись выбранных тезисов")

    # Исторический список следующего пакета относится к полным материалам.
    # Новое согласие хранится отдельно и не меняет прежнюю оценку тезисов.
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
        "material_kind": "conference_abstract",
        "topic_decision": "physics_candidate",
        "next_batch_proposal": False,
        "completeness": "complete_abstract_not_full_paper",
        "contains_other_publications": False,
        "human_review_status": "pending",
        "original_bitstream_item_relation_verified": False,
        "new_server_relation_verified": False,
        "assigns_grnti_label": False,
        "assigns_training_eligibility": False,
        "registered": False,
    }, "проверка тезисов")
    probe = _object(review.get("extraction_probe"), "extraction_probe")
    _expect(probe, {"pdf_sha256": article.pdf_sha256}, "PDF пробного извлечения")
    page_count = probe.get("pages")

    if type(page_count) is not int or page_count <= 0:
        raise ValueError("Проверка должна содержать положительное число страниц PDF")

    pages = list(range(1, page_count + 1))

    for field in ("target_pdf_pages", "reviewed_pdf_pages"):
        value = review.get(field)

        if not isinstance(value, list) or any(type(page) is not int for page in value) or value != pages:
            raise ValueError(f"{field}: тезисы должны покрывать все страницы PDF без обрезки")


def _validate_genre_approval(root: Path, article: VerifiedArticle) -> None:
    """Проверить хеш отдельного решения и разрешение только регистрации этих тезисов."""

    reference = _object(article.entry.get("genre_approval"), "genre_approval")
    path = _manifest_path(root, reference.get("path"))
    body = path.read_bytes()
    _expect(reference, {"sha256": sha256_bytes(body)}, "genre_approval")
    decision_id = _text(reference.get("decision_id"), "decision_id")
    approval = _json_object(body, "согласие на жанр тезисов")
    review = article.entry["material_review"]
    _expect(approval, {
        "schema_version": "jinr-abstract-scope-approval-v1",
        "approved": True,
        "decision_id": decision_id,
        "genre": "conference_abstract",
        "scope": "registration_only",
        "source_review": {"path": review["path"], "sha256": review["sha256"]},
    }, "согласие на жанр тезисов")
    review_ids = approval.get("approved_review_ids")

    if not isinstance(review_ids, list) or not review_ids:
        raise ValueError("approved_review_ids должен содержать идентификаторы разрешённых тезисов")

    identifiers = [_text(value, "approved_review_ids") for value in review_ids]

    if len(set(identifiers)) != len(identifiers):
        raise ValueError("approved_review_ids повторяет идентификатор проверки")

    if review["review_id"] not in identifiers:
        raise ValueError("genre_approval не разрешает регистрацию выбранного review_id")
