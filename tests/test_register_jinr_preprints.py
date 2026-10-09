"""Проверки безопасных режимов команды регистрации препринтов ОИЯИ без сети."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest

from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from scripts import register_jinr_preprints
from src.corpus.local_registration import LocalFileRegistration
from src.corpus.manifests import ManifestPlan, canonical_json


class RegisterJinrPreprintsCommandTests(unittest.TestCase):
    """Проверки выходных файлов, предварительного режима и явных записей прав."""

    def setUp(self) -> None:
        """Создать временный проект и подменить только чтение проверенных описей."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.manifests = self.root / "manifests"
        self.imports = self.manifests / "imports"
        self.downloads = self.imports / "jinr_api/preprint_downloads"
        self.downloads.mkdir(parents=True)
        (self.downloads / "first.json").write_text("{}\n", encoding="utf-8")
        (self.manifests / "works.jsonl").write_text("исходные записи\n", encoding="utf-8")

        self.card = LocalFileRegistration(
            relative_path="data/raw/jinr_api/responses/aa/preprint.pdf",
            source_url="http://www1.jinr.ru/Preprints/2024/05(P11-2024-5).pdf",
            canonical_url="https://pubrepo.jinr.ru/entities/publication/11111111-2222-4333-8444-555555555555",
            retrieved_at="2026-09-28T12:00:00+00:00",
            title="Синтетический препринт",
            authors=["Иванов И. И."],
            doi=None,
            published_at=None,
            section=None,
            language="ru",
            genre="preprint",
            abstract="Синтетическая аннотация.",
            keywords=[],
            pacs_codes_raw=[],
            udc_codes_raw=[],
            acquisition_agent="NIR-corpus-bot/0.1",
            eligibility_status="pending",
            exclusion_reason=None,
        )

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.output = self.patches.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        self.errors = self.patches.enter_context(patch("sys.stderr", new_callable=io.StringIO))
        self.read_download = self.patches.enter_context(
            patch.object(register_jinr_preprints, "read_preprint_download")
        )
        self.cards = self.patches.enter_context(
            patch.object(register_jinr_preprints, "preprint_extraction_cards", return_value=[self.card])
        )
        self.plan_download = self.patches.enter_context(
            patch.object(register_jinr_preprints, "plan_preprint_download", return_value=ManifestPlan())
        )
        self.store_type = self.patches.enter_context(
            patch.object(register_jinr_preprints, "ManifestStore")
        )
        self.reconcile = self.patches.enter_context(
            patch.object(register_jinr_preprints, "reconcile_preprint_plan")
        )

    def _snapshot(self) -> dict[str, bytes]:
        """Снять содержимое всех файлов временного проекта для сравнения."""

        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }

    def test_check_only_requires_no_rights_and_leaves_registries_untouched(self) -> None:
        """Проверка файлов не открывает хранилище и не требует прав на регистрацию."""

        before = self._snapshot()

        result = register_jinr_preprints.main(["--check-files-only"], project_root=self.root)

        self.assertEqual(result, 0)
        self.assertEqual(self._snapshot(), before)
        self.read_download.assert_called_once_with(self.root, self.downloads / "first.json")
        self.plan_download.assert_not_called()
        self.store_type.assert_not_called()
        self.reconcile.assert_not_called()
        self.assertIn("допуск по правам не изменялись", self.output.getvalue())

    def test_check_only_exports_cards_without_changing_registry(self) -> None:
        """Экспорт карточек не превращает проверку файлов в регистрацию."""

        output_path = self.imports / "checked_preprints.jsonl"
        registry_before = (self.manifests / "works.jsonl").read_bytes()

        result = register_jinr_preprints.main(
            ["--check-files-only", "--cards-output", str(output_path)],
            project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output_path.read_text(encoding="utf-8")), asdict(self.card))
        self.assertEqual((self.manifests / "works.jsonl").read_bytes(), registry_before)
        self.store_type.assert_not_called()

    def test_check_only_dry_run_writes_nothing(self) -> None:
        """Предварительная проверка не создаёт даже выходную папку карточек."""

        before = self._snapshot()
        relative_output = "manifests/imports/new_directory/cards.jsonl"

        result = register_jinr_preprints.main(
            ["--check-files-only", "--dry-run", "--cards-output", relative_output],
            project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.assertEqual(self._snapshot(), before)
        self.assertFalse((self.imports / "new_directory").exists())
        self.store_type.assert_not_called()

    def test_registration_requires_both_rights_groups(self) -> None:
        """Регистрация без любого из двух явных наборов прав останавливается до чтения."""

        for arguments in (
            [],
            ["--rights-record-id", "pdf-right"],
            ["--metadata-rights-record-id", "metadata-right"],
        ):
            with self.subTest(arguments=arguments):
                before = self._snapshot()

                with self.assertRaises(SystemExit) as exception:
                    register_jinr_preprints.main(arguments, project_root=self.root)

                self.assertEqual(exception.exception.code, 2)
                self.assertEqual(self._snapshot(), before)

        self.read_download.assert_not_called()
        self.plan_download.assert_not_called()
        self.store_type.assert_not_called()

    def test_registration_dry_run_passes_flag_to_store_without_export(self) -> None:
        """Регистрационная проверка передаёт dry_run хранилищу и отдельно карточки не пишет."""

        before = self._snapshot()
        store = self.store_type.return_value
        store.commit.return_value = MagicMock(dry_run=True, inserted={}, updated={})
        reconciled = ManifestPlan()
        expected_hashes = {"works": "a" * 64}
        self.reconcile.return_value = reconciled, expected_hashes

        result = register_jinr_preprints.main(
            [
                "--dry-run",
                "--rights-record-id", "pdf-acquisition",
                "--rights-record-id", "pdf-storage",
                "--metadata-rights-record-id", "metadata-acquisition",
                "--metadata-rights-record-id", "metadata-storage",
                "--cards-output", "manifests/imports/checked_preprints.jsonl",
            ],
            project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.assertEqual(self._snapshot(), before)
        store.commit.assert_called_once_with(
            reconciled,
            dry_run=True,
            expected_snapshot_hashes=expected_hashes,
        )
        self.plan_download.assert_called_once()
        options = self.plan_download.call_args.args[2]
        self.assertEqual(options.acquisition_method, "crawler")
        self.assertEqual(options.acquisition_scope, "bulk")
        self.assertEqual(options.rights_record_ids, ("pdf-acquisition", "pdf-storage"))
        self.assertEqual(
            self.plan_download.call_args.kwargs["metadata_rights_record_ids"],
            ("metadata-acquisition", "metadata-storage"),
        )

    def test_all_downloads_are_checked_before_cards_are_written(self) -> None:
        """Ошибка второй описи не оставляет частичный выходной JSONL."""

        (self.downloads / "second.json").write_text("{}\n", encoding="utf-8")
        self.read_download.side_effect = [object(), ValueError("Повреждённое свидетельство")]
        before = self._snapshot()

        with self.assertRaisesRegex(ValueError, "Повреждённое свидетельство"):
            register_jinr_preprints.main(
                ["--check-files-only", "--cards-output", "manifests/imports/cards.jsonl"],
                project_root=self.root,
            )

        self.assertEqual(self._snapshot(), before)
        self.store_type.assert_not_called()

    def test_export_rejects_paths_outside_imports_or_wrong_extension(self) -> None:
        """Карточки нельзя записать поверх другого раздела проекта или не в JSONL."""

        invalid_paths = (
            "cards.jsonl",
            "data/cards.jsonl",
            "manifests/imports/../../cards.jsonl",
            "manifests/imports/cards.json",
            str(self.root.parent / "outside_preprint_cards.jsonl"),
        )

        for output_path in invalid_paths:
            with self.subTest(output_path=output_path):
                before = self._snapshot()

                with self.assertRaisesRegex(ValueError, "manifests/imports/"):
                    register_jinr_preprints.main(
                        ["--check-files-only", "--cards-output", output_path],
                        project_root=self.root,
                    )

                self.assertEqual(self._snapshot(), before)

    def test_export_rejects_symlink_escape(self) -> None:
        """Ссылка внутри imports не позволяет записать карточки в другой каталог."""

        outside_directory = self.root / "unrelated"
        outside_directory.mkdir()
        (self.imports / "redirect").symlink_to(outside_directory, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "manifests/imports/"):
            register_jinr_preprints.main(
                ["--check-files-only", "--cards-output", "manifests/imports/redirect/cards.jsonl"],
                project_root=self.root,
            )

        self.assertFalse((outside_directory / "cards.jsonl").exists())

    def test_export_never_replaces_different_existing_file(self) -> None:
        """Совпавшее имя другого файла отклоняется с сохранением его байтов."""

        output_path = self.imports / "cards.jsonl"
        output_path.write_text("не заменять\n", encoding="utf-8")
        before = self._snapshot()

        with self.assertRaisesRegex(ValueError, "Другой файл уже существует"):
            register_jinr_preprints.main(
                ["--check-files-only", "--cards-output", str(output_path)],
                project_root=self.root,
            )

        self.assertEqual(self._snapshot(), before)

    def test_export_reuses_identical_existing_file_without_rewriting(self) -> None:
        """Повторный экспорт тех же карточек не меняет файл и время записи."""

        output_path = self.imports / "cards.jsonl"
        output_path.write_text(canonical_json(asdict(self.card)) + "\n", encoding="utf-8")
        before = output_path.stat().st_mtime_ns

        result = register_jinr_preprints.main(
            ["--check-files-only", "--cards-output", str(output_path)],
            project_root=self.root,
        )

        self.assertEqual(result, 0)
        self.assertEqual(output_path.stat().st_mtime_ns, before)

    def test_save_checked_cards_rechecks_existing_content(self) -> None:
        """Изменение файла между планированием и сохранением не перезаписывается."""

        output_path = self.imports / "cards.jsonl"
        blob = register_jinr_preprints._cards_blob(self.root, output_path, b"new\n")
        output_path.write_bytes(b"changed concurrently\n")

        with self.assertRaisesRegex(ValueError, "Выходной файл изменился"):
            register_jinr_preprints._save_checked_cards(self.root, blob)

        self.assertEqual(output_path.read_bytes(), b"changed concurrently\n")


class CombinePreprintPlansTests(unittest.TestCase):
    """Проверки единственного HTTP-события общей страницы API и конфликтов."""

    def test_common_api_event_is_deduplicated(self) -> None:
        """Две работы одной страницы API ссылаются на одно неизменяемое событие."""

        common_event: dict[str, Any] = {"retrieval_id": "api-page", "body_sha256": "a" * 64}
        first = ManifestPlan(
            works=[{"work_id": "first"}],
            retrieval_events=[common_event, {"retrieval_id": "first-pdf"}],
        )
        second = ManifestPlan(
            works=[{"work_id": "second"}],
            retrieval_events=[copy.deepcopy(common_event), {"retrieval_id": "second-pdf"}],
        )

        combined = register_jinr_preprints._combine_plans([first, second])

        self.assertEqual(len(combined.works), 2)
        self.assertEqual(len(combined.retrieval_events), 3)
        self.assertEqual(
            [event["retrieval_id"] for event in combined.retrieval_events],
            ["api-page", "first-pdf", "second-pdf"],
        )
        self.assertEqual(len(first.retrieval_events), 2)
        self.assertEqual(len(second.retrieval_events), 2)

    def test_conflicting_records_are_rejected_in_each_identity_collection(self) -> None:
        """Одинаковый ID с различными данными не затеняется при объединении."""

        for field_name, primary_key in (
            ("works", "work_id"),
            ("artifacts", "artifact_record_id"),
            ("retrieval_events", "retrieval_id"),
            ("work_aliases", "alias_record_id"),
        ):
            with self.subTest(field_name=field_name):
                first = ManifestPlan()
                second = ManifestPlan()
                getattr(first, field_name).append({primary_key: "same-id", "value": "first"})
                getattr(second, field_name).append({primary_key: "same-id", "value": "second"})

                with self.assertRaisesRegex(ValueError, f"Противоречащие записи {field_name}"):
                    register_jinr_preprints._combine_plans([first, second])


if __name__ == "__main__":
    unittest.main()
