"""Проверки безопасной публикации отдельной версии очищенного текста."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest

from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from scripts.clean_texts import main
from src.collect.pdf_text import (
    PdfPageText,
    PdfTextExtraction,
    _combine_page_texts,
    export_pdf_pages,
)
from src.corpus.extraction_registration import plan_extracted_text
from src.corpus.manifests import AuditReport, ManifestError
from src.preprocess.cleaning_io import (
    PreparedCleaning,
    prepare_cleaning,
    publish_cleaning,
)
from tests import test_rebuild_from_pdf as rebuild_tests

EXTRACTION_VERSION = "cleaning-input-test-v1"
CLEANING_VERSION = "text-clean-conservative-v3"
EXPECTED_ERRORS = (ValueError, ManifestError)
PAGE_TEXTS = (
    "Титульный лист\nИванов И. И.",
    "Русский экспери-\nмент.\nEnglish abstract.\nСписок литературы: [1] Автор.",
    "Редактор Иванов\nПодписано в печать.",
)
EXPECTED_TEXT = (
    "Русский эксперимент.\nEnglish abstract.\nСписок литературы: [1] Автор."
)


class CleanTextsTests(unittest.TestCase):
    """Проверки происхождения, строгого плана и неизменяемого пакета."""

    def setUp(self) -> None:
        """Составить синтетический корпус из зарегистрированного PDF и TXT."""

        self.fixture = rebuild_tests.RebuildFromPdfTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.project_root = self.fixture.project_root.resolve()
        self.schema_dir = self.fixture.schema_dir
        self.store = self.fixture.store
        self.plan_path = self.project_root / "manifests" / "cleaning-plan.json"
        self._create_source()

    def _create_source(
        self,
        *,
        letter: str = "a",
        method: str = "pdf",
        page_texts: tuple[str, ...] = PAGE_TEXTS,
    ) -> None:
        """Зарегистрировать независимый источник и подготовить план очистки."""

        self.fixture.prepare_input((letter,))
        self.parent_artifact = next(
            artifact
            for artifact in self.store.records("artifacts")
            if artifact["path"] == f"data/raw/manual/r241{letter}.pdf"
        )
        self.source_pdf_path = self.project_root / self.parent_artifact["path"]
        pages = tuple(
            PdfPageText(page_index, page_index + 1, text)
            for page_index, text in enumerate(page_texts)
        )
        extraction = PdfTextExtraction(
            text=_combine_page_texts(pages, method=method),
            method=method,
            readable=True,
            pages=pages,
        )
        registration = plan_extracted_text(
            self.parent_artifact,
            extraction.text,
            extraction_method=method,
            extraction_version=EXTRACTION_VERSION,
            extracted_at="2024-01-11T12:30:00+03:00",
            ocr_version="5.5.3" if method == "pdf_ocr_layout" else None,
        )
        self.store.commit(registration)
        self.source_artifact = registration.artifacts[0]
        self.source_text_path = self.project_root / self.source_artifact["path"]
        self.pages_dir = self.project_root / "data" / "qa" / f"pages-{letter}"
        exported = export_pdf_pages(
            self.source_pdf_path,
            extraction,
            self.pages_dir,
            extraction_version=EXTRACTION_VERSION,
            source_pdf_path=self.parent_artifact["path"],
            source_pdf_sha256=self.parent_artifact["sha256"],
        )
        self.pages_manifest_path = exported.manifest_path
        self.plan: dict[str, Any] = {
            "schema_version": "text-cleaning-plan-v2",
            "preprocessing_version": CLEANING_VERSION,
            "documents": [
                {
                    "work_id": self.source_artifact["work_id"],
                    "source_artifact_id": self.source_artifact["artifact_id"],
                    "pages_manifest_path": self.pages_manifest_path.relative_to(
                        self.project_root
                    ).as_posix(),
                    "pages_manifest_sha256": hashlib.sha256(
                        self.pages_manifest_path.read_bytes()
                    ).hexdigest(),
                    "exclude_pages": [
                        {"page_number": 1, "reason": "Титульный лист."},
                        {"page_number": 3, "reason": "Выходные данные."},
                    ],
                    "join_words": ["эксперимент"],
                    "exclude_lines": [],
                }
            ],
        }
        self._write_plan()
        self.assertTrue(self.store.audit().ok)

    def _write_plan(self) -> None:
        """Сохранить текущий план с намеренно неканоническими отступами."""

        self.plan_path.write_text(
            json.dumps(self.plan, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _prepare(self) -> PreparedCleaning:
        """Проверить план и получить ещё не опубликованный пакет."""

        return prepare_cleaning(
            self.plan_path,
            project_root=self.project_root,
            schema_dir=self.schema_dir,
        )

    def _page_records(self) -> list[dict[str, Any]]:
        """Прочитать индекс тестовых страниц для контролируемой порчи."""

        return [
            json.loads(line)
            for line in self.pages_manifest_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

    def _write_page_records(self, records: list[dict[str, Any]]) -> None:
        """Переписать индекс и закрепить его новые байты в тестовом плане."""

        self.pages_manifest_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        self.plan["documents"][0]["pages_manifest_sha256"] = hashlib.sha256(
            self.pages_manifest_path.read_bytes()
        ).hexdigest()
        self._write_plan()

    def _snapshot(self, directory: Path) -> dict[str, tuple[bytes, int]]:
        """Снять байты и время изменения обычных файлов заданного каталога."""

        return {
            path.relative_to(directory).as_posix(): (
                path.read_bytes(), path.stat().st_mtime_ns
            )
            for path in directory.rglob("*")
            if path.is_file()
        }

    def test_dry_run_and_preparation_do_not_write_output(self) -> None:
        """Подготовка и оба предварительных режима не меняют файлы проекта."""

        original_files = self._snapshot(self.project_root)
        prepared = self._prepare()

        self.assertFalse(prepared.output_dir.exists())
        self.assertFalse(publish_cleaning(prepared, dry_run=True))

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return_code = main(
                [str(self.plan_path), "--dry-run"],
                project_root=self.project_root,
                schema_dir=self.schema_dir,
            )

        self.assertEqual(return_code, 0)
        self.assertFalse(prepared.output_dir.exists())
        self.assertFalse(
            (self.project_root / "data" / "qa" / "preprocessing").exists()
        )
        self.assertEqual(self._snapshot(self.project_root), original_files)

    def test_publication_preserves_sources_and_exact_repeat(self) -> None:
        """Пакет сохраняет аннотации, реестры и времена повторной публикации."""

        original_files = self._snapshot(self.project_root)
        plan_bytes = self.plan_path.read_bytes()
        prepared = self._prepare()
        plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
        text_name = f"documents/{self.source_artifact['sha256']}/text.txt"
        page_name = f"documents/{self.source_artifact['sha256']}/pages/page_0002.txt"

        self.assertEqual(
            prepared.output_dir,
            self.project_root / "data" / "qa" / "preprocessing"
            / CLEANING_VERSION / plan_sha256,
        )
        self.assertEqual(
            set(prepared.files), {"plan.json", "report.json", text_name, page_name}
        )
        self.assertEqual(prepared.files["plan.json"], plan_bytes)
        self.assertEqual(prepared.files[text_name].decode("utf-8"), EXPECTED_TEXT)
        self.assertEqual(prepared.files[page_name].decode("utf-8"), EXPECTED_TEXT)
        self.assertEqual(prepared.report["status"], "candidate_not_approved")
        self.assertEqual(len(prepared.report["documents"]), 1)
        document_report = prepared.report["documents"][0]
        self.assertEqual(document_report["kept_pages"], [2])
        self.assertEqual(len(document_report["excluded_pages"]), 2)
        self.assertTrue(document_report["changes"])
        self.assertEqual(prepared.report["totals"]["documents"], 1)
        self.assertEqual(prepared.report["totals"]["pages_before"], 3)
        self.assertEqual(prepared.report["totals"]["pages_after"], 1)
        self.assertEqual(prepared.report["totals"]["excluded_pages"], 2)
        self.assertEqual(json.loads(prepared.files["report.json"]), prepared.report)
        self.assertTrue(publish_cleaning(prepared))

        for relative_path, expected_bytes in prepared.files.items():
            self.assertEqual(
                (prepared.output_dir / relative_path).read_bytes(), expected_bytes
            )

        for relative_path, (expected_bytes, expected_mtime) in original_files.items():
            source_path = self.project_root / relative_path
            self.assertEqual(source_path.read_bytes(), expected_bytes)
            self.assertEqual(source_path.stat().st_mtime_ns, expected_mtime)

        published_files = self._snapshot(prepared.output_dir)
        directory_mtime = prepared.output_dir.stat().st_mtime_ns
        repeated = self._prepare()

        self.assertEqual(repeated.files, prepared.files)
        self.assertEqual(repeated.report, prepared.report)
        self.assertFalse(publish_cleaning(repeated))
        self.assertEqual(self._snapshot(prepared.output_dir), published_files)
        self.assertEqual(prepared.output_dir.stat().st_mtime_ns, directory_mtime)
        self.assertTrue(self.store.audit().ok)

    def test_registered_ocr_text_uses_its_own_page_separator(self) -> None:
        """Проверка сборки принимает зарегистрированный OCR со своим разделителем."""

        self._create_source(letter="b", method="pdf_ocr_layout")
        prepared = self._prepare()
        text_name = f"documents/{self.source_artifact['sha256']}/text.txt"

        self.assertEqual(self.source_artifact["representation"], "ocr_text")
        self.assertEqual(prepared.files[text_name].decode("utf-8"), EXPECTED_TEXT)

    def test_explicit_header_removal_is_logged_and_preserves_sources(self) -> None:
        """Точная строка удаляется только из копии с причиной и исходным номером."""

        header = "Журнал\u00a0физики  "
        self._create_source(
            letter="b",
            page_texts=(
                "Титульный лист",
                header + "\r\nРусский экспери-\r\nмент.\r\nE = mc²\r\n"
                "Список литературы: Журнал физики.",
                "Выходные сведения",
            ),
        )
        exclusion = {
            "page_number": 2,
            "line_number": 1,
            "expected_text": header,
            "reason": "Подтверждённый колонтитул, не название раздела.",
        }
        self.plan["documents"][0]["exclude_lines"] = [exclusion]
        self._write_plan()
        baseline = self._snapshot(self.project_root)
        prepared = self._prepare()
        report = prepared.report["documents"][0]

        self.assertEqual(prepared.report["schema_version"], "text-cleaning-report-v2")
        self.assertEqual(report["excluded_lines"], [exclusion])
        self.assertEqual(prepared.report["totals"]["excluded_lines"], 1)
        self.assertEqual(
            prepared.files[report["output_path"]].decode(),
            "\nРусский эксперимент.\nE = mc²\nСписок литературы: Журнал физики.",
        )
        self.assertIn(
            {
                "page_number": 2,
                "line_number": 1,
                "code": "remove_explicit_line",
                "before": header,
                "after": "",
            },
            report["changes"],
        )
        self.assertTrue(publish_cleaning(prepared))

        for name, (content, mtime) in baseline.items():
            path = self.project_root / name
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, mtime)

        published = self._snapshot(prepared.output_dir)
        self.assertFalse(publish_cleaning(self._prepare()))
        self.assertEqual(self._snapshot(prepared.output_dir), published)

    def test_header_text_must_match_before_whitespace_normalization(self) -> None:
        """Похожая строка без исходного NBSP и пробелов не разрешает удаление."""

        self._create_source(
            letter="b",
            page_texts=("Титул", "Журнал\u00a0физики  \nОсновной текст.", "Редактор"),
        )
        self.plan["documents"][0]["exclude_lines"] = [{
            "page_number": 2,
            "line_number": 1,
            "expected_text": "Журнал физики",
            "reason": "Проверка точного совпадения.",
        }]
        self._write_plan()
        baseline = self._snapshot(self.project_root)

        with self.assertRaises(ValueError):
            self._prepare()

        self.assertEqual(self._snapshot(self.project_root), baseline)

    def test_line_exclusions_reject_conflicts_and_unknown_locations(self) -> None:
        """Отсутствующая строка, исключённая страница и дубликат блокируют план."""

        valid = {
            "page_number": 2,
            "line_number": 3,
            "expected_text": "English abstract.",
            "reason": "Синтетическая строка для проверки адресации, не правило корпуса.",
        }
        invalid_lists = [
            [valid, copy.deepcopy(valid)],
            [{**valid, "page_number": 1, "line_number": 1, "expected_text": "Титульный лист"}],
            [{**valid, "page_number": 4}],
            [{**valid, "line_number": 999}],
            [{**valid, "expected_text": "Другой текст"}],
        ]

        for exclusions in invalid_lists:
            with self.subTest(exclusions=exclusions):
                self.plan["documents"][0]["exclude_lines"] = exclusions
                self._write_plan()
                baseline = self._snapshot(self.project_root)

                with self.assertRaises(ValueError):
                    self._prepare()

                self.assertEqual(self._snapshot(self.project_root), baseline)

    def test_line_exclusion_schema_rejects_malformed_rules(self) -> None:
        """План не допускает неполных правил, переносов в строке и неверных типов."""

        valid = {
            "page_number": 2,
            "line_number": 3,
            "expected_text": "English abstract.",
            "reason": "Синтетическое исключение для проверки схемы.",
        }
        invalid_fields = [
            ("unknown", True), ("page_number", True), ("page_number", 0),
            ("line_number", True), ("line_number", 0), ("line_number", "3"),
            ("expected_text", ""), ("expected_text", " \t"),
            ("expected_text", "English abstract.\n"),
            ("expected_text", "English abstract.\r"),
            ("reason", ""), ("reason", " \t"),
        ]

        for field_name, value in invalid_fields:
            with self.subTest(field=field_name, value=value):
                self.plan["documents"][0]["exclude_lines"] = [{**valid, field_name: value}]
                self._write_plan()

                with self.assertRaises(ValueError):
                    self._prepare()

        for missing_field in valid:
            with self.subTest(missing=missing_field):
                exclusion = dict(valid)
                del exclusion[missing_field]
                self.plan["documents"][0]["exclude_lines"] = [exclusion]
                self._write_plan()

                with self.assertRaises(ValueError):
                    self._prepare()

    def test_new_plan_requires_explicit_line_exclusion_list(self) -> None:
        """Новая схема требует явный, при необходимости пустой список строк."""

        del self.plan["documents"][0]["exclude_lines"]
        self._write_plan()

        with self.assertRaisesRegex(ValueError, "схемы"):
            self._prepare()

    def test_line_removal_cannot_empty_retained_page(self) -> None:
        """Исключения строк не обходят явное решение об удалении целой страницы."""

        self._create_source(
            letter="b", page_texts=("Титул", "Единственная строка", "Редактор")
        )
        self.plan["documents"][0]["exclude_lines"] = [{
            "page_number": 2,
            "line_number": 1,
            "expected_text": "Единственная строка",
            "reason": "Проверка запрета пустого остатка.",
        }]
        self._write_plan()

        with self.assertRaises(ValueError):
            self._prepare()

    def test_reason_change_creates_new_package_without_replacing_previous(self) -> None:
        """Обоснование входит в хеш плана и не переписывает прежний журнал."""

        self.plan["documents"][0]["exclude_lines"] = [{
            "page_number": 2,
            "line_number": 3,
            "expected_text": "English abstract.",
            "reason": "Первое синтетическое обоснование для теста.",
        }]
        self._write_plan()
        original = self._prepare()
        publish_cleaning(original)
        baseline = self._snapshot(original.output_dir)
        self.plan["documents"][0]["exclude_lines"][0]["reason"] = "Уточнённое обоснование."
        self._write_plan()
        updated = self._prepare()

        self.assertNotEqual(updated.output_dir, original.output_dir)
        self.assertEqual(
            updated.report["documents"][0]["output_sha256"],
            original.report["documents"][0]["output_sha256"],
        )
        self.assertTrue(publish_cleaning(updated))
        self.assertEqual(self._snapshot(original.output_dir), baseline)

    def test_cli_publishes_the_prepared_package(self) -> None:
        """Обычный запуск команды публикует проверенные байты и возвращает ноль."""

        prepared = self._prepare()

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return_code = main(
                [str(self.plan_path)],
                project_root=self.project_root,
                schema_dir=self.schema_dir,
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(
            {
                relative_path: content
                for relative_path, (content, _) in self._snapshot(
                    prepared.output_dir
                ).items()
            },
            prepared.files,
        )

    def test_plan_schema_rejects_unknown_fields_and_malformed_values(self) -> None:
        """План не допускает неизвестных ключей, неверных хешей и типов."""

        baseline = copy.deepcopy(self.plan)
        invalid_fields = [
            ("root", "unknown", True),
            ("root", "schema_version", "other-v1"),
            ("root", "preprocessing_version", "../escape"),
            ("root", "documents", []),
            ("document", "unknown", True),
            ("document", "source_artifact_id", "sha256:short"),
            ("document", "source_artifact_id", "sha256:" + "A" * 64),
            ("document", "pages_manifest_sha256", "not-a-hash"),
            ("document", "join_words", ["два слова"]),
            ("exclusion", "unknown", True),
            ("exclusion", "page_number", True),
            ("exclusion", "page_number", 0),
            ("exclusion", "reason", ""),
        ]

        for scope, field_name, value in invalid_fields:
            with self.subTest(scope=scope, field=field_name, value=value):
                self.plan = copy.deepcopy(baseline)
                target = self.plan

                if scope != "root":
                    target = self.plan["documents"][0]

                if scope == "exclusion":
                    target = target["exclude_pages"][0]

                target[field_name] = value
                self._write_plan()

                with self.assertRaises(EXPECTED_ERRORS):
                    self._prepare()

    def test_historical_plan_is_not_silently_run_with_new_rules(self) -> None:
        """Исторические v1 и v2 не получают новые правила под прежней версией."""

        self.plan["schema_version"] = "text-cleaning-plan-v1"
        del self.plan["documents"][0]["exclude_lines"]

        for version in ("text-clean-conservative-v1", "text-clean-conservative-v2"):
            with self.subTest(version=version):
                self.plan["preprocessing_version"] = version
                self._write_plan()
                baseline = self._snapshot(self.project_root)

                with self.assertRaisesRegex(ValueError, "не поддерживается"):
                    self._prepare()

                self.assertEqual(self._snapshot(self.project_root), baseline)

    def test_duplicate_json_keys_are_rejected_at_each_depth(self) -> None:
        """Повтор ключа не должен незаметно заменять прежнее значение плана."""

        plan_text = self.plan_path.read_text(encoding="utf-8")

        for field_name, value in (
            ("schema_version", "text-cleaning-plan-v2"),
            ("work_id", self.source_artifact["work_id"]),
        ):
            with self.subTest(field=field_name):
                marker = f'"{field_name}":'
                duplicated = plan_text.replace(
                    marker,
                    f'{marker} {json.dumps(value)}, {marker}',
                    1,
                )
                self.plan_path.write_text(duplicated, encoding="utf-8")

                with self.assertRaises(EXPECTED_ERRORS):
                    self._prepare()

    def test_duplicate_work_and_duplicate_exclusion_are_rejected(self) -> None:
        """Одна работа или страница не может иметь два решения в одном плане."""

        baseline = copy.deepcopy(self.plan)

        for duplicated_list in ("documents", "exclude_pages"):
            with self.subTest(field=duplicated_list):
                self.plan = copy.deepcopy(baseline)
                records = self.plan["documents"]

                if duplicated_list == "exclude_pages":
                    records = records[0]["exclude_pages"]

                records.append(copy.deepcopy(records[0]))
                self._write_plan()

                with self.assertRaises(EXPECTED_ERRORS):
                    self._prepare()

    def test_exclusions_cannot_remove_unknown_or_all_pages(self) -> None:
        """Удаление несуществующей страницы и пустой остаток отклоняются."""

        for page_numbers in ([4], [1, 2, 3]):
            with self.subTest(pages=page_numbers):
                self.plan["documents"][0]["exclude_pages"] = [
                    {"page_number": page_number, "reason": "Тестовое решение."}
                    for page_number in page_numbers
                ]
                self._write_plan()

                with self.assertRaises(EXPECTED_ERRORS):
                    self._prepare()

    def test_empty_retained_page_cannot_create_empty_candidate(self) -> None:
        """Наличие физической страницы не оправдывает пустой очищенный текст."""

        self._create_source(letter="b", page_texts=("Титул", "", "Редактор"))

        with self.assertRaises(EXPECTED_ERRORS):
            self._prepare()

    def test_unregistered_artifact_and_wrong_work_are_rejected(self) -> None:
        """Чужая работа и отсутствующий артефакт не подменяют источник."""

        baseline = copy.deepcopy(self.plan)

        for field_name, value in (
            ("source_artifact_id", "sha256:" + "0" * 64),
            ("source_artifact_id", self.parent_artifact["artifact_id"]),
            ("work_id", "doi:10.1000/phys.unknown"),
        ):
            with self.subTest(field=field_name, value=value):
                self.plan = copy.deepcopy(baseline)
                self.plan["documents"][0][field_name] = value
                self._write_plan()

                with self.assertRaises(EXPECTED_ERRORS):
                    self._prepare()

    def test_manifest_audit_failure_stops_processing(self) -> None:
        """Нарушение целостности рабочего реестра блокирует даже верный план."""

        original_files = self._snapshot(self.project_root)
        report = AuditReport(counts={}, errors=("Повреждён реестр.",), warnings=())

        with (
            patch.object(type(self.store), "audit", return_value=report) as audit,
            self.assertRaises(EXPECTED_ERRORS),
        ):
            self._prepare()

        audit.assert_called_once()
        self.assertEqual(self._snapshot(self.project_root), original_files)

    def test_source_pdf_and_text_tampering_are_detected(self) -> None:
        """Изменение байтов родительского PDF или полного TXT блокирует очистку."""

        for source_path in (self.source_pdf_path, self.source_text_path):
            with self.subTest(path=source_path.name):
                original_bytes = source_path.read_bytes()
                source_path.write_bytes(original_bytes + b"\nchanged")

                try:
                    with self.assertRaises(EXPECTED_ERRORS):
                        self._prepare()

                finally:
                    source_path.write_bytes(original_bytes)

    def test_pages_manifest_digest_must_match_plan(self) -> None:
        """Индекс нельзя изменить даже пробелом без нового согласованного хеша."""

        self.pages_manifest_path.write_bytes(
            self.pages_manifest_path.read_bytes() + b"\n"
        )

        with self.assertRaises(EXPECTED_ERRORS):
            self._prepare()

    def test_page_bytes_must_match_pinned_page_digest(self) -> None:
        """Изменение отдельной страницы обнаруживается до любых преобразований."""

        page_path = self.pages_dir / "page_0002.txt"
        page_path.write_bytes(page_path.read_bytes() + " Подмена.".encode())

        with self.assertRaises(EXPECTED_ERRORS):
            self._prepare()

    def test_reassembly_rejects_self_consistent_but_foreign_page_text(self) -> None:
        """Пересчитанные хеши не разрешают подменить содержимое полного текста."""

        page_path = self.pages_dir / "page_0002.txt"
        replacement = "Другой физический текст."
        page_path.write_text(replacement, encoding="utf-8")
        records = self._page_records()
        records[1]["sha256"] = hashlib.sha256(page_path.read_bytes()).hexdigest()
        records[1]["characters"] = len(replacement)
        self._write_page_records(records)

        with self.assertRaises(EXPECTED_ERRORS):
            self._prepare()

    def test_page_index_provenance_and_schema_are_strict(self) -> None:
        """Индекс закрепляет порядок, происхождение и метод каждой страницы."""

        baseline = self._page_records()
        invalid_fields = [
            ("schema_version", "other-v1"),
            ("unknown", "лишнее поле"),
            ("sha256", "short"),
            ("characters", 1),
            ("page_number", 9),
            ("page_index", 9),
            ("source_pdf_sha256", "0" * 64),
            ("source_pdf_path", "data/raw/manual/another.pdf"),
            ("extraction_version", "other-version-v1"),
            ("extraction_method", "pdf_ocr_layout"),
        ]

        for field_name, value in invalid_fields:
            with self.subTest(field=field_name, value=value):
                records = copy.deepcopy(baseline)
                records[1][field_name] = value
                self._write_page_records(records)

                with self.assertRaises(EXPECTED_ERRORS):
                    self._prepare()

    def test_page_order_duplicates_and_gaps_are_rejected(self) -> None:
        """Индекс должен содержать страницы ровно один раз в исходном порядке."""

        baseline = self._page_records()

        for indices in ([1, 0, 2], [0, 1, 1, 2], [0, 2]):
            with self.subTest(indices=indices):
                self._write_page_records(
                    [copy.deepcopy(baseline[index]) for index in indices]
                )

                with self.assertRaises(EXPECTED_ERRORS):
                    self._prepare()

    def test_manifest_and_page_paths_cannot_escape_their_roots(self) -> None:
        """Абсолютные пути и переходы к родителю не становятся входом очистки."""

        baseline = copy.deepcopy(self.plan)
        outside_manifest = self.project_root / "manifests" / "pages-copy.jsonl"
        outside_manifest.write_bytes(self.pages_manifest_path.read_bytes())

        for invalid_path in (
            "../outside/pages.jsonl",
            str(self.pages_manifest_path.resolve()),
            outside_manifest.relative_to(self.project_root).as_posix(),
        ):
            with self.subTest(manifest_path=invalid_path):
                self.plan = copy.deepcopy(baseline)
                self.plan["documents"][0]["pages_manifest_path"] = invalid_path
                self._write_plan()

                with self.assertRaises(EXPECTED_ERRORS):
                    self._prepare()

        self.plan = baseline
        page_records = self._page_records()
        page_path = self.pages_dir / "page_0002.txt"
        outside_page = self.pages_dir.parent / page_path.name
        outside_page.write_bytes(page_path.read_bytes())

        for invalid_path in ("../page_0002.txt", str(page_path.resolve())):
            with self.subTest(page_path=invalid_path):
                records = copy.deepcopy(page_records)
                records[1]["path"] = invalid_path
                self._write_page_records(records)

                with self.assertRaises(EXPECTED_ERRORS):
                    self._prepare()

    def test_external_page_symlink_is_rejected_even_with_matching_bytes(self) -> None:
        """Совпадающие байты не разрешают читать страницу вне дерева проекта."""

        with tempfile.TemporaryDirectory() as outside_directory:
            outside_path = Path(outside_directory) / "page.txt"
            page_path = self.pages_dir / "page_0002.txt"
            outside_path.write_bytes(page_path.read_bytes())
            page_path.unlink()
            page_path.symlink_to(outside_path)

            with self.assertRaises(EXPECTED_ERRORS):
                self._prepare()

    def test_external_manifest_symlink_is_rejected(self) -> None:
        """Сам индекс не может быть ссылкой на чужой каталог с теми же байтами."""

        with tempfile.TemporaryDirectory() as outside_directory:
            outside_path = Path(outside_directory) / "pages.jsonl"
            outside_path.write_bytes(self.pages_manifest_path.read_bytes())
            self.pages_manifest_path.unlink()
            self.pages_manifest_path.symlink_to(outside_path)

            with self.assertRaises(EXPECTED_ERRORS):
                self._prepare()

    def test_external_output_directory_symlink_never_receives_files(self) -> None:
        """Ссылка в пути публикации не выводит производные файлы из проекта."""

        with tempfile.TemporaryDirectory() as outside_directory:
            outside_path = Path(outside_directory)
            output_root = self.project_root / "data" / "qa" / "preprocessing"
            output_root.symlink_to(outside_path, target_is_directory=True)

            with self.assertRaises(EXPECTED_ERRORS):
                publish_cleaning(self._prepare())

            self.assertEqual(list(outside_path.iterdir()), [])

    def test_existing_different_package_is_never_overwritten(self) -> None:
        """Чужой файл по адресу пакета сохраняется при конфликте публикации."""

        prepared = self._prepare()
        prepared.output_dir.mkdir(parents=True)
        conflict_path = prepared.output_dir / "report.json"
        conflict_path.write_bytes(b"user-owned report")
        original_files = self._snapshot(prepared.output_dir)

        with self.assertRaises(EXPECTED_ERRORS):
            publish_cleaning(prepared)

        self.assertEqual(self._snapshot(prepared.output_dir), original_files)

    def test_extra_file_in_published_package_is_a_conflict(self) -> None:
        """Повторная публикация проверяет точный состав уже записанного пакета."""

        prepared = self._prepare()
        publish_cleaning(prepared)
        (prepared.output_dir / "extra.txt").write_text("Чужой файл.", encoding="utf-8")
        original_files = self._snapshot(prepared.output_dir)

        with self.assertRaises(EXPECTED_ERRORS):
            publish_cleaning(prepared)

        self.assertEqual(self._snapshot(prepared.output_dir), original_files)

    def test_changed_report_in_published_package_is_not_restored(self) -> None:
        """Совпадение списка имён не разрешает перезаписать изменённый отчёт."""

        prepared = self._prepare()
        publish_cleaning(prepared)
        report_path = prepared.output_dir / "report.json"
        report_path.write_bytes(b"changed report")
        original_files = self._snapshot(prepared.output_dir)

        with self.assertRaises(EXPECTED_ERRORS):
            publish_cleaning(prepared)

        self.assertEqual(self._snapshot(prepared.output_dir), original_files)

    def test_failed_atomic_publication_leaves_no_package_or_lock(self) -> None:
        """Ошибка финального переименования убирает временный пакет и блокировку."""

        prepared = self._prepare()
        original_files = self._snapshot(self.project_root)

        with (
            patch.object(
                Path, "rename", side_effect=OSError("Тестовый сбой публикации.")
            ) as rename,
            self.assertRaises(OSError),
        ):
            publish_cleaning(prepared)

        rename.assert_called_once()
        self.assertFalse(prepared.output_dir.exists())
        self.assertEqual(list(prepared.output_dir.parent.iterdir()), [])
        self.assertEqual(self._snapshot(self.project_root), original_files)
        self.assertTrue(publish_cleaning(prepared))

    def test_existing_publication_lock_is_preserved_on_refusal(self) -> None:
        """Чужая блокировка сохраняет байты и время изменения после отказа."""

        prepared = self._prepare()
        prepared.output_dir.parent.mkdir(parents=True)
        lock_path = (
            prepared.output_dir.parent / f".{prepared.output_dir.name}.lock"
        )
        lock_path.write_bytes(b"other-process-lock")
        lock_mtime = lock_path.stat().st_mtime_ns
        original_files = self._snapshot(self.project_root)

        with self.assertRaises(EXPECTED_ERRORS):
            publish_cleaning(prepared)

        self.assertFalse(prepared.output_dir.exists())
        self.assertEqual(list(prepared.output_dir.parent.iterdir()), [lock_path])
        self.assertEqual(lock_path.read_bytes(), b"other-process-lock")
        self.assertEqual(lock_path.stat().st_mtime_ns, lock_mtime)
        self.assertEqual(self._snapshot(self.project_root), original_files)

    def test_published_symlink_with_identical_bytes_is_rejected(self) -> None:
        """Совпадающий отчёт по ссылке не считается точным повтором пакета."""

        prepared = self._prepare()
        publish_cleaning(prepared)
        report_path = prepared.output_dir / "report.json"
        duplicate_path = self.project_root / "data" / "qa" / "report-copy.json"
        duplicate_path.write_bytes(report_path.read_bytes())
        report_path.unlink()
        report_path.symlink_to(duplicate_path)
        symlink_mtime = report_path.lstat().st_mtime_ns
        original_files = self._snapshot(self.project_root)

        with self.assertRaises(EXPECTED_ERRORS):
            publish_cleaning(prepared)

        self.assertTrue(report_path.is_symlink())
        self.assertEqual(report_path.readlink(), duplicate_path)
        self.assertEqual(report_path.lstat().st_mtime_ns, symlink_mtime)
        self.assertEqual(self._snapshot(self.project_root), original_files)

    def test_second_staging_write_failure_cleans_own_temporary_files(self) -> None:
        """Сбой записи после первого файла убирает только собственный пакет."""

        prepared = self._prepare()
        original_files = self._snapshot(self.project_root)
        original_open = Path.open
        staging_paths: list[Path] = []

        @contextmanager
        def fail_second_write(
            path: Path,
            mode: str = "r",
            *arguments: Any,
            **keywords: Any,
        ) -> Iterator[Any]:
            """Прервать запись второго файла после успешной записи первого."""

            with original_open(path, mode, *arguments, **keywords) as stream:
                if mode == "xb":
                    self.assertTrue(path.is_relative_to(prepared.output_dir.parent))
                    staging_paths.append(path)

                    if len(staging_paths) == 2:
                        self.assertGreater(staging_paths[0].stat().st_size, 0)
                        failing_stream = MagicMock(wraps=stream)
                        failing_stream.write.side_effect = OSError(
                            "Тестовый сбой записи второго файла."
                        )
                        yield failing_stream
                        return

                yield stream

        with (
            patch.object(Path, "open", new=fail_second_write),
            self.assertRaises(OSError),
        ):
            publish_cleaning(prepared)

        self.assertEqual(len(staging_paths), 2)
        self.assertFalse(prepared.output_dir.exists())
        self.assertEqual(list(prepared.output_dir.parent.iterdir()), [])
        self.assertTrue(all(not path.exists() for path in staging_paths))
        self.assertEqual(self._snapshot(self.project_root), original_files)
        self.assertTrue(publish_cleaning(prepared))

    def test_cli_expected_error_has_no_traceback_or_partial_output(self) -> None:
        """Ожидаемая ошибка плана возвращает код один без трассировки и записи."""

        self.plan["documents"][0]["pages_manifest_sha256"] = "0" * 64
        self._write_plan()
        original_files = self._snapshot(self.project_root)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = main(
                [str(self.plan_path)],
                project_root=self.project_root,
                schema_dir=self.schema_dir,
            )

        self.assertEqual(return_code, 1)
        self.assertTrue((stdout.getvalue() + stderr.getvalue()).strip())
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())
        self.assertEqual(self._snapshot(self.project_root), original_files)


if __name__ == "__main__":
    unittest.main()
