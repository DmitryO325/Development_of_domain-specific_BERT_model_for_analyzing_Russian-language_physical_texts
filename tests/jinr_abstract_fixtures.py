"""Синтетические тезисы ОИЯИ с историческим обзором и отдельным согласием."""

from __future__ import annotations

import copy
import json

from pathlib import Path
from typing import Any

from src.corpus.jinr_abstract_input import ABSTRACT_SELECTION_RULE
from src.corpus.manifests import sha256_bytes
from tests.jinr_collection_fixtures import CollectionFixture


class AbstractFixture(CollectionFixture):
    """Создать пакет тезисов, не ослабляя отдельный полнотекстовый образец."""

    def __init__(self, root: Path, *, aliases: int = 1) -> None:
        """Подготовить Book chapter, историческую оценку и отдельное решение о жанре."""

        self.approval: dict[str, Any] = {}
        self.approval_path = "manifests/imports/abstract_scope_approval.json"
        super().__init__(root, aliases=aliases)
        self.path = root / "manifests/imports/abstracts.json"
        self.entry.update({
            "selection_id": f"jinr-abstract-selection:{self.entry['pdf']['sha256']}",
            "genre": "conference_abstract",
        })
        self.selection[0].update({
            "selection_id": self.entry["selection_id"],
            "selection_version": "jinr-abstract-selection-v1",
        })
        self.batch.update({
            "schema_version": "jinr-abstract-registration-preparation-v1",
            "proposed_profile": "jinr_abstracts",
            "selection_rule": copy.deepcopy(ABSTRACT_SELECTION_RULE),
        })
        self.report["records"][0].update({
            "material_kind": "conference_abstract",
            "next_batch_proposal": False,
            "completeness": "complete_abstract_not_full_paper",
        })
        self.report["proposed_next_batch"] = []
        self.save_sources()
        self.approval = {
            "schema_version": "jinr-abstract-scope-approval-v1",
            "approved": True,
            "decision_id": "DEC-023",
            "genre": "conference_abstract",
            "scope": "registration_only",
            "approved_review_ids": [self.report["records"][0]["review_id"]],
        }
        self.save_approval()

    def save_review(self, *, sync_sources: bool = True) -> None:
        """Обновить синтетический обзор и ссылку согласия после тестовой мутации."""

        super().save_review(sync_sources=sync_sources)

        if self.approval:
            self.save_approval()

    def save_approval(self, *, sync_review: bool = True) -> None:
        """Сохранить отдельное согласие и его хеш, не меняя исторический обзор."""

        if sync_review:
            self.approval["source_review"] = {
                field: self.entry["material_review"][field] for field in ("path", "sha256")
            }

        body = json.dumps(self.approval, ensure_ascii=False).encode("utf-8")
        self._write(self.approval_path, body)
        self.entry["genre_approval"] = {
            "path": self.approval_path,
            "sha256": sha256_bytes(body),
            "decision_id": self.approval["decision_id"],
        }
        self.save_batch()
