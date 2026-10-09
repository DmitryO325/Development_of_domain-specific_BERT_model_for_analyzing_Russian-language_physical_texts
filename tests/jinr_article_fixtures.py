"""Синтетический пакет Article с проверяемыми PDF, API и исходными описями."""

from __future__ import annotations

import copy
import json

from pathlib import Path
from typing import Any
from uuid import UUID

from src.collect.base import HttpResponseSnapshot
from src.corpus.jinr_article_input import ACQUISITION_FIELDS, SELECTION_RULE
from src.corpus.manifests import sha256_bytes

ITEM_UUID = "c1de2fec-3e07-4e15-903c-370df5e5a9c8"
SEARCH_URL = "https://pubrepo-api.jinr.ru/server/api/discover/search/objects?page=0&size=100"
RETRIEVED_AT = "2026-09-21T12:00:00+00:00"


class ArticleFixture:
    """Создать согласованные тестовые источники в отдельном временном каталоге."""

    def __init__(self, root: Path, *, aliases: int = 1) -> None:
        """Подготовить одну статью с заданным числом независимо полученных копий PDF."""

        self.root = root
        self.path = root / "manifests/imports/articles.json"
        self.selection_path = "manifests/results/selection.jsonl"
        self.matches_path = "manifests/imports/matches.jsonl"
        self.inventory_path = "manifests/imports/inventory.jsonl"
        self.pdf_bytes = b"%PDF-1.7\nSynthetic Article\n%%EOF\n"
        self.item: dict[str, Any] = {
            "id": ITEM_UUID, "uuid": ITEM_UUID, "type": "item",
            "handle": "123456789/100",
            "_links": {"self": {"href": f"https://pubrepo-api.jinr.ru/server/api/core/items/{ITEM_UUID}"}},
            "metadata": {
                "dc.title": [{"value": "Нейтронные свойства вещества"}],
                "dc.contributor.author": [{"value": "Иванов И. И."}],
                "dc.date.issued": [{"value": "2024"}],
                "dc.identifier.doi": [{"value": "10.1000/ARTICLE"}],
                "dc.type": [{"value": "Article"}],
                "dc.language.iso": [{"value": "ru"}],
                "dc.relation.ispartof": [{"value": "Тестовый физический журнал"}],
                "dc.identifier.uri": [{"value": "https://pubrepo.jinr.ru/handle/123456789/100"}],
                "dc.description.abstract": [{"value": "Аннотация статьи."}],
                "dc.subject": [{"value": "нейтроны"}],
            },
        }
        self.inventory: list[dict[str, Any]] = []
        self.matches: list[dict[str, Any]] = []
        digest = sha256_bytes(self.pdf_bytes)

        for index in range(aliases):
            bitstream_uuid = str(UUID(int=index + 1))
            path = f"data/raw/pdf/manual/{bitstream_uuid}.pdf"
            self._write(path, self.pdf_bytes)
            self.inventory.append({
                "inventory_schema_version": "jinr-download-inventory-v1",
                "intake_id": f"jinr-bitstream:{bitstream_uuid}", "bitstream_uuid": bitstream_uuid,
                "staged_relative_path": path, "sha256": digest, "bytes": len(self.pdf_bytes),
                "source_url": f"https://pubrepo-api.jinr.ru/server/api/core/bitstreams/{bitstream_uuid}/content",
                "acquisition_agent": "Yandex", "acquisition_method": "manual_download",
                "acquisition_scope": "bulk", "original_filename": f"original-{index}.pdf",
                "retrieved_at": f"2026-09-03T20:00:{index:02d}+03:00",
                "file_modified_at": f"2026-09-03T20:00:{index:02d}+03:00",
                "pdf_signature_valid": True, "item_uuid": None, "canonical_item_url": None,
            })
            self.matches.append({
                "schema_version": "jinr-local-pdf-metadata-match-v1",
                "intake_id": self.inventory[-1]["intake_id"], "pdf_path": path,
                "pdf_sha256": digest, "bitstream_uuid": bitstream_uuid,
                "original_filename": self.inventory[-1]["original_filename"],
                "download_source_url": self.inventory[-1]["source_url"],
                "match_status": "document_identity_match", "confidence": "high",
                "match_methods": ["exact_doi_in_first_pdf_page"],
                "bitstream_item_relation_verified": False, "candidate_item_ids": [ITEM_UUID],
                "evidence": {"pdf_page_numbers": [1], "first_page_dois": ["10.1000/article"]},
                "inventory_source": {"path": self.inventory_path, "line": index + 1},
                "matched_item": {},
            })

        self.entry: dict[str, Any] = {
            "selection_id": f"jinr-article-selection:{digest}", "selection_display_index": 1,
            "item_uuid": ITEM_UUID, "genre": None, "journal_id": None, "flags": [],
            "pdf": {"path": self.inventory[-1]["staged_relative_path"], "sha256": digest,
                    "aliases": [row["staged_relative_path"] for row in self.inventory]},
            "acquisition_evidence": [
                {"inventory_path": self.inventory_path, "inventory_line": index + 1,
                 **{field: row[source] for field, source in ACQUISITION_FIELDS.items()}}
                for index, row in enumerate(self.inventory)
            ],
            "identity_evidence": {
                "matches_path": self.matches_path, "matches_line": aliases,
                "match_status": "document_identity_match", "confidence": "high",
                "match_methods": ["exact_doi_in_first_pdf_page"],
                "bitstream_item_relation_verified": False,
            },
            "review_status": {"human_selection": "pending", "registration": "not_started"},
        }
        self.selection: list[dict[str, Any]] = [{
            "selection_version": "jinr-article-selection-v1",
            "selection_id": self.entry["selection_id"], "display_index": 1,
            "decision": "physics_candidate", "identity_status": "document_identity_match",
            "human_review_status": "pending", "item_uuid": ITEM_UUID,
            "bitstream_uuid": self.inventory[-1]["bitstream_uuid"],
            "pdf_path": self.entry["pdf"]["path"], "pdf_sha256": digest,
            "pdf_aliases": list(self.entry["pdf"]["aliases"]), "flags": [],
        }]
        self.batch: dict[str, Any] = {
            "schema_version": "jinr-article-registration-preparation-v1",
            "status": "prepared_not_registered", "proposed_profile": "jinr_articles",
            "registration_performed": False, "network_used": False,
            "candidate_count": 1, "pdf_path_count": aliases,
            "selection_rule": copy.deepcopy(SELECTION_RULE), "entries": [self.entry],
        }
        self.save_metadata()

    def _write(self, path: str, body: bytes) -> None:
        """Записать только синтетические данные внутри переданного временного каталога."""

        destination = self.root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)

    def save_metadata(self, *, sync_copies: bool = True) -> None:
        """Пересоздать API, канонические HTTP-метаданные и кеш после изменения карточки."""

        body = json.dumps({"_embedded": {"searchResult": {"_embedded": {
            "objects": [{"_embedded": {"indexableObject": self.item}}],
        }}}}, ensure_ascii=False).encode("utf-8")
        digest = sha256_bytes(body)
        relative_path = f"responses/{digest[:2]}/{digest}.json"
        snapshot = HttpResponseSnapshot(
            requested_url=SEARCH_URL, final_url=SEARCH_URL, status_code=200,
            headers=(("content-length", str(len(body))), ("content-type", "application/json")),
            retrieved_at=RETRIEVED_AT, body=body,
        )
        metadata_body = snapshot.canonical_metadata()
        metadata_digest = sha256_bytes(metadata_body)
        metadata_path = f"responses/{digest[:2]}/{digest}.{metadata_digest}.http.json"
        cache = {
            "kind": "json", "bytes": len(body), "requested_url": SEARCH_URL,
            "relative_path": relative_path, "sha256": digest,
            "response_metadata_path": metadata_path, "response_metadata_sha256": metadata_digest,
        }
        cache_body = json.dumps(cache, ensure_ascii=False).encode("utf-8")
        cache_path = "manifests/imports/jinr_api/cache/page.json"
        self._write(f"data/raw/jinr_api/{relative_path}", body)
        self._write(f"data/raw/jinr_api/{metadata_path}", metadata_body)
        self._write(cache_path, cache_body)
        source = {
            "api_response_path": f"data/raw/jinr_api/{relative_path}",
            "api_response_sha256": digest, "api_requested_url": SEARCH_URL,
            "cache_record_path": cache_path, "cache_record_sha256": sha256_bytes(cache_body),
        }
        self.entry["metadata_source"] = copy.deepcopy(source)
        self.selection[0]["metadata_source"] = {
            field: source[field] for field in ("api_response_path", "api_response_sha256")
        }

        for match in self.matches:
            match["metadata_source"] = copy.deepcopy(source)

        if sync_copies:
            self._sync_metadata_copies()

        self.save_sources()

    def _sync_metadata_copies(self) -> None:
        """Обновить производные поля для сценариев с согласованным новым API."""

        title = self.item["metadata"]["dc.title"][0]["value"]
        self.entry["title"] = title
        self.selection[0]["title"] = title
        self.selection[0]["language_as_reported"] = ["ru"]

        for match in self.matches:
            match["matched_item"].update({"item_uuid": self.item["uuid"], "title": title, "language": ["ru"]})

        for entry_field, source_field, api_field in (
            ("metadata_document_type_as_reported", "document_type", "dc.type"),
            ("journal_title_as_reported", "journal", "dc.relation.ispartof"),
            ("issued_as_reported", "issued", "dc.date.issued"),
            ("doi_as_reported", "doi", "dc.identifier.doi"),
        ):
            values = list(dict.fromkeys(row["value"] for row in self.item["metadata"].get(api_field, [])))
            self.entry[entry_field] = values

            if source_field != "document_type":
                self.selection[0][source_field] = list(values)

            for match in self.matches:
                match["matched_item"][source_field] = list(values)

    def save_sources(self) -> None:
        """Обновить JSONL-описи и все их контрольные суммы в правильном порядке."""

        self.batch["source_files"] = []

        for path, records in (
            (self.inventory_path, self.inventory),
            (self.matches_path, self.matches),
            (self.selection_path, self.selection),
        ):
            body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records).encode("utf-8")
            self._write(path, body)
            digest = sha256_bytes(body)
            self.batch["source_files"].append({"path": path, "sha256": digest})

            if path == self.inventory_path:
                for match in self.matches:
                    match["inventory_source"]["sha256"] = digest

        self.save_batch()

    def save_batch(self) -> None:
        """Сохранить только производный тестовый пакет."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.batch, ensure_ascii=False), encoding="utf-8")
