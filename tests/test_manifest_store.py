from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.collect.base import Document
from src.corpus.legacy import plan_legacy_document
from src.corpus.manifests import ManifestConflictError, ManifestError, ManifestStore
from src.corpus.profiles import get_source_profile
from src.corpus.schema_validation import SchemaValidationError

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-08-27T12:00:00+03:00"


class ManifestStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp.name)
        (self.project_root / "data" / "raw" / "pdf").mkdir(parents=True)
        self.store = ManifestStore(
            project_root=self.project_root,
            manifest_dir=self.project_root / "manifests",
            schema_dir=ROOT / "manifests" / "schemas",
        )
        self.profile = get_source_profile("ufn")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def document(self, **changes) -> Document:
        values = {
            "source": "ufn.ru",
            "url": "https://ufn.ru/ru/articles/2024/1/a/",
            "title": "Квантовые свойства плазмы",
            "text": "Русский физический текст " * 8,
            "authors": ["Иванов И. И."],
            "section": "Обзоры актуальных проблем",
            "extra": {"year": "2024", "text_source": "html"},
        }
        values.update(changes)
        return Document(**values)

    def right(self, operation: str, *, status: str = "allowed", expires=None) -> dict:
        return {
            "schema_version": "rights-v1",
            "created_at": STAMP,
            "rights_record_id": f"right-{operation}",
            "scope_type": "source",
            "scope_id": self.profile.source_id,
            "operation": operation,
            "status": status,
            "access_basis": "Синтетическое разрешение для модульного теста.",
            "basis_type": "explicit_license",
            "acquisition_method": "manual_download" if operation == "acquisition" else None,
            "acquisition_scope": "sample" if operation == "acquisition" else None,
            "terms_url": "https://example.invalid/test-license",
            "rights_checked_at": "2026-08-27",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": expires,
            "supersedes_rights_record_id": None,
        }

    def plan(
        self,
        document: Document | None = None,
        *,
        include_rights: bool = True,
        rights: list[dict] | None = None,
        **kwargs,
    ):
        selected_rights = rights if rights is not None else [
            self.right("acquisition"),
            self.right("storage"),
        ]
        rights_ids = [item["rights_record_id"] for item in selected_rights]
        plan = plan_legacy_document(
            document or self.document(),
            profile=self.profile,
            project_root=self.project_root,
            imported_at=STAMP,
            acquisition_method="manual_download",
            acquisition_scope="sample",
            rights_record_ids=rights_ids if include_rights else [],
            **kwargs,
        )
        if include_rights:
            plan.rights.extend(selected_rights)
        return plan

    def test_mapping_writes_utf8_content_addressed_file(self) -> None:
        plan = self.plan()
        result = self.store.commit(plan)
        self.assertEqual(result.inserted["works"], 1)
        self.assertEqual(result.inserted["artifacts"], 1)
        self.assertEqual(result.written_blobs, 1)

        artifact = self.store.records("artifacts")[0]
        stored = self.project_root / artifact["path"]
        self.assertEqual(stored.read_bytes(), self.document().text.encode("utf-8"))
        self.assertEqual(artifact["artifact_id"], f"sha256:{artifact['sha256']}")
        self.assertEqual(artifact["content_role"], "metadata_only")
        manifest_text = (self.project_root / "manifests" / "works.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertIn("Квантовые свойства", manifest_text)
        self.assertNotIn("\\u041a", manifest_text)
        self.assertTrue(manifest_text.endswith("\n"))

    def test_exact_second_commit_is_noop(self) -> None:
        plan = self.plan()
        self.store.commit(plan)
        second = self.store.commit(plan)
        self.assertEqual(second.inserted, {"rights": 0, "works": 0, "artifacts": 0})
        self.assertEqual(second.unchanged["works"], 1)
        self.assertEqual(second.unchanged["artifacts"], 1)
        lines = (self.project_root / "manifests" / "works.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)

    def test_same_work_id_with_changed_record_is_conflict(self) -> None:
        self.store.commit(self.plan())
        changed = self.document(title="Другое название")
        # URL УФН задаёт тот же устойчивый source-native work_id.
        with self.assertRaises(ManifestConflictError):
            self.store.commit(self.plan(changed))

    def test_pdf_and_derived_text_are_two_linked_artifacts(self) -> None:
        pdf_path = self.project_root / "data" / "raw" / "pdf" / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.7\nsynthetic-test")
        document = self.document(
            pdf_url="https://ufn.ru/test.pdf",
            extra={
                "year": "2024",
                "text_source": "pdf_ocr_layout",
                "pdf_path": "data/raw/pdf/sample.pdf",
            },
        )
        self.store.commit(self.plan(document))
        artifacts = self.store.records("artifacts")
        self.assertEqual(len(artifacts), 2)
        pdf = next(item for item in artifacts if item["representation"] == "pdf")
        text = next(item for item in artifacts if item["representation"] == "ocr_text")
        self.assertEqual(text["parent_artifact_id"], pdf["artifact_id"])

    def test_html_fallback_is_not_child_of_pdf(self) -> None:
        pdf_path = self.project_root / "data" / "raw" / "pdf" / "sample.pdf"
        pdf_path.write_bytes(b"%PDF-1.7\nsynthetic-test")
        document = self.document(
            extra={
                "year": "2024",
                "text_source": "html",
                "pdf_path": "data/raw/pdf/sample.pdf",
            }
        )
        plan = self.plan(document)
        text = next(item for item in plan.artifacts if item["representation"] == "plain_text")
        self.assertIsNone(text["parent_artifact_id"])
        self.assertEqual(text["content_role"], "metadata_only")

    def test_unknown_right_reference_aborts_before_writing(self) -> None:
        plan = self.plan(include_rights=False)
        plan.artifacts[0]["rights_record_ids"] = ["missing-right"]
        with self.assertRaises(ManifestError):
            self.store.commit(plan)
        self.assertFalse((self.project_root / "manifests" / "works.jsonl").exists())
        self.assertFalse((self.project_root / plan.blobs[0].relative_path).exists())

    def test_schema_error_aborts_before_writing(self) -> None:
        plan = self.plan()
        plan.works[0] = copy.deepcopy(plan.works[0])
        plan.works[0]["language"] = "r"
        with self.assertRaises(SchemaValidationError):
            self.store.commit(plan)
        self.assertFalse((self.project_root / "manifests" / "works.jsonl").exists())

    def test_audit_checks_real_hash(self) -> None:
        self.store.commit(self.plan())
        report = self.store.audit()
        self.assertTrue(report.ok)
        self.assertEqual(report.counts["works"], 1)

        artifact = self.store.records("artifacts")[0]
        (self.project_root / artifact["path"]).write_bytes(b"changed")
        broken = self.store.audit()
        self.assertFalse(broken.ok)
        self.assertTrue(any("размер" in error or "SHA-256" in error for error in broken.errors))

    def test_empty_store_does_not_report_success(self) -> None:
        report = self.store.audit()
        self.assertFalse(report.ok)
        self.assertTrue(any("отсутствуют" in error for error in report.errors))

    def test_year_outside_v1_is_kept_pending_with_warning(self) -> None:
        plan = self.plan(self.document(extra={"year": "2026", "text_source": "html"}))
        self.assertEqual(plan.works[0]["eligibility_status"], "pending")
        self.assertTrue(any("2000–2025" in warning for warning in plan.warnings))

    def test_preflight_without_rights_needs_explicit_inventory_mode(self) -> None:
        plan = self.plan(include_rights=False)
        with self.assertRaises(ManifestError):
            self.store.preflight([plan])
        result = self.store.preflight([plan], allow_unresolved_rights=True)
        self.assertEqual(result.inserted["works"], 1)
        self.assertFalse((self.project_root / "manifests").exists())

    def test_batch_preflight_detects_cross_line_conflict_without_writes(self) -> None:
        first = self.plan()
        second = self.plan(self.document(title="Другое название той же статьи"))
        with self.assertRaises(ManifestConflictError):
            self.store.preflight([first, second])
        self.assertFalse((self.project_root / "manifests").exists())

    def test_rss_clean_text_is_metadata_plain_text_without_fake_retrieval(self) -> None:
        document = self.document(
            source="ufn.ru:rss",
            published="Wed, 21 Aug 2024 10:00:00 +0300",
            extra={"format": "rss_description"},
        )
        plan = self.plan(document)
        artifact = plan.artifacts[0]
        self.assertEqual(artifact["content_role"], "metadata_only")
        self.assertEqual(artifact["representation"], "plain_text")
        self.assertEqual(artifact["retrievals"], [])
        self.assertEqual(plan.works[0]["abstract"], document.text.strip())

    def test_prohibited_acquisition_right_blocks_commit(self) -> None:
        rights = [self.right("acquisition", status="prohibited"), self.right("storage")]
        with self.assertRaises(ManifestError):
            self.store.commit(self.plan(rights=rights))

    def test_wrong_operation_cannot_substitute_for_acquisition(self) -> None:
        redistribution = self.right("acquisition")
        redistribution["rights_record_id"] = "right-redistribution"
        redistribution["operation"] = "redistribution"
        redistribution["acquisition_method"] = None
        redistribution["acquisition_scope"] = None
        rights = [redistribution, self.right("storage")]
        with self.assertRaises(ManifestError):
            self.store.commit(self.plan(rights=rights))

    def test_conflicting_active_rights_are_conservative(self) -> None:
        blocked = self.right("acquisition", status="prohibited")
        blocked["rights_record_id"] = "right-acquisition-blocked"
        rights = [self.right("acquisition"), blocked, self.right("storage")]
        with self.assertRaises(ManifestError):
            self.store.commit(self.plan(rights=rights))

    def test_more_specific_unreferenced_prohibition_blocks(self) -> None:
        plan = self.plan()
        blocked = self.right("acquisition", status="prohibited")
        blocked["rights_record_id"] = "right-work-acquisition-blocked"
        blocked["scope_type"] = "work"
        blocked["scope_id"] = plan.works[0]["work_id"]
        # Запрет намеренно не включён в rights_record_ids.
        plan.rights.append(blocked)
        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_prohibition_scoped_to_work_alias_blocks(self) -> None:
        plan = self.plan()
        plan.works[0]["work_aliases"] = ["work:old-source-id"]
        blocked = self.right("acquisition", status="prohibited")
        blocked["rights_record_id"] = "right-work-alias-acquisition-blocked"
        blocked["scope_type"] = "work"
        blocked["scope_id"] = "work:old-source-id"
        plan.rights.append(blocked)
        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_more_specific_permission_overrides_broader_prohibition(self) -> None:
        blocked = self.right("acquisition", status="prohibited")
        allowed = self.right("acquisition")
        allowed["rights_record_id"] = "right-work-acquisition-allowed"
        plan = self.plan(rights=[blocked, allowed, self.right("storage")])
        allowed["scope_type"] = "work"
        allowed["scope_id"] = plan.works[0]["work_id"]
        result = self.store.preflight([plan])
        self.assertEqual(result.inserted["works"], 1)

    def test_acquisition_right_must_match_actual_mode(self) -> None:
        acquisition = self.right("acquisition")
        acquisition["acquisition_method"] = "crawler"
        acquisition["acquisition_scope"] = "bulk"
        with self.assertRaises(ManifestError):
            self.store.preflight(
                [self.plan(rights=[acquisition, self.right("storage")])]
            )

    def test_conditional_right_requires_fulfilment_evidence(self) -> None:
        conditional = self.right("acquisition", status="conditional")
        conditional["rights_conditions"] = ["Получить письменное разрешение."]

        with self.assertRaises(ManifestError):
            self.store.preflight(
                [self.plan(rights=[conditional, self.right("storage")])]
            )

        timestamp_only = copy.deepcopy(conditional)
        timestamp_only["conditions_satisfied_at"] = STAMP
        with self.assertRaises(SchemaValidationError):
            self.store.preflight(
                [self.plan(rights=[timestamp_only, self.right("storage")])]
            )

        evidence_only = copy.deepcopy(conditional)
        evidence_only["conditions_evidence_sha256"] = "b" * 64
        with self.assertRaises(SchemaValidationError):
            self.store.preflight(
                [self.plan(rights=[evidence_only, self.right("storage")])]
            )

        fulfilled = copy.deepcopy(conditional)
        fulfilled["conditions_satisfied_at"] = STAMP
        fulfilled["conditions_evidence_sha256"] = "b" * 64
        result = self.store.preflight(
            [self.plan(rights=[fulfilled, self.right("storage")])]
        )
        self.assertEqual(result.inserted["artifacts"], 1)

    def test_saved_pdf_requires_storage_before_processing(self) -> None:
        pdf_path = self.project_root / "data" / "raw" / "pdf" / "storage.pdf"
        pdf_path.write_bytes(b"%PDF-1.7\nsynthetic-storage-test")
        document = self.document(
            extra={
                "year": "2024",
                "text_source": "pdf",
                "pdf_path": "data/raw/pdf/storage.pdf",
            }
        )
        without_storage = self.plan(document, rights=[self.right("acquisition")])
        pdf = next(
            item for item in without_storage.artifacts if item["representation"] == "pdf"
        )
        without_storage.artifacts = [pdf]
        without_storage.blobs = [
            item for item in without_storage.blobs if item.relative_path == pdf["path"]
        ]
        with self.assertRaises(ManifestError):
            self.store.preflight([without_storage])

        with_storage = self.plan(document)
        pdf = next(
            item for item in with_storage.artifacts if item["representation"] == "pdf"
        )
        with_storage.artifacts = [pdf]
        with_storage.blobs = [
            item for item in with_storage.blobs if item.relative_path == pdf["path"]
        ]
        self.store.preflight([with_storage])

    def test_supersedes_requires_later_record(self) -> None:
        previous = self.right("acquisition", status="prohibited")
        previous["created_at"] = "2026-08-27T12:00:00+03:00"
        successor = self.right("acquisition")
        successor["rights_record_id"] = "right-acquisition-successor"
        successor["supersedes_rights_record_id"] = previous["rights_record_id"]
        successor["created_at"] = previous["created_at"]
        with self.assertRaises(ManifestError):
            self.store.preflight(
                [self.plan(rights=[previous, successor, self.right("storage")])]
            )

    def test_later_successor_replaces_previous_prohibition(self) -> None:
        previous = self.right("acquisition", status="prohibited")
        previous["created_at"] = "2026-08-27T11:00:00+03:00"
        successor = self.right("acquisition")
        successor["rights_record_id"] = "right-acquisition-successor"
        successor["supersedes_rights_record_id"] = previous["rights_record_id"]
        successor["created_at"] = STAMP
        result = self.store.preflight(
            [self.plan(rights=[previous, successor, self.right("storage")])]
        )
        self.assertEqual(result.inserted["artifacts"], 1)

    def test_expired_storage_right_blocks_processed_text(self) -> None:
        rights = [self.right("acquisition"), self.right("storage", expires="2025-01-01")]
        with self.assertRaises(ManifestError):
            self.store.commit(self.plan(rights=rights))

    def test_unknown_text_provenance_is_not_full_text(self) -> None:
        for text_source in ("html", "rss", "legacy_text", "unknown", "pdf_fake"):
            with self.subTest(text_source=text_source):
                plan = self.plan(
                    self.document(extra={"year": "2024", "text_source": text_source})
                )
                self.assertEqual(plan.artifacts[0]["content_role"], "metadata_only")
                self.assertIsNone(plan.artifacts[0]["parent_artifact_id"])

    def test_pdf_marker_without_local_pdf_is_not_full_text(self) -> None:
        plan = self.plan(
            self.document(extra={"year": "2024", "text_source": "pdf"})
        )
        self.assertEqual(plan.artifacts[0]["content_role"], "metadata_only")
        self.assertIsNone(plan.artifacts[0]["parent_artifact_id"])

    def test_legacy_full_text_claim_without_pdf_parent_is_rejected(self) -> None:
        plan = self.plan()
        plan.artifacts[0]["content_role"] = "full_text"
        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_future_right_is_rejected(self) -> None:
        future = self.right("acquisition")
        future["created_at"] = "2099-01-02T12:00:00+03:00"
        future["rights_checked_at"] = "2099-01-02"
        with self.assertRaises(ManifestError):
            self.store.preflight(
                [self.plan(rights=[future, self.right("storage")])]
            )

    def test_no_public_blob_staging_without_manifest_plan(self) -> None:
        self.assertFalse(hasattr(self.store, "stage_blobs"))

    def test_created_at_after_updated_at_is_rejected_before_write(self) -> None:
        plan = self.plan()
        plan.works[0]["created_at"] = "2026-08-28T12:00:00+03:00"
        with self.assertRaises(ManifestError):
            self.store.commit(plan)
        self.assertFalse((self.project_root / "manifests" / "works.jsonl").exists())

    def test_missing_planned_blob_is_rejected_before_write(self) -> None:
        plan = self.plan()
        plan.blobs.clear()
        with self.assertRaises(ManifestError):
            self.store.commit(plan)
        self.assertFalse((self.project_root / "manifests" / "works.jsonl").exists())

    def test_artifact_id_mismatch_is_rejected_before_write(self) -> None:
        plan = self.plan()
        plan.artifacts[0]["artifact_id"] = f"sha256:{'0' * 64}"
        with self.assertRaises(ManifestError):
            self.store.commit(plan)

    def test_parent_cycle_is_rejected_before_write(self) -> None:
        plan = self.plan()
        plan.artifacts[0]["parent_artifact_id"] = plan.artifacts[0]["artifact_id"]
        with self.assertRaises(ManifestError):
            self.store.commit(plan)

    def test_different_import_timestamp_is_still_noop(self) -> None:
        first = self.plan()
        self.store.commit(first)
        second = self.plan()
        second.works[0]["created_at"] = "2026-08-28T12:00:00+03:00"
        second.works[0]["updated_at"] = "2026-08-28T12:00:00+03:00"
        second.artifacts[0]["created_at"] = "2026-08-28T12:00:00+03:00"
        second.artifacts[0]["updated_at"] = "2026-08-28T12:00:00+03:00"
        result = self.store.commit(second)
        self.assertEqual(result.unchanged["works"], 1)
        self.assertEqual(result.unchanged["artifacts"], 1)

    def test_jsonl_is_one_object_per_line(self) -> None:
        self.store.commit(self.plan())
        path = self.project_root / "manifests" / "artifacts.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            self.assertIsInstance(json.loads(line), dict)


if __name__ == "__main__":
    unittest.main()
