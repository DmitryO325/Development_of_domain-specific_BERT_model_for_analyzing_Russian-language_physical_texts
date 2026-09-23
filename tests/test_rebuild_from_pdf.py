"""Проверки команды извлечения зарегистрированных локальных PDF."""

from __future__ import annotations

import io
import json
import tempfile
import unittest

from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from scripts.rebuild_from_pdf import DEFAULT_EXTRACTION_VERSION, run_extraction
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
            ocr_layout: str = "ufn",
        ) -> tuple[str, str, bool]:
            """Вернуть детерминированный читаемый текст для теста."""

            self.assertIsNone(text_dir)
            self.assertTrue(try_ocr)
            self.assertEqual(ocr_layout, "ufn")
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
                record["extraction_version"] == "pdf-text-test-v1"
                for record in report_records
            )
        )
        self.assertTrue(
            all(
                record["review_status"] == "pending"
                for record in report_records
            )
        )
        self.assertTrue(self.store.audit().ok)

    def test_cli_layouts_use_separate_versions_and_keep_previous_results(self) -> None:
        """Каждый макет должен передаваться в OCR и сохраняться в своей версии."""

        input_path = self.prepare_input(("a",))
        expected_versions = {
            "ufn": "pdf-text-rus-eng-v2",
            "single-column": "pdf-text-rus-eng-single-column-v3",
            "two-column": "pdf-text-rus-eng-two-column-v3",
        }
        previous_files: dict[Path, bytes] = {}

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
            ocr_layout: str = "ufn",
        ) -> tuple[str, str, bool]:
            """Вернуть различимый текст каждого макета без реального OCR."""

            self.assertTrue(try_ocr)
            self.assertIsNone(text_dir)
            return (
                f"Распознанный физический текст: {ocr_layout}.",
                "pdf_ocr_layout",
                True,
            )

        version_resolver = MagicMock(return_value="5.5.3")

        for layout, version in expected_versions.items():
            with self.subTest(ocr_layout=layout):
                return_code = run_extraction(
                    [str(input_path), "--ocr-layout", layout],
                    project_root=self.project_root,
                    schema_dir=self.schema_dir,
                    extractor=extractor,
                    ocr_version_resolver=version_resolver,
                )

                self.assertEqual(return_code, 0)
                report_path = (
                    self.manifest_dir
                    / "results"
                    / f"manual_{version}_extraction.jsonl"
                )
                report = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(report["ocr_layout"], layout)
                self.assertEqual(report["extraction_version"], version)
                self.assertTrue(report["ocr_attempted"])
                self.assertEqual(report["status"], "registered")
                text_path = self.project_root / report["artifact_path"]
                self.assertEqual(
                    text_path.read_text(encoding="utf-8"),
                    f"Распознанный физический текст: {layout}.",
                )

                for previous_path, previous_content in previous_files.items():
                    self.assertEqual(previous_path.read_bytes(), previous_content)

                previous_files[report_path] = report_path.read_bytes()
                previous_files[text_path] = text_path.read_bytes()

        self.assertEqual(version_resolver.call_count, 3)
        self.assertEqual(len(self.store.records("artifacts")), 4)
        self.assertTrue(self.store.audit().ok)

    def test_custom_version_and_no_ocr_preserve_selected_layout(self) -> None:
        """Свой номер версии и --no-ocr не должны терять выбранный макет."""

        input_path = self.prepare_input(("a",))

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
            ocr_layout: str = "ufn",
        ) -> tuple[str, str, bool]:
            """Проверить параметры и вернуть встроенный текст PDF."""

            self.assertFalse(try_ocr)
            self.assertEqual(ocr_layout, "single-column")
            return "Встроенный русский физический текст.", "pdf", True

        version_resolver = MagicMock()
        return_code = run_extraction(
            [
                str(input_path),
                "--ocr-layout",
                "single-column",
                "--extraction-version",
                "jinr-single-column-test-v1",
                "--no-ocr",
            ],
            project_root=self.project_root,
            schema_dir=self.schema_dir,
            extractor=extractor,
            ocr_version_resolver=version_resolver,
        )

        report_path = (
            self.manifest_dir
            / "results"
            / "manual_jinr-single-column-test-v1_extraction.jsonl"
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(return_code, 0)
        self.assertEqual(report["ocr_layout"], "single-column")
        self.assertEqual(
            report["extraction_version"],
            "jinr-single-column-test-v1",
        )
        self.assertFalse(report["ocr_attempted"])
        self.assertIsNone(report["ocr_method"])
        self.assertIsNone(report["ocr_version"])
        version_resolver.assert_not_called()

    def test_unknown_cli_layout_is_rejected_before_reading_input(self) -> None:
        """Опечатка в макете должна завершать разбор CLI до чтения файлов."""

        extractor = MagicMock()

        with (
            patch("sys.stderr", new=io.StringIO()),
            self.assertRaises(SystemExit) as exit_context,
        ):
            run_extraction(
                ["missing.jsonl", "--ocr-layout", "three-column"],
                project_root=self.project_root,
                schema_dir=self.schema_dir,
                extractor=extractor,
            )

        self.assertEqual(exit_context.exception.code, 2)
        extractor.assert_not_called()

    def test_reserved_versions_reject_other_layouts_before_reading_input(self) -> None:
        """Нельзя записать новый макет под стандартной версией другого макета."""

        versions = {
            "ufn": "pdf-text-rus-eng-v2",
            "single-column": "pdf-text-rus-eng-single-column-v3",
            "two-column": "pdf-text-rus-eng-two-column-v3",
        }
        extractor = MagicMock()

        for layout in versions:
            for other_layout, version in versions.items():
                if layout == other_layout:
                    continue

                with (
                    self.subTest(ocr_layout=layout, version=version),
                    self.assertRaisesRegex(ValueError, "другому макету"),
                ):
                    run_extraction(
                        [
                            "missing.jsonl",
                            "--ocr-layout",
                            layout,
                            "--extraction-version",
                            version,
                        ],
                        project_root=self.project_root,
                        schema_dir=self.schema_dir,
                        extractor=extractor,
                    )

        extractor.assert_not_called()

    def test_dry_run_layouts_leave_manifests_and_texts_unchanged(self) -> None:
        """Проверка каждого макета должна обходиться без записи результатов."""

        input_path = self.prepare_input(("a",))
        original_snapshot = self.store.snapshot_hashes()
        visited_layouts: list[str] = []

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
            ocr_layout: str = "ufn",
        ) -> tuple[str, str, bool]:
            """Зафиксировать выбранный макет и вернуть текст для проверки."""

            visited_layouts.append(ocr_layout)
            return f"Русский физический текст: {ocr_layout}.", "pdf_ocr_layout", True

        for layout in ("ufn", "single-column", "two-column"):
            with self.subTest(ocr_layout=layout):
                return_code = run_extraction(
                    [str(input_path), "--ocr-layout", layout, "--dry-run"],
                    project_root=self.project_root,
                    schema_dir=self.schema_dir,
                    extractor=extractor,
                    ocr_version_resolver=MagicMock(return_value="5.5.3"),
                )

                self.assertEqual(return_code, 0)
                self.assertEqual(self.store.snapshot_hashes(), original_snapshot)
                self.assertFalse(
                    (self.project_root / "data" / "extracted").exists()
                )
                self.assertFalse((self.manifest_dir / "results").exists())

        self.assertEqual(visited_layouts, ["ufn", "single-column", "two-column"])

    def test_repeated_run_reuses_registered_text(self) -> None:
        """Повторный запуск не должен добавлять артефакты или события."""

        input_path = self.prepare_input(("a",))

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
            ocr_layout: str = "ufn",
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
        report_path = (
            self.manifest_dir
            / "results"
            / "manual_pdf-text-test-v1_extraction.jsonl"
        )
        report_record = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report_record["status"], "reused")
        self.assertTrue(report_record["artifact_reused"])

    def test_new_default_preserves_previous_extraction_and_report(self) -> None:
        """Новая версия OCR должна сохранить прежний текст и локальный отчёт."""

        input_path = self.prepare_input(("a",))
        extracted_text = "Русский текст прежнего распознавания."

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
            ocr_layout: str = "ufn",
        ) -> tuple[str, str, bool]:
            """Вернуть заданный результат OCR без запуска Tesseract."""

            return extracted_text, "pdf_ocr_layout", True

        def ocr_version_resolver() -> str:
            """Вернуть одинаковую версию движка для двух языковых режимов."""

            return "5.5.3"

        previous_report_path = (
            self.manifest_dir / "results" / "manual_extraction.jsonl"
        )
        arguments = [str(input_path), "--manifest-dir", str(self.manifest_dir)]

        self.assertEqual(
            run_extraction(
                [
                    *arguments,
                    "--extraction-version",
                    "pdf-text-v1",
                    "--report",
                    str(previous_report_path),
                ],
                project_root=self.project_root,
                schema_dir=self.schema_dir,
                extractor=extractor,
                ocr_version_resolver=ocr_version_resolver,
            ),
            0,
        )
        previous_report = previous_report_path.read_bytes()
        previous_artifact = next(
            artifact
            for artifact in self.store.records("artifacts")
            if artifact["representation"] == "ocr_text"
        )
        previous_text_path = self.project_root / previous_artifact["path"]
        previous_text = previous_text_path.read_bytes()
        extracted_text = "Русский текст нового распознавания с Bell Labs."

        self.assertEqual(
            run_extraction(
                arguments,
                project_root=self.project_root,
                schema_dir=self.schema_dir,
                extractor=extractor,
                ocr_version_resolver=ocr_version_resolver,
            ),
            0,
        )
        self.assertEqual(previous_report_path.read_bytes(), previous_report)
        self.assertEqual(previous_text_path.read_bytes(), previous_text)
        artifacts = self.store.records("artifacts")
        self.assertEqual(len(artifacts), 3)
        self.assertIn(previous_artifact, artifacts)
        new_artifact = next(
            artifact
            for artifact in artifacts
            if artifact["extraction_version"] == "pdf-text-rus-eng-v2"
        )
        self.assertNotEqual(
            new_artifact["artifact_id"],
            previous_artifact["artifact_id"],
        )
        self.assertTrue(
            new_artifact["path"].startswith("data/extracted/pdf-text-rus-eng-v2/")
        )
        report_path = (
            self.manifest_dir
            / "results"
            / "manual_pdf-text-rus-eng-v2_extraction.jsonl"
        )
        report_record = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report_record["status"], "registered")
        self.assertEqual(report_record["extraction_version"], "pdf-text-rus-eng-v2")
        self.assertFalse(report_record["artifact_reused"])
        self.assertTrue(self.store.audit().ok)

    def test_unreadable_text_is_reported_without_artifact(self) -> None:
        """Нечитаемый результат нельзя регистрировать как успешный текст."""

        input_path = self.prepare_input(("a",))

        def extractor(
            pdf_path: Path,
            *,
            text_dir: Path | None = None,
            try_ocr: bool = True,
            ocr_layout: str = "ufn",
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
        report_path = (
            self.manifest_dir
            / "results"
            / f"manual_{DEFAULT_EXTRACTION_VERSION}_extraction.jsonl"
        )
        report_record = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report_record["status"], "not_registered_unreadable")
        self.assertEqual(report_record["schema_version"], "extraction-pilot-v2")
        self.assertEqual(
            report_record["extraction_version"],
            "pdf-text-rus-eng-v2",
        )
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
            ocr_layout: str = "ufn",
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
        report_path = (
            self.manifest_dir
            / "results"
            / f"manual_{DEFAULT_EXTRACTION_VERSION}_extraction.jsonl"
        )
        report_record = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report_record["status"], "failed")
        self.assertEqual(
            report_record["extraction_version"],
            "pdf-text-rus-eng-v2",
        )
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
            ocr_layout: str = "ufn",
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
        report_path = (
            self.manifest_dir
            / "results"
            / f"manual_{DEFAULT_EXTRACTION_VERSION}_extraction.jsonl"
        )
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
            ocr_layout: str = "ufn",
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
            ocr_layout: str = "ufn",
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
