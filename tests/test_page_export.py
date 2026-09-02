"""Проверки постраничного извлечения и экспорта текста PDF."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.rebuild_from_pdf import _build_parser, run_extraction
from src.collect import pdf_text as pdf_text_module
from src.collect.pdf_text import (
    PdfPageExportResult,
    PdfPageText,
    PdfTextExtraction,
    export_pdf_pages,
    extract_best_text_result,
    extract_pages_from_pdf,
    extract_pages_from_pdf_ocr,
)

ROOT = Path(__file__).resolve().parents[1]


class _FakePage:
    """Минимальная страница для имитации встроенного текстового слоя."""

    def __init__(self, text: str) -> None:
        """Сохранить текст, который вернёт PyMuPDF."""

        self.text = text

    def get_text(self, mode: str) -> str:
        """Вернуть тестовый текст только для обычного текстового режима."""

        if mode != "text":
            raise AssertionError(f"Неожиданный режим PyMuPDF: {mode}")

        return self.text


class _FakeDocument:
    """Минимальный документ с явной загрузкой страниц по индексу."""

    def __init__(self, pages: list[_FakePage]) -> None:
        """Сохранить страницы и начальный статус документа."""

        self.pages = pages
        self.page_count = len(pages)
        self.closed = False

    def load_page(self, page_index: int) -> _FakePage:
        """Вернуть страницу по нулевому индексу."""

        return self.pages[page_index]

    def close(self) -> None:
        """Отметить закрытие тестового документа."""

        self.closed = True


class PageExtractionTests(unittest.TestCase):
    """Проверки физических границ страниц и единственного прохода OCR."""

    def test_embedded_text_preserves_every_physical_page(self) -> None:
        """Пустая страница и нумерация не должны исчезать из результата."""

        document = _FakeDocument(
            [
                _FakePage("1\nПервая страница\n2"),
                _FakePage("\n\n"),
                _FakePage("3\nТретья страница\n4"),
            ]
        )

        with patch("src.collect.pdf_text._fitz_open", return_value=document):
            pages = extract_pages_from_pdf(Path("article.pdf"))

        self.assertEqual(
            [(page.page_index, page.page_number) for page in pages],
            [(0, 1), (1, 2), (2, 3)],
        )
        self.assertEqual(
            [page.text for page in pages],
            ["Первая страница", "", "Третья страница"],
        )
        self.assertTrue(document.closed)

    def test_ocr_receives_stable_page_indices_once(self) -> None:
        """OCR должен обработать каждую страницу один раз в исходном порядке."""

        document = _FakeDocument([_FakePage(""), _FakePage("")])
        visited_indices: list[int] = []

        def recognize_page(
            page: _FakePage,
            *,
            page_index: int = 0,
            dpi: int = 200,
            lang: str = "rus",
        ) -> str:
            """Зафиксировать индекс и вернуть различимый текст страницы."""

            self.assertIs(page, document.pages[page_index])
            self.assertEqual(dpi, 200)
            self.assertEqual(lang, "rus")
            visited_indices.append(page_index)
            return f"Страница {page_index + 1}"

        with (
            patch("src.collect.pdf_text._require_tesseract"),
            patch("src.collect.pdf_text._fitz_open", return_value=document),
            patch(
                "src.collect.pdf_text._ocr_page_layout",
                side_effect=recognize_page,
            ),
        ):
            pages = extract_pages_from_pdf_ocr(Path("article.pdf"))

        self.assertEqual(visited_indices, [0, 1])
        self.assertEqual([page.text for page in pages], ["Страница 1", "Страница 2"])
        self.assertTrue(document.closed)

    def test_best_result_reuses_pages_from_single_ocr_pass(self) -> None:
        """Общий OCR-текст должен собираться из уже полученных страниц."""

        raw_pages = (PdfPageText(0, 1, "???"),)
        first_text = ("Физический текст первой страницы. " * 8).strip()
        second_text = ("Физический текст второй страницы. " * 8).strip()
        ocr_pages = (
            PdfPageText(0, 1, first_text),
            PdfPageText(1, 2, second_text),
        )

        with (
            patch(
                "src.collect.pdf_text.extract_pages_from_pdf",
                return_value=raw_pages,
            ) as embedded_extractor,
            patch(
                "src.collect.pdf_text.extract_pages_from_pdf_ocr",
                return_value=ocr_pages,
            ) as ocr_extractor,
        ):
            result = extract_best_text_result(Path("article.pdf"))

        embedded_extractor.assert_called_once_with(Path("article.pdf"))
        ocr_extractor.assert_called_once_with(Path("article.pdf"))
        self.assertEqual(result.method, "pdf_ocr_layout")
        self.assertTrue(result.readable)
        self.assertEqual(result.pages, ocr_pages)
        self.assertEqual(result.text, f"{first_text}\n\n{second_text}")


class PageExportTests(unittest.TestCase):
    """Проверки чистых TXT-файлов, индекса и явной опции CLI."""

    def test_export_writes_clean_text_and_exact_metadata(self) -> None:
        """Хеш и число символов должны описывать точные байты каждого TXT."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "article.pdf"
            pdf_path.write_bytes(b"%PDF-test\n%%EOF")
            source_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            first_text = "Энергия E = mc²"
            second_text = "Вторая страница"
            extraction = PdfTextExtraction(
                text=f"{first_text}\n{second_text}",
                method="pdf",
                readable=True,
                pages=(
                    PdfPageText(0, 1, first_text),
                    PdfPageText(1, 2, second_text),
                ),
            )
            output_dir = root / "pages"

            result = export_pdf_pages(
                pdf_path,
                extraction,
                output_dir,
                extraction_version="pdf-text-test-v1",
                source_pdf_path="data/raw/article.pdf",
                source_pdf_sha256=source_sha256,
            )

            records = [
                json.loads(line)
                for line in result.manifest_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

            self.assertEqual(len(result.pages), 2)
            self.assertEqual(
                (output_dir / "page_0001.txt").read_text(encoding="utf-8"),
                first_text,
            )
            self.assertFalse(
                (output_dir / "page_0001.txt").read_text(
                    encoding="utf-8"
                ).startswith("#")
            )
            self.assertEqual(records[0]["page_index"], 0)
            self.assertEqual(records[0]["page_number"], 1)
            self.assertEqual(records[0]["path"], "page_0001.txt")
            self.assertEqual(
                records[0]["extraction_version"],
                "pdf-text-test-v1",
            )
            self.assertEqual(records[0]["characters"], len(first_text))
            self.assertEqual(
                records[0]["sha256"],
                hashlib.sha256(first_text.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(records[0]["source_pdf_sha256"], source_sha256)
            self.assertEqual(records[1]["page_index"], 1)
            self.assertEqual(records[1]["page_number"], 2)

    def test_export_rejects_text_not_assembled_from_pages(self) -> None:
        """Индекс нельзя записать для страниц от другого результата."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "article.pdf"
            pdf_path.write_bytes(b"%PDF-test\n%%EOF")
            extraction = PdfTextExtraction(
                text="Другой общий текст",
                method="pdf",
                readable=True,
                pages=(PdfPageText(0, 1, "Страница"),),
            )
            output_dir = root / "pages"

            with self.assertRaisesRegex(ValueError, "Общий текст не совпадает"):
                export_pdf_pages(
                    pdf_path,
                    extraction,
                    output_dir,
                    extraction_version="pdf-text-test-v1",
                )

            self.assertFalse(output_dir.exists())

    def test_identical_repeated_export_does_not_rewrite_files(self) -> None:
        """Точный повтор должен только проверить опубликованный набор."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "article.pdf"
            pdf_path.write_bytes(b"%PDF-test\n%%EOF")
            output_dir = root / "pages"
            extraction = PdfTextExtraction(
                text="Первая\nВторая\nТретья",
                method="pdf",
                readable=True,
                pages=(
                    PdfPageText(0, 1, "Первая"),
                    PdfPageText(1, 2, "Вторая"),
                    PdfPageText(2, 3, "Третья"),
                ),
            )

            export_pdf_pages(
                pdf_path,
                extraction,
                output_dir,
                extraction_version="pdf-text-test-v1",
            )

            with patch(
                "src.collect.pdf_text._write_bytes_atomic"
            ) as writer:
                export_pdf_pages(
                    pdf_path,
                    extraction,
                    output_dir,
                    extraction_version="pdf-text-test-v1",
                )

            writer.assert_not_called()

    def test_repeated_export_rejects_conflict_without_changes(self) -> None:
        """Другие байты той же версии не должны менять опубликованный набор."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "article.pdf"
            pdf_path.write_bytes(b"%PDF-test\n%%EOF")
            output_dir = root / "pages"
            first_extraction = PdfTextExtraction(
                text="Первая\nВторая",
                method="pdf",
                readable=True,
                pages=(
                    PdfPageText(0, 1, "Первая"),
                    PdfPageText(1, 2, "Вторая"),
                ),
            )
            conflicting_extraction = PdfTextExtraction(
                text="Новая первая",
                method="pdf",
                readable=True,
                pages=(PdfPageText(0, 1, "Новая первая"),),
            )

            export_pdf_pages(
                pdf_path,
                first_extraction,
                output_dir,
                extraction_version="pdf-text-test-v1",
            )
            original_files = {
                path.name: path.read_bytes() for path in output_dir.iterdir()
            }

            with self.assertRaisesRegex(
                ValueError,
                "Конфликт постраничного экспорта",
            ):
                export_pdf_pages(
                    pdf_path,
                    conflicting_extraction,
                    output_dir,
                    extraction_version="pdf-text-test-v1",
                )

            current_files = {
                path.name: path.read_bytes() for path in output_dir.iterdir()
            }
            self.assertEqual(current_files, original_files)

    def test_failed_initial_export_leaves_no_partial_catalogue(self) -> None:
        """Ошибка в staging не должна публиковать часть набора страниц."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "article.pdf"
            pdf_path.write_bytes(b"%PDF-test\n%%EOF")
            output_dir = root / "pages"
            extraction = PdfTextExtraction(
                text="Первая\nВторая",
                method="pdf",
                readable=True,
                pages=(
                    PdfPageText(0, 1, "Первая"),
                    PdfPageText(1, 2, "Вторая"),
                ),
            )
            real_writer = pdf_text_module._write_bytes_atomic
            call_count = 0

            def failing_writer(path: Path, data: bytes) -> None:
                """Остановить вторую запись после успешной первой."""

                nonlocal call_count
                call_count += 1

                if call_count == 2:
                    raise OSError("тестовый сбой записи")

                real_writer(path, data)

            with (
                patch(
                    "src.collect.pdf_text._write_bytes_atomic",
                    side_effect=failing_writer,
                ),
                self.assertRaisesRegex(OSError, "тестовый сбой записи"),
            ):
                export_pdf_pages(
                    pdf_path,
                    extraction,
                    output_dir,
                    extraction_version="pdf-text-test-v1",
                )

            self.assertFalse(output_dir.exists())
            self.assertEqual(list(root.glob(".*.staging")), [])

    def test_atomic_writer_removes_part_file_after_fsync_error(self) -> None:
        """Временный файл должен удаляться даже при ошибке fsync."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output_path = root / "page_0001.txt"

            with (
                patch(
                    "src.collect.pdf_text.os.fsync",
                    side_effect=OSError("тестовый сбой fsync"),
                ),
                self.assertRaisesRegex(OSError, "тестовый сбой fsync"),
            ):
                pdf_text_module._write_bytes_atomic(output_path, b"text")

            self.assertFalse(output_path.exists())
            self.assertEqual(list(root.glob(".*.part")), [])

    def test_cli_requires_explicit_page_export_flag(self) -> None:
        """Постраничная запись должна быть выключена по умолчанию."""

        parser = _build_parser(Path("/project"))
        default_options = parser.parse_args(["input.jsonl"])
        enabled_options = parser.parse_args(
            [
                "input.jsonl",
                "--export-pages",
                "--page-output-dir",
                "data/qa/custom",
            ]
        )

        self.assertFalse(default_options.export_pages)
        self.assertTrue(enabled_options.export_pages)
        self.assertEqual(
            enabled_options.page_output_dir,
            Path("data/qa/custom"),
        )

    def test_cli_exports_pages_from_same_extraction_result(self) -> None:
        """Команда должна вызвать извлечение один раз и записать путь в отчёт."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            pdf_path = project_root / "data" / "raw" / "article.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"%PDF-test\n%%EOF")
            source_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            page_text = "Нечитаемый для порога короткий текст"
            extraction = PdfTextExtraction(
                text=page_text,
                method="pdf_unreadable",
                readable=False,
                pages=(PdfPageText(0, 1, page_text),),
            )
            registration = SimpleNamespace(
                relative_path="data/raw/article.pdf",
                title="Тестовая статья",
            )
            parent_artifact = {
                "artifact_id": f"sha256:{source_sha256}",
                "work_id": "work:test-page-export",
                "path": "data/raw/article.pdf",
                "sha256": source_sha256,
            }
            extraction_calls: list[Path] = []

            def extractor(
                source_path: Path,
                *,
                text_dir: Path | None = None,
                try_ocr: bool = True,
            ) -> PdfTextExtraction:
                """Вернуть один заранее подготовленный постраничный результат."""

                self.assertIsNone(text_dir)
                self.assertFalse(try_ocr)
                extraction_calls.append(source_path)
                return extraction

            manifest_dir = project_root / "manifests"
            page_output_dir = project_root / "data" / "qa" / "pages"

            with (
                patch(
                    "scripts.rebuild_from_pdf.read_local_file_registrations",
                    return_value=[registration],
                ),
                patch(
                    "scripts.rebuild_from_pdf.find_registered_pdf_artifact",
                    return_value=parent_artifact,
                ),
            ):
                return_code = run_extraction(
                    [
                        "input.jsonl",
                        "--manifest-dir",
                        str(manifest_dir),
                        "--no-ocr",
                        "--export-pages",
                        "--page-output-dir",
                        str(page_output_dir),
                    ],
                    project_root=project_root,
                    schema_dir=ROOT / "manifests" / "schemas",
                    extractor=extractor,
                )

            report_path = manifest_dir / "results" / "input_extraction.jsonl"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            exported_manifest = (
                page_output_dir
                / "pdf-text-v1"
                / source_sha256
                / "pdf_unreadable"
                / "pages.jsonl"
            )

            self.assertEqual(return_code, 0)
            self.assertEqual(extraction_calls, [pdf_path.resolve()])
            self.assertTrue(exported_manifest.is_file())
            self.assertEqual(report["page_exported_pages"], 1)
            self.assertEqual(
                report["page_export_manifest_path"],
                exported_manifest.relative_to(project_root).as_posix(),
            )

    def test_dry_run_validates_export_only_in_temporary_directory(self) -> None:
        """Предварительная проверка должна выполнить экспорт без сохранения."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            project_root = Path(temporary_directory)
            pdf_path = project_root / "data" / "raw" / "article.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"%PDF-test\n%%EOF")
            source_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            page_text = "Короткий текст страницы"
            extraction = PdfTextExtraction(
                text=page_text,
                method="pdf_unreadable",
                readable=False,
                pages=(PdfPageText(0, 1, page_text),),
            )
            registration = SimpleNamespace(
                relative_path="data/raw/article.pdf",
                title="Тестовая статья",
            )
            parent_artifact = {
                "artifact_id": f"sha256:{source_sha256}",
                "work_id": "work:test-page-export-dry-run",
                "path": "data/raw/article.pdf",
                "sha256": source_sha256,
            }
            temporary_export_paths: list[Path] = []

            def extractor(
                source_path: Path,
                *,
                text_dir: Path | None = None,
                try_ocr: bool = True,
            ) -> PdfTextExtraction:
                """Вернуть подготовленный результат без обращения к OCR."""

                self.assertEqual(source_path, pdf_path.resolve())
                self.assertIsNone(text_dir)
                self.assertFalse(try_ocr)
                return extraction

            def page_exporter(
                source_path: Path,
                page_extraction: PdfTextExtraction,
                output_dir: Path,
                *,
                extraction_version: str,
                source_pdf_path: str | None = None,
                source_pdf_sha256: str | None = None,
            ) -> PdfPageExportResult:
                """Зафиксировать временный путь и вызвать настоящий экспорт."""

                temporary_export_paths.append(output_dir)
                return export_pdf_pages(
                    source_path,
                    page_extraction,
                    output_dir,
                    extraction_version=extraction_version,
                    source_pdf_path=source_pdf_path,
                    source_pdf_sha256=source_pdf_sha256,
                )

            manifest_dir = project_root / "manifests"
            configured_output = project_root / "data" / "qa" / "pages"

            with (
                patch(
                    "scripts.rebuild_from_pdf.read_local_file_registrations",
                    return_value=[registration],
                ),
                patch(
                    "scripts.rebuild_from_pdf.find_registered_pdf_artifact",
                    return_value=parent_artifact,
                ),
            ):
                return_code = run_extraction(
                    [
                        "input.jsonl",
                        "--manifest-dir",
                        str(manifest_dir),
                        "--no-ocr",
                        "--export-pages",
                        "--page-output-dir",
                        str(configured_output),
                        "--dry-run",
                    ],
                    project_root=project_root,
                    schema_dir=ROOT / "manifests" / "schemas",
                    extractor=extractor,
                    page_exporter=page_exporter,
                )

            self.assertEqual(return_code, 0)
            self.assertEqual(len(temporary_export_paths), 1)
            self.assertFalse(temporary_export_paths[0].exists())
            self.assertFalse(configured_output.exists())
            self.assertFalse(
                (manifest_dir / "results" / "input_extraction.jsonl").exists()
            )


if __name__ == "__main__":
    unittest.main()
