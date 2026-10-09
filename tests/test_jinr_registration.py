"""Проверки офлайн-регистрации препринтов и их HTTP-происхождения."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any

from src.collect.base import HttpResponseSnapshot
from src.corpus.jinr_registration import (
    plan_preprint_download,
    preprint_extraction_cards,
    read_preprint_download,
    reconcile_preprint_plan,
)
from src.corpus.manifests import ManifestPlan, ManifestStore, sha256_bytes
from src.corpus.registration import RegistrationOptions

ROOT = Path(__file__).resolve().parents[1]
ITEM_UUID = "c1de2fec-3e07-4e15-903c-370df5e5a9c8"
PDF_URL = "http://www1.jinr.ru/Preprints/2024/05(P11-2024-5).pdf"
SEARCH_URL = "https://pubrepo-api.jinr.ru/server/api/discover/search/objects?page=0&size=100"
PDF_BYTES = b"%PDF-1.7\nSynthetic preprint\n%%EOF\n"
RETRIEVED_AT = "2026-09-28T19:05:46.997699+00:00"


class JinrRegistrationTests(unittest.TestCase):
    """Проверки источников, семантики препринта и неизменяемой регистрации."""

    def setUp(self) -> None:
        """Подготовить синтетические исходные ответы в отдельном каталоге."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.path = self.root / "manifests/imports/preprint.json"
        self.path.parent.mkdir(parents=True)
        self.item = {
            "uuid": ITEM_UUID,
            "id": ITEM_UUID,
            "type": "item",
            "_links": {"self": {"href": f"https://pubrepo-api.jinr.ru/server/api/core/items/{ITEM_UUID}"}},
            "metadata": {
                "dc.title": [{"value": "Квантовые свойства плазмы"}],
                "dc.contributor.author": [{"value": "Иванов И. И."}],
                "dc.date.issued": [{"value": "2024"}],
                "dc.type": [{"value": "Preprint"}],
                "dc.language.iso": [{"value": "ru"}],
                "dc.identifier.uri": [{"value": "https://pubrepo.jinr.ru/handle/123456789/100"}],
                "dc.description.abstract": [{"value": "Аннотация препринта."}],
                "dc.subject": [{"value": "плазма"}],
                "local.identifier.JINR-preprint": [{"value": "P11-2024-5"}],
                "local.publication.uri": [{"value": PDF_URL}],
            },
        }
        self.description = {
            "schema_version": "jinr-preprint-download-v1",
            "status": "downloaded",
            "source_field": "local.publication.uri",
            "item_uuid": ITEM_UUID,
            "item_title": "Квантовые свойства плазмы",
            "metadata_sources": [],
            "files": [self.save_response(PDF_URL, PDF_BYTES, "application/pdf", pdf=True)],
        }
        self.save_metadata()
        self.options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="crawler",
            acquisition_scope="bulk",
            rights_record_ids=("right-crawler", "right-storage"),
            extraction_method="not_started",
            extraction_version="not-started-v1",
            response_representation="pdf",
            request_context_type="work",
        )

    def tearDown(self) -> None:
        """Удалить только временные данные проверки."""

        self.temporary_directory.cleanup()

    def save_response(
        self, url: str, body: bytes, mime_type: str, *, pdf: bool = False,
        status: int = 200, final_url: str | None = None,
    ) -> dict[str, Any]:
        """Создать синтетическое неизменяемое тело ответа и HTTP-свидетельство."""

        digest = sha256_bytes(body)
        directory = self.root / "data/raw/responses" / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        body_path = directory / f"{digest}.{'pdf' if pdf else 'json'}"
        body_path.write_bytes(body)
        snapshot = HttpResponseSnapshot(
            requested_url=url,
            final_url=final_url or url,
            status_code=status,
            headers=(("content-length", str(len(body))), ("content-type", mime_type)),
            retrieved_at=RETRIEVED_AT,
            body=body,
        )
        metadata = snapshot.canonical_metadata()
        metadata_sha256 = sha256_bytes(metadata)
        metadata_path = directory / f"{digest}.{metadata_sha256}.http.json"
        metadata_path.write_bytes(metadata)
        evidence: dict[str, Any] = {
            "requested_url": url,
            "body_path": str(body_path),
            "body_sha256": digest,
            "response_metadata_path": str(metadata_path),
            "response_metadata_sha256": metadata_sha256,
        }

        if pdf:
            evidence["size_bytes"] = len(body)

        return evidence

    def save_metadata(self) -> None:
        """Пересоздать исходную страницу API после намеренного изменения тестовой карточки."""

        response = {"_embedded": {"searchResult": {"_embedded": {
            "objects": [{"_embedded": {"indexableObject": self.item}}],
        }}}}
        body = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.description["metadata_sources"] = [self.save_response(SEARCH_URL, body, "application/json")]
        self.save_description()

    def save_description(self) -> None:
        """Записать производную тестовую опись."""

        self.path.write_text(json.dumps(self.description, ensure_ascii=False), encoding="utf-8")

    def plan(self, collected_at: str | None = None) -> ManifestPlan:
        """Получить план с раздельными разрешениями на API и PDF."""

        return plan_preprint_download(
            self.root, self.path, self.options,
            collected_at=collected_at,
            metadata_rights_record_ids=("right-api", "right-storage"),
        )

    def test_plan_preserves_preprint_and_http_provenance(self) -> None:
        """Год, жанр и HTTP-происхождение должны сохраняться без выдуманных полей."""

        timestamp = "2026-09-29T12:00:00+00:00"
        plan = self.plan(collected_at=timestamp)
        work = plan.works[0]
        artifact = plan.artifacts[0]
        metadata_event, pdf_event = plan.retrieval_events
        alias = plan.work_aliases[0]

        self.assertEqual(work["genre"], "preprint")
        self.assertEqual(work["published_year"], 2024)
        self.assertIsNone(work["published_at"])
        self.assertIsNone(work["journal_id"])
        self.assertIsNone(work["journal_title"])
        self.assertEqual(work["eligibility_status"], "pending")
        self.assertEqual(work["abstract"], "Аннотация препринта.")
        self.assertEqual(plan.blobs, [])
        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(artifact["representation"], "pdf")
        self.assertEqual(artifact["acquisition_method"], "crawler")
        self.assertEqual(pdf_event["http_status"], 200)
        self.assertEqual(pdf_event["final_url"], PDF_URL)
        self.assertEqual(pdf_event["retrieved_at"], RETRIEVED_AT)
        self.assertEqual(pdf_event["response_path"], artifact["path"])
        self.assertEqual(metadata_event["acquisition_method"], "api")
        self.assertEqual(metadata_event["request_context_type"], "source")
        self.assertEqual(metadata_event["rights_record_ids"], ["right-api", "right-storage"])
        self.assertEqual(alias["source_retrieval_id"], metadata_event["retrieval_id"])
        self.assertEqual(alias["evidence_sha256"], metadata_event["response_sha256"])

        for record in (work, artifact, metadata_event, pdf_event, alias):
            self.assertEqual(record["created_at"], timestamp)

        self.assertEqual(alias["verified_at"], timestamp)

    def test_extraction_cards_do_not_claim_manual_download(self) -> None:
        """Карточки для извлечения не должны приписывать загрузку браузеру."""

        download = read_preprint_download(self.root, self.path)
        cards = preprint_extraction_cards(download)

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].genre, "preprint")
        self.assertIn("download_jinr.py", cards[0].acquisition_agent)
        self.assertIsNone(cards[0].published_at)
        self.assertTrue((self.root / cards[0].relative_path).is_file())

    def test_full_date_and_doi_are_not_discarded(self) -> None:
        """При наличии точной даты и DOI сохраняются именно эти сведения."""

        self.item["metadata"]["dc.date.issued"] = [{"value": "2024-05-28"}]
        self.item["metadata"]["dc.identifier.doi"] = [{"value": "https://doi.org/10.1000/PREPRINT"}]
        self.save_metadata()
        plan = self.plan()

        self.assertEqual(plan.works[0]["published_at"], "2024-05-28")
        self.assertEqual(plan.works[0]["doi"], "10.1000/preprint")
        self.assertEqual(len(plan.work_aliases), 2)

    def test_modified_body_metadata_and_size_are_rejected(self) -> None:
        """Хеши и размер должны проверяться до составления реестровых записей."""

        original = copy.deepcopy(self.description)

        for field, value in (("body_sha256", "0" * 64), ("response_metadata_sha256", "0" * 64), ("size_bytes", 1)):
            with self.subTest(field=field):
                self.description = copy.deepcopy(original)
                self.description["files"][0][field] = value
                self.save_description()

                with self.assertRaises(ValueError):
                    self.plan()

    def test_description_identity_title_and_source_url_are_verified(self) -> None:
        """Опись не может переименовать статью или назначить другой UUID и PDF."""

        original = copy.deepcopy(self.description)

        for field, value in (("item_uuid", "c1b898f8-e68c-4aed-8d11-3f48001dbaaa"), ("item_title", "Другое название")):
            with self.subTest(field=field):
                self.description = copy.deepcopy(original)
                self.description[field] = value
                self.save_description()

                with self.assertRaises(ValueError):
                    self.plan()

        self.description = original
        self.item["metadata"]["local.publication.uri"] = [{"value": "http://www1.jinr.ru/Preprints/2024/other.pdf"}]
        self.save_metadata()

        with self.assertRaisesRegex(ValueError, "local.publication.uri"):
            self.plan()

    def test_path_escape_and_symlink_escape_are_rejected(self) -> None:
        """Даже существующий PDF нельзя читать за пределами project/data."""

        original = Path(self.description["files"][0]["body_path"])
        outside_data = self.root / "outside.pdf"
        outside_data.write_bytes(PDF_BYTES)
        symlink = original.parent / "escape.pdf"
        symlink.symlink_to(outside_data)

        for path in (outside_data, symlink):
            with self.subTest(path=path):
                self.description["files"][0]["body_path"] = str(path)
                self.save_description()

                with self.assertRaisesRegex(ValueError, "каталог"):
                    self.plan()

    def test_http_status_mime_redirect_signature_are_rejected(self) -> None:
        """Корректные хеши не заменяют проверку успешного PDF-ответа."""

        for changes in (
            {"status": 403},
            {"mime_type": "text/html"},
            {"final_url": "https://example.org/wrong.pdf"},
            {"body": b"not a PDF"},
            {"url": "https://example.org/file.pdf"},
        ):
            with self.subTest(changes=changes):
                values = {"url": PDF_URL, "body": PDF_BYTES, "mime_type": "application/pdf", "pdf": True, **changes}
                self.description["files"] = [self.save_response(**values)]
                self.save_description()

                with self.assertRaises(ValueError):
                    self.plan()

    def test_non_preprint_or_non_russian_item_is_rejected(self) -> None:
        """Домен репозитория сам по себе не подтверждает жанр или язык."""

        for field, value in (("dc.type", "Article"), ("dc.language.iso", "en")):
            with self.subTest(field=field):
                original = copy.deepcopy(self.item)
                self.item["metadata"][field] = [{"value": value}]
                self.save_metadata()

                with self.assertRaises(ValueError):
                    self.plan()

                self.item = original

    def test_repeated_item_in_metadata_is_rejected(self) -> None:
        """Несколько доказательств одного UUID не должны незаметно выбирать карточку."""

        self.description["metadata_sources"] *= 2
        self.save_description()

        with self.assertRaisesRegex(ValueError, "повторяет"):
            self.plan()

    def right(self, name: str) -> dict[str, Any]:
        """Создать разрешение только для синтетического интеграционного теста."""

        acquisition = name != "storage"

        return {
            "schema_version": "rights-v1",
            "created_at": "2026-09-01T00:00:00+00:00",
            "rights_record_id": f"right-{name}",
            "scope_type": "source_group",
            "scope_id": "F04_JINR_REPOSITORY",
            "operation": "acquisition" if acquisition else "storage",
            "status": "allowed",
            "access_basis": "Исключительно синтетическое право в тесте.",
            "basis_type": "explicit_license",
            "acquisition_method": name if acquisition else None,
            "acquisition_scope": "bulk" if acquisition else None,
            "terms_url": "https://example.invalid/license",
            "rights_checked_at": "2026-09-01",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": None,
            "supersedes_rights_record_id": None,
        }

    def test_manifest_commit_is_idempotent_with_valid_test_rights(self) -> None:
        """Повторная регистрация не меняет исходные файлы и не удваивает записи."""

        store = ManifestStore(
            project_root=self.root,
            manifest_dir=self.root / "manifests",
            schema_dir=ROOT / "manifests/schemas",
        )
        first_timestamp = "2026-09-29T12:00:00+00:00"
        second_timestamp = "2026-09-30T12:00:00+00:00"
        plan = self.plan(collected_at=first_timestamp)
        plan.rights = [self.right(name) for name in ("api", "crawler", "storage")]
        preview = store.preflight([copy.deepcopy(plan)])
        result = store.commit(plan)
        original_events = copy.deepcopy(store.records("retrieval_events"))
        original_aliases = copy.deepcopy(store.records("work_aliases"))
        reversed_options = replace(self.options, rights_record_ids=tuple(reversed(self.options.rights_record_ids)))
        repeated_candidate = plan_preprint_download(
            self.root, self.path, reversed_options, collected_at=second_timestamp,
            metadata_rights_record_ids=("right-storage", "right-api"),
        )
        repeated, expected_hashes = reconcile_preprint_plan(store, repeated_candidate)
        repeated_result = store.commit(repeated, expected_snapshot_hashes=expected_hashes)

        self.assertTrue(preview.dry_run)
        self.assertEqual(result.written_blobs, 0)
        self.assertEqual(sum(repeated_result.inserted.values()), 0)
        self.assertEqual(len(store.records("works")), 1)
        self.assertEqual(len(store.records("artifacts")), 1)
        self.assertEqual(len(store.records("retrieval_events")), 2)
        self.assertEqual(store.records("retrieval_events"), original_events)
        self.assertEqual(store.records("work_aliases"), original_aliases)

        for kind in ("works", "artifacts", "retrieval_events", "work_aliases"):
            for record in store.records(kind):
                self.assertEqual(record["created_at"], first_timestamp)

        for alias in store.records("work_aliases"):
            self.assertEqual(alias["verified_at"], first_timestamp)

        self.assertTrue(store.audit().ok)

    def test_changed_publication_year_creates_schema_valid_conflict(self) -> None:
        """Изменение года не должно молча перезаписывать существующую карточку."""

        store = ManifestStore(
            project_root=self.root,
            manifest_dir=self.root / "manifests",
            schema_dir=ROOT / "manifests/schemas",
        )
        initial = self.plan()
        initial.rights = [self.right(name) for name in ("api", "crawler", "storage")]
        store.commit(initial)
        self.item["metadata"]["dc.date.issued"] = [{"value": "2025"}]
        self.save_metadata()
        plan, expected_hashes = reconcile_preprint_plan(store, self.plan())

        self.assertEqual(plan.works[0]["published_year"], 2024)
        self.assertEqual(plan.works[0]["eligibility_status"], "quarantined")
        self.assertEqual(len(plan.identity_conflicts), 1)
        store.commit(plan, expected_snapshot_hashes=expected_hashes)
        self.assertTrue(store.audit().ok)


if __name__ == "__main__":
    unittest.main()
