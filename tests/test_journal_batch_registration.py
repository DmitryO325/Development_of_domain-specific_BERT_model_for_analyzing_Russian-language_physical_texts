"""Проверки атомарного импорта фиксированной партии с узкими основаниями DEC-012."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest

from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import register_journal_articles
from src.corpus.journal_batch_input import FrozenJournalBatch, read_frozen_journal_batch
from src.corpus.journal_batch_registration import (
    read_batch_assessment,
    reconcile_batch_registration,
)
from src.corpus.journal_batch_rights import COMMITMENTS
from src.corpus.journal_registration import plan_collected_article
from src.corpus.manifests import ManifestConflictError, ManifestPlan, ManifestStore, sha256_bytes
from tests.test_journal_registration import RIGHTS_IDS, JournalFixture

REGISTERED_AT = "2026-10-13T20:00:00.000000+00:00"
POLICY_SNAPSHOT = (
    "## DEC-012. Только синтетическое основание теста\n\n"
    "**Состояние:** принято 19.08.2026.\n"
    "basis_type=lawful_access_research_assumption\n"
    "Это тестовый текст, не фактическое разрешение какого-либо издателя."
)


class JournalBatchRegistrationTests(unittest.TestCase):
    """Проверки регистрации и команды без доступа к настоящему корпусу или сети."""

    def setUp(self) -> None:
        """Создать отдельные исходники, закреплённый состав и синтетическую проверку."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.fixture = JournalFixture(Path(temporary_directory.name))
        self.store = self.fixture.store()
        directory = self.fixture.root / "manifests/imports/test_frozen_batch_v1"
        directory.mkdir(parents=True)
        self.batch_path = directory / "prepared_batch.json"
        self.assessment_path = directory / "assessment.json"
        packet = {
            "schema_version": "prepared-journal-registration-batch-v1",
            "status": "prepared_not_registered",
            "prepared_at": "2026-10-13T13:00:00+00:00",
            "policy": {
                "path": "docs/DECISIONS.md",
                "file_sha256": sha256_bytes(POLICY_SNAPSHOT.encode("utf-8")),
                "dec012_snapshot": POLICY_SNAPSHOT,
            },
            "raw_roots": {"jetp": self.fixture.raw_root.relative_to(self.fixture.root).as_posix()},
            "records": [{
                "source": "jetp",
                "record_path": self.fixture.record_path.relative_to(self.fixture.root).as_posix(),
                "record_sha256": sha256_bytes(self.fixture.record_path.read_bytes()),
                "has_pdf": True,
            }],
            "skipped": [],
            "counts": {"jetp": {"cards": 1, "pdfs": 1, "skipped_non_russian": 0}},
        }
        self._save_json(self.batch_path, packet)
        self.batch = self._read_batch()
        self.assessment: dict[str, Any] = {
            "policy": {
                "decision_id": "DEC-012",
                "adopted_at": "2026-08-19T00:00:00+03:00",
                "snapshot": POLICY_SNAPSHOT,
            },
            "prepared_batch_sha256": self.batch.sha256,
            "commitments": {name: True for name in COMMITMENTS},
            "reviewed_responses": [{
                "response_metadata_sha256": response.metadata_sha256(),
                "response_sha256": sha256_bytes(response.body),
            } for response in (self.fixture.card, self.fixture.pdf)],
            "review_notes": "Все подтверждения синтетические и относятся только к временному тесту.",
        }
        self._save_json(self.assessment_path, self.assessment)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.stdout = self.patches.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        self.stderr = self.patches.enter_context(patch("sys.stderr", new_callable=io.StringIO))
        self.patches.enter_context(patch.object(register_journal_articles, "PROJECT_ROOT", self.fixture.root))
        self.store_factory = self.patches.enter_context(
            patch.object(register_journal_articles, "ManifestStore", side_effect=self._store_factory)
        )
        self.clock = self.patches.enter_context(
            patch("src.corpus.journal_batch_registration.datetime", wraps=datetime)
        )
        self.clock.now.return_value = datetime.fromisoformat(REGISTERED_AT)

    def _save_json(self, path: Path, value: dict[str, Any]) -> None:
        """Записать только синтетический JSON во временный каталог теста."""

        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _read_batch(self) -> FrozenJournalBatch:
        """Прочитать точные исходники временного пакета обычным рабочим загрузчиком."""

        return read_frozen_journal_batch(self.batch_path, project_root=self.fixture.root)

    def _store_factory(self, **arguments: Any) -> ManifestStore:
        """Подставить настоящие схемы, сохранив все данные внутри временного проекта."""

        self.assertEqual(arguments["project_root"], self.fixture.root)
        self.assertEqual(arguments["manifest_dir"], self.fixture.manifest_dir)

        return self.store

    def _arguments(self) -> list[str]:
        """Составить команду с явным согласием и путями только к тестовой партии."""

        return [
            "register-batch", str(self.batch_path),
            "--assessment", str(self.assessment_path),
            "--manifest-dir", str(self.fixture.manifest_dir),
            "--accept-dec012",
        ]

    def test_register_and_repeat_preserve_history_and_eligibility(self) -> None:
        """Атомарный импорт и повтор не добавляют допуск, дубли или новые даты прав."""

        source_bytes = self.fixture.record_path.read_bytes()
        self.assertEqual(register_journal_articles.main(self._arguments()), 0, self.stderr.getvalue())
        first_snapshot = self.fixture.snapshot()
        first_hashes = self.store.snapshot_hashes()
        first_rights = copy.deepcopy(self.store.records("rights"))

        self.assertEqual(register_journal_articles.main(self._arguments()), 0, self.stderr.getvalue())
        self.assertEqual(self.fixture.snapshot(), first_snapshot)
        self.assertEqual(self.store.snapshot_hashes(), first_hashes)
        self.assertEqual(self.store.records("rights"), first_rights)
        self.assertEqual(self.fixture.record_path.read_bytes(), source_bytes)
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertEqual(len(self.store.records("artifacts")), 2)
        self.assertEqual(len(self.store.records("retrieval_events")), 2)
        self.assertEqual(len(first_rights), 4)
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "pending")
        self.assertEqual({right["operation"] for right in first_rights}, {"acquisition", "storage"})
        self.assertEqual({right["scope_type"] for right in first_rights}, {"artifact"})
        self.assertEqual({right["created_at"] for right in first_rights}, {REGISTERED_AT})
        self.assertTrue(all(right["status"] == "conditional" for right in first_rights))
        self.assertTrue(self.store.audit().ok)

    def test_dry_run_does_not_create_registry_or_artifact_files(self) -> None:
        """Предварительный прогон проверяет права, но не пишет ни одного нового файла."""

        before = self.fixture.snapshot()

        result = register_journal_articles.main(self._arguments() + ["--dry-run"])

        self.assertEqual(result, 0, self.stderr.getvalue())
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.store.records("rights"), [])
        self.assertEqual(self.store.records("works"), [])
        self.assertIn("Предварительная проверка", self.stdout.getvalue())

    def test_explicit_dec012_flag_is_required(self) -> None:
        """Одного файла проверки недостаточно для подразумеваемого принятия основания."""

        arguments = self._arguments()
        arguments.remove("--accept-dec012")
        before = self.fixture.snapshot()

        with self.assertRaises(SystemExit) as exception:
            register_journal_articles.main(arguments)

        self.assertEqual(exception.exception.code, 2)
        self.store_factory.assert_not_called()
        self.assertEqual(self.fixture.snapshot(), before)

    def test_assessment_for_another_batch_is_rejected_without_writes(self) -> None:
        """Проверка другой контрольной суммы не разрешает регистрацию текущей партии."""

        self.assessment["prepared_batch_sha256"] = "0" * 64
        self._save_json(self.assessment_path, self.assessment)
        before = self.fixture.snapshot()

        self.assertEqual(register_journal_articles.main(self._arguments()), 1)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertIn("другому составу партии", self.stderr.getvalue())

    def test_changed_packet_bytes_require_a_new_assessment(self) -> None:
        """Изменение даже сериализации пакета нарушает закреплённую контрольную сумму."""

        self.batch_path.write_bytes(self.batch_path.read_bytes() + b"\n")
        before = self.fixture.snapshot()

        self.assertEqual(register_journal_articles.main(self._arguments()), 1)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.store.records("works"), [])

    def test_changed_assessment_cannot_rewrite_registered_rights(self) -> None:
        """Повтор не подменяет свидетельство уже существующих неизменяемых оснований."""

        self.assertEqual(register_journal_articles.main(self._arguments()), 0, self.stderr.getvalue())
        self.assessment["review_notes"] = "Изменённое синтетическое свидетельство."
        self._save_json(self.assessment_path, self.assessment)
        before = self.fixture.snapshot()

        self.assertEqual(register_journal_articles.main(self._arguments()), 1)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertTrue(self.store.audit().ok)

    def test_policy_snapshot_must_match_frozen_batch(self) -> None:
        """Основание нельзя незаметно заменить другой версией проектной политики."""

        self.assessment["policy"]["snapshot"] += "\nДругая версия."
        self._save_json(self.assessment_path, self.assessment)

        with self.assertRaisesRegex(ValueError, "Политика"):
            read_batch_assessment(self.assessment_path, self.batch)

    def test_programmatic_import_checks_the_same_policy_snapshot(self) -> None:
        """Прямой вызов регистратора также не должен подменять закреплённую политику."""

        changed = copy.deepcopy(self.assessment)
        changed["policy"]["snapshot"] = "Другое синтетическое основание."
        before = self.fixture.snapshot()

        with self.assertRaises(ValueError):
            reconcile_batch_registration(self.store, self.batch, changed)

        self.assertEqual(self.fixture.snapshot(), before)

    def test_partially_existing_batch_rights_are_rejected(self) -> None:
        """Неполный набор существующих оснований требует проверки вместо дополнения наугад."""

        plan, _ = reconcile_batch_registration(self.store, self.batch, self.assessment)
        right = plan.rights[0]
        evidence = [blob for blob in plan.blobs if blob.sha256 == right["rights_evidence_sha256"]]
        self.store.commit(ManifestPlan(rights=[right], blobs=evidence))
        before = self.fixture.snapshot()

        with self.assertRaisesRegex(ManifestConflictError, "частично"):
            reconcile_batch_registration(self.store, self.batch, self.assessment)

        self.assertEqual(self.fixture.snapshot(), before)

    def test_existing_eligibility_and_previous_rights_remain_unchanged(self) -> None:
        """Импорт новых событий не сбрасывает прежний допуск и не добавляет ML-основания."""

        initial = plan_collected_article(self.fixture.article(), rights_record_ids=RIGHTS_IDS)
        initial.rights = self.fixture.rights()
        initial.works[0].update({
            "eligibility_status": "eligible", "genre": "research_article", "published_at": "2026-10-01",
        })
        self.store.commit(ManifestPlan(works=initial.works, rights=initial.rights))
        previous_rights = {right["rights_record_id"]: right for right in self.store.records("rights")}

        self.assertEqual(register_journal_articles.main(self._arguments()), 0, self.stderr.getvalue())

        current_rights = {right["rights_record_id"]: right for right in self.store.records("rights")}
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "eligible")
        self.assertEqual(self.store.records("works")[0]["published_at"], "2026-10-01")
        self.assertEqual({key: current_rights[key] for key in previous_rights}, previous_rights)
        self.assertEqual({right["operation"] for right in current_rights.values()}, {"acquisition", "storage"})
        self.assertTrue(self.store.audit().ok)

    def test_existing_legacy_decisions_are_not_silently_rewritten(self) -> None:
        """Несовместимость новых оснований со старой историей останавливает весь импорт."""

        initial = plan_collected_article(self.fixture.article(), rights_record_ids=RIGHTS_IDS)
        initial.rights = self.fixture.rights()
        self.store.commit(initial)
        before = self.fixture.snapshot()
        old_decisions = copy.deepcopy(self.store.records("operation_decisions"))

        result = register_journal_articles.main(self._arguments())

        self.assertEqual(result, 1)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.store.records("operation_decisions"), old_decisions)
        self.assertIn("operation_decisions", self.stderr.getvalue())
        self.assertTrue(self.store.audit().ok)

    def test_repeat_checks_new_storage_prohibition_at_current_time(self) -> None:
        """Первая дата регистрации не должна скрывать более поздний запрет хранения."""

        self.assertEqual(register_journal_articles.main(self._arguments()), 0, self.stderr.getvalue())
        prohibition = next(right for right in self.fixture.rights() if right["operation"] == "storage")
        prohibition.update({
            "rights_record_id": "test-journal-storage-prohibition-later",
            "created_at": "2026-10-13T21:00:00+00:00",
            "rights_checked_at": "2026-10-13",
            "status": "prohibited",
            "access_basis": "Синтетический запрет хранения, внесённый после первоначального импорта.",
        })
        self.store.commit(ManifestPlan(rights=[prohibition]))
        self.clock.now.return_value = datetime.fromisoformat("2026-10-13T22:00:00+00:00")
        before = self.fixture.snapshot()
        old_decisions = copy.deepcopy(self.store.records("operation_decisions"))

        result = register_journal_articles.main(self._arguments())

        self.assertEqual(result, 1)
        self.assertIn("test-journal-storage-prohibition-later", self.stderr.getvalue())
        self.assertIn("storage имеет статус prohibited", self.stderr.getvalue())
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.store.records("operation_decisions"), old_decisions)
        self.assertTrue(self.store.audit().ok)


if __name__ == "__main__":
    unittest.main()
