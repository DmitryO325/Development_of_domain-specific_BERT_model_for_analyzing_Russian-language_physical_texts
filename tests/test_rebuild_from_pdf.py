"""Проверки команды извлечения зарегистрированных локальных PDF."""

from __future__ import annotations

import json
import tempfile
import unittest

from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.rebuild_from_pdf import run_extraction
from src.corpus.local_registration import LocalFileRegistration, plan_local_file
from src.corpus.manifests import ManifestPlan, ManifestStore, canonical_json
from src.corpus.profiles import get_source_profile
from src.corpus.registration import RegistrationOptions

ROOT = Path(__file__).resolve().parents[1]
PDF_BYTES = b"%PDF-1.7\nscript extraction test\n%%EOF\n"
RETRIEVED_AT = "2024-01-10T12:30:00+03:00"


class RebuildFromPdfTests(unittest.TestCase):
    """Интеграционные проверки нового сценария производных текстов."""

    def setUp(self) -> None:
        """Создать временный проект, профиль и рабочий реестр."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temporary_directory.name)
        self.manifest_dir = self.project_root / "manifests"
        self.schema_dir = ROOT / "manifests" / "schemas"
        self.profile = get_source_profile("ufn")
        self.store = ManifestStore(
            project_root=self.project_root,
            manifest_dir=self.manifest_dir,
            schema_dir=self.schema_dir,
        )
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
        self.store.commit(
            ManifestPlan(
                rights=[self.right("acquisition"), self.right("storage")]
            )
        )

    def tearDown(self) -> None:
        """Удалить временный проект после проверки."""

        self.temporary_directory.cleanup()

    def registration(self, letter: str) -> LocalFileRegistration:
        """Создать карточку одного локального PDF по букве статьи."""

        return LocalFileRegistration(
            relative_path=f"data/raw/manual/r241{letter}.pdf",
            source_url=(
                "https://ufn.ru/ufn2024/ufn2024_1/Russian/"
                f"r241{letter}.pdf"
            ),
            canonical_url=f"https://ufn.ru/ru/articles/2024/1/{letter}/",
            retrieved_at=RETRIEVED_AT,
            title=f"Физическая статья {letter}",
            authors=["Иванов И. И."],
            doi=f"10.1000/phys.{letter}",
            published_at="2024-01-10",
            section="Обзоры актуальных проблем",
            language="ru",
            genre="review_article",
            abstract="Аннотация физической статьи.",
            keywords=["физика"],
            pacs_codes_raw=[],
            udc_codes_raw=[],
            acquisition_agent="Ручная проверка",
            eligibility_status="pending",
            exclusion_reason=None,
        )

    def right(self, operation: str) -> dict[str, Any]:
        """Создать разрешающую запись права для временного источника."""

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

    def prepare_input(
        self,
        letters: tuple[str, ...],
        *,
        register_pdfs: bool = True,
    ) -> Path:
        """Создать PDF, карточки и при необходимости родительские артефакты."""

        registrations = [self.registration(letter) for letter in letters]
        plans: list[ManifestPlan] = []

        for registration in registrations:
            pdf_path = self.project_root / registration.relative_path
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(PDF_BYTES + registration.title.encode("utf-8"))

            if register_pdfs:
                plans.append(
                    plan_local_file(
                        registration,
                        self.profile,
                        self.options,
                        project_root=self.project_root,
                    )
                )

        if plans:
            combined = ManifestPlan()

            for plan in plans:
                combined.works.extend(plan.works)
                combined.artifacts.extend(plan.artifacts)
                combined.retrieval_events.extend(plan.retrieval_events)
                combined.work_aliases.extend(plan.work_aliases)

            self.store.commit(combined)

        input_path = self.manifest_dir / "imports" / "manual.jsonl"
        input_path.parent.mkdir(parents=True, exist_ok=True)
        input_path.write_text(
            "".join(
                f"{canonical_json(asdict(registration))}\n"
                for registration in registrations
            ),
            encoding="utf-8",
        )

        return input_path

    def test_registered_pdfs_create_child_artifacts_and_report(self) -> None:
        """Две карточки должны создать два текста без новых событий загрузки."""

        input_path = self.prepare_input(("a", "b"))
        extracted_paths: list[Path] = []

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
        ) -> tuple[str, str, bool]:
            """Вернуть детерминированный читаемый текст для теста."""

            self.assertIsNone(text_dir)
            self.assertTrue(try_ocr)
            extracted_paths.append(pdf_path)
            return (
                f"Русский физический текст из {pdf_path.stem}.",
                "pdf",
                True,
            )

        report_path = self.manifest_dir / "results" / "pilot.jsonl"
        return_code = run_extraction(
            [
                str(input_path),
                "--manifest-dir",
                str(self.manifest_dir),
                "--extraction-version",
                "pdf-text-test-v1",
                "--report",
                str(report_path),
            ],
            project_root=self.project_root,
            schema_dir=self.schema_dir,
            extractor=extractor,
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(
            extracted_paths,
            [
                self.project_root.resolve() / "data/raw/manual/r241a.pdf",
                self.project_root.resolve() / "data/raw/manual/r241b.pdf",
            ],
        )
        self.assertEqual(len(self.store.records("artifacts")), 4)
        self.assertEqual(len(self.store.records("retrieval_events")), 2)
        report_records = [
            json.loads(line)
            for line in report_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [record["status"] for record in report_records],
            ["registered", "registered"],
        )
        self.assertTrue(
            all(
                record["review_status"] == "pending"
                for record in report_records
            )
        )
        self.assertTrue(self.store.audit().ok)

    def test_repeated_run_reuses_registered_text(self) -> None:
        """Повторный запуск не должен добавлять артефакты или события."""

        input_path = self.prepare_input(("a",))

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
        ) -> tuple[str, str, bool]:
            """Вернуть одинаковый текст при каждом запуске."""

            return "Русский физический текст.", "pdf", True

        arguments = [
            str(input_path),
            "--manifest-dir",
            str(self.manifest_dir),
            "--extraction-version",
            "pdf-text-test-v1",
        ]

        self.assertEqual(
            run_extraction(
                arguments,
                project_root=self.project_root,
                schema_dir=self.schema_dir,
                extractor=extractor,
            ),
            0,
        )
        self.assertEqual(
            run_extraction(
                arguments,
                project_root=self.project_root,
                schema_dir=self.schema_dir,
                extractor=extractor,
            ),
            0,
        )
        self.assertEqual(len(self.store.records("artifacts")), 2)
        self.assertEqual(len(self.store.records("retrieval_events")), 1)
        report_path = self.manifest_dir / "results" / "manual_extraction.jsonl"
        report_record = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report_record["status"], "reused")
        self.assertTrue(report_record["artifact_reused"])

    def test_unreadable_text_is_reported_without_artifact(self) -> None:
        """Нечитаемый результат нельзя регистрировать как успешный текст."""

        input_path = self.prepare_input(("a",))

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
        ) -> tuple[str, str, bool]:
            """Вернуть результат, не прошедший автоматическую проверку."""

            return "???", "pdf_unreadable", False

        def ocr_version_resolver() -> str:
            """Вернуть версию OCR для проверки отчёта."""

            return "5.5.3"

        return_code = run_extraction(
            [str(input_path), "--manifest-dir", str(self.manifest_dir)],
            project_root=self.project_root,
            schema_dir=self.schema_dir,
            extractor=extractor,
            ocr_version_resolver=ocr_version_resolver,
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(len(self.store.records("artifacts")), 1)
        report_path = self.manifest_dir / "results" / "manual_extraction.jsonl"
        report_record = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report_record["status"], "not_registered_unreadable")
        self.assertEqual(report_record["schema_version"], "extraction-pilot-v2")
        self.assertEqual(report_record["extraction_version"], "pdf-text-v1")
        self.assertEqual(report_record["ocr_method"], "tesseract")
        self.assertEqual(report_record["ocr_version"], "5.5.3")
        self.assertTrue(report_record["ocr_attempted"])
        self.assertEqual(report_record["automatic_readability"], "failed")
        self.assertIsNone(report_record["artifact_id"])

    def test_missing_parent_returns_error_and_keeps_report(self) -> None:
        """Незарегистрированный PDF должен дать ошибку с локальным отчётом."""

        input_path = self.prepare_input(("a",), register_pdfs=False)

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
        ) -> tuple[str, str, bool]:
            """Не должен вызываться без родительского артефакта."""

            self.fail("Извлечение не должно начинаться без регистрации PDF")

        return_code = run_extraction(
            [str(input_path), "--manifest-dir", str(self.manifest_dir)],
            project_root=self.project_root,
            schema_dir=self.schema_dir,
            extractor=extractor,
        )

        self.assertEqual(return_code, 1)
        report_path = self.manifest_dir / "results" / "manual_extraction.jsonl"
        report_record = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report_record["status"], "failed")
        self.assertEqual(report_record["extraction_version"], "pdf-text-v1")
        self.assertEqual(report_record["automatic_readability"], "not_evaluated")
        self.assertIn("не найден", report_record["error_detail"])

    def test_ocr_version_failure_preserves_attempt_in_report(self) -> None:
        """Ошибка версии OCR не должна скрывать уже выполненную попытку."""

        input_path = self.prepare_input(("a",))

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
        ) -> tuple[str, str, bool]:
            """Вернуть читаемый результат OCR."""

            return "Распознанный русский физический текст.", "pdf_ocr_layout", True

        def ocr_version_resolver() -> str:
            """Имитировать ошибку определения версии OCR."""

            raise RuntimeError("версия Tesseract недоступна")

        return_code = run_extraction(
            [str(input_path), "--manifest-dir", str(self.manifest_dir)],
            project_root=self.project_root,
            schema_dir=self.schema_dir,
            extractor=extractor,
            ocr_version_resolver=ocr_version_resolver,
        )

        self.assertEqual(return_code, 1)
        report_path = self.manifest_dir / "results" / "manual_extraction.jsonl"
        report_record = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report_record["status"], "failed")
        self.assertEqual(report_record["extraction_method"], "pdf_ocr_layout")
        self.assertEqual(report_record["ocr_method"], "tesseract")
        self.assertTrue(report_record["ocr_attempted"])
        self.assertIsNone(report_record["ocr_version"])
        self.assertEqual(report_record["automatic_readability"], "passed")

    def test_duplicate_pdf_card_is_rejected_before_extraction(self) -> None:
        """Повторный relative_path не должен запускать OCR дважды."""

        input_path = self.prepare_input(("a",))
        original_content = input_path.read_text(encoding="utf-8")
        input_path.write_text(original_content * 2, encoding="utf-8")

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
        ) -> tuple[str, str, bool]:
            """Не должен вызываться при повторной карточке."""

            self.fail("Извлечение не должно начинаться при повторной карточке")

        with self.assertRaisesRegex(ValueError, "повторный relative_path"):
            run_extraction(
                [str(input_path), "--manifest-dir", str(self.manifest_dir)],
                project_root=self.project_root,
                schema_dir=self.schema_dir,
                extractor=extractor,
            )

    def test_invalid_version_is_rejected_before_extraction(self) -> None:
        """Опечатка в версии не должна запускать дорогостоящий OCR."""

        input_path = self.prepare_input(("a",))

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
        ) -> tuple[str, str, bool]:
            """Не должен вызываться при недопустимой версии."""

            self.fail("Извлечение не должно начинаться при неверной версии")

        with self.assertRaisesRegex(ValueError, "extraction_version"):
            run_extraction(
                [
                    str(input_path),
                    "--manifest-dir",
                    str(self.manifest_dir),
                    "--extraction-version",
                    "../bad-version",
                ],
                project_root=self.project_root,
                schema_dir=self.schema_dir,
                extractor=extractor,
            )


if __name__ == "__main__":
    unittest.main()
