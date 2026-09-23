"""Проверки безопасной подготовки инженерного запуска OCR QA."""

from __future__ import annotations

import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from src.collect.pdf_text import PdfPageRender, PdfPageText, _validated_page_indices
from src.corpus.ocr_qa_preparation import (
    _PreparedPage,
    _first_pass_sample_ids,
    _publish_new_file,
    _serialized_page_text,
    _variant_extractor,
    _variants,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class OcrQaPreparationTests(unittest.TestCase):
    """Проверки плана, вспомогательных файлов и защиты от перезаписи."""

    def test_real_page_plan_matches_schema(self) -> None:
        """Фактический план калибровки должен проходить свою схему."""

        schema_path = (
            PROJECT_ROOT
            / "manifests"
            / "schemas"
            / "ocr_qa_page_plan.schema.json"
        )
        plan_path = (
            PROJECT_ROOT
            / "manifests"
            / "plans"
            / "ufn_ocr_calibration_v1.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        )

        self.assertEqual(list(validator.iter_errors(plan)), [])
        self.assertEqual(len(plan["pages"]), 29)
        self.assertEqual(
            sum(page.get("first_pass") is True for page in plan["pages"]),
            5,
        )

    def test_selected_page_indices_keep_order(self) -> None:
        """Выбранные индексы должны сохранять объявленный порядок."""

        self.assertEqual(
            _validated_page_indices((3, 1, 4), page_count=5),
            (3, 1, 4),
        )

    def test_selected_page_indices_reject_invalid_values(self) -> None:
        """Пустые, повторные и логические индексы должны отклоняться."""

        with self.assertRaises(ValueError):
            _validated_page_indices((), page_count=5)

        with self.assertRaises(ValueError):
            _validated_page_indices((1, 1), page_count=5)

        with self.assertRaises(ValueError):
            _validated_page_indices((5,), page_count=5)

        with self.assertRaises(TypeError):
            _validated_page_indices((True,), page_count=5)

    def test_pdf_page_render_rejects_non_png_bytes(self) -> None:
        """Карточка изображения не должна принимать произвольные байты."""

        with self.assertRaises(ValueError):
            PdfPageRender(
                page_index=0,
                page_number=1,
                png=b"not-png",
                width=100,
                height=200,
            )

    def test_first_pass_uses_plan_flags(self) -> None:
        """Первый проход должен определяться планом, а не именами UFN."""

        pages = [
            self._prepared_page("page_alpha", first_pass=False),
            self._prepared_page("page_beta", first_pass=True),
            self._prepared_page("page_gamma", first_pass=True),
        ]

        self.assertEqual(
            _first_pass_sample_ids(pages),
            ("page_beta", "page_gamma"),
        )

    def test_first_pass_has_generic_fallback(self) -> None:
        """План без отметок должен получать до пяти первых страниц."""

        pages = [
            self._prepared_page(f"page_{page_index}", first_pass=False)
            for page_index in range(7)
        ]

        self.assertEqual(
            _first_pass_sample_ids(pages),
            tuple(f"page_{page_index}" for page_index in range(5)),
        )

    def test_page_serialization_normalizes_unicode_and_line_endings(self) -> None:
        """Сериализация должна давать NFC и только окончания строк LF."""

        decomposed = " е\u0308ж\r\nтест\r "

        self.assertEqual(_serialized_page_text(decomposed), "ёж\nтест")

    def test_calibration_variants_keep_explicit_languages(self) -> None:
        """Новый рабочий язык не должен объединять русскую и смешанную абляции."""

        with (
            patch(
                "src.corpus.ocr_qa_preparation.importlib.metadata.version",
                return_value="test-version",
            ),
            patch(
                "src.corpus.ocr_qa_preparation._tesseract_version",
                return_value="test-tesseract",
            ),
            patch(
                "src.corpus.ocr_qa_preparation._tessdata_versions",
                return_value={"rus": "test-rus", "eng": "test-eng"},
            ),
            patch(
                "src.corpus.ocr_qa_preparation._implementation_files",
                return_value=[],
            ),
        ):
            variants = _variants(
                project_root=Path("/project"),
                snapshot_root=Path("/project/snapshot"),
            )

        self.assertEqual(
            [(variant.variant_id, variant.language) for variant in variants],
            [("variant_a", None), ("variant_b", "rus"), ("variant_c", "rus+eng")],
        )

        for variant in variants[1:]:
            with self.subTest(variant_id=variant.variant_id):
                pages = (PdfPageText(0, 1, "Текст страницы"),)

                with patch(
                    "src.corpus.ocr_qa_preparation.extract_selected_pages_from_pdf_ocr",
                    return_value=pages,
                ) as extractor:
                    result = _variant_extractor(variant)(Path("article.pdf"), (0,))

                extractor.assert_called_once_with(
                    Path("article.pdf"),
                    (0,),
                    lang=variant.language,
                    dpi=200,
                )
                self.assertEqual(variant.config["language"], variant.language)
                self.assertEqual(result, pages)

    def test_publish_new_file_preserves_existing_target(self) -> None:
        """Повторная публикация не должна удалить или заменить прежний файл."""

        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source_path = root / "source.json"
            output_path = root / "output.json"
            source_path.write_bytes(b"new")
            output_path.write_bytes(b"old")

            with self.assertRaises(FileExistsError):
                _publish_new_file(source_path, output_path)

            self.assertEqual(output_path.read_bytes(), b"old")

    @staticmethod
    def _prepared_page(
        page_sample_id: str,
        *,
        first_pass: bool,
    ) -> _PreparedPage:
        """Собрать минимальную страницу для проверки очереди."""

        return _PreparedPage(
            plan={"first_pass": first_pass},
            artifact={},
            page_index=0,
            page_number=1,
            page_sample_id=page_sample_id,
        )


if __name__ == "__main__":
    unittest.main()
