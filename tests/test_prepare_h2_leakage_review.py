"""Проверки происхождения резервов, безопасного экспорта и офлайн-команды H2."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import unittest

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import prepare_h2_leakage_review
from src.corpus.h2_candidate_export import CandidateExportResult
from src.corpus.h2_candidate_input import read_candidate_snapshot
from src.corpus.h2_candidates import build_candidate_queue
from src.corpus.h2_leakage import build_h2_leakage_review
from src.corpus.h2_leakage_io import export_leakage_review, read_practice_holds
from tests import test_h2_candidate_input

OUTPUT_NAMES = {
    "constraints.jsonl", "evaluation_holds.jsonl", "issues.jsonl", "summary.json", "START_HERE.txt",
}


def _json_line(value: Any) -> bytes:
    """Создать точную строку JSONL синтетического источника вместе с LF."""

    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _inventory(data: bytes) -> dict[str, Any]:
    """Получить контрольную сумму и размер точных тестовых байтов."""

    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


class LeakageReviewTests(unittest.TestCase):
    """Изолированные пакеты связывают все источники и не меняют рабочие данные."""

    def setUp(self) -> None:
        """Подготовить полный синтетический снимок и связанную пробу без Excel."""

        fixture = test_h2_candidate_input.CandidateInputTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.root = fixture.root
        self.snapshot_path = self.root / prepare_h2_leakage_review.DEFAULT_SNAPSHOT
        self.snapshot_path.parent.mkdir()
        fixture.path = self.snapshot_path
        fixture.save_description()
        self.snapshot = read_candidate_snapshot(self.root, self.snapshot_path)
        candidates, _ = build_candidate_queue(self.snapshot)
        self.packet_dir = self.root / prepare_h2_leakage_review.DEFAULT_PRACTICE_DIR
        self.output_dir = self.root / "manifests/results/leakage_v1"
        self.packet_dir.mkdir()
        self.registry = self.root / "manifests/works.jsonl"
        self.registry.write_text("Неизменный рабочий реестр\n", encoding="utf-8")
        self.inputs: list[dict[str, Any]] = []
        self.mappings: list[dict[str, Any]] = []
        self.holds: list[dict[str, Any]] = []
        self.candidate_lines: list[bytes] = []
        self.cleaned_lines: list[bytes] = []

        for position, candidate in enumerate(candidates, 1):
            candidate_line = _json_line(candidate)
            candidate_hash = _inventory(candidate_line)["sha256"]
            self.candidate_lines.append(candidate_line)
            cleaned = {
                "candidate_id": candidate["candidate_id"], "item_uuid": candidate["item_uuid"],
                "title_original": candidate["title"], "abstract_original": candidate["abstract"],
                "title_clean": candidate["title"], "abstract_clean": candidate["abstract"],
                "source_candidate_line_sha256": candidate_hash,
            }
            cleaned_line = _json_line(cleaned)
            self.cleaned_lines.append(cleaned_line)
            model_input = {field: candidate[field] for field in ("title", "abstract")}
            canonical = json.dumps(model_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            input_hash = _inventory(canonical)["sha256"]
            item = {"annotation_item_id": f"practice-{position}", **model_input, "input_sha256": input_hash}
            self.inputs.append(item)
            work_id = "work:known" if position == 1 else None
            mapping = {
                "annotation_item_id": item["annotation_item_id"],
                "candidate_id": candidate["candidate_id"], "item_uuid": candidate["item_uuid"],
                "work_id": work_id, "source": candidate["source"], "input_sha256": input_hash,
                "source_candidate_line": position, "source_candidate_line_sha256": candidate_hash,
                "source_cleaned_line": position, "source_cleaned_line_sha256": _inventory(cleaned_line)["sha256"],
            }
            self.mappings.append(mapping)
            self.holds.append({
                "candidate_id": candidate["candidate_id"], "item_uuid": candidate["item_uuid"],
                "work_id": work_id, "input_sha256": input_hash,
                "status": "reserved_for_practice", "seen_by_annotators": None,
                "exclude_from_future_evaluation": True, "related_versions_require_check": True,
                "automatic_split_enforcement": False, "reason": "Консервативный резерв будущей пробы.",
            })

        self.candidate_path = "manifests/results/candidates_v1/candidates.jsonl"
        self.cleaned_path = "manifests/results/cleaned_v1/cleaned_candidates.jsonl"
        self.packet: dict[str, Any] = {
            "format_version": "h2-practice-draft-v1", "packet_id": self.packet_dir.name,
            "status": "awaiting_guide_approval", "guide_approved": False,
            "annotation_started": False, "formal_calibration": False, "formal_audit": False,
            "training_ready": False, "item_count": len(self.inputs), "registered_work_count": 1,
            "snapshot": {
                "path": self.snapshot.provenance["snapshot_path"],
                "sha256": self.snapshot.provenance["snapshot_sha256"],
                "validated_pages": self.snapshot.provenance["total_pages"],
                "validated_items": self.snapshot.provenance["total_items"],
            },
            "output_files": {}, "source_files": {},
        }

        for name, data in (
            (self.candidate_path, b"".join(self.candidate_lines)),
            (self.cleaned_path, b"".join(self.cleaned_lines)),
        ):
            path = self.root / name
            path.parent.mkdir()
            path.write_bytes(data)
            self.packet["source_files"][name] = _inventory(data)

        self._save_records("inputs.jsonl", self.inputs)
        self._save_records("private/mapping.jsonl", self.mappings)
        self._save_records("private/evaluation_hold.jsonl", self.holds)

    def _save_packet(self) -> None:
        """Обновить паспорт только внутри временного синтетического проекта."""

        (self.packet_dir / "packet.json").write_bytes(_json_line(self.packet))

    def _save_records(self, name: str, records: list[dict[str, Any]]) -> None:
        """Заменить тестовые строки и их опись для проверки смысловых конфликтов."""

        self._save_packet_file(name, b"".join(_json_line(record) for record in records))

    def _save_packet_file(self, name: str, data: bytes) -> None:
        """Заменить один файл тестового пакета и синхронизировать его хеш."""

        path = self.packet_dir / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        self.packet["output_files"][name] = _inventory(data)
        self._save_packet()

    def _read(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Проверить подготовленную пробу относительно полного снимка."""

        return read_practice_holds(self.root, self.packet_dir, self.snapshot)

    def _review(self) -> dict[str, Any]:
        """Собрать настоящие предложения и включить проверенное происхождение пробы."""

        holds, provenance = self._read()
        review = build_h2_leakage_review(self.snapshot, holds)
        review["summary"]["practice_provenance"] = provenance

        return review

    def _export(
        self, output: Path | None = None, *, dry_run: bool = False, review: dict[str, Any] | None = None,
    ) -> CandidateExportResult:
        """Запустить настоящий экспорт с отдельным назначением и проверенной пробой."""

        return export_leakage_review(
            self.root, output if output is not None else self.output_dir,
            self._review() if review is None else review,
            snapshot_path=self.snapshot_path, practice_dir=self.packet_dir, dry_run=dry_run,
        )

    def _tree(self) -> dict[str, tuple[bytes, int]]:
        """Снять точные байты и время файлов для контроля любых побочных записей."""

        return {
            path.relative_to(self.root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*") if path.is_file() and not path.is_symlink()
        }

    def _command(self, arguments: list[str]) -> tuple[int, str]:
        """Выполнить всю команду с запретом сетевых обращений и перехватом вывода."""

        output = io.StringIO()

        with redirect_stdout(output), patch("urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена")):
            result = prepare_h2_leakage_review.main(arguments, project_root=self.root)

        return result, output.getvalue()

    def test_holds_and_complete_provenance_are_preserved(self) -> None:
        """Исходные резервы сохраняются точно, а тексты подтверждены обеими строками."""

        before = self._tree()
        snapshot_before = copy.deepcopy(self.snapshot)
        holds, provenance = self._read()

        self.assertEqual(holds, self.holds)
        self.assertEqual(self.snapshot, snapshot_before)
        self.assertEqual(self._tree(), before)
        self.assertEqual(provenance["source_packet_sha256"], _inventory((self.packet_dir / "packet.json").read_bytes())["sha256"])
        self.assertEqual(provenance["verified_source_files"], self.packet["source_files"])
        self.assertEqual(provenance["output_files"], self.packet["output_files"])
        self.assertEqual(provenance["verified_input_count"], 2)
        self.assertFalse(provenance["excel_read"])
        self.assertIsNone(holds[0]["seen_by_annotators"])

    def test_changed_inventory_bytes_or_hash_are_rejected(self) -> None:
        """Изменённые байты или размер не скрываются за корректным JSON."""

        for field, value in (("bytes", 1), ("sha256", "0" * 64)):
            with self.subTest(field=field):
                old = self.packet["output_files"]["inputs.jsonl"][field]
                self.packet["output_files"]["inputs.jsonl"][field] = value
                self._save_packet()

                with self.assertRaisesRegex(ValueError, "SHA-256 или размер"):
                    self._read()

                self.packet["output_files"]["inputs.jsonl"][field] = old

    def test_missing_output_file_and_inventory_entry_are_rejected(self) -> None:
        """Отсутствующие обязательные входы не превращаются в пустые резервы."""

        self.packet["output_files"].pop("private/evaluation_hold.jsonl")
        self._save_packet()

        with self.assertRaisesRegex(ValueError, "отсутствуют"):
            self._read()

        self._save_records("private/evaluation_hold.jsonl", self.holds)
        (self.packet_dir / "private/evaluation_hold.jsonl").unlink()

        with self.assertRaisesRegex(ValueError, "Нужен обычный файл"):
            self._read()

    def test_duplicate_json_keys_are_rejected_in_packet_and_records(self) -> None:
        """Строгий JSON сохраняет единственность решения в паспорте и каждой строке."""

        path = self.packet_dir / "packet.json"
        path.write_bytes(b'{"packet_id":"a","packet_id":"b"}')

        with self.assertRaisesRegex(ValueError, "Повторный ключ"):
            self._read()

        self._save_packet()
        original = _json_line(self.inputs[0])
        duplicated = original.rstrip()[:-1] + b',"annotation_item_id":"other"}\n'
        self._save_packet_file("inputs.jsonl", duplicated + _json_line(self.inputs[1]))

        with self.assertRaisesRegex(ValueError, "Повторный ключ"):
            self._read()

    def test_changed_snapshot_and_source_reference_are_rejected(self) -> None:
        """Новая версия снимка или другое происхождение mapping требуют отдельной пробы."""

        self.packet["snapshot"]["sha256"] = "0" * 64
        self._save_packet()

        with self.assertRaisesRegex(ValueError, "Снимок пакета"):
            self._read()

        self.packet["snapshot"]["sha256"] = self.snapshot.provenance["snapshot_sha256"]
        self.mappings[0]["source"]["item_index"] = 99
        self._save_records("private/mapping.jsonl", self.mappings)

        with self.assertRaisesRegex(ValueError, "Происхождение mapping"):
            self._read()

    def test_uuid_candidate_work_and_hash_links_are_required(self) -> None:
        """Несогласованные UUID, кандидаты, работы и входы останавливают чтение."""

        for field, value in (
            ("item_uuid", "00000000-0000-0000-0000-000000000001"),
            ("candidate_id", "jinr:other"), ("work_id", "work:other"), ("input_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                holds = copy.deepcopy(self.holds)
                holds[0][field] = value
                self._save_records("private/evaluation_hold.jsonl", holds)

                with self.assertRaises(ValueError):
                    self._read()

    def test_duplicate_empty_and_missing_records_are_rejected(self) -> None:
        """Резерв не допускает повторную карточку, пустой ID или неверную численность."""

        for records in ([self.holds[0], self.holds[0]], [self.holds[0]], []):
            with self.subTest(records=records):
                self._save_records("private/evaluation_hold.jsonl", records)

                with self.assertRaises(ValueError):
                    self._read()

        self._save_records("private/evaluation_hold.jsonl", self.holds)
        self.inputs[0]["annotation_item_id"] = " "
        self._save_records("inputs.jsonl", self.inputs)

        with self.assertRaisesRegex(ValueError, "непустой строкой"):
            self._read()

    def test_recomputed_input_hash_does_not_hide_invented_clean_text(self) -> None:
        """Согласованные локальные хеши не заменяют связь с очищенным исходником."""

        self.inputs[0]["title"] = "Выдуманное название"
        text = {field: self.inputs[0][field] for field in ("title", "abstract")}
        digest = _inventory(json.dumps(text, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())["sha256"]
        self.inputs[0]["input_sha256"] = digest
        self.mappings[0]["input_sha256"] = digest
        self.holds[0]["input_sha256"] = digest
        self._save_records("inputs.jsonl", self.inputs)
        self._save_records("private/mapping.jsonl", self.mappings)
        self._save_records("private/evaluation_hold.jsonl", self.holds)

        with self.assertRaisesRegex(ValueError, "очищенным текстом"):
            self._read()

    def test_changed_original_source_file_is_rejected(self) -> None:
        """Потоковая проверка обнаруживает изменения даже вне выбранной строки."""

        path = self.root / self.cleaned_path
        path.write_bytes(path.read_bytes() + b"\n")

        with self.assertRaisesRegex(ValueError, "исходного JSONL изменён"):
            self._read()

    def test_source_line_hash_or_number_mismatch_is_rejected(self) -> None:
        """Ссылки на другую строку не проходят при правильном хеше всего файла."""

        for field, value in (("source_cleaned_line_sha256", "0" * 64), ("source_cleaned_line", 999)):
            with self.subTest(field=field):
                mappings = copy.deepcopy(self.mappings)
                mappings[0][field] = value
                self._save_records("private/mapping.jsonl", mappings)

                with self.assertRaises(ValueError):
                    self._read()

    def test_changed_guide_is_only_recorded_as_reported(self) -> None:
        """Старый хеш документа не выдаётся за повторно проверенный источник текстов."""

        self.packet["source_files"]["docs/old-guide.md"] = {"sha256": "a" * 64, "bytes": 100}
        self._save_packet()
        _, provenance = self._read()

        self.assertIn("docs/old-guide.md", provenance["source_files_as_reported"])
        self.assertNotIn("docs/old-guide.md", provenance["verified_source_files"])

    def test_unsafe_inventory_paths_and_symlinks_are_rejected(self) -> None:
        """Опись не читает файлы вне пакета, переходы вверх или символические ссылки."""

        for name in ("../works.jsonl", str(self.registry), "../../outside.json"):
            with self.subTest(name=name):
                self.packet["output_files"][name] = _inventory(b"")
                self._save_packet()

                with self.assertRaises(ValueError):
                    self._read()

                self.packet["output_files"].pop(name)

        self._save_packet()
        inputs_path = self.packet_dir / "inputs.jsonl"
        inputs_path.unlink()
        inputs_path.symlink_to(self.registry)

        with self.assertRaisesRegex(ValueError, "Символическая ссылка"):
            self._read()

    def test_excel_is_never_opened(self) -> None:
        """Посторонний Excel не нужен, а включённый в опись отклоняется до чтения."""

        workbook = self.packet_dir / "answers.xlsx"
        workbook.write_bytes(b"invalid workbook")
        self._read()
        self.packet["output_files"]["answers.xlsx"] = _inventory(workbook.read_bytes())
        self._save_packet()

        with self.assertRaisesRegex(ValueError, "Excel-файлы"):
            self._read()

    def test_new_export_preserves_review_and_sources(self) -> None:
        """Пять новых файлов сохраняют происхождение и честные ограничения результата."""

        review = self._review()
        original_review = copy.deepcopy(review)
        before = self._tree()
        result = self._export(review=review)

        self.assertEqual(result.created_files, 5)
        self.assertEqual(set(result.hashes), OUTPUT_NAMES)
        self.assertEqual({path.name for path in self.output_dir.iterdir()}, OUTPUT_NAMES)
        self.assertEqual(review, original_review)
        self.assertEqual({name: self._tree()[name] for name in before}, before)
        summary = json.loads((self.output_dir / "summary.json").read_bytes())
        self.assertTrue(summary["proposals_only"])
        self.assertFalse(summary["automatic_split_enforcement"])
        self.assertFalse(summary["training_ready"])
        self.assertEqual(summary["practice_provenance"], review["summary"]["practice_provenance"])

        for name, digest in result.hashes.items():
            self.assertEqual(digest, _inventory((self.output_dir / name).read_bytes())["sha256"])

        for name in ("constraints", "evaluation_holds", "issues"):
            records = [json.loads(line) for line in (self.output_dir / f"{name}.jsonl").read_bytes().splitlines()]
            self.assertEqual(records, review[name])

    def test_exact_repeat_and_dry_repeat_do_not_write(self) -> None:
        """Точный повтор сохраняет все байты и времена без создания каталогов."""

        first = self._export()
        before = self._tree()

        with patch.object(Path, "mkdir", side_effect=AssertionError("Запись запрещена")):
            for dry_run in (False, True):
                result = self._export(dry_run=dry_run)
                self.assertEqual(result.created_files, 0)
                self.assertEqual(result.unchanged_files, 5)
                self.assertEqual(result.hashes, first.hashes)

        self.assertEqual(self._tree(), before)

    def test_dry_run_does_not_create_parents(self) -> None:
        """Пробный запуск вычисляет будущие хеши и не создаёт ничего на диске."""

        output = self.root / "manifests/results/missing/nested/review"
        before = self._tree()

        with patch.object(Path, "mkdir", side_effect=AssertionError("Запись запрещена")):
            result = self._export(output, dry_run=True)

        self.assertEqual(result.created_files, 0)
        self.assertEqual(result.unchanged_files, 0)
        self.assertEqual(set(result.hashes), OUTPUT_NAMES)
        self.assertEqual(self._tree(), before)
        self.assertFalse(output.parent.exists())

    def test_changed_and_incomplete_exports_are_preserved(self) -> None:
        """Правки человека и неполные результаты требуют нового каталога версии."""

        self._export()
        (self.output_dir / "issues.jsonl").write_text("Ручная заметка\n", encoding="utf-8")
        before = self._tree()

        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), self.assertRaisesRegex(ValueError, "не перезаписываются"):
                self._export(dry_run=dry_run)

        self.assertEqual(self._tree(), before)
        (self.output_dir / "constraints.jsonl").unlink()
        before = self._tree()

        with self.assertRaisesRegex(ValueError, "не является полным пакетом"):
            self._export()

        self.assertEqual(self._tree(), before)

    def test_changed_summary_and_untrue_safety_flags_stop_before_writes(self) -> None:
        """Прямой экспорт не допускает фиктивную готовность и не обновляет прежнюю сводку."""

        review = self._review()

        for field, value in (
            ("training_ready", True), ("automatic_split_enforcement", True),
            ("proposals_only", False), ("splits_checked", True), ("registries_changed", True),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(review)
                changed["summary"][field] = value

                with self.assertRaisesRegex(ValueError, "предварительный пакет"):
                    self._export(review=changed)

                self.assertFalse(self.output_dir.exists())

        self._export(review=review)
        before = self._tree()
        review["summary"]["note"] = "Изменённая сводка"

        with (
            patch.object(Path, "mkdir", side_effect=AssertionError("Запись запрещена")),
            self.assertRaisesRegex(ValueError, "Существующий файл отличается"),
        ):
            self._export(review=review)

        self.assertEqual(self._tree(), before)

    def test_output_paths_cannot_overlap_sources_or_escape_results(self) -> None:
        """Оба исходных пакета и внешние каталоги защищены даже при dry-run."""

        before = self._tree()

        for output in (
            self.snapshot_path.parent, self.packet_dir, self.packet_dir / "nested",
            self.root / "manifests/results", self.root / "data/review",
            self.root / "manifests/results/../other", self.root.parent / "outside",
        ):
            for dry_run in (False, True):
                with self.subTest(output=output, dry_run=dry_run), self.assertRaises(ValueError):
                    self._export(output, dry_run=dry_run)

        self.assertEqual(self._tree(), before)

    def test_output_symlink_or_occupied_parent_is_rejected(self) -> None:
        """Небезопасное назначение и родительский файл выявляются до первой записи."""

        link = self.root / "manifests/results/link"
        link.symlink_to(self.packet_dir, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "Символическая ссылка"):
            self._export(link / "review", dry_run=True)

        occupied = self.root / "manifests/results/occupied"
        occupied.write_text("Сохранить", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "занят файлом"):
            self._export(occupied / "nested/review", dry_run=True)

    def test_cli_defaults_run_complete_pipeline_and_repeat(self) -> None:
        """Команда читает реальные тестовые источники и повторяет результат без сети."""

        arguments = ["--output-dir", str(self.output_dir)]
        before = self._tree()
        result, output = self._command(arguments)
        self.assertEqual(result, 0)
        self.assertIn("Создано файлов: 5", output)
        self.assertIn("защита от утечки не завершена", output)
        self.assertEqual({name: self._tree()[name] for name in before}, before)
        repeated_before = self._tree()
        result, output = self._command(arguments)
        self.assertEqual(result, 0)
        self.assertIn("Создано файлов: 0; без изменений: 5", output)
        self.assertEqual(self._tree(), repeated_before)

    def test_cli_relative_inputs_and_dry_run_are_read_only(self) -> None:
        """Относительные пути берутся от корня проекта, и dry-run не пишет файлы."""

        before = self._tree()
        result, output = self._command([
            "--snapshot", str(prepare_h2_leakage_review.DEFAULT_SNAPSHOT),
            "--practice-dir", str(prepare_h2_leakage_review.DEFAULT_PRACTICE_DIR),
            "--output-dir", "manifests/results/leakage_v1", "--dry-run",
        ])

        self.assertEqual(result, 0)
        self.assertIn("Пробный запуск", output)
        self.assertEqual(self._tree(), before)
        self.assertFalse(self.output_dir.exists())

    def test_cli_rejects_corrupt_packet_before_export(self) -> None:
        """Повреждённый исходник завершает команду понятной ошибкой без результата."""

        (self.packet_dir / "inputs.jsonl").write_bytes(b"{}\n")
        errors = io.StringIO()

        with redirect_stderr(errors), self.assertRaises(SystemExit) as exception:
            self._command(["--output-dir", str(self.output_dir)])

        self.assertEqual(exception.exception.code, 2)
        self.assertIn("SHA-256 или размер", errors.getvalue())
        self.assertFalse(self.output_dir.exists())

    def test_cli_requires_output_and_has_no_apply_or_network_flags(self) -> None:
        """Назначение обязательно; режимы применения, принуждения и сети отсутствуют."""

        for arguments in ([], ["--output-dir", str(self.output_dir), "--apply"], ["--output-dir", str(self.output_dir), "--network"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exception:
                self._command(arguments)

            self.assertEqual(exception.exception.code, 2)

        self.assertFalse(self.output_dir.exists())


if __name__ == "__main__":
    unittest.main()
