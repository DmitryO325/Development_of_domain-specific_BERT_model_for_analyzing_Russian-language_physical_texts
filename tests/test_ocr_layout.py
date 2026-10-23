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

    def test_explicit_header_overrides_automatic_detection(self) -> None:
        """Ручная граница делит первую страницу вместо ошибочного детектора."""

        page = _fake_document().load_page(0)

        with (
            patch.object(pdf_text, "_detect_two_column_split_y") as detector,
            patch.object(
                pdf_text, "_ocr_page_full", return_value="Шапка\n"
            ) as header,
            patch.object(
                pdf_text, "_ocr_columns_in_rect", return_value="Колонки\n"
            ) as columns,
        ):
            result = pdf_text._ocr_page_layout(
                page, first_page_split_y=330.5, dpi=250, lang="rus+eng"
            )

        detector.assert_not_called()
        header.assert_called_once_with(
            page, clip=fitz.Rect(0, 0, 600, 330.5), dpi=250, lang="rus+eng"
        )
        columns.assert_called_once_with(
            page, fitz.Rect(0, 330.5, 600, 800), dpi=250, lang="rus+eng",
            keep_short_regions=True,
        )
        self.assertEqual(result, "Шапка\n\nКолонки")

    def test_explicit_header_preserves_short_column_region(self) -> None:
        """Даже последние 10 пунктов после ручной границы распознаются."""

        page = _fake_document().load_page(0)

        with (
            patch.object(pdf_text, "_detect_two_column_split_y") as detector,
            patch.object(
                pdf_text, "_ocr_page_full", return_value="Шапка"
            ) as header,
            patch.object(
                pdf_text, "_ocr_page_region", side_effect=["Левая", "Правая"]
            ) as region,
        ):
            result = pdf_text._ocr_page_layout(page, first_page_split_y=790)

        detector.assert_not_called()
        header.assert_called_once_with(
            page, clip=fitz.Rect(0, 0, 600, 790), dpi=200, lang="rus+eng"
        )
        self.assertEqual(region.call_count, 2)
        self.assertEqual(
            region.call_args_list[0].args[1], fitz.Rect(0, 790, 293, 800)
        )
        self.assertEqual(
            region.call_args_list[1].args[1], fitz.Rect(307, 790, 600, 800)
        )
        self.assertEqual(result, "Шапка\n\nЛевая\n\nПравая")

    def test_explicit_header_does_not_split_later_pages(self) -> None:
        """Граница относится к первой физической странице, не к каждой."""

        page = _fake_document().load_page(0)
        page.rect = fitz.Rect(0, 0, 600, 300)

        with (
            patch.object(
                pdf_text, "_detect_two_column_split_y", return_value=None
            ) as detector,
            patch.object(pdf_text, "_ocr_page_full") as header,
            patch.object(
                pdf_text, "_ocr_columns_in_rect", return_value="Колонки"
            ) as columns,
        ):
            result = pdf_text._ocr_page_layout(
                page, page_index=1, first_page_split_y=330.5
            )

        detector.assert_called_once_with(page, page_index=1)
        header.assert_not_called()
        columns.assert_called_once_with(
            page, page.rect, dpi=200, lang="rus+eng"
        )
        self.assertEqual(result, "Колонки")

    def test_explicit_header_rejects_page_boundary_and_beyond(self) -> None:
        """Нельзя молча превратить ручные колонки в целую страницу."""

        page = _fake_document().load_page(0)

        for boundary in (800, 801):
            with (
                self.subTest(boundary=boundary),
                patch.object(pdf_text, "_ocr_page_full") as header,
                patch.object(pdf_text, "_ocr_columns_in_rect") as columns,
            ):
                with self.assertRaisesRegex(ValueError, "меньше высоты"):
                    pdf_text._ocr_page_layout(
                        page, first_page_split_y=boundary
                    )

                header.assert_not_called()
                columns.assert_not_called()


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

    def test_invalid_header_fails_before_pdf_access(self) -> None:
        """Неподходящие макеты и неконечные границы не запускают обработку."""

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
        settings = (
            ("ufn", 0), ("ufn", -1), ("ufn", float("inf")),
            ("ufn", float("nan")), ("ufn", True),
            ("single-column", 330), ("two-column", 330),
        )

        for operation in operations:
            for layout, boundary in settings:
                with (
                    self.subTest(
                        operation=operation.func.__name__,
                        layout=layout,
                        boundary=boundary,
                    ),
                    patch.object(pdf_text, "_require_tesseract") as require_ocr,
                    patch.object(pdf_text, "_fitz_open") as open_pdf,
                    patch.object(pdf_text, "extract_pages_from_pdf") as extract,
                    patch.object(pdf_text, "_cache_path") as cache_path,
                    patch.object(pdf_text, "download_pdf") as download,
                ):
                    with self.assertRaises(ValueError):
                        operation(ocr_layout=layout, first_page_split_y=boundary)

                    require_ocr.assert_not_called()
                    open_pdf.assert_not_called()
                    extract.assert_not_called()
                    cache_path.assert_not_called()
                    download.assert_not_called()

    def test_explicit_header_reaches_all_page_extractors(self) -> None:
        """Полный и выборочный проход сохраняют физические индексы и границу."""

        for selected in (False, True):
            document = _fake_document(3)
            indices = [2, 0] if selected else [0, 1, 2]

            with (
                self.subTest(selected=selected),
                patch.object(pdf_text, "_require_tesseract"),
                patch.object(pdf_text, "_fitz_open", return_value=document),
                patch.object(
                    pdf_text, "_ocr_page_layout", return_value="Страница"
                ) as recognize,
            ):
                if selected:
                    pdf_text.extract_selected_pages_from_pdf_ocr(
                        Path("article.pdf"), indices, first_page_split_y=330
                    )

                else:
                    pdf_text.extract_pages_from_pdf_ocr(
                        Path("article.pdf"), first_page_split_y=330
                    )

            self.assertEqual(
                [call.kwargs["page_index"] for call in recognize.call_args_list],
                indices,
            )
            self.assertTrue(all(
                call.kwargs["first_page_split_y"] == 330
                for call in recognize.call_args_list
            ))
            document.close.assert_called_once_with()

    def test_selected_pages_validate_actual_first_page_height(self) -> None:
        """Выбор страницы 2 не должен переносить на неё проверку высоты шапки."""

        document = _fake_document(2)
        document.load_page(0).rect = fitz.Rect(0, 0, 600, 300)

        with (
            patch.object(pdf_text, "_require_tesseract"),
            patch.object(pdf_text, "_fitz_open", return_value=document),
            patch.object(pdf_text, "_ocr_page_layout") as recognize,
        ):
            with self.assertRaisesRegex(ValueError, "меньше высоты"):
                pdf_text.extract_selected_pages_from_pdf_ocr(
                    Path("article.pdf"), [1], first_page_split_y=330
                )

        recognize.assert_not_called()
        document.close.assert_called_once_with()

    def test_best_text_wrappers_forward_explicit_header(self) -> None:
        """Обёртки и общий TXT должны сохранять выбранную вручную границу."""

        for extractor in (
            pdf_text.extract_text_from_pdf_ocr,
            pdf_text.extract_best_text_result,
            pdf_text.extract_best_text,
            pdf_text.extract_text_from_pdf_checked,
        ):
            with (
                self.subTest(extractor=extractor.__name__),
                patch.object(
                    pdf_text, "extract_pages_from_pdf",
                    return_value=(PdfPageText(0, 1, "???"),),
                ),
                patch.object(
                    pdf_text, "extract_pages_from_pdf_ocr",
                    return_value=(PdfPageText(0, 1, READABLE_TEXT),),
                ) as recognize,
            ):
                extractor(Path("article.pdf"), first_page_split_y=330)

            self.assertEqual(recognize.call_args.kwargs["first_page_split_y"], 330)

    def test_invalid_header_is_not_hidden_by_ocr_fallback(self) -> None:
        """Ошибка высоты должна прерывать обработку, а не выдавать PDF-слой."""

        document = _fake_document(1)

        with (
            patch.object(
                pdf_text, "extract_pages_from_pdf",
                return_value=(PdfPageText(0, 1, "???"),),
            ),
            patch.object(pdf_text, "_require_tesseract"),
            patch.object(pdf_text, "_fitz_open", return_value=document),
            patch.object(pdf_text, "save_text_sidecar") as save,
        ):
            with self.assertRaisesRegex(ValueError, "меньше высоты"):
                pdf_text.extract_best_text_result(
                    Path("article.pdf"), text_dir=Path("texts"),
                    first_page_split_y=800,
                )

        save.assert_not_called()
        document.close.assert_called_once_with()

    def test_invalid_header_does_not_redownload_cached_pdf(self) -> None:
        """Неверная граница шапки не означает повреждение сохранённого PDF."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_dir = Path(temporary_directory)
            pdf_path = cache_dir / "article.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n%%EOF")
            document = _fake_document(1)

            with (
                patch.object(pdf_text, "download_pdf") as download,
                patch.object(
                    pdf_text, "extract_pages_from_pdf",
                    return_value=(PdfPageText(0, 1, "???"),),
                ),
                patch.object(pdf_text, "_require_tesseract"),
                patch.object(pdf_text, "_fitz_open", return_value=document),
            ):
                with self.assertRaisesRegex(ValueError, "меньше высоты"):
                    pdf_text.pdf_to_text(
                        "https://example.test/article.pdf", cache_dir,
                        first_page_split_y=800,
                    )

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

    def test_explicit_header_versions_are_canonical_and_distinct(self) -> None:
        """Число 330 и 330.0 задаёт один вариант, дробные границы — разные."""

        expected = "pdf-text-rus-eng-v2-header-330pt"
        self.assertEqual(
            pdf_text.pdf_extraction_version(first_page_split_y=330), expected
        )
        self.assertEqual(
            pdf_text.pdf_extraction_version(first_page_split_y=330.0), expected
        )
        self.assertEqual(
            pdf_text.pdf_extraction_version(first_page_split_y=330.5),
            "pdf-text-rus-eng-v2-header-330.5pt",
        )
        self.assertNotEqual(
            pdf_text.pdf_extraction_version(first_page_split_y=330.5),
            pdf_text.pdf_extraction_version(first_page_split_y=330.500000000001),
        )

    def test_explicit_header_sidecar_preserves_automatic_result(self) -> None:
        """Ручной вариант получает свой путь и записывает точную границу."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            text_dir = Path(temporary_directory)
            pdf_path = Path("article.pdf")
            automatic_path = pdf_text.save_text_sidecar(
                pdf_path, "Автоматический макет", "pdf_ocr_layout",
                text_dir=text_dir,
            )
            automatic_bytes = automatic_path.read_bytes()

            with (
                patch.object(
                    pdf_text, "extract_pages_from_pdf",
                    return_value=(PdfPageText(0, 1, "???"),),
                ),
                patch.object(
                    pdf_text, "extract_pages_from_pdf_ocr",
                    return_value=(PdfPageText(0, 1, READABLE_TEXT),),
                ),
            ):
                pdf_text.extract_best_text_result(
                    pdf_path, text_dir=text_dir, first_page_split_y=330.5
                )

            explicit_path = pdf_text.text_sidecar_path(
                pdf_path, "pdf_ocr_layout", text_dir=text_dir,
                first_page_split_y=330.5,
            )
            self.assertEqual(automatic_path.read_bytes(), automatic_bytes)
            self.assertNotEqual(automatic_path, explicit_path)
            saved = explicit_path.read_text(encoding="utf-8")
            self.assertIn("# first page header boundary: 330.5 pt from top", saved)
            self.assertIn("pdf-text-rus-eng-v2-header-330.5pt", saved)


if __name__ == "__main__":
    unittest.main()
