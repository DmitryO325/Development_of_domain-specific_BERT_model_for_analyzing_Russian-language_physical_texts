"""Проверки безопасных режимов команды регистрации материалов сборников ОИЯИ."""

from __future__ import annotations

import io
import tempfile
import unittest

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import register_jinr_collections
from src.corpus.manifests import ManifestConcurrencyError, ManifestError, ManifestPlan


class RegisterJinrCollectionsCommandTests(unittest.TestCase):
    """Проверки раздельных прав, явной записи и повторной сверки снимков."""

    def setUp(self) -> None:
        """Создать временный проект и подменить чтение пакета и хранилище."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.path = self.root / "collections.json"
        self.batch = MagicMock(entries=(MagicMock(
            pdf_path="data/raw/test.pdf",
            entry={"pdf": {"aliases": ["data/raw/test.pdf", "data/raw/test-copy.pdf"]}},
        ),))
        self.plan = ManifestPlan()
        self.reconciled = ManifestPlan()
        self.expected_hashes = {"works": "a" * 64}
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.output = patches.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        self.errors = patches.enter_context(patch("sys.stderr", new_callable=io.StringIO))
        self.read_batch = patches.enter_context(patch.object(
            register_jinr_collections, "read_collection_batch", return_value=self.batch,
        ))
        self.plan_batch = patches.enter_context(patch.object(
            register_jinr_collections, "plan_collection_batch", return_value=self.plan,
        ))
        self.store_type = patches.enter_context(patch.object(register_jinr_collections, "ManifestStore"))
        self.store = self.store_type.return_value
        self.store.commit.return_value = MagicMock(dry_run=True, inserted={}, updated={})
        self.reconcile = patches.enter_context(patch.object(
            register_jinr_collections, "reconcile_collection_plan",
            return_value=(self.reconciled, self.expected_hashes),
        ))

    def _arguments(self) -> list[str]:
        """Передать явные независимые права на PDF и метаданные API."""

        return [
            str(self.path), "--rights-record-id", "pdf-right",
            "--metadata-rights-record-id", "api-right",
        ]

    def test_check_files_only_does_not_plan_or_open_store(self) -> None:
        """Проверка файлов без прав не создаёт реестры даже вместе с --commit."""

        result = register_jinr_collections.main(
            [str(self.path), "--check-files-only", "--commit"], project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.read_batch.assert_called_once_with(self.root, self.path)
        self.plan_batch.assert_not_called()
        self.store_type.assert_not_called()
        self.assertIn("материалы сборников: 1; путей PDF: 2", self.output.getvalue())
        self.assertIn("pending", self.output.getvalue())

    def test_default_is_dry_run_with_collection_reconciler(self) -> None:
        """Без --commit общая транзакция только проверяет отдельный план сборников."""

        result = register_jinr_collections.main(self._arguments(), project_root=self.root)

        self.assertEqual(result, 0)
        self.reconcile.assert_called_once_with(self.store, self.plan)
        self.store.commit.assert_called_once_with(
            self.reconciled, dry_run=True, expected_snapshot_hashes=self.expected_hashes,
        )
        self.assertIn("Предварительная проверка завершена", self.output.getvalue())

    def test_commit_must_be_explicit(self) -> None:
        """Запись разрешается только явным параметром --commit."""

        self.store.commit.return_value.dry_run = False
        register_jinr_collections.main(self._arguments() + ["--commit"], project_root=self.root)

        self.store.commit.assert_called_once_with(
            self.reconciled, dry_run=False, expected_snapshot_hashes=self.expected_hashes,
        )
        self.assertIn("Регистрация завершена", self.output.getvalue())

    def test_rights_and_manual_provenance_are_separate(self) -> None:
        """Новый жанр не меняет ручное происхождение и не заимствует чужие разрешения."""

        register_jinr_collections.main(self._arguments(), project_root=self.root)
        options = self.plan_batch.call_args.args[2]

        self.assertEqual(options.rights_record_ids, ("pdf-right",))
        self.assertEqual(options.acquisition_method, "manual_download")
        self.assertEqual(options.acquisition_scope, "bulk")
        self.assertEqual(options.response_representation, "pdf")
        self.assertEqual(options.extraction_version, "jinr-collection-pdf-v1")
        self.assertEqual(
            self.plan_batch.call_args.kwargs["metadata_rights_record_ids"], ("api-right",),
        )

    def test_missing_rights_or_conflicting_modes_stop_before_reading(self) -> None:
        """Неполные права и противоречивые режимы не запускают обработку."""

        for arguments in (
            [str(self.path)],
            [str(self.path), "--rights-record-id", "pdf-right"],
            [str(self.path), "--metadata-rights-record-id", "api-right"],
            self._arguments() + ["--dry-run", "--commit"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as exception:
                register_jinr_collections.main(arguments, project_root=self.root)

            self.assertEqual(exception.exception.code, 2)

        self.read_batch.assert_not_called()
        self.store_type.assert_not_called()

    def test_invalid_batch_stops_without_partial_plan(self) -> None:
        """Неверный отбор или повреждённое свидетельство не открывают хранилище."""

        self.read_batch.side_effect = ValueError("Материал оказался тезисами")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_collections.main(self._arguments() + ["--commit"], project_root=self.root)

        self.assertEqual(exception.exception.code, 2)
        self.assertIn("Материал оказался тезисами", self.errors.getvalue())
        self.plan_batch.assert_not_called()
        self.store_type.assert_not_called()

    def test_conflict_prevents_commit(self) -> None:
        """Неразрешённый конфликт или неподходящие права останавливают запись."""

        self.reconcile.side_effect = ManifestError("Конфликт названия сборника")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_collections.main(self._arguments() + ["--commit"], project_root=self.root)

        self.assertEqual(exception.exception.code, 2)
        self.store.commit.assert_not_called()

    def test_concurrency_retries_reconciliation(self) -> None:
        """После изменения реестров заново согласуется отдельный план сборников."""

        self.store.commit.side_effect = [
            ManifestConcurrencyError("Изменился снимок"),
            MagicMock(dry_run=False, inserted={}, updated={}),
        ]
        result = register_jinr_collections.main(self._arguments() + ["--commit"], project_root=self.root)

        self.assertEqual(result, 0)
        self.assertEqual(self.reconcile.call_count, 2)
        self.assertEqual(self.store.commit.call_count, 2)


if __name__ == "__main__":
    unittest.main()
