"""Проверки безопасных режимов команды регистрации журнальных статей ОИЯИ."""

from __future__ import annotations

import io
import tempfile
import unittest

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from scripts import register_jinr_articles
from src.corpus.manifests import ManifestConcurrencyError, ManifestError, ManifestPlan


class RegisterJinrArticlesCommandTests(unittest.TestCase):
    """Проверки явной записи, разделения прав и повторной сверки снимков."""

    def setUp(self) -> None:
        """Создать изолированный проект и подменить проверку пакета и хранилище."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.manifests = self.root / "manifests"
        self.imports = self.manifests / "imports"
        self.imports.mkdir(parents=True)
        self.batch_path = self.imports / "articles.json"
        self.batch_path.write_text("{}\n", encoding="utf-8")
        (self.manifests / "works.jsonl").write_text("исходные записи\n", encoding="utf-8")
        self.batch = MagicMock(
            entries=(
                MagicMock(entry={
                    "genre": None,
                    "journal_id": None,
                    "pdf": {
                        "path": "data/raw/first.pdf",
                        "aliases": ["data/raw/first.pdf", "data/raw/first-copy.pdf"],
                    },
                }),
                MagicMock(entry={
                    "genre": "research_article",
                    "journal_id": "synthetic_journal",
                    "pdf": {
                        "path": "data/raw/second.pdf",
                        "aliases": ["data/raw/second.pdf"],
                    },
                }),
            ),
        )
        self.plan = ManifestPlan()
        self.reconciled = ManifestPlan()
        self.expected_hashes = {"works": "a" * 64}

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.output = self.patches.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        self.errors = self.patches.enter_context(patch("sys.stderr", new_callable=io.StringIO))
        self.read_batch = self.patches.enter_context(
            patch.object(register_jinr_articles, "read_article_batch", return_value=self.batch)
        )
        self.plan_batch = self.patches.enter_context(
            patch.object(register_jinr_articles, "plan_article_batch", return_value=self.plan)
        )
        self.store_type = self.patches.enter_context(
            patch.object(register_jinr_articles, "ManifestStore")
        )
        self.store = self.store_type.return_value
        self.store.commit.return_value = MagicMock(dry_run=True, inserted={}, updated={})
        self.reconcile = self.patches.enter_context(
            patch.object(
                register_jinr_articles, "reconcile_article_plan",
                return_value=(self.reconciled, self.expected_hashes),
            )
        )

    def _snapshot(self) -> dict[str, bytes]:
        """Снять содержимое временного проекта для проверки отсутствия записи."""

        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def _planning_arguments(self) -> list[str]:
        """Передать независимые явные списки прав на PDF и метаданные."""

        return [
            str(self.batch_path),
            "--rights-record-id", "pdf-acquisition",
            "--rights-record-id", "pdf-storage",
            "--metadata-rights-record-id", "metadata-acquisition",
            "--metadata-rights-record-id", "metadata-storage",
        ]

    def test_check_only_needs_no_rights_and_does_not_open_store(self) -> None:
        """Проверка происхождения не планирует регистрацию и не открывает реестры."""

        before = self._snapshot()

        result = register_jinr_articles.main(
            [str(self.batch_path), "--check-files-only"], project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.assertEqual(self._snapshot(), before)
        self.read_batch.assert_called_once_with(self.root, self.batch_path)
        self.plan_batch.assert_not_called()
        self.store_type.assert_not_called()
        self.reconcile.assert_not_called()
        self.assertIn("статьи: 2; путей PDF: 3", self.output.getvalue())
        self.assertIn("genre — 1; journal_id — 1", self.output.getvalue())
        self.assertIn("pending", self.output.getvalue())

    def test_check_only_remains_read_only_with_commit_flag(self) -> None:
        """Проверка файлов сохраняет свой режим даже с указанием --commit."""

        before = self._snapshot()

        result = register_jinr_articles.main(
            [str(self.batch_path), "--check-files-only", "--commit"],
            project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.assertEqual(self._snapshot(), before)
        self.plan_batch.assert_not_called()
        self.store_type.assert_not_called()

    def test_planning_requires_both_explicit_rights_lists(self) -> None:
        """Отсутствующий список прав останавливает команду до чтения пакета."""

        before = self._snapshot()

        for arguments in (
            [],
            ["--rights-record-id", "pdf-right"],
            ["--metadata-rights-record-id", "metadata-right"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit) as exception:
                    register_jinr_articles.main(
                        [str(self.batch_path), *arguments], project_root=self.root,
                    )

                self.assertEqual(exception.exception.code, 2)

        self.assertEqual(self._snapshot(), before)
        self.read_batch.assert_not_called()
        self.plan_batch.assert_not_called()
        self.store_type.assert_not_called()
        self.assertIn(
            "нужны --rights-record-id и --metadata-rights-record-id", self.errors.getvalue(),
        )

    def test_one_input_batch_is_required(self) -> None:
        """Команда не выбирает пакет сама и не принимает список входных пакетов."""

        for arguments in (
            ["--check-files-only"],
            [str(self.batch_path), str(self.batch_path), "--check-files-only"],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit) as exception:
                    register_jinr_articles.main(arguments, project_root=self.root)

                self.assertEqual(exception.exception.code, 2)

        self.read_batch.assert_not_called()
        self.store_type.assert_not_called()

    def test_cards_export_is_not_available(self) -> None:
        """Регистратор статей не принимает параметр экспорта карточек препринтов."""

        with self.assertRaises(SystemExit) as exception:
            register_jinr_articles.main(
                [str(self.batch_path), "--check-files-only", "--cards-output", "cards.jsonl"],
                project_root=self.root,
            )

        self.assertEqual(exception.exception.code, 2)
        self.read_batch.assert_not_called()
        self.store_type.assert_not_called()

    def test_invalid_batch_has_readable_error_and_no_partial_registration(self) -> None:
        """Повреждённое свидетельство не запускает планирование и регистрацию."""

        before = self._snapshot()
        self.read_batch.side_effect = ValueError("Контрольная сумма PDF не совпадает")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_articles.main(
                self._planning_arguments() + ["--commit"], project_root=self.root,
            )

        self.assertEqual(exception.exception.code, 2)
        self.assertIn("Контрольная сумма PDF не совпадает", self.errors.getvalue())
        self.assertNotIn("Traceback", self.errors.getvalue())
        self.assertEqual(self._snapshot(), before)
        self.plan_batch.assert_not_called()
        self.store_type.assert_not_called()

    def test_missing_batch_has_readable_error(self) -> None:
        """Ошибка файловой системы переводится в понятную ошибку команды."""

        self.read_batch.side_effect = FileNotFoundError("Пакет статей не найден")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_articles.main(
                [str(self.batch_path), "--check-files-only"], project_root=self.root,
            )

        self.assertEqual(exception.exception.code, 2)
        self.assertIn("Не удалось обработать пакет статей", self.errors.getvalue())
        self.store_type.assert_not_called()

    def test_default_mode_is_dry_run(self) -> None:
        """Без явного --commit хранилище получает только предварительную проверку."""

        before = self._snapshot()

        result = register_jinr_articles.main(self._planning_arguments(), project_root=self.root)

        self.assertEqual(result, 0)
        self.assertEqual(self._snapshot(), before)
        self.store.commit.assert_called_once_with(
            self.reconciled, dry_run=True, expected_snapshot_hashes=self.expected_hashes,
        )
        self.assertIn("Предварительная проверка завершена", self.output.getvalue())

    def test_explicit_dry_run_uses_read_only_commit(self) -> None:
        """Параметр --dry-run сохраняет тот же безопасный режим хранилища."""

        result = register_jinr_articles.main(
            self._planning_arguments() + ["--dry-run"], project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.store.commit.assert_called_once_with(
            self.reconciled, dry_run=True, expected_snapshot_hashes=self.expected_hashes,
        )

    def test_only_explicit_commit_allows_registry_write(self) -> None:
        """Только --commit передаёт хранилищу разрешение записи."""

        self.store.commit.return_value.dry_run = False

        result = register_jinr_articles.main(
            self._planning_arguments() + ["--commit"], project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.store.commit.assert_called_once_with(
            self.reconciled, dry_run=False, expected_snapshot_hashes=self.expected_hashes,
        )
        self.assertIn("Регистрация завершена", self.output.getvalue())

    def test_commit_and_dry_run_are_mutually_exclusive(self) -> None:
        """Противоречивый выбор записи и проверки отвергается до чтения пакета."""

        with self.assertRaises(SystemExit) as exception:
            register_jinr_articles.main(
                self._planning_arguments() + ["--commit", "--dry-run"], project_root=self.root,
            )

        self.assertEqual(exception.exception.code, 2)
        self.read_batch.assert_not_called()
        self.store_type.assert_not_called()

    def test_planning_preserves_manual_pdf_origin_and_separate_metadata_rights(self) -> None:
        """План получает исходное ручное получение PDF и отдельный набор прав API."""

        register_jinr_articles.main(self._planning_arguments(), project_root=self.root)

        self.plan_batch.assert_called_once()
        self.assertEqual(self.plan_batch.call_args.args[:2], (self.root, self.batch_path))
        options = self.plan_batch.call_args.args[2]
        self.assertEqual(options.content_role, "full_text")
        self.assertEqual(options.acquisition_method, "manual_download")
        self.assertEqual(options.acquisition_scope, "bulk")
        self.assertEqual(options.response_representation, "pdf")
        self.assertEqual(options.request_context_type, "work")
        self.assertEqual(options.extraction_method, "not_started")
        self.assertEqual(options.rights_record_ids, ("pdf-acquisition", "pdf-storage"))
        self.assertEqual(
            self.plan_batch.call_args.kwargs["metadata_rights_record_ids"],
            ("metadata-acquisition", "metadata-storage"),
        )
        self.assertTrue(self.plan_batch.call_args.kwargs["collected_at"])
        self.store_type.assert_called_once_with(
            project_root=self.root,
            manifest_dir=self.manifests,
            schema_dir=self.manifests / "schemas",
        )
        self.reconcile.assert_called_once_with(self.store, self.plan)

    def test_incomplete_classification_stops_before_store_creation(self) -> None:
        """Незаполненные решения по статье не создают частичный план записи."""

        self.plan_batch.side_effect = ValueError("Не заполнены genre и journal_id")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_articles.main(self._planning_arguments(), project_root=self.root)

        self.assertEqual(exception.exception.code, 2)
        self.assertIn("Не заполнены genre и journal_id", self.errors.getvalue())
        self.store_type.assert_not_called()

    def test_plan_warnings_are_displayed(self) -> None:
        """Ограничения локального сопоставления PDF остаются видны при планировании."""

        warning = "Сопоставление локальное; серверная связь bitstream→item не проверена"
        self.plan.warnings.append(warning)

        result = register_jinr_articles.main(self._planning_arguments(), project_root=self.root)

        self.assertEqual(result, 0)
        self.assertIn(f"Предупреждение: {warning}", self.output.getvalue())

    def test_denied_rights_stop_before_commit(self) -> None:
        """Отказ прав на сверке не приводит к попытке записи пакета."""

        before = self._snapshot()
        self.reconcile.side_effect = ManifestError("Нет применимого разрешения на хранение")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_articles.main(
                self._planning_arguments() + ["--commit"], project_root=self.root,
            )

        self.assertEqual(exception.exception.code, 2)
        self.assertIn("Нет применимого разрешения на хранение", self.errors.getvalue())
        self.assertEqual(self._snapshot(), before)
        self.store.commit.assert_not_called()

    def test_concurrency_retries_with_fresh_reconciliation(self) -> None:
        """После гонки используется новый согласованный план и новый хеш снимка."""

        second_plan = ManifestPlan()
        second_hashes = {"works": "b" * 64}
        self.reconcile.side_effect = [
            (self.reconciled, self.expected_hashes),
            (second_plan, second_hashes),
        ]
        self.store.commit.side_effect = [
            ManifestConcurrencyError("Снимок изменился"),
            MagicMock(dry_run=False, inserted={}, updated={}),
        ]

        result = register_jinr_articles.main(
            self._planning_arguments() + ["--commit"], project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.assertEqual(self.reconcile.call_count, 2)
        self.plan_batch.assert_called_once()
        self.assertEqual(self.store.commit.call_args_list, [
            call(self.reconciled, dry_run=False, expected_snapshot_hashes=self.expected_hashes),
            call(second_plan, dry_run=False, expected_snapshot_hashes=second_hashes),
        ])

    def test_concurrency_is_limited_to_three_attempts(self) -> None:
        """Непрерывная гонка завершается понятной ошибкой после третьей попытки."""

        self.store.commit.side_effect = ManifestConcurrencyError("Снимок снова изменился")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_articles.main(self._planning_arguments(), project_root=self.root)

        self.assertEqual(exception.exception.code, 2)
        self.assertEqual(self.reconcile.call_count, 3)
        self.assertEqual(self.store.commit.call_count, 3)
        self.assertIn("Снимок снова изменился", self.errors.getvalue())

    def test_non_concurrency_error_is_not_retried(self) -> None:
        """Обычная ошибка целостности не маскируется повторными записями."""

        self.store.commit.side_effect = ManifestError("Конфликт содержимого артефакта")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_articles.main(self._planning_arguments(), project_root=self.root)

        self.assertEqual(exception.exception.code, 2)
        self.assertEqual(self.reconcile.call_count, 1)
        self.assertEqual(self.store.commit.call_count, 1)


if __name__ == "__main__":
    unittest.main()
