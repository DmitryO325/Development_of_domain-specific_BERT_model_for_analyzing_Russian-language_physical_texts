"""Проверки безопасного экспорта и офлайн-команды разбора повторов H2."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest

from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import prepare_h2_duplicate_review
from src.corpus.h2_candidate_export import CandidateExportResult
from src.corpus.h2_duplicate_export import export_duplicate_review
from tests import test_h2_candidate_input

OUTPUT_NAMES = {
    "cards.jsonl", "groups.jsonl", "pairs.jsonl", "relations.jsonl", "summary.json", "START_HERE.txt",
}


def _review() -> dict[str, Any]:
    """Создать минимальные записи с исходным текстом и незаполненным решением."""

    return {
        "cards": [{
            "item_uuid": "test-item", "title": "Название с «кавычками»",
            "abstract": "Первая строка.\nВторая строка.", "is_h2_candidate": True,
        }],
        "groups": [{
            "group_id": "abstract:test", "item_uuids": ["test-item", "other-item"],
            "review_decision": None,
        }],
        "pairs": [{
            "pair_id": "pair:test", "item_uuids": ["test-item", "other-item"],
            "review_decision": None, "comment": "",
        }],
        "relations": [{
            "relation_id": "relation:test", "source_item_uuid": "test-item",
            "target_item_uuid": "other-item", "raw_value": "other-item",
        }],
        "summary": {
            "status": "review_required", "training_ready": False,
            "provenance": {"snapshot_sha256": "a" * 64},
        },
    }


class DuplicateReviewExportTests(unittest.TestCase):
    """Экспорт сохраняет байты и никогда не перезаписывает ручные заметки."""

    def setUp(self) -> None:
        """Подготовить временный проект с отдельным источником и результатом."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.snapshot = self.root / "manifests/results/snapshot_v1/snapshot.json"
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.write_text('{"source": "снимок"}\n', encoding="utf-8")
        self.output = self.root / "manifests/results/duplicates_v1"
        self.review = _review()

    def _export(
        self, output: Path | None = None, *, dry_run: bool = False,
    ) -> CandidateExportResult:
        """Экспортировать подготовленный пакет или выполнить пробную проверку."""

        return export_duplicate_review(
            self.root, output if output is not None else self.output, self.review,
            snapshot_path=self.snapshot, dry_run=dry_run,
        )

    def _tree_bytes(self) -> dict[str, bytes]:
        """Снять обычные файлы проекта для контроля отсутствия побочных изменений."""

        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def test_new_bundle_preserves_records_and_inputs(self) -> None:
        """Все четыре набора сохраняются без потери Unicode и изменения источника."""

        before = copy.deepcopy(self.review)
        original = self.snapshot.read_bytes()
        result = self._export()

        self.assertEqual(result.created_files, 6)
        self.assertEqual(result.unchanged_files, 0)
        self.assertEqual(set(result.hashes), OUTPUT_NAMES)
        self.assertEqual({path.name for path in self.output.iterdir()}, OUTPUT_NAMES)
        self.assertEqual(self.snapshot.read_bytes(), original)
        self.assertEqual(self.review, before)

        for name in ("cards", "groups", "pairs", "relations"):
            lines = (self.output / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(line) for line in lines], self.review[name])

        self.assertIn("«кавычками»", (self.output / "cards.jsonl").read_text(encoding="utf-8"))

    def test_summary_hashes_match_every_other_output(self) -> None:
        """Сводка связывает точные файлы и не объявляет автоматическое объединение."""

        result = self._export()
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))

        self.assertEqual(summary["provenance"], self.review["summary"]["provenance"])
        self.assertFalse(summary["training_ready"])
        self.assertFalse(summary["export"]["automatic_merge"])
        self.assertFalse(summary["export"]["registry_changes"])
        self.assertEqual(set(summary["export"]["files"]), OUTPUT_NAMES - {"summary.json"})

        for name, record in summary["export"]["files"].items():
            data = (self.output / name).read_bytes()
            self.assertEqual(record["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(record["bytes"], len(data))

        for name, digest in result.hashes.items():
            self.assertEqual(digest, hashlib.sha256((self.output / name).read_bytes()).hexdigest())

    def test_exact_repeat_does_not_write_or_change_timestamps(self) -> None:
        """Полный совпадающий пакет проверяется без mkdir и без обновления времени."""

        first = self._export()
        original = self._tree_bytes()
        timestamps = {path.name: path.stat().st_mtime_ns for path in self.output.iterdir()}

        with patch.object(Path, "mkdir", side_effect=AssertionError("Запись запрещена")):
            repeated = self._export()

        self.assertEqual(repeated.created_files, 0)
        self.assertEqual(repeated.unchanged_files, 6)
        self.assertEqual(repeated.hashes, first.hashes)
        self.assertEqual(self._tree_bytes(), original)
        self.assertEqual(
            {path.name: path.stat().st_mtime_ns for path in self.output.iterdir()}, timestamps,
        )

    def test_dry_run_does_not_create_missing_parents(self) -> None:
        """Пробный запуск вычисляет хеши, но не создаёт даже родительские каталоги."""

        output = self.root / "manifests/results/new/nested/review_v1"
        before = self._tree_bytes()

        with patch.object(Path, "mkdir", side_effect=AssertionError("Запись запрещена")):
            result = self._export(output, dry_run=True)

        self.assertEqual(result.created_files, 0)
        self.assertEqual(result.unchanged_files, 0)
        self.assertEqual(set(result.hashes), OUTPUT_NAMES)
        self.assertFalse((self.root / "manifests/results/new").exists())
        self.assertEqual(self._tree_bytes(), before)

    def test_dry_run_on_exact_repeat_is_read_only(self) -> None:
        """Пробный повтор проверяет все шесть существующих файлов без записи."""

        first = self._export()
        before = self._tree_bytes()

        with patch.object(Path, "mkdir", side_effect=AssertionError("Запись запрещена")):
            repeated = self._export(dry_run=True)

        self.assertEqual(repeated.hashes, first.hashes)
        self.assertEqual(repeated.unchanged_files, 6)
        self.assertEqual(self._tree_bytes(), before)

    def test_manual_decision_refuses_normal_and_dry_run_repeat(self) -> None:
        """Правка решения остаётся нетронутой при любом варианте повторного запуска."""

        self._export()
        (self.output / "pairs.jsonl").write_text("Ручное решение\n", encoding="utf-8")
        before = self._tree_bytes()

        for dry_run in (False, True):
            with (
                self.subTest(dry_run=dry_run),
                self.assertRaisesRegex(ValueError, "правки просмотра не перезаписываются"),
            ):
                self._export(dry_run=dry_run)

        self.assertEqual(self._tree_bytes(), before)

    def test_changed_last_file_fails_before_any_write(self) -> None:
        """Несовпадающая сводка не вызывает перезапись совпадающих наборов."""

        self._export()
        self.review["summary"]["new_note"] = "Другая версия"
        before = self._tree_bytes()

        with (
            patch.object(Path, "mkdir", side_effect=AssertionError("Запись запрещена")),
            self.assertRaisesRegex(ValueError, "Существующий файл отличается"),
        ):
            self._export()

        self.assertEqual(self._tree_bytes(), before)

    def test_incomplete_or_extra_files_are_preserved(self) -> None:
        """Неполный пакет и каталог с чужими заметками не дополняются автоматически."""

        self.output.mkdir()
        (self.output / "notes.txt").write_text("Моя заметка", encoding="utf-8")
        before = self._tree_bytes()

        with self.assertRaisesRegex(ValueError, "не является полным пакетом"):
            self._export()

        self.assertEqual(self._tree_bytes(), before)

        self._export(self.root / "manifests/results/complete_v1")
        self.output = self.root / "manifests/results/complete_v1"
        (self.output / "notes.txt").write_text("Ещё заметка", encoding="utf-8")
        before = self._tree_bytes()

        with self.assertRaisesRegex(ValueError, "не является полным пакетом"):
            self._export(dry_run=True)

        self.assertEqual(self._tree_bytes(), before)

    def test_unsafe_paths_refused_before_any_write(self) -> None:
        """Корневые, внешние и пересекающиеся со снимком пути недопустимы."""

        before = self._tree_bytes()

        for output in (
            self.root / "data/results", self.root / "manifests/results",
            self.root / "manifests/results/../unsafe", self.root.parent / "outside",
            self.snapshot.parent, self.snapshot.parent / "nested",
        ):
            for dry_run in (False, True):
                with self.subTest(output=output, dry_run=dry_run), self.assertRaises(ValueError):
                    self._export(output, dry_run=dry_run)

        self.assertEqual(self._tree_bytes(), before)

    def test_output_ancestor_of_snapshot_is_refused(self) -> None:
        """Результат не может охватывать каталог исходного снимка сверху."""

        self.snapshot = self.snapshot.parent / "nested/snapshot.json"

        with self.assertRaisesRegex(ValueError, "пересекаться"):
            self._export(self.snapshot.parent.parent, dry_run=True)

    def test_symlink_in_output_path_or_file_is_refused(self) -> None:
        """Ссылки не разрешаются даже при целевом пути внутри проекта."""

        linked = self.root / "manifests/results/linked"
        linked.symlink_to(self.snapshot.parent, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "Символическая ссылка"):
            self._export(linked / "review", dry_run=True)

        self._export()
        (self.output / "cards.jsonl").unlink()
        (self.output / "cards.jsonl").symlink_to(self.snapshot)

        with self.assertRaisesRegex(ValueError, "небезопасен"):
            self._export()

    def test_file_instead_of_output_directory_is_preserved(self) -> None:
        """Файл на месте нового каталога не удаляется и не заменяется."""

        self.output.write_text("Не удалять", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "занят файлом"):
            self._export()

        self.assertEqual(self.output.read_text(encoding="utf-8"), "Не удалять")

    def test_file_instead_of_parent_is_refused_in_dry_run(self) -> None:
        """Пробный запуск проверяет конфликт родительского пути так же, как обычный."""

        parent = self.output.parent / "occupied"
        parent.write_text("Занято", encoding="utf-8")

        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), self.assertRaisesRegex(ValueError, "занят файлом"):
                self._export(parent / "new/review_v1", dry_run=dry_run)

        self.assertEqual(parent.read_text(encoding="utf-8"), "Занято")

    def test_relative_output_resolves_against_project_root(self) -> None:
        """Относительное назначение не зависит от текущего каталога процесса."""

        result = self._export(Path("manifests/results/duplicates_v1"))
        self.assertEqual(result.output_dir, self.output)
        self.assertTrue((self.output / "summary.json").is_file())

    def test_empty_lists_export_without_inventing_records(self) -> None:
        """Пустые наборы получают пустые JSONL и честную памятку."""

        for name in ("cards", "groups", "pairs", "relations"):
            self.review[name] = []

        self._export()

        for name in ("cards", "groups", "pairs", "relations"):
            self.assertEqual((self.output / f"{name}.jsonl").read_bytes(), b"")

        self.assertIn(
            "не список подтверждённых дублей",
            (self.output / "START_HERE.txt").read_text(encoding="utf-8"),
        )

    def test_invalid_payloads_fail_without_creating_directory(self) -> None:
        """Некорректные JSON-структуры и служебный export не оставляют файлы."""

        for key, value in (
            ("summary", None), ("summary", {"export": {}}),
            ("cards", {}), ("pairs", [None]), ("relations", [{"value": float("nan")}]),
        ):
            with self.subTest(key=key, value=value):
                self.review = _review()
                self.review[key] = value

                with self.assertRaises(ValueError):
                    self._export()

                self.assertFalse(self.output.exists())


class DuplicateReviewCommandTests(unittest.TestCase):
    """CLI работает только с локальными источниками и явным каталогом версии."""

    def setUp(self) -> None:
        """Подменить построение, оставив действующий безопасный экспорт."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.snapshot_path = self.root / prepare_h2_duplicate_review.DEFAULT_SNAPSHOT
        self.snapshot_path.parent.mkdir(parents=True)
        self.snapshot_path.write_text("{}\n", encoding="utf-8")
        self.output_dir = self.root / "manifests/results/duplicates_v1"
        self.registry = self.root / "manifests/works.jsonl"
        self.registry.write_text("Неизменный реестр\n", encoding="utf-8")
        self.snapshot = object()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.output = self.patches.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        self.errors = self.patches.enter_context(patch("sys.stderr", new_callable=io.StringIO))
        self.reader = self.patches.enter_context(patch.object(
            prepare_h2_duplicate_review, "read_candidate_snapshot", return_value=self.snapshot,
        ))
        self.builder = self.patches.enter_context(patch.object(
            prepare_h2_duplicate_review, "build_duplicate_review", return_value=_review(),
        ))

    def test_default_snapshot_and_registry_are_preserved(self) -> None:
        """Команда использует закреплённый снимок, не изменяя исходник и реестр."""

        original = self.registry.read_bytes(), self.snapshot_path.read_bytes()
        result = prepare_h2_duplicate_review.main(
            ["--output-dir", str(self.output_dir)], project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.reader.assert_called_once_with(self.root, self.snapshot_path)
        self.builder.assert_called_once_with(self.snapshot)
        self.assertEqual((self.registry.read_bytes(), self.snapshot_path.read_bytes()), original)
        self.assertIn("Групп совпадений: 1; пар для проверки: 1", self.output.getvalue())
        self.assertIn("не подтверждённые дубли", self.output.getvalue())
        self.assertIn("Создано файлов: 6", self.output.getvalue())

    def test_output_directory_is_required(self) -> None:
        """Без назначения результата источники даже не читаются."""

        with self.assertRaises(SystemExit) as exception:
            prepare_h2_duplicate_review.main([], project_root=self.root)

        self.assertEqual(exception.exception.code, 2)
        self.reader.assert_not_called()
        self.builder.assert_not_called()

    def test_relative_snapshot_uses_project_root(self) -> None:
        """Явный относительный путь разрешается относительно проекта."""

        relative = Path("manifests/results/other/snapshot.json")
        prepare_h2_duplicate_review.main(
            ["--snapshot", str(relative), "--output-dir", str(self.output_dir), "--dry-run"],
            project_root=self.root,
        )

        self.reader.assert_called_once_with(self.root, self.root / relative)
        self.assertFalse(self.output_dir.exists())
        self.assertIn("Пробный запуск", self.output.getvalue())

    def test_invalid_snapshot_stops_before_export(self) -> None:
        """Несовпадающие свидетельства прекращают запуск до создания каталога."""

        self.reader.side_effect = ValueError("SHA-256 не совпадает")

        with self.assertRaises(SystemExit) as exception:
            prepare_h2_duplicate_review.main(
                ["--output-dir", str(self.output_dir)], project_root=self.root,
            )

        self.assertEqual(exception.exception.code, 2)
        self.assertIn("SHA-256 не совпадает", self.errors.getvalue())
        self.builder.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_unsafe_output_is_rejected_even_in_dry_run(self) -> None:
        """Пробный запуск не игнорирует конфликт с исходным каталогом."""

        with self.assertRaises(SystemExit) as exception:
            prepare_h2_duplicate_review.main(
                ["--output-dir", str(self.snapshot_path.parent), "--dry-run"],
                project_root=self.root,
            )

        self.assertEqual(exception.exception.code, 2)
        self.assertIn("пересекаться", self.errors.getvalue())
        self.assertEqual(self.snapshot_path.read_bytes(), b"{}\n")

    def test_network_merge_and_force_switches_do_not_exist(self) -> None:
        """Команда не позволяет скачивать, применять решения или перезаписывать."""

        for flag in ("--download", "--network", "--merge", "--apply", "--force"):
            with self.subTest(flag=flag):
                with self.assertRaises(SystemExit) as exception:
                    prepare_h2_duplicate_review.main(
                        ["--output-dir", str(self.output_dir), flag], project_root=self.root,
                    )

                self.assertEqual(exception.exception.code, 2)

        self.reader.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_cli_repeat_reports_no_new_files(self) -> None:
        """Повторная команда проверяет прежние байты без создания новой версии."""

        arguments = ["--output-dir", str(self.output_dir)]
        prepare_h2_duplicate_review.main(arguments, project_root=self.root)
        before = {path.name: path.read_bytes() for path in self.output_dir.iterdir()}
        prepare_h2_duplicate_review.main(arguments, project_root=self.root)

        self.assertEqual({path.name: path.read_bytes() for path in self.output_dir.iterdir()}, before)
        self.assertIn("Создано файлов: 0; без изменений: 6", self.output.getvalue())


class DuplicateReviewIntegrationTests(unittest.TestCase):
    """Проверки полного пути от закреплённых ответов API до локального пакета."""

    def test_pipeline_preserves_sources_and_repeat_is_read_only(self) -> None:
        """Реальные reader, builder и export сохраняют происхождение и пустые решения."""

        fixture = test_h2_candidate_input.CandidateInputTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.path = fixture.root / "manifests/results/source_snapshot/snapshot.json"
        fixture.path.parent.mkdir()
        fixture.items[0]["metadata"]["local.relation.has-preprint"] = [{
            "value": test_h2_candidate_input.SECOND_UUID,
        }]
        fixture.replace_page(0)
        original = {path: path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()}
        output_dir = fixture.root / "manifests/results/review_v1"
        arguments = ["--snapshot", str(fixture.path), "--output-dir", str(output_dir)]
        output = io.StringIO()

        with redirect_stdout(output), patch(
            "urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена"),
        ):
            result = prepare_h2_duplicate_review.main(arguments, project_root=fixture.root)

        self.assertEqual(result, 0)
        report = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(
            report["provenance"]["snapshot_sha256"], hashlib.sha256(fixture.path.read_bytes()).hexdigest(),
        )
        self.assertFalse(report["training_ready"])
        self.assertEqual(report["relation_record_count"], 1)
        self.assertEqual(report["pair_count"], 1)
        pairs = [
            json.loads(line)
            for line in (output_dir / "pairs.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertIsNone(pairs[0]["review_decision"])
        self.assertEqual(pairs[0]["comment"], "")
        self.assertEqual(set(pairs[0]["item_uuids"]), {
            test_h2_candidate_input.FIRST_UUID, test_h2_candidate_input.SECOND_UUID,
        })
        created = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in output_dir.iterdir()}

        with redirect_stdout(output), patch.object(
            Path, "mkdir", side_effect=AssertionError("Запись запрещена"),
        ):
            prepare_h2_duplicate_review.main(arguments, project_root=fixture.root)
            prepare_h2_duplicate_review.main(arguments + ["--dry-run"], project_root=fixture.root)

        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual(
            {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in output_dir.iterdir()}, created,
        )


if __name__ == "__main__":
    unittest.main()
