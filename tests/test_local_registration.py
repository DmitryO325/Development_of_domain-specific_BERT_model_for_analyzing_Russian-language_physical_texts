"""Проверки регистрации вручную загруженных PDF-файлов."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest

from pathlib import Path
from typing import Any

from src.corpus.local_registration import (
    LocalFileRegistration,
    plan_local_file,
    read_local_file_registrations,
)
from src.corpus.manifests import ManifestPlan, ManifestStore, sha256_bytes
from src.corpus.profiles import get_source_profile
from src.corpus.registration import RegistrationOptions

ROOT = Path(__file__).resolve().parents[1]
RETRIEVED_AT = "2024-01-10T12:30:00+03:00"
PDF_BYTES = b"%PDF-1.7\nmanual test file\n%%EOF\n"


class LocalFileRegistrationTests(unittest.TestCase):
    """Проверки плана и записи локального PDF."""

    def setUp(self) -> None:
        """Создать временный проект, PDF, профиль и параметры."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary_directory.name)
        self.pdf_path = self.project_root / "data" / "raw" / "manual" / "r241a.pdf"
        self.pdf_path.parent.mkdir(parents=True)
        self.pdf_path.write_bytes(PDF_BYTES)
        self.profile = get_source_profile("ufn")
        self.options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="manual_download",
            acquisition_scope="sample",
            rights_record_ids=("right-acquisition", "right-storage"),
            extraction_method="not_started",
            extraction_version="not-started-v1",
            response_representation="pdf",
            request_context_type="work",
        )

    def tearDown(self) -> None:
        """Удалить временный проект после проверки."""

        self.temporary_directory.cleanup()

    def registration(self, **changes: Any) -> LocalFileRegistration:
        """Создать карточку локального PDF с необязательными заменами."""

        values: dict[str, Any] = {
            "relative_path": "data/raw/manual/r241a.pdf",
            "source_url": "https://ufn.ru/ufn2024/ufn2024_1/Russian/r241a.pdf",
            "canonical_url": "https://ufn.ru/ru/articles/2024/1/a/",
            "retrieved_at": RETRIEVED_AT,
            "title": "Квантовые свойства плазмы",
            "authors": ["Иванов И. И."],
            "doi": "https://doi.org/10.1000/PHYS.1",
            "published_at": "2024-01-10",
            "section": "Обзоры актуальных проблем",
            "language": "ru",
            "genre": "review_article",
            "abstract": "Аннотация физической статьи.",
            "keywords": ["плазма", "квантовые свойства"],
            "pacs_codes_raw": [],
            "udc_codes_raw": [],
            "acquisition_agent": "Yandex Browser",
            "eligibility_status": "pending",
            "exclusion_reason": None,
        }
        values.update(changes)

        return LocalFileRegistration(**values)

    def right(self, operation: str) -> dict[str, Any]:
        """Создать разрешающую запись права для интеграционной проверки."""

        return {
            "schema_version": "rights-v1",
            "created_at": "2024-01-09T10:00:00+03:00",
            "rights_record_id": f"right-{operation}",
            "scope_type": "source",
            "scope_id": self.profile.source_id,
            "operation": operation,
            "status": "allowed",
            "access_basis": "Синтетическое разрешение для теста.",
            "basis_type": "explicit_license",
            "acquisition_method": (
                "manual_download" if operation == "acquisition" else None
            ),
            "acquisition_scope": "sample" if operation == "acquisition" else None,
            "terms_url": "https://example.invalid/test-license",
            "rights_checked_at": "2024-01-09",
            "derivative_scope": None,
            "rights_conditions": [],
            "conditions_satisfied_at": None,
            "conditions_evidence_sha256": None,
            "rights_evidence_sha256": "a" * 64,
            "rights_expires_at": None,
            "supersedes_rights_record_id": None,
        }

    def test_plan_describes_existing_pdf_without_blob(self) -> None:
        """План должен ссылаться на точные байты и ручное получение."""

        plan = plan_local_file(
            self.registration(),
            self.profile,
            self.options,
            project_root=self.project_root,
        )

        self.assertEqual(len(plan.works), 1)
        self.assertEqual(len(plan.artifacts), 1)
        self.assertEqual(len(plan.retrieval_events), 1)
        self.assertEqual(len(plan.work_aliases), 1)
        self.assertEqual(plan.blobs, [])

        digest = sha256_bytes(PDF_BYTES)
        work = plan.works[0]
        artifact = plan.artifacts[0]
        event = plan.retrieval_events[0]
        alias = plan.work_aliases[0]

        self.assertEqual(work["doi"], "10.1000/phys.1")
        self.assertIn(
            "source_native_id:S01_UFN_RU:article-2024-1-a",
            work["work_aliases"],
        )
        self.assertEqual(artifact["artifact_id"], f"sha256:{digest}")
        self.assertEqual(artifact["path"], "data/raw/manual/r241a.pdf")
        self.assertEqual(artifact["content_role"], "full_text")
        self.assertEqual(artifact["representation"], "pdf")
        self.assertEqual(artifact["extraction_status"], "not_started")
        self.assertEqual(artifact["processing_status"], "not_started")
        self.assertEqual(event["outcome"], "succeeded")
        self.assertEqual(event["acquisition_agent"], "Yandex Browser")
        self.assertIsNone(event["http_status"])
        self.assertIsNone(event["final_url"])
        self.assertEqual(event["response_path"], artifact["path"])
        self.assertEqual(event["response_sha256"], digest)
        self.assertEqual(event["response_bytes"], len(PDF_BYTES))
        self.assertEqual(alias["alias_type"], "source_native_id")
        self.assertEqual(alias["source_retrieval_id"], event["retrieval_id"])

    def test_plan_passes_preflight_and_commit(self) -> None:
        """План должен проходить полную проверку и запись без копирования PDF."""

        plan = plan_local_file(
            self.registration(),
            self.profile,
            self.options,
            project_root=self.project_root,
        )
        plan.rights = [self.right("acquisition"), self.right("storage")]
        store = ManifestStore(
            project_root=self.project_root,
            manifest_dir=self.project_root / "manifests",
            schema_dir=ROOT / "manifests" / "schemas",
        )

        preview = store.preflight([copy.deepcopy(plan)])
        result = store.commit(plan)

        self.assertTrue(preview.dry_run)
        self.assertEqual(preview.written_blobs, 0)
        self.assertFalse(result.dry_run)
        self.assertEqual(result.written_blobs, 0)
        self.assertEqual(len(store.records("works")), 1)
        self.assertEqual(len(store.records("artifacts")), 1)
        self.assertEqual(len(store.records("retrieval_events")), 1)
        self.assertTrue(store.audit().ok)
        self.assertEqual(self.pdf_path.read_bytes(), PDF_BYTES)

    def test_rejects_path_outside_data(self) -> None:
        """Путь за пределами data/ не должен попадать в план."""

        outside_path = self.project_root / "outside.pdf"
        outside_path.write_bytes(PDF_BYTES)

        with self.assertRaisesRegex(ValueError, "внутри data"):
            plan_local_file(
                self.registration(relative_path="outside.pdf"),
                self.profile,
                self.options,
                project_root=self.project_root,
            )

    def test_rejects_non_manual_options(self) -> None:
        """Конвейер локальных PDF не должен маскировать иной способ сбора."""

        options = RegistrationOptions(
            content_role="full_text",
            acquisition_method="crawler",
            acquisition_scope="sample",
            rights_record_ids=("right-acquisition", "right-storage"),
            extraction_method="not_started",
            extraction_version="not-started-v1",
            response_representation="pdf",
            request_context_type="work",
        )

        with self.assertRaisesRegex(ValueError, "acquisition_method"):
            plan_local_file(
                self.registration(),
                self.profile,
                options,
                project_root=self.project_root,
            )

    def test_rejects_invalid_url_dates_and_doi(self) -> None:
        """Некорректные URL, даты и DOI должны отклоняться до сборки плана."""

        invalid_changes = (
            ({"source_url": "file:///tmp/article.pdf"}, "source_url"),
            ({"retrieved_at": "2024-01-10T12:30:00"}, "часовой пояс"),
            ({"published_at": "2024-02-30"}, "published_at"),
            ({"published_at": "20240110"}, "published_at"),
            ({"doi": "not-a-doi"}, "doi"),
        )

        for changes, message in invalid_changes:
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, message):
                    plan_local_file(
                        self.registration(**changes),
                        self.profile,
                        self.options,
                        project_root=self.project_root,
                    )

    def test_eligible_accepts_dates_outside_previous_period(self) -> None:
        """Допуск работы не должен зависеть от прежнего диапазона годов."""

        for published_at in ("1918-01-01", "2026-09-02"):
            with self.subTest(published_at=published_at):
                plan = plan_local_file(
                    self.registration(
                        eligibility_status="eligible",
                        published_at=published_at,
                    ),
                    self.profile,
                    self.options,
                    project_root=self.project_root,
                )

                self.assertEqual(plan.works[0]["eligibility_status"], "eligible")
                self.assertEqual(plan.works[0]["published_at"], published_at)

    def test_eligible_requires_publication_date(self) -> None:
        """Допущенная работа должна иметь точную дату публикации."""

        with self.assertRaisesRegex(ValueError, "published_at"):
            plan_local_file(
                self.registration(
                    eligibility_status="eligible",
                    published_at=None,
                ),
                self.profile,
                self.options,
                project_root=self.project_root,
            )

    def test_reads_registration_jsonl_and_rejects_duplicate_keys(self) -> None:
        """Общий загрузчик должен читать карточки и отклонять повторные ключи."""

        input_path = self.project_root / "manifests" / "imports" / "manual.jsonl"
        input_path.parent.mkdir(parents=True)
        record = self.registration().__dict__
        input_path.write_text(
            json.dumps(record, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        registrations = read_local_file_registrations(input_path)

        self.assertEqual(registrations, [self.registration()])

        input_path.write_text(
            '{"relative_path":"a","relative_path":"b"}\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "повторный ключ"):
            read_local_file_registrations(input_path)


if __name__ == "__main__":
    unittest.main()
