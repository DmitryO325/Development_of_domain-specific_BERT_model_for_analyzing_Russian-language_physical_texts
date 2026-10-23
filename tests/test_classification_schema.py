"""Проверки исходных кодов ГРНТИ и МРНТИ в схеме реестра научных работ."""

from __future__ import annotations

import json
import unittest

from pathlib import Path
from typing import Any

from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLASSIFICATION_FIELDS = ("grnti_codes_raw", "mrnti_codes_raw")


class ClassificationSchemaTests(unittest.TestCase):
    """Проверки совместимости и подтверждений для двух независимых рубрикаторов."""

    def setUp(self) -> None:
        """Загрузить каталог схем и синтетическую запись без новых полей."""

        self.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")
        template_path = PROJECT_ROOT / "manifests" / "templates" / "works.example.jsonl"
        self.record: dict[str, Any] = json.loads(
            template_path.read_text(encoding="utf-8").splitlines()[0]
        )

        for field_name in CLASSIFICATION_FIELDS:
            self.record.pop(field_name, None)

    def _signal(self, code: str = "29.19.29") -> dict[str, Any]:
        """Создать исходный код с синтетическим подтверждением происхождения."""

        return {
            "code": code,
            "source_url": "https://example.invalid/articles/work-demo-0001/metadata",
            "retrieved_at": "2026-10-10T12:00:00+03:00",
            "evidence_sha256": "a" * 64,
        }

    def test_legacy_record_without_classification_fields_is_valid(self) -> None:
        """Прежняя запись не требует добавления необязательных полей."""

        self.catalog.validate("works", self.record)

        for field_name in CLASSIFICATION_FIELDS:
            self.assertNotIn(field_name, self.record)

    def test_empty_classification_arrays_are_valid(self) -> None:
        """Оба поля можно явно сохранить пустыми без назначения кодов."""

        record = {**self.record, "grnti_codes_raw": [], "mrnti_codes_raw": []}

        self.catalog.validate("works", record)

    def test_fields_accept_signals_independently(self) -> None:
        """Наличие одного рубрикатора не требует другого и не создаёт его."""

        for field_name in CLASSIFICATION_FIELDS:
            with self.subTest(field_name=field_name):
                record = {**self.record, field_name: [self._signal()]}

                self.catalog.validate("works", record)

                other_field = next(
                    name for name in CLASSIFICATION_FIELDS if name != field_name
                )
                self.assertNotIn(other_field, record)

    def test_matching_codes_remain_in_separate_fields(self) -> None:
        """Одинаковая запись кода допустима в обоих независимых рубрикаторах."""

        record = {
            **self.record,
            "grnti_codes_raw": [self._signal()],
            "mrnti_codes_raw": [self._signal(), self._signal("29.15.19")],
        }

        self.catalog.validate("works", record)

        self.assertEqual(len(record["grnti_codes_raw"]), 1)
        self.assertEqual(len(record["mrnti_codes_raw"]), 2)

    def test_fields_require_arrays_of_signal_objects(self) -> None:
        """Пустое значение, строка, объект вместо массива и голый код запрещены."""

        invalid_values = (None, "29.19.29", 29, {}, ["29.19.29"], [None], [29])

        for field_name in CLASSIFICATION_FIELDS:
            for value in invalid_values:
                with self.subTest(field_name=field_name, value=value):
                    record = {**self.record, field_name: value}

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)

    def test_signal_requires_each_provenance_field(self) -> None:
        """Без кода, источника, времени или хеша подтверждение неполно."""

        required_fields = ("code", "source_url", "retrieved_at", "evidence_sha256")

        for field_name in CLASSIFICATION_FIELDS:
            for missing_field in required_fields:
                with self.subTest(field_name=field_name, missing_field=missing_field):
                    signal = self._signal()
                    del signal[missing_field]
                    record = {**self.record, field_name: [signal]}

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)

    def test_signal_rejects_wrong_types_and_empty_code(self) -> None:
        """Реквизиты подтверждения должны быть строками, а код непустым."""

        invalid_fields = (
            ("code", ""),
            ("code", 29),
            ("code", None),
            ("source_url", []),
            ("source_url", None),
            ("retrieved_at", 2026),
            ("evidence_sha256", None),
        )

        for field_name in CLASSIFICATION_FIELDS:
            for invalid_field, value in invalid_fields:
                with self.subTest(
                    field_name=field_name, invalid_field=invalid_field, value=value
                ):
                    signal = {**self._signal(), invalid_field: value}
                    record = {**self.record, field_name: [signal]}

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)

    def test_signal_rejects_invalid_dates_and_hashes(self) -> None:
        """Дата должна существовать и содержать пояс, а хеш соответствовать SHA-256."""

        invalid_fields = (
            ("retrieved_at", "2026-02-30T12:00:00Z"),
            ("retrieved_at", "2026-10-10T12:00:00"),
            ("retrieved_at", "2026-10-10"),
            ("evidence_sha256", ""),
            ("evidence_sha256", "a" * 63),
            ("evidence_sha256", "a" * 65),
            ("evidence_sha256", "g" * 64),
        )

        for field_name in CLASSIFICATION_FIELDS:
            for invalid_field, value in invalid_fields:
                with self.subTest(
                    field_name=field_name, invalid_field=invalid_field, value=value
                ):
                    signal = {**self._signal(), invalid_field: value}
                    record = {**self.record, field_name: [signal]}

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("works", record)

    def test_signal_rejects_unknown_properties(self) -> None:
        """В исходный код нельзя незаметно добавить признак основной рубрики."""

        for field_name in CLASSIFICATION_FIELDS:
            with self.subTest(field_name=field_name):
                signal = {**self._signal(), "is_primary": True}
                record = {**self.record, field_name: [signal]}

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", record)


if __name__ == "__main__":
    unittest.main()
