"""Проверки офлайн-команды H2, сохранности источников и защищённого экспорта."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import tempfile
import unittest

from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import prepare_h2_candidates
from src.corpus.h2_candidate_export import CandidateExportResult, export_candidate_queue
from tests import test_h2_candidate_input


def _candidate(title: str = "Исследование физики") -> dict[str, object]:
    """Создать синтетическую карточку без готовой метки и решения о допуске."""

    return {
        "candidate_id": "h2-candidate:test-item",
        "item_uuid": "test-item",
        "source_url": "https://example.invalid/items/test-item",
        "title": title,
        "abstract": 'Первая строка; «физика».\nВторая строка с "кавычками".',
        "year": 2020,
        "journal": "Журнал физики",
        "journal_key": "журнал физики",
        "doi": None,
        "edn": None,
        "doi_values": [],
        "edn_values": [],
        "rubrics_as_reported": ["Физика"],
        "udc_as_reported": ["53"],
        "has_title_abstract": True,
        "physics_signal": "physics_rubric",
        "review_flags": ["check_title_language"],
        "duplicate_groups": [],
        "review_status": "not_reviewed",
        "label": None,
        "split": None,
        "source": {"path": "manifests/imports/synthetic.json", "sha256": "a" * 64},
        "metadata_as_reported": {"dc.title": [{"value": title}]},
    }


class CandidateExportTests(unittest.TestCase):
    """Проверки точных байтов, повторного запуска и отказа от опасных путей."""

    def setUp(self) -> None:
        """Создать временный проект со снимком и независимым каталогом результата."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.snapshot = self.root / "manifests/results/analysis/analysis.json"
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.write_text('{"source": "снимок"}\n', encoding="utf-8")
        self.output = self.root / "manifests/results/h2_queue_v1"
        self.rows = [_candidate()]
        self.summary = {"status": "candidates_not_labeled", "input_provenance": {"version": 1}}

    def _export(self, output: Path | None = None) -> CandidateExportResult:
        """Вызвать экспорт со стандартной карточкой и заданным выходным путём."""

        return export_candidate_queue(
            self.root, output if output is not None else self.output,
            self.rows, self.summary, snapshot_path=self.snapshot,
        )

    def _tree_bytes(self) -> dict[str, bytes]:
        """Снять все обычные файлы для проверки полного отсутствия изменений."""

        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def _csv_rows(self) -> list[dict[str, str]]:
        """Прочитать таблицу тем же разделителем, сохранив переносы внутри ячейки."""

        with (self.output / "review.csv").open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream, delimiter=";"))

    def test_new_bundle_preserves_unicode_newlines_and_source(self) -> None:
        """CSV и JSONL сохраняют текст, а исходный снимок остаётся неизменным."""

        original = self.snapshot.read_bytes()
        result = export_candidate_queue(
            self.root, self.output, self.rows, self.summary, snapshot_path=self.snapshot,
        )

        self.assertEqual(result.created_files, 3)
        self.assertEqual(result.unchanged_files, 0)
        self.assertEqual(self.snapshot.read_bytes(), original)
        self.assertEqual(set(result.hashes), {"candidates.jsonl", "review.csv", "summary.json"})
        jsonl = (self.output / "candidates.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len(jsonl.splitlines()), 1)
        self.assertEqual(json.loads(jsonl), self.rows[0])
        self.assertNotIn("\\u", jsonl)
        self.assertTrue((self.output / "review.csv").read_bytes().startswith(codecs.BOM_UTF8))
        row = self._csv_rows()[0]
        self.assertEqual(row["Название"], self.rows[0]["title"])
        self.assertEqual(row["Аннотация"], self.rows[0]["abstract"])
        self.assertEqual(row["Решение при просмотре"], "")
        self.assertEqual(row["Комментарий"], "")
        self.assertEqual(row["Статус просмотра"], "Не просмотрено")
        self.assertEqual(row["Причины ручной проверки"], "Проверить язык названия")

    def test_summary_hashes_match_exact_output_bytes(self) -> None:
        """Отчёт хранит контрольные суммы без изменения переданной сводки."""

        before = json.dumps(self.summary, ensure_ascii=False, sort_keys=True)
        self._export()
        report = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(report["input_provenance"], self.summary["input_provenance"])
        self.assertFalse(report["export"]["review_is_blind_labeling"])
        self.assertEqual(report["export"]["candidate_count"], 1)

        for name, record in report["export"]["files"].items():
            data = (self.output / name).read_bytes()
            self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(record["bytes"], len(data))

        self.assertEqual(json.dumps(self.summary, ensure_ascii=False, sort_keys=True), before)

    def test_identical_repeat_performs_no_writes(self) -> None:
        """Точный повтор не открывает файл на запись и не меняет временные метки."""

        self._export()
        before = self._tree_bytes()
        timestamps = {path.name: path.stat().st_mtime_ns for path in self.output.iterdir()}

        with patch.object(Path, "mkdir", side_effect=AssertionError("Не должно быть записи")):
            result = export_candidate_queue(
                self.root, self.output, self.rows, self.summary, snapshot_path=self.snapshot,
            )

        self.assertEqual(result.created_files, 0)
        self.assertEqual(result.unchanged_files, 3)
        self.assertEqual(self._tree_bytes(), before)
        self.assertEqual(
            {path.name: path.stat().st_mtime_ns for path in self.output.iterdir()}, timestamps,
        )

    def test_review_edits_are_never_overwritten(self) -> None:
        """Ручной комментарий в таблице останавливает повтор до любых записей."""

        self._export()
        (self.output / "review.csv").write_text("Пользовательский комментарий", encoding="utf-8")
        before = self._tree_bytes()

        with patch.object(Path, "mkdir", side_effect=AssertionError("Не должно быть записи")):
            with self.assertRaisesRegex(ValueError, "правки просмотра не перезаписываются"):
                self._export()

        self.assertEqual(self._tree_bytes(), before)

    def test_changed_rows_fail_before_any_writes(self) -> None:
        """Изменение входного названия требует новую версию результата."""

        self._export()
        self.rows[0]["title"] = "Новое название"
        before = self._tree_bytes()

        with self.assertRaisesRegex(ValueError, "Существующий файл отличается"):
            self._export()

        self.assertEqual(self._tree_bytes(), before)

    def test_incomplete_bundle_is_not_repaired(self) -> None:
        """Частичный каталог не дополняется автоматически."""

        self.output.mkdir()
        (self.output / "candidates.jsonl").write_text("[]\n", encoding="utf-8")
        before = self._tree_bytes()

        with self.assertRaisesRegex(ValueError, "не является полным пакетом"):
            self._export()

        self.assertEqual(self._tree_bytes(), before)

    def test_extra_files_are_not_ignored_or_deleted(self) -> None:
        """Посторонняя заметка в существующем пакете остаётся нетронутой."""

        self._export()
        (self.output / "notes.txt").write_text("Важная заметка", encoding="utf-8")
        before = self._tree_bytes()

        with self.assertRaisesRegex(ValueError, "не является полным пакетом"):
            self._export()

        self.assertEqual(self._tree_bytes(), before)

    def test_changed_summary_does_not_rewrite_matching_data_files(self) -> None:
        """Проверка последнего файла выполняется до любых изменений первых файлов."""

        self._export()
        self.summary["status"] = "другая сводка"
        before = self._tree_bytes()

        with patch.object(Path, "mkdir", side_effect=AssertionError("Не должно быть записи")):
            with self.assertRaisesRegex(ValueError, "Существующий файл отличается"):
                self._export()

        self.assertEqual(self._tree_bytes(), before)

    def test_formulas_with_hidden_prefix_are_escaped_only_in_csv(self) -> None:
        """Формулы с пробелами и управляющими символами не запускаются из ячейки."""

        titles = ["=1+1", "+1", "-2", "@SUM(A1)", "  =1", "\t\r\n+1", "\x00=1", "\u200b@A1"]
        self.rows = [_candidate(title) for title in titles]
        self._export()

        self.assertEqual([row["Название"] for row in self._csv_rows()], ["'" + title for title in titles])
        json_rows = [
            json.loads(line)
            for line in (self.output / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([row["title"] for row in json_rows], titles)

    def test_other_text_columns_are_protected_too(self) -> None:
        """Защита формул применяется не только к названию, но и к другим текстам."""

        self.rows[0]["abstract"] = " =1"
        self.rows[0]["rubrics_as_reported"] = ["+1", "Физика"]
        self.rows[0]["review_flags"] = ["@unknown"]
        self._export()
        row = self._csv_rows()[0]

        self.assertEqual(row["Аннотация"], "' =1")
        self.assertEqual(row["Рубрики в источнике — не метки H2"], "'+1\nФизика")
        self.assertEqual(row["Причины ручной проверки"], "'@unknown")

    def test_paths_outside_results_or_with_parent_hop_are_refused(self) -> None:
        """Экспорт не пишет в исходники, корни каталогов и пути с переходом вверх."""

        before = self._tree_bytes()

        for path in (
            self.root / "data/output",
            self.root / "manifests/results",
            self.root / "manifests/results/../unsafe",
            self.root.parent / "outside-results",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self._export(path)

        self.assertEqual(self._tree_bytes(), before)

    def test_snapshot_parent_overlap_is_refused(self) -> None:
        """Результат не создаётся рядом с исходным снимком либо поверх его дерева."""

        for path in (self.snapshot.parent, self.snapshot.parent / "new-version"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "пересекаться"):
                    self._export(path)

    def test_symlink_directory_and_target_are_refused(self) -> None:
        """Символические ссылки не допускаются даже при нахождении цели в проекте."""

        actual = self.root / "manifests/results/actual"
        actual.mkdir()
        linked = self.root / "manifests/results/linked"
        linked.symlink_to(actual, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "Символическая ссылка"):
            self._export(linked / "queue")

        self.assertEqual(list(actual.iterdir()), [])
        self._export()
        (self.output / "review.csv").unlink()
        (self.output / "review.csv").symlink_to(self.snapshot)

        with self.assertRaisesRegex(ValueError, "небезопасен"):
            self._export()

    def test_relative_output_resolves_from_project_root(self) -> None:
        """Относительный путь не зависит от текущего рабочего каталога процесса."""

        self._export(Path("manifests/results/h2_queue_v1"))
        self.assertTrue((self.output / "summary.json").is_file())

    def test_existing_file_instead_of_directory_is_preserved(self) -> None:
        """Занятый обычным файлом выходной путь не удаляется и не заменяется."""

        self.output.write_text("Важный файл", encoding="utf-8")
        before = self._tree_bytes()

        with self.assertRaisesRegex(ValueError, "занят файлом"):
            self._export()

        self.assertEqual(self._tree_bytes(), before)

    def test_empty_queue_still_has_headers_and_summary(self) -> None:
        """Пустой набор экспортируется явно, без выдуманных кандидатов."""

        self.rows = []
        self._export()

        self.assertEqual((self.output / "candidates.jsonl").read_bytes(), b"")
        self.assertEqual(self._csv_rows(), [])
        report = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(report["export"]["candidate_count"], 0)


class PrepareH2CandidatesCommandTests(unittest.TestCase):
    """Проверки явного назначения результата и неизменности реестров через CLI."""

    def setUp(self) -> None:
        """Подменить чтение и построение, сохранив настоящий безопасный экспорт."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.snapshot_path = self.root / prepare_h2_candidates.DEFAULT_SNAPSHOT
        self.snapshot_path.parent.mkdir(parents=True)
        self.snapshot_path.write_text("{}\n", encoding="utf-8")
        self.output_dir = self.root / "manifests/results/h2_cli_v1"
        self.registry = self.root / "manifests/works.jsonl"
        self.registry.write_text("Старый реестр\n", encoding="utf-8")
        self.snapshot = object()
        self.rows = [_candidate()]
        self.summary = {"status": "candidates_not_labeled"}
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.output = self.patches.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        self.errors = self.patches.enter_context(patch("sys.stderr", new_callable=io.StringIO))
        self.reader = self.patches.enter_context(patch.object(
            prepare_h2_candidates, "read_candidate_snapshot", return_value=self.snapshot,
        ))
        self.builder = self.patches.enter_context(patch.object(
            prepare_h2_candidates, "build_candidate_queue", return_value=(self.rows, self.summary),
        ))

    def test_default_snapshot_is_offline_and_registry_is_unchanged(self) -> None:
        """Команда вызывает только офлайн-помощники и оставляет реестр без изменений."""

        original = self.registry.read_bytes()

        result = prepare_h2_candidates.main(
            ["--output-dir", str(self.output_dir)], project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.reader.assert_called_once_with(self.root, self.snapshot_path)
        self.builder.assert_called_once_with(self.snapshot)
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertIn("Кандидатов для просмотра: 1", self.output.getvalue())
        self.assertIn("не слепая разметка", self.output.getvalue())
        self.assertIn("Создано файлов: 3", self.output.getvalue())

    def test_output_directory_must_be_explicit(self) -> None:
        """Без явного каталога команда не читает источники и ничего не создаёт."""

        with self.assertRaises(SystemExit) as exception:
            prepare_h2_candidates.main([], project_root=self.root)

        self.assertEqual(exception.exception.code, 2)
        self.reader.assert_not_called()
        self.builder.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_relative_snapshot_uses_project_root(self) -> None:
        """Явный относительный снимок передаётся читателю внутри заданного проекта."""

        relative = Path("manifests/results/source_v2/analysis.json")

        prepare_h2_candidates.main(
            ["--snapshot", str(relative), "--output-dir", str(self.output_dir)],
            project_root=self.root,
        )

        self.reader.assert_called_once_with(self.root, self.root / relative)

    def test_invalid_snapshot_stops_before_export(self) -> None:
        """Повреждённый снимок не оставляет каталог результата."""

        self.reader.side_effect = ValueError("SHA-256 источника не совпадает")

        with self.assertRaises(SystemExit) as exception:
            prepare_h2_candidates.main(
                ["--output-dir", str(self.output_dir)], project_root=self.root,
            )

        self.assertEqual(exception.exception.code, 2)
        self.builder.assert_not_called()
        self.assertFalse(self.output_dir.exists())
        self.assertIn("SHA-256 источника не совпадает", self.errors.getvalue())

    def test_no_network_commit_or_force_switches_exist(self) -> None:
        """CLI не принимает сетевой запуск, регистрацию или принудительную перезапись."""

        for flag in ("--download", "--network", "--commit", "--force"):
            with self.subTest(flag=flag):
                with self.assertRaises(SystemExit) as exception:
                    prepare_h2_candidates.main(
                        ["--output-dir", str(self.output_dir), flag], project_root=self.root,
                    )

                self.assertEqual(exception.exception.code, 2)

        self.reader.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_cli_repeat_does_not_change_files(self) -> None:
        """Повтор команды сообщает ноль новых файлов при прежнем снимке."""

        arguments = ["--output-dir", str(self.output_dir)]
        prepare_h2_candidates.main(arguments, project_root=self.root)
        before = {path.name: path.read_bytes() for path in self.output_dir.iterdir()}

        prepare_h2_candidates.main(arguments, project_root=self.root)

        self.assertEqual({path.name: path.read_bytes() for path in self.output_dir.iterdir()}, before)
        self.assertIn("Создано файлов: 0; без изменений: 3", self.output.getvalue())


class PrepareH2CandidatesIntegrationTests(unittest.TestCase):
    """Проверка полной цепочки от свидетельств API до очереди без подмены помощников."""

    def test_real_pipeline_preserves_sources_and_repeat_writes_nothing(self) -> None:
        """Настоящие reader, builder и export сохраняют происхождение и пустые метки."""

        fixture = test_h2_candidate_input.CandidateInputTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.path = fixture.root / "manifests/results/source_snapshot/analysis.json"
        fixture.path.parent.mkdir()
        fixture.save_description()
        original_files = {
            path: path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()
        }
        output_dir = fixture.root / "manifests/results/h2_integration_v1"
        arguments = ["--snapshot", str(fixture.path), "--output-dir", str(output_dir)]
        output = io.StringIO()

        with redirect_stdout(output):
            result = prepare_h2_candidates.main(arguments, project_root=fixture.root)

        self.assertEqual(result, 0)
        rows = [
            json.loads(line)
            for line in (output_dir / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        report = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
        snapshot_hash = hashlib.sha256(fixture.path.read_bytes()).hexdigest()
        entries = {entry["page"]: entry for entry in fixture.entries}
        self.assertEqual(len(rows), 2)
        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual(report["with_title_abstract"], 2)
        self.assertEqual(report["provenance"]["snapshot_sha256"], snapshot_hash)
        self.assertEqual(report["labelled_count"], 0)
        self.assertFalse(report["training_ready"])

        for row in rows:
            self.assertIsNone(row["label"])
            self.assertIsNone(row["split"])
            self.assertEqual(row["review_status"], "not_reviewed")
            self.assertEqual(row["source"]["snapshot_sha256"], snapshot_hash)
            entry = entries[row["source"]["page_number"]]
            self.assertEqual(row["source"]["page_sha256"], entry["sha256"])
            self.assertEqual(
                row["source"]["http_evidence_sha256"], entry["http_evidence_sha256"],
            )

        created_files = {
            path: (path.read_bytes(), path.stat().st_mtime_ns) for path in output_dir.iterdir()
        }

        with redirect_stdout(output):
            repeated_result = prepare_h2_candidates.main(arguments, project_root=fixture.root)

        self.assertEqual(repeated_result, 0)
        self.assertIn("Создано файлов: 0; без изменений: 3", output.getvalue())
        self.assertEqual({path: path.read_bytes() for path in original_files}, original_files)
        self.assertEqual(
            {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in output_dir.iterdir()},
            created_files,
        )


if __name__ == "__main__":
    unittest.main()
