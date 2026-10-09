"""Проверки календарных дат и часовых поясов временных меток реестров."""

from __future__ import annotations

import json
import unittest

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from jsonschema import FormatChecker

from src.corpus.schema_validation import SchemaCatalog, SchemaValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SchemaDatetimeTests(unittest.TestCase):
    """Проверки date-time независимо от необязательных пакетов jsonschema."""

    def setUp(self) -> None:
        """Подготовить каталог схем и корректный пример работы."""

        self.catalog = SchemaCatalog(PROJECT_ROOT / "manifests" / "schemas")
        template_path = (
            PROJECT_ROOT / "manifests" / "templates" / "works.example.jsonl"
        )
        self.record: dict[str, Any] = json.loads(
            template_path.read_text(encoding="utf-8").splitlines()[0]
        )

    def test_valid_calendar_dates_and_offsets_are_accepted(self) -> None:
        """Корректные даты, доли секунды и оба знака смещения допустимы."""

        timestamps = (
            "2024-02-29T00:00:00Z",
            "2026-09-29T13:14:15+00:00",
            "2026-09-29T13:14:15+03:00",
            "2026-09-29T13:14:15-05:30",
            "2026-09-29T13:14:15.1Z",
            "2026-09-29T13:14:15.123456+03:00",
            "2026-09-29T13:14:15.123456789-05:30",
            datetime.now(timezone.utc).isoformat(),
        )

        for timestamp in timestamps:
            with self.subTest(timestamp=timestamp):
                self.record["created_at"] = timestamp
                self.catalog.validate("works", self.record)

    def test_invalid_calendar_dates_are_rejected(self) -> None:
        """Синтаксически полная метка должна содержать существующую дату."""

        timestamps = (
            "2026-02-29T12:00:00Z",
            "2026-02-30T12:00:00+03:00",
            "2024-04-31T12:00:00-05:00",
            "0000-01-01T12:00:00Z",
            "2026-00-01T12:00:00Z",
            "2026-13-01T12:00:00Z",
            "2026-01-00T12:00:00Z",
        )

        self._assert_invalid_timestamps(timestamps)

    def test_naive_and_incomplete_timestamps_are_rejected(self) -> None:
        """Временная метка без секунд, разделителя T или пояса недопустима."""

        timestamps = (
            "2026-09-29",
            "2026-09-29T12:00:00",
            "2026-09-29T12:00Z",
            "2026-09-29 12:00:00Z",
            "20260929T120000Z",
            "2026-09-29T12:00:00.123456",
            "2026-09-29T12:00:00,123Z",
            "2026-09-29T12:00:00.Z",
            "2026-09-29T12:00:00Z\n",
            " 2026-09-29T12:00:00Z",
            "",
        )

        self._assert_invalid_timestamps(timestamps)

    def test_malformed_offsets_and_times_are_rejected(self) -> None:
        """Некорректное время и смещения обоих знаков отклоняются."""

        timestamps = (
            "2026-09-29T24:00:00Z",
            "2026-09-29T12:60:00Z",
            "2026-09-29T12:00:60Z",
            "2026-09-29T12:00:00+24:00",
            "2026-09-29T12:00:00-24:00",
            "2026-09-29T12:00:00+03:60",
            "2026-09-29T12:00:00-03:60",
            "2026-09-29T12:00:00+0300",
            "2026-09-29T12:00:00-0300",
            "2026-09-29T12:00:00+03",
            "2026-09-29T12:00:00-03",
            "2026-09-29T12:00:00+03:00:00",
            "2026-09-29T12:00:00Z+03:00",
            "2026-09-29t12:00:00z",
        )

        self._assert_invalid_timestamps(timestamps)

    def test_other_format_checkers_remain_available(self) -> None:
        """Локальная проверка времени не должна заменять остальные форматы."""

        original_checkers = dict(FormatChecker().checkers)
        format_checker = getattr(self.catalog.validator("works"), "format_checker", None)

        if not isinstance(format_checker, FormatChecker):
            self.fail("Валидатор не получил проверку форматов")

        for name, checker in original_checkers.items():
            if name == "date-time":
                continue

            with self.subTest(format=name):
                self.assertEqual(format_checker.checkers[name], checker)

        self.record["published_at"] = "2026-02-30"

        with self.assertRaises(SchemaValidationError):
            self.catalog.validate("works", self.record)

    def test_custom_checker_does_not_change_global_registration(self) -> None:
        """Каталог должен настраивать свой экземпляр, а не весь jsonschema."""

        original_checkers = dict(FormatChecker().checkers)

        self.catalog.validator("works")

        self.assertEqual(FormatChecker().checkers, original_checkers)

    def test_datetime_is_checked_without_optional_format_dependency(self) -> None:
        """Без внешнего RFC 3339-пакета невалидная дата всё равно отклоняется."""

        checkers_without_datetime = {
            name: checker
            for name, checker in FormatChecker.checkers.items()
            if name != "date-time"
        }
        self.record["created_at"] = "2026-02-30T12:00:00Z"

        with (
            mock.patch.dict(
                FormatChecker.checkers,
                checkers_without_datetime,
                clear=True,
            ),
            self.assertRaises(SchemaValidationError),
        ):
            self.catalog.validate("works", self.record)

    def _assert_invalid_timestamps(self, timestamps: tuple[str, ...]) -> None:
        """Проверить отклонение каждой ошибочной метки без побочных записей."""

        for timestamp in timestamps:
            with self.subTest(timestamp=timestamp):
                self.record["created_at"] = timestamp

                with self.assertRaises(SchemaValidationError):
                    self.catalog.validate("works", self.record)
