"""Проверки явного выбора макета OCR и сохранения отдельных результатов."""

from __future__ import annotations

import tempfile
import unittest

from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz

from src.collect import pdf_text
from src.collect.pdf_text import PdfPageText

READABLE_TEXT = "Русскоязычный научный текст о физических явлениях. " * 10
LAYOUT_VERSIONS = {
    "ufn": "pdf-text-rus-eng-v2",
    "single-column": "pdf-text-rus-eng-single-column-v3",
    "two-column": "pdf-text-rus-eng-two-column-v3",
}


def _fake_document(page_count: int = 2) -> MagicMock:
    """Создать страницы без чтения PDF, запуска OCR и сетевых запросов."""

    document = MagicMock()
    document.page_count = page_count
    pages = []

    for _ in range(page_count):
        page = MagicMock()
        page.rect = fitz.Rect(0, 0, 600, 800)
        pages.append(page)

    document.load_page.side_effect = pages.__getitem__
    return document


class OcrPageLayoutTests(unittest.TestCase):
    """Проверки областей распознавания для каждого поддержанного макета."""

    def test_single_column_uses_whole_page_at_any_index(self) -> None:
        """Одноколоночная страница не должна разрезаться даже после первой."""

        page = _fake_document().load_page(0)

        for page_index in (0, 1, 7):
            with (
                self.subTest(page_index=page_index),
                patch.object(pdf_text, "_detect_two_column_split_y") as detector,
                patch.object(pdf_text, "_ocr_columns_in_rect") as columns,
                patch.object(
                    pdf_text,
                    "_ocr_page_full",
                    return_value="Полная строка без разрезания",
                ) as whole_page,
            ):
                result = pdf_text._ocr_page_layout(
                    page,
                    page_index=page_index,
                    dpi=250,
                    lang="rus",
                    ocr_layout="single-column",
                )

                whole_page.assert_called_once_with(
                    page,
                    clip=page.rect,
                    dpi=250,
                    lang="rus",
                )
                columns.assert_not_called()
                detector.assert_not_called()
                self.assertEqual(result, "Полная строка без разрезания")

    def test_two_columns_use_entire_page_without_header_detection(self) -> None:
        """Явные две колонки не должны зависеть от номера страницы и шапки."""

        page = _fake_document().load_page(0)

        for page_index in (0, 1, 7):
            with (
                self.subTest(page_index=page_index),
                patch.object(pdf_text, "_detect_two_column_split_y") as detector,
                patch.object(pdf_text, "_ocr_page_full") as whole_page,
                patch.object(
                    pdf_text,
                    "_ocr_columns_in_rect",
                    return_value="Левая колонка\n\nПравая колонка",
                ) as columns,
            ):
                result = pdf_text._ocr_page_layout(
                    page,
                    page_index=page_index,
                    dpi=250,
                    lang="rus",
                    ocr_layout="two-column",
                )

                columns.assert_called_once_with(
                    page,
                    page.rect,
                    dpi=250,
                    lang="rus",
                )
                whole_page.assert_not_called()
                detector.assert_not_called()
                self.assertEqual(result, "Левая колонка\n\nПравая колонка")

    def test_default_ufn_keeps_mixed_header_and_columns(self) -> None:
        """Макет по умолчанию должен сохранять шапку УФН над колонками."""

        page = _fake_document().load_page(0)

        with (
            patch.object(
                pdf_text,
                "_detect_two_column_split_y",
                return_value=200,
            ) as detector,
            patch.object(
                pdf_text,
                "_ocr_page_full",
                return_value="Заголовок\n",
            ) as whole_page,
            patch.object(
                pdf_text,
                "_ocr_columns_in_rect",
                return_value="Основной текст\n",
            ) as columns,
        ):
            result = pdf_text._ocr_page_layout(page)

        detector.assert_called_once_with(page, page_index=0)
        whole_page.assert_called_once_with(
            page,
            clip=fitz.Rect(0, 0, 600, 200),
            dpi=200,
            lang="rus+eng",
        )
        columns.assert_called_once_with(
            page,
            fitz.Rect(0, 200, 600, 800),
            dpi=200,
            lang="rus+eng",
        )
        self.assertEqual(result, "Заголовок\n\nОсновной текст")

    def test_ufn_keeps_two_column_pages_without_header(self) -> None:
        """УФН должен сохранять прежнюю обработку страниц без общей шапки."""

        page = _fake_document().load_page(0)

        with (
            patch.object(
                pdf_text,
                "_detect_two_column_split_y",
                return_value=None,
            ) as detector,
            patch.object(pdf_text, "_ocr_page_full") as whole_page,
            patch.object(
                pdf_text,
                "_ocr_columns_in_rect",
                return_value="Колонки",
            ) as columns,
        ):
            result = pdf_text._ocr_page_layout(page, page_index=3)

        detector.assert_called_once_with(page, page_index=3)
        columns.assert_called_once_with(
            page,
            page.rect,
            dpi=200,
            lang="rus+eng",
        )
        whole_page.assert_not_called()
        self.assertEqual(result, "Колонки")

    def test_ufn_keeps_detected_whole_page(self) -> None:
        """Отсутствие колонок в первой странице УФН сохраняет целую область."""

        page = _fake_document().load_page(0)

        with (
            patch.object(
                pdf_text,
                "_detect_two_column_split_y",
                return_value=800,
            ),
            patch.object(pdf_text, "_ocr_columns_in_rect") as columns,
            patch.object(
                pdf_text,
                "_ocr_page_full",
                return_value="Цельная страница",
            ) as whole_page,
        ):
            result = pdf_text._ocr_page_layout(page, ocr_layout="ufn")

        whole_page.assert_called_once_with(
            page,
            clip=page.rect,
            dpi=200,
            lang="rus+eng",
        )
        columns.assert_not_called()
        self.assertEqual(result, "Цельная страница")


