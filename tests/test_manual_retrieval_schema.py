"""Проверки схемы событий ручного получения файлов."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NON_MANUAL_METHODS = ("api", "crawler", "platform_export", "other")


class ManualRetrievalSchemaTests(unittest.TestCase):
    """Проверить различия ручного и сетевого успешного получения."""

    catalog: SchemaCatalog

    @classmethod
    def setUpClass(cls) -> None:
        """Подготовить каталог схем для набора тестов."""

        cls.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")

    @staticmethod
    def _example_records() -> list[dict[str, Any]]:
        """Загрузить все прежние примеры событий получения."""

        path = (
            PROJECT_ROOT
            / "manifests"
            / "templates"
            / "retrieval_events.example.jsonl"
        )

        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def _succeeded_record(self) -> dict[str, Any]:
        """Вернуть копию прежнего примера успешного получения."""

        return self._example_records()[0]

    def test_old_examples_remain_valid(self) -> None:
        """Прежние примеры должны проходить обновлённую схему."""

        for record in self._example_records():
            self.catalog.validate("retrieval_events", record)

    def test_manual_success_accepts_unknown_http_details(self) -> None:
        """Ручное получение допускает неизвестные финальный URL и HTTP-статус."""

        record = self._succeeded_record()
        record["final_url"] = None
        record["http_status"] = None
        record["response_headers"] = {}
        record["acquisition_agent"] = "researcher_01"

        self.catalog.validate("retrieval_events", record)

    def test_acquisition_agent_accepts_string_or_null(self) -> None:
        """Необязательный исполнитель получения принимает строку или null."""

        for acquisition_agent in ("researcher_01", None):
            with self.subTest(acquisition_agent=acquisition_agent):
                record = self._succeeded_record()
                record["acquisition_agent"] = acquisition_agent

                self.catalog.validate("retrieval_events", record)

    def test_manual_success_requires_saved_response(self) -> None:
        """Успешное ручное получение требует путь, хеш и размер файла."""

        for field_name in ("response_path", "response_sha256", "response_bytes"):
            with self.subTest(field_name=field_name):
                record = self._succeeded_record()
                record["final_url"] = None
                record["http_status"] = None
                record[field_name] = None

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("retrieval_events", record)

    def test_non_manual_success_requires_http_details(self) -> None:
        """Другие успешные способы требуют финальный URL и HTTP-статус."""

        for acquisition_method in NON_MANUAL_METHODS:
            valid_record = self._succeeded_record()
            valid_record["acquisition_method"] = acquisition_method
            self.catalog.validate("retrieval_events", valid_record)

            for field_name in ("final_url", "http_status"):
                with self.subTest(
                    acquisition_method=acquisition_method,
                    field_name=field_name,
                ):
                    record = self._succeeded_record()
                    record["acquisition_method"] = acquisition_method
                    record[field_name] = None

                    with self.assertRaises(SchemaValidationError):
                        self.catalog.validate("retrieval_events", record)

    def test_success_rejects_unsuccessful_http_status(self) -> None:
        """Известный HTTP-статус успешного события должен быть от 200 до 399."""

        for acquisition_method in ("manual_download", *NON_MANUAL_METHODS):
            with self.subTest(acquisition_method=acquisition_method):
                record = self._succeeded_record()
                record["acquisition_method"] = acquisition_method
                record["http_status"] = 404

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("retrieval_events", record)


if __name__ == "__main__":
    unittest.main()
