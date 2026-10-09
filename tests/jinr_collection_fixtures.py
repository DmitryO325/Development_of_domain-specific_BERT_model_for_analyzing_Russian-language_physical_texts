"""Синтетический полнотекстовый материал сборника с отдельной проверкой границ."""

from __future__ import annotations

import copy
import json

from pathlib import Path
from typing import Any

from src.corpus.jinr_collection_input import COLLECTION_SELECTION_RULE
from src.corpus.manifests import sha256_bytes
from tests.jinr_article_fixtures import ArticleFixture


class CollectionFixture(ArticleFixture):
    """Создать проверяемый пакет Book chapter без изменения журнального образца."""

    def __init__(self, root: Path, *, aliases: int = 1) -> None:
        """Подготовить исходники и свидетельство о полном материале научного сборника."""

        self.report: dict[str, Any] = {}
        self.review_path = "manifests/imports/collection_review.json"
        super().__init__(root, aliases=aliases)
        self.path = root / "manifests/imports/collections.json"
        self.item["metadata"]["dc.type"] = [{"value": "Book chapter"}]
        self.item["metadata"]["dc.relation.ispartof"] = [{"value": "Сборник физических исследований"}]
        self.entry.update({
            "selection_id": f"jinr-collection-selection:{self.entry['pdf']['sha256']}",
            "genre": "collection_paper",
        })
        self.selection[0].update({
            "selection_id": self.entry["selection_id"],
            "selection_version": "jinr-collection-selection-v1",
        })
        self.batch.update({
            "schema_version": "jinr-collection-registration-preparation-v1",
            "proposed_profile": "jinr_collections",
            "selection_rule": copy.deepcopy(COLLECTION_SELECTION_RULE),
        })
        self.save_metadata()
        review_id = f"jinr-collection-review:{self.entry['pdf']['sha256']}"
        self.report = {
            "report_version": "jinr-collection-materials-preparation-v1",
            "status": "awaiting_genre_scope_approval",
            "reviewed_by": "assistant",
            "proposed_next_batch": [review_id],
            "implementation_gate": {"approved": False, "python_changes_authorized": False},
            "records": [{
                "review_id": review_id,
                "matches_line": aliases,
                "pdf_path": self.entry["pdf"]["path"],
                "pdf_sha256": self.entry["pdf"]["sha256"],
                "item_uuid": self.item["uuid"],
                "reviewed_identity_status": "document_identity_match",
                "main_text_language": "ru",
                "material_kind": "full_collection_paper",
                "topic_decision": "physics_candidate",
                "next_batch_proposal": True,
                "completeness": "complete_paper",
                "contains_other_publications": False,
                "target_pdf_pages": [1, 2, 3],
                "reviewed_pdf_pages": [1, 2, 3],
                "human_review_status": "pending",
                "original_bitstream_item_relation_verified": False,
                "new_server_relation_verified": False,
                "assigns_grnti_label": False,
                "assigns_training_eligibility": False,
                "registered": False,
                "extraction_probe": {"pdf_sha256": self.entry["pdf"]["sha256"], "pages": 3},
            }],
        }
        self._sync_metadata_copies()
        self.save_review()

    def _sync_metadata_copies(self) -> None:
        """Сохранить название сборника отдельно, не переписывая исходное поле matches."""

        super()._sync_metadata_copies()
        self.entry["collection_title_as_reported"] = self.entry.pop("journal_title_as_reported")
        self.selection[0]["collection_title"] = self.selection[0].pop("journal")

        if self.report:
            self.report["records"][0].update({
                "title_as_reported": self.entry["title"],
                "metadata_source": copy.deepcopy(self.entry["metadata_source"]),
                **{
                    field: copy.deepcopy(self.entry[field]) for field in (
                        "metadata_document_type_as_reported", "collection_title_as_reported",
                        "issued_as_reported", "doi_as_reported",
                    )
                },
            })

    def save_sources(self) -> None:
        """Обновить контрольные суммы исходников и связанного тестового свидетельства."""

        super().save_sources()

        if self.report:
            self.save_review()

    def save_review(self, *, sync_sources: bool = True) -> None:
        """Сохранить свидетельство и его хеш, при необходимости обновив ссылки на описи."""

        if sync_sources:
            self.report["inputs"] = [
                copy.deepcopy(source) for source in self.batch["source_files"]
                if source["path"] in {self.matches_path, self.inventory_path}
            ]

        body = json.dumps(self.report, ensure_ascii=False).encode("utf-8")
        self._write(self.review_path, body)
        self.entry["material_review"] = {
            "path": self.review_path,
            "sha256": sha256_bytes(body),
            "review_id": self.report["records"][0]["review_id"],
        }
        self.save_batch()