class OcrLayoutPropagationTests(unittest.TestCase):
    """Проверки передачи макета через публичные функции извлечения."""

    def test_page_extractors_forward_layout_language_and_indices(self) -> None:
        """Оба постраничных способа должны передавать выбранный макет и язык."""

        for ocr_layout in LAYOUT_VERSIONS:
            for selected in (False, True):
                with self.subTest(ocr_layout=ocr_layout, selected=selected):
                    document = _fake_document(3)
                    expected_indices = [2, 0] if selected else [0, 1, 2]

                    with (
                        patch.object(pdf_text, "_require_tesseract"),
                        patch.object(
                            pdf_text,
                            "_fitz_open",
                            return_value=document,
                        ),
                        patch.object(
                            pdf_text,
                            "_ocr_page_layout",
                            return_value="Содержимое страницы",
                        ) as recognize_page,
                    ):
                        if selected:
                            pages = pdf_text.extract_selected_pages_from_pdf_ocr(
                                Path("article.pdf"),
                                expected_indices,
                                ocr_layout=ocr_layout,
                                lang="rus",
                                dpi=250,
                            )

                        else:
                            pages = pdf_text.extract_pages_from_pdf_ocr(
                                Path("article.pdf"),
                                ocr_layout=ocr_layout,
                                lang="rus",
                                dpi=250,
                            )

                    self.assertEqual(
                        [page.page_index for page in pages],
                        expected_indices,
                    )
                    self.assertEqual(
                        recognize_page.call_count,
                        len(expected_indices),
                    )

                    for page_index, call in zip(
                        expected_indices,
                        recognize_page.call_args_list,
                        strict=True,
                    ):
                        self.assertEqual(call.args, (document.load_page(page_index),))
                        self.assertEqual(
                            call.kwargs,
                            {
                                "page_index": page_index,
                                "dpi": 250,
                                "lang": "rus",
                                "ocr_layout": ocr_layout,
                            },
                        )

                    document.close.assert_called_once_with()

    def test_text_wrapper_forwards_layout_and_language(self) -> None:
        """Сборка общего текста не должна терять явные макет и язык OCR."""

        for ocr_layout in LAYOUT_VERSIONS:
            with self.subTest(ocr_layout=ocr_layout), patch.object(
                pdf_text,
                "extract_pages_from_pdf_ocr",
                return_value=(PdfPageText(0, 1, "Текст страницы"),),
            ) as extract_pages:
                result = pdf_text.extract_text_from_pdf_ocr(
                    Path("article.pdf"),
                    ocr_layout=ocr_layout,
                    lang="rus",
                    dpi=250,
                )

                extract_pages.assert_called_once_with(
                    Path("article.pdf"),
                    lang="rus",
                    dpi=250,
                    ocr_layout=ocr_layout,
                )
                self.assertEqual(result, "Текст страницы")

    def test_best_text_wrappers_forward_layout_to_ocr(self) -> None:
        """Все обёртки выбора текста должны сохранять макет до вызова OCR."""

        for extractor in (
            pdf_text.extract_best_text_result,
            pdf_text.extract_best_text,
            pdf_text.extract_text_from_pdf_checked,
        ):
            for ocr_layout in ("single-column", "two-column"):
                with self.subTest(extractor=extractor.__name__, layout=ocr_layout):
                    document = _fake_document(1)

                    with (
                        patch.object(
                            pdf_text,
                            "extract_pages_from_pdf",
                            return_value=(PdfPageText(0, 1, "???"),),
                        ),
                        patch.object(pdf_text, "_require_tesseract"),
                        patch.object(
                            pdf_text,
                            "_fitz_open",
                            return_value=document,
                        ),
                        patch.object(
                            pdf_text,
                            "_ocr_page_layout",
                            return_value=READABLE_TEXT,
                        ) as recognize_page,
                    ):
                        extractor(Path("article.pdf"), ocr_layout=ocr_layout)

                    recognize_page.assert_called_once_with(
                        document.load_page(0),
                        page_index=0,
                        dpi=200,
                        lang="rus+eng",
                        ocr_layout=ocr_layout,
                    )

    def test_download_wrapper_preserves_layout_without_network(self) -> None:
        """PDF из кэша должен получать заданный макет и отдельный TXT."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_dir = Path(temporary_directory) / "pdf"
            cache_dir.mkdir()
            pdf_path = cache_dir / "article.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
            text_dir = Path(temporary_directory) / "texts"
            document = _fake_document(1)

            with (
                patch.object(pdf_text, "download_pdf") as download,
                patch.object(
                    pdf_text,
                    "extract_pages_from_pdf",
                    return_value=(PdfPageText(0, 1, "???"),),
                ),
                patch.object(pdf_text, "_require_tesseract"),
                patch.object(pdf_text, "_fitz_open", return_value=document),
                patch.object(
                    pdf_text,
                    "_ocr_page_layout",
                    return_value=READABLE_TEXT,
                ) as recognize_page,
            ):
                text, result_path, readable, method = pdf_text.pdf_to_text(
                    "https://example.test/article.pdf",
                    cache_dir,
                    text_dir=text_dir,
                    ocr_layout="single-column",
                )

            download.assert_not_called()
            self.assertEqual(result_path, pdf_path)
            self.assertTrue(readable)
            self.assertEqual(text, READABLE_TEXT.strip())
            self.assertEqual(method, "pdf_ocr_layout")
            self.assertEqual(
                recognize_page.call_args.kwargs["ocr_layout"],
                "single-column",
            )
            saved_text = (
                text_dir
                / LAYOUT_VERSIONS["single-column"]
                / "article_pdf_ocr_layout.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("# ocr layout: single-column", saved_text)

    def test_readable_embedded_text_does_not_trigger_ocr(self) -> None:
        """Макет меняет только OCR и не отменяет читаемый текстовый слой."""

        for ocr_layout in LAYOUT_VERSIONS:
            with (
                self.subTest(ocr_layout=ocr_layout),
                patch.object(
                    pdf_text,
                    "extract_pages_from_pdf",
                    return_value=(PdfPageText(0, 1, READABLE_TEXT),),
                ),
                patch.object(pdf_text, "extract_pages_from_pdf_ocr") as recognize,
            ):
                result = pdf_text.extract_best_text_result(
                    Path("article.pdf"),
                    ocr_layout=ocr_layout,
                )

                self.assertEqual(result.method, "pdf")
                self.assertTrue(result.readable)
                recognize.assert_not_called()

    def test_disabled_ocr_keeps_unreadable_embedded_text(self) -> None:
        """Явный макет не должен включать OCR вопреки флагу try_ocr."""

        with (
            patch.object(
                pdf_text,
                "extract_pages_from_pdf",
                return_value=(PdfPageText(0, 1, "???"),),
            ),
            patch.object(pdf_text, "extract_pages_from_pdf_ocr") as recognize,
        ):
            result = pdf_text.extract_best_text_result(
                Path("article.pdf"),
                ocr_layout="single-column",
                try_ocr=False,
            )

        self.assertEqual(result.method, "pdf_unreadable")
        self.assertFalse(result.readable)
        self.assertEqual(result.text, "???")
        recognize.assert_not_called()

    def test_invalid_layout_fails_before_pdf_access(self) -> None:
        """Неизвестный макет должен отклоняться до проверки OCR и файлов."""

        path = Path("article.pdf")
        operations = (
            partial(pdf_text.extract_pages_from_pdf_ocr, path),
            partial(pdf_text.extract_selected_pages_from_pdf_ocr, path, [0]),
            partial(pdf_text.extract_text_from_pdf_ocr, path),
            partial(pdf_text.extract_best_text_result, path),
            partial(pdf_text.extract_best_text, path),
            partial(pdf_text.extract_text_from_pdf_checked, path),
            partial(
                pdf_text.pdf_to_text,
                "https://example.test/article.pdf",
                Path("cache"),
            ),
        )

        for operation in operations:
            with (
                self.subTest(operation=operation.func.__name__),
                patch.object(pdf_text, "_require_tesseract") as require_ocr,
                patch.object(pdf_text, "_fitz_open") as open_pdf,
                patch.object(pdf_text, "_cache_path") as cache_path,
                patch.object(pdf_text, "download_pdf") as download,
            ):
                with self.assertRaises(ValueError):
                    operation(ocr_layout="unknown-layout")

                require_ocr.assert_not_called()
                open_pdf.assert_not_called()
                cache_path.assert_not_called()
                download.assert_not_called()


class OcrLayoutSidecarTests(unittest.TestCase):
    """Проверки версий и независимого хранения разных макетов."""

    def test_layouts_have_distinct_versions_and_keep_ufn_version(self) -> None:
        """Новые версии не должны изменять идентификатор исторического УФН."""

        for ocr_layout, version in LAYOUT_VERSIONS.items():
            with self.subTest(ocr_layout=ocr_layout):
                self.assertEqual(
                    pdf_text.pdf_extraction_version(ocr_layout),
                    version,
                )

        self.assertEqual(len(set(LAYOUT_VERSIONS.values())), 3)

        with self.assertRaises(ValueError):
            pdf_text.pdf_extraction_version("unknown-layout")

    def test_sidecars_preserve_previous_layout_outputs(self) -> None:
        """Запись трёх макетов не должна перезаписывать предыдущие результаты."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            text_dir = Path(temporary_directory)
            pdf_path = Path("article.pdf")
            legacy_path = text_dir / "article_pdf_ocr_layout.txt"
            legacy_content = "Прежний русский OCR\n".encode("utf-8")
            legacy_path.write_bytes(legacy_content)
            snapshots = {legacy_path: legacy_content}

            for ocr_layout, version in LAYOUT_VERSIONS.items():
                output_path = pdf_text.save_text_sidecar(
                    pdf_path,
                    f"Текст для {ocr_layout}",
                    "pdf_ocr_layout",
                    text_dir=text_dir,
                    ocr_layout=ocr_layout,
                )

                self.assertEqual(
                    output_path,
                    text_dir / version / "article_pdf_ocr_layout.txt",
                )
                content = output_path.read_text(encoding="utf-8")
                if ocr_layout == "ufn":
                    self.assertIn(
                        "# layout: top=1 col (title/PACS/DOI), "
                        "bottom=left col then right col",
                        content,
                    )
                    self.assertNotIn("# ocr layout:", content)

                else:
                    self.assertIn(f"# ocr layout: {ocr_layout}", content)
                self.assertIn(f"# extraction version: {version}", content)
                self.assertIn("# ocr language: rus+eng", content)

                for previous_path, previous_content in snapshots.items():
                    self.assertEqual(previous_path.read_bytes(), previous_content)

                snapshots[output_path] = output_path.read_bytes()

    def test_embedded_sidecar_paths_do_not_depend_on_ocr_layout(self) -> None:
        """Пути встроенного текста не должны зависеть от неиспользованного OCR."""

        for ocr_layout in LAYOUT_VERSIONS:
            for method in ("pdf", "pdf_unreadable"):
                with self.subTest(ocr_layout=ocr_layout, method=method):
                    self.assertEqual(
                        pdf_text.text_sidecar_path(
                            Path("article.pdf"),
                            method,
                            text_dir=Path("texts"),
                            ocr_layout=ocr_layout,
                        ),
                        Path("texts") / f"article_{method}.txt",
                    )

    def test_invalid_sidecar_layout_does_not_create_files(self) -> None:
        """Некорректный макет должен отклоняться до создания каталога TXT."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            text_dir = Path(temporary_directory) / "texts"

            with self.assertRaises(ValueError):
                pdf_text.save_text_sidecar(
                    Path("article.pdf"),
                    "Текст",
                    "pdf_ocr_layout",
                    text_dir=text_dir,
                    ocr_layout="unknown-layout",
                )

            self.assertFalse(text_dir.exists())


if __name__ == "__main__":
    unittest.main()
