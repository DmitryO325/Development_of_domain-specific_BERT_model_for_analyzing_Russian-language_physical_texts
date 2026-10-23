"""Офлайн-проверки происхождения и безопасной публикации очищенной очереди H2."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.corpus.h2_candidate_export import export_candidate_queue
from src.corpus.h2_candidates import build_candidate_queue
from src.preprocess.h2_cleaning_io import (
    CODE_NAMES,
    CSV_COLUMNS,
    PreparedH2Cleaning,
    prepare_h2_cleaning,
    publish_h2_cleaning,
)
from tests.test_h2_candidates import _item, _snapshot


class H2CleaningIOTests(unittest.TestCase):
    """Только синтетические исходники, без сети, реестров и настоящей очереди."""

    def setUp(self) -> None:
        """Подготовить независимый проект с экспортированной очередью из трёх карточек."""

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.queue = self.root / "manifests/results/source"
        self.output = self.root / "manifests/results/cleaned-v1"
        rows, summary = build_candidate_queue(_snapshot(
            _item(1, **{"dc.title": ["Русское  название"], "dc.description.abstract": ["В работе  изучены ядра."]}),
            _item(2, **{"dc.description.abstract": []}),
            _item(3, **{"dc.title": ["Первый  вариант", "Другой  вариант"]}),
        ))
        export_candidate_queue(
            self.root, self.queue, rows, summary,
            snapshot_path=Path("manifests/results/input-snapshot/analysis.json"),
        )

    def prepare(self) -> PreparedH2Cleaning:
        """Проверить текущий синтетический пакет без записи результата."""

        return prepare_h2_cleaning(self.root, self.queue, self.output)

    def records(self, prepared: PreparedH2Cleaning) -> list[dict[str, Any]]:
        """Прочитать производные записи из подготовленных в памяти байтов."""

        return [json.loads(line) for line in prepared.files["cleaned_candidates.jsonl"].splitlines()]

    def rewrite_candidates(self, payload: bytes) -> None:
        """Обновить синтетический JSONL и его контрольные данные для проверки содержания."""

        path = self.queue / "candidates.jsonl"
        path.write_bytes(payload)
        report_path = self.queue / "summary.json"
        report = json.loads(report_path.read_bytes())
        report["export"]["files"]["candidates.jsonl"] = {
            "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    def original_rows(self) -> list[dict[str, Any]]:
        """Прочитать текущие исходные карточки только для изменений синтетического примера."""

        return [json.loads(line) for line in (self.queue / "candidates.jsonl").read_bytes().splitlines()]

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        """Сохранить тестовые карточки, оставив исходное ожидаемое число записей."""

        payload = b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8") for row in rows)
        self.rewrite_candidates(payload)

    def test_preparation_does_not_write_and_preserves_every_row(self) -> None:
        """Порядок, пустые аннотации и исходные байты остаются неизменными."""

        before = {path.name: path.read_bytes() for path in self.queue.iterdir()}
        prepared = self.prepare()
        rows = self.records(prepared)
        original = self.original_rows()

        self.assertFalse(self.output.exists())
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.queue.iterdir()})
        self.assertEqual([row["item_uuid"] for row in rows], [row["item_uuid"] for row in original])
        self.assertEqual([row["position"] for row in rows], [1, 2, 3])
        self.assertEqual(prepared.report["counts"]["candidates"], 3)
        self.assertEqual(prepared.report["counts"]["missing_abstract"], 1)
        self.assertEqual(prepared.report["status"], "candidate_not_approved")
        self.assertFalse(prepared.report["training_ready"])

        for row in rows:
            self.assertIsNone(row["label"])
            self.assertIsNone(row["split"])
            self.assertFalse(row["training_ready"])

    def test_line_provenance_includes_exact_lf(self) -> None:
        """Контрольная сумма строки включает LF, а не только разобранный объект JSON."""

        prepared = self.prepare()
        lines = (self.queue / "candidates.jsonl").read_bytes().splitlines(keepends=True)

        for row, line in zip(self.records(prepared), lines, strict=True):
            self.assertEqual(row["source_candidate_line_sha256"], hashlib.sha256(line).hexdigest())
            self.assertNotEqual(row["source_candidate_line_sha256"], hashlib.sha256(line.rstrip(b"\n")).hexdigest())

    def test_changes_reproduce_clean_text_and_flat_log(self) -> None:
        """Замены используют исходные координаты и представлены также в плоском журнале."""

        prepared = self.prepare()
        rows = self.records(prepared)
        journal = [json.loads(line) for line in prepared.files["changes.jsonl"].splitlines()]
        count = 0

        for row in rows:
            for field in ("title", "abstract"):
                original = row[f"{field}_original"]
                restored = original
                changes = [change for change in row["changes"] if change["field"] == field]

                for change in reversed(changes):
                    self.assertEqual(original[change["start"]:change["end"]], change["before"])
                    restored = restored[:change["start"]] + change["after"] + restored[change["end"]:]
                    self.assertIn({
                        "position": row["position"], "candidate_id": row["candidate_id"],
                        "item_uuid": row["item_uuid"], **change,
                    }, journal)
                    count += 1

                self.assertEqual(restored, row[f"{field}_clean"])

        self.assertGreater(count, 0)
        self.assertEqual(count, len(journal))
        self.assertEqual(count, prepared.report["counts"]["changes"])
        self.assertEqual(count, sum(prepared.report["change_counts"].values()))

    def test_multiple_variants_are_never_collapsed_or_selected(self) -> None:
        """Поле с несколькими вариантами сохраняется полностью вместе с разделителем."""

        prepared = self.prepare()
        row = next(row for row in self.records(prepared) if "multiple_title_variants" in row["source_review_flags"])

        self.assertEqual(row["title_original"], "Первый  вариант\n\nДругой  вариант")
        self.assertEqual(row["title_clean"], row["title_original"])
        self.assertFalse(any(change["field"] == "title" for change in row["changes"]))
        self.assertIn("multiple_title_variants", [issue["code"] for issue in row["issues"]])

    def test_existing_human_csv_notes_are_fingerprinted_not_overwritten(self) -> None:
        """Изменённый человеком CSV допустим и фиксируется по фактическим байтам."""

        path = self.queue / "review.csv"
        notes = path.read_bytes() + "\nЗаметка человека;проверить тему\n".encode()
        path.write_bytes(notes)
        prepared = self.prepare()
        relative = path.relative_to(self.root).as_posix()

        self.assertEqual(prepared.source_hashes[relative], hashlib.sha256(notes).hexdigest())
        self.assertTrue(publish_h2_cleaning(prepared))
        self.assertEqual(path.read_bytes(), notes)
        review = csv.DictReader(io.StringIO(prepared.files["review.csv"].decode("utf-8-sig")), delimiter=";")

        for row in review:
            self.assertEqual(row["Решение при просмотре"], "")
            self.assertEqual(row["Комментарий"], "")

    def test_csv_bom_headers_russian_reasons_and_formula_protection(self) -> None:
        """Опасная формула защищена только в CSV; исходник и очищенный JSONL неизменны."""

        rows = self.original_rows()
        rows[0]["title"] = "\u200b\t=1+1"
        rows[0]["review_flags"].append("possible_duplicate_title_year")
        self.set_rows(rows)
        prepared = self.prepare()
        data = prepared.files["review.csv"]
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")), delimiter=";")
        first = next(reader)

        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(tuple(reader.fieldnames or []), CSV_COLUMNS)
        self.assertTrue(first["Название — оригинал"].startswith("'"))
        self.assertEqual(self.records(prepared)[0]["title_original"], "\u200b\t=1+1")
        self.assertIn("Возможный дубль", first["Причины проверки"])

    def test_output_provenance_and_code_hashes_are_exact(self) -> None:
        """Отчёт содержит точные размеры и SHA-256 источников, кода и результатов."""

        for name in CODE_NAMES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# synthetic code\n")

        prepared = self.prepare()

        for name, expected in prepared.report["source_files"].items():
            data = (self.root / name).read_bytes()
            self.assertEqual(expected, {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})

        self.assertEqual(set(prepared.report["code_files"]), set(CODE_NAMES))

        for name, expected in prepared.report["export"]["files"].items():
            data = prepared.files[name]
            self.assertEqual(expected, {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})

        self.assertEqual(json.loads(prepared.files["summary.json"]), prepared.report)

    def test_exact_repeat_does_not_write_any_file(self) -> None:
        """Повтор идентичен по содержимому и времени изменения всех файлов."""

        prepared = self.prepare()
        self.assertTrue(publish_h2_cleaning(prepared))
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.output.iterdir()}
        self.assertEqual(prepared.files, self.prepare().files)
        self.assertFalse(publish_h2_cleaning(self.prepare()))
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.output.iterdir()})

    def test_dry_run_has_no_filesystem_effects(self) -> None:
        """Проверочный запуск не создаёт выходные и временные каталоги."""

        self.output = self.root / "manifests/results/not-created/nested"
        self.assertFalse(publish_h2_cleaning(self.prepare(), dry_run=True))
        self.assertFalse(self.output.parent.exists())

    def test_changed_source_before_publish_is_rejected(self) -> None:
        """Любой из трёх исходных файлов проверяется повторно перед записью."""

        for name in ("candidates.jsonl", "summary.json", "review.csv"):
            with self.subTest(name=name):
                prepared = self.prepare()
                path = self.queue / name
                original = path.read_bytes()
                path.write_bytes(original + b" ")

                with self.assertRaisesRegex(ValueError, "Исходник изменился"):
                    publish_h2_cleaning(prepared)

                self.assertFalse(self.output.exists())
                path.write_bytes(original)

    def test_changed_code_before_publish_is_rejected(self) -> None:
        """Зафиксированный исходник очистки также нельзя менять между фазами."""

        path = self.root / CODE_NAMES[0]
        path.parent.mkdir(parents=True)
        path.write_bytes(b"first\n")
        prepared = self.prepare()
        path.write_bytes(b"second\n")

        with self.assertRaisesRegex(ValueError, "Исходник изменился"):
            publish_h2_cleaning(prepared)

    def test_corrupted_hash_or_size_is_rejected(self) -> None:
        """Повреждённый JSONL не принимается даже при синтаксически допустимом JSON."""

        path = self.queue / "candidates.jsonl"
        path.write_bytes(path.read_bytes() + b" ")

        with self.assertRaisesRegex(ValueError, "SHA-256 или размер"):
            self.prepare()

    def test_wrong_count_is_rejected(self) -> None:
        """Число фактических строк не может расходиться с обоими счётчиками отчёта."""

        rows = self.original_rows()
        self.set_rows(rows[:2])

        with self.assertRaisesRegex(ValueError, "Число строк"):
            self.prepare()

    def test_duplicate_identifiers_are_rejected(self) -> None:
        """Дубли идентификаторов не сливаются и не пропускаются молча."""

        rows = self.original_rows()
        rows[1] = copy.deepcopy(rows[0])
        self.set_rows(rows)

        with self.assertRaisesRegex(ValueError, "повторное поле"):
            self.prepare()

    def test_labels_splits_and_missing_nulls_are_rejected(self) -> None:
        """Очистка не принимает назначенные или неявно отсутствующие метки и разбиения."""

        original = self.original_rows()

        for field in ("label", "split"):
            for missing in (False, True):
                with self.subTest(field=field, missing=missing):
                    rows = copy.deepcopy(original)

                    if missing:
                        rows[0].pop(field)

                    else:
                        rows[0][field] = "assigned"

                    self.set_rows(rows)

                    with self.assertRaisesRegex(ValueError, "явно содержать null"):
                        self.prepare()

    def test_invalid_fields_are_rejected(self) -> None:
        """Неверные типы, UUID, URL и признаки нельзя маскировать сериализацией."""

        original = self.original_rows()
        variants = {
            "title": None, "abstract": ["текст"], "candidate_id": "other:1", "item_uuid": "invalid",
            "source_url": "file:///private/data", "review_flags": [12],
        }

        for field, value in variants.items():
            with self.subTest(field=field):
                rows = copy.deepcopy(original)
                rows[0][field] = value
                self.set_rows(rows)

                with self.assertRaises(ValueError):
                    self.prepare()

    def test_json_duplicates_nan_and_overflow_are_rejected(self) -> None:
        """Строгий JSON запрещает дубликаты ключей, NaN и переполнение float."""

        original = (self.queue / "candidates.jsonl").read_bytes()

        for prefix in (b'{"title": "repeated",', b'{"unexpected": NaN,', b'{"unexpected": 1e999,'):
            with self.subTest(prefix=prefix):
                self.rewrite_candidates(prefix + original[1:])

                with self.assertRaises(ValueError):
                    self.prepare()

    def test_summary_json_is_strict(self) -> None:
        """Повторный ключ в исходном отчёте также недопустим."""

        path = self.queue / "summary.json"
        path.write_bytes(b'{"candidate_count": 3,' + path.read_bytes()[1:])

        with self.assertRaisesRegex(ValueError, "Повторный ключ"):
            self.prepare()

    def test_missing_lf_and_blank_lines_are_rejected(self) -> None:
        """Строки происхождения имеют явный LF, пустые строки не пропускаются."""

        original = (self.queue / "candidates.jsonl").read_bytes()

        for payload in (original.rstrip(b"\n"), original + b"\n"):
            with self.subTest(payload_length=len(payload)):
                self.rewrite_candidates(payload)

                with self.assertRaisesRegex(ValueError, "непустая строка JSONL"):
                    self.prepare()

    def test_traversal_outside_and_overlapping_paths_are_rejected(self) -> None:
        """Результат не может находиться в источнике, над ним или вне области результатов."""

        for output in (
            self.queue, self.queue / "child", self.queue.parent, self.root / "elsewhere",
            self.root / "manifests/results/../bad", self.root.parent / "outside",
        ):
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    prepare_h2_cleaning(self.root, self.queue, output)

    def test_source_file_symlink_is_rejected(self) -> None:
        """Даже ссылка на файл внутри проекта не принимается как исходный файл."""

        path = self.queue / "review.csv"
        target = self.root / "copied.csv"
        path.rename(target)
        path.symlink_to(target)

        with self.assertRaisesRegex(ValueError, "Символическая ссылка"):
            self.prepare()

    def test_output_parent_symlink_is_rejected(self) -> None:
        """Выходной путь не проходит через ссылку на другой каталог."""

        link = self.root / "manifests/results/link"
        target = self.root / "target"
        target.mkdir()
        link.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "Символическая ссылка"):
            prepare_h2_cleaning(self.root, self.queue, link / "result")

    def test_symlink_inserted_after_preparation_is_rejected(self) -> None:
        """Повторная проверка пути не разрешает подмену каталога после подготовки."""

        prepared = self.prepare()
        self.output.symlink_to(self.queue, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "Символическая ссылка"):
            publish_h2_cleaning(prepared)

    def test_modified_existing_result_and_notes_are_never_overwritten(self) -> None:
        """Решения человека в уже созданном результате нельзя затереть повторным запуском."""

        prepared = self.prepare()
        publish_h2_cleaning(prepared)
        path = self.output / "review.csv"
        changed = path.read_bytes() + b"human note\n"
        path.write_bytes(changed)

        with self.assertRaisesRegex(ValueError, "перезапись запрещена"):
            publish_h2_cleaning(prepared)

        self.assertEqual(path.read_bytes(), changed)

    def test_incomplete_existing_result_is_rejected_even_in_dry_run(self) -> None:
        """Пустой или неполный существующий каталог не считается свободным результатом."""

        self.output.mkdir()

        with self.assertRaisesRegex(ValueError, "неполон"):
            publish_h2_cleaning(self.prepare(), dry_run=True)

    def test_modified_prepared_payload_is_rejected(self) -> None:
        """Изменение словаря байтов после подготовки обнаруживается до публикации."""

        prepared = self.prepare()
        prepared.files["review.csv"] += b"change"

        with self.assertRaisesRegex(ValueError, "содержимое изменилось"):
            publish_h2_cleaning(prepared)

        self.assertFalse(self.output.exists())

    def test_modified_prepared_path_is_rejected(self) -> None:
        """Новый адрес пакета нельзя подставить без обновления происхождения."""

        prepared = replace(self.prepare(), output_dir=self.output.parent / "different")

        with self.assertRaisesRegex(ValueError, "не совпадают с отчётом"):
            publish_h2_cleaning(prepared)

    def test_prepared_extra_file_is_rejected(self) -> None:
        """Произвольные дополнительные пути не могут попасть в пакет."""

        prepared = self.prepare()
        prepared.files["../outside"] = b"bad"

        with self.assertRaisesRegex(ValueError, "состав"):
            publish_h2_cleaning(prepared)

    def test_publication_failure_cleans_only_own_partial_files(self) -> None:
        """Ошибка связывания откатывает созданные файлы и не меняет исходники."""

        prepared = self.prepare()
        original_link = os.link
        calls = 0

        def failing_link(source: Path, destination: Path) -> None:
            """Имитировать ошибку диска после первой успешной записи."""

            nonlocal calls
            calls += 1

            if calls == 2:
                raise OSError("Синтетическая ошибка диска")

            original_link(source, destination)

        with patch("src.preprocess.h2_cleaning_io.os.link", side_effect=failing_link):
            with self.assertRaisesRegex(OSError, "Синтетическая ошибка"):
                publish_h2_cleaning(prepared)

        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.output.parent.glob(".h2-cleaning-*")), [])
        self.assertTrue(publish_h2_cleaning(prepared))

    def test_empty_queue_is_a_valid_empty_candidate_package(self) -> None:
        """Пустой исходный экспорт сохраняется как пустой пакет, без выдуманных карточек."""

        self.rewrite_candidates(b"")
        path = self.queue / "summary.json"
        report = json.loads(path.read_bytes())
        report["candidate_count"] = 0
        report["export"]["candidate_count"] = 0
        path.write_text(json.dumps(report), encoding="utf-8")
        prepared = self.prepare()

        self.assertEqual(prepared.report["counts"]["candidates"], 0)
        self.assertEqual(prepared.files["cleaned_candidates.jsonl"], b"")
        self.assertEqual(prepared.files["changes.jsonl"], b"")
        self.assertTrue(publish_h2_cleaning(prepared))

    def test_failure_preserves_changes_in_just_created_file(self) -> None:
        """При откате нельзя удалить файл, который успел изменить другой процесс."""

        prepared = self.prepare()
        original_link = os.link
        first_path: Path | None = None

        def conflicting_link(source: Path, destination: Path) -> None:
            """Изменить ранее созданный файл перед имитацией ошибки следующей записи."""

            nonlocal first_path

            if first_path is not None:
                first_path.write_bytes(b"foreign change")
                raise OSError("Конфликт записи")

            original_link(source, destination)
            first_path = destination

        with patch("src.preprocess.h2_cleaning_io.os.link", side_effect=conflicting_link):
            with self.assertRaisesRegex(OSError, "Конфликт записи"):
                publish_h2_cleaning(prepared)

        self.assertIsNotNone(first_path)
        self.assertEqual(next(self.output.iterdir()).read_bytes(), b"foreign change")


if __name__ == "__main__":
    unittest.main()
