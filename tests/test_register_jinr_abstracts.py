"""Проверки безопасных режимов офлайн-команды регистрации тезисов ОИЯИ."""

from __future__ import annotations

import io
import tempfile
import unittest

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import register_jinr_abstracts
from src.corpus.jinr_abstract_input import read_abstract_batch
from src.corpus.manifests import ManifestConcurrencyError, ManifestError, ManifestPlan
from tests.jinr_abstract_fixtures import AbstractFixture


class RegisterJinrAbstractsCommandTests(unittest.TestCase):
    """Проверить явную запись, раздельные права и отсутствие сетевых обращений."""

    def setUp(self) -> None:
        """Подменить план и хранилище, изолировав команду во временном проекте."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.path = self.root / "abstracts.json"
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
        self.network = patches.enter_context(patch(
            "urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена"),
        ))
        self.read_batch = patches.enter_context(patch.object(
            register_jinr_abstracts, "read_abstract_batch", return_value=self.batch,
        ))
        self.plan_batch = patches.enter_context(patch.object(
            register_jinr_abstracts, "plan_abstract_batch", return_value=self.plan,
        ))
        self.store_type = patches.enter_context(patch.object(register_jinr_abstracts, "ManifestStore"))
        self.store = self.store_type.return_value
        self.store.commit.return_value = MagicMock(dry_run=True, inserted={}, updated={})
        self.reconcile = patches.enter_context(patch.object(
            register_jinr_abstracts, "reconcile_abstract_plan",
            return_value=(self.reconciled, self.expected_hashes),
        ))

    def _arguments(self) -> list[str]:
        """Передать явные независимые права на ручные PDF и метаданные API."""

        return [
            str(self.path), "--rights-record-id", "pdf-right",
            "--metadata-rights-record-id", "api-right",
        ]

    def test_check_files_only_never_opens_store_even_with_commit(self) -> None:
        """Проверка свидетельств без прав не планирует и не записывает реестры."""

        result = register_jinr_abstracts.main(
            [str(self.path), "--check-files-only", "--commit"], project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.read_batch.assert_called_once_with(self.root, self.path)
        self.plan_batch.assert_not_called()
        self.store_type.assert_not_called()
        self.network.assert_not_called()
        self.assertIn("тезисы: 1; путей PDF: 2", self.output.getvalue())
        self.assertIn("pending", self.output.getvalue())

    def test_actual_file_check_keeps_history_and_uses_no_network(self) -> None:
        """Команда проверяет настоящий синтетический пакет, сохраняя байты всех файлов."""

        fixture = AbstractFixture(self.root)
        self.read_batch.side_effect = read_abstract_batch
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        result = register_jinr_abstracts.main(
            [str(fixture.path), "--check-files-only"], project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        self.network.assert_not_called()
        self.store_type.assert_not_called()

    def test_default_and_explicit_dry_run_only_check_plan(self) -> None:
        """Оба предварительных режима применяют отдельный согласователь без записи."""

        for extra in ([], ["--dry-run"]):
            with self.subTest(extra=extra):
                self.store.commit.reset_mock()
                self.reconcile.reset_mock()
                result = register_jinr_abstracts.main(self._arguments() + extra, project_root=self.root)

                self.assertEqual(result, 0)
                self.reconcile.assert_called_once_with(self.store, self.plan)
                self.store.commit.assert_called_once_with(
                    self.reconciled, dry_run=True, expected_snapshot_hashes=self.expected_hashes,
                )

        self.network.assert_not_called()
        self.assertIn("Предварительная проверка завершена", self.output.getvalue())

    def test_commit_must_be_explicit(self) -> None:
        """Запись разрешается только явным параметром --commit."""

        self.store.commit.return_value.dry_run = False
        register_jinr_abstracts.main(self._arguments() + ["--commit"], project_root=self.root)

        self.store.commit.assert_called_once_with(
            self.reconciled, dry_run=False, expected_snapshot_hashes=self.expected_hashes,
        )
        self.assertIn("Регистрация завершена", self.output.getvalue())

    def test_rights_and_manual_provenance_stay_separate(self) -> None:
        """Профиль тезисов не меняет ручное происхождение и не заимствует чужие права."""

        register_jinr_abstracts.main(self._arguments(), project_root=self.root)
        options = self.plan_batch.call_args.args[2]

        self.assertEqual(options.rights_record_ids, ("pdf-right",))
        self.assertEqual(options.acquisition_method, "manual_download")
        self.assertEqual(options.acquisition_scope, "bulk")
        self.assertEqual(options.response_representation, "pdf")
        self.assertEqual(options.extraction_version, "jinr-abstract-pdf-v1")
        self.assertEqual(self.plan_batch.call_args.kwargs["metadata_rights_record_ids"], ("api-right",))

    def test_missing_rights_or_conflicting_modes_stop_before_reading(self) -> None:
        """Неполные права и противоречивые режимы не запускают обработку."""

        for arguments in (
            [str(self.path)],
            [str(self.path), "--rights-record-id", "pdf-right"],
            [str(self.path), "--metadata-rights-record-id", "api-right"],
            self._arguments() + ["--dry-run", "--commit"],
        ):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as exception:
                register_jinr_abstracts.main(arguments, project_root=self.root)

            self.assertEqual(exception.exception.code, 2)

        self.read_batch.assert_not_called()
        self.store_type.assert_not_called()

    def test_invalid_approval_stops_before_partial_plan(self) -> None:
        """Отсутствующее согласие останавливает команду до открытия хранилища."""

        self.read_batch.side_effect = ValueError("Отсутствует genre_approval")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_abstracts.main(self._arguments() + ["--commit"], project_root=self.root)

        self.assertEqual(exception.exception.code, 2)
        self.assertIn("genre_approval", self.errors.getvalue())
        self.plan_batch.assert_not_called()
        self.store_type.assert_not_called()

    def test_conflict_prevents_commit(self) -> None:
        """Неразрешённый конфликт останавливает регистрацию тезисов."""

        self.reconcile.side_effect = ManifestError("Конфликт названия сборника")

        with self.assertRaises(SystemExit) as exception:
            register_jinr_abstracts.main(self._arguments() + ["--commit"], project_root=self.root)

        self.assertEqual(exception.exception.code, 2)
        self.store.commit.assert_not_called()

    def test_concurrency_retries_reconciliation(self) -> None:
        """Изменение снимка вызывает новую сверку отдельного плана тезисов."""

        self.store.commit.side_effect = [
            ManifestConcurrencyError("Изменился снимок"),
            MagicMock(dry_run=False, inserted={}, updated={}),
        ]
        result = register_jinr_abstracts.main(self._arguments() + ["--commit"], project_root=self.root)

        self.assertEqual(result, 0)
        self.assertEqual(self.reconcile.call_count, 2)
        self.assertEqual(self.store.commit.call_count, 2)


if __name__ == "__main__":
    unittest.main()
