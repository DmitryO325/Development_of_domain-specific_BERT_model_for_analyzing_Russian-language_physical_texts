"""Проверки приоритета явных ограничений перед узким допущением для партии журналов."""

from __future__ import annotations

import copy
import unittest

from typing import Any
from unittest.mock import patch

from src.corpus.journal_batch_constraints import ensure_no_conflicting_rights
from src.corpus.manifests import ManifestError, ManifestPlan
from tests import test_manifest_store as store_fixtures

RETRIEVED_AT = "2026-08-27T12:00:00+03:00"
REGISTERED_AT = "2026-08-29T12:00:00+03:00"


class JournalBatchConstraintsTests(unittest.TestCase):
    """Проверять область запрета и историю без изменения обычного выбора прав реестром."""

    def setUp(self) -> None:
        """Подготовить изолированное хранилище и получение конкретного артефакта."""

        self.fixture = store_fixtures.ManifestStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.plan = self.fixture.plan(include_rights=False)
        artifact = self.plan.artifacts[0]
        artifact.update(acquisition_method="crawler", acquisition_scope="bulk")
        self.event = {
            "retrieval_id": artifact["retrievals"][0]["retrieval_id"],
            "request_context_type": "artifact", "request_context_id": artifact["artifact_record_id"],
            "retrieved_at": RETRIEVED_AT,
            "acquisition_method": "crawler", "acquisition_scope": "bulk",
        }
        self.plan.retrieval_events.append(self.event)

    def _right(self, operation: str = "acquisition", **changes: Any) -> dict[str, Any]:
        """Создать применимое ограничение источника с возможностью уточнить тестовый контекст."""

        right = self.fixture.right(operation, status="prohibited")

        if operation == "acquisition":
            right.update(acquisition_method="crawler", acquisition_scope="bulk")

        right.update(changes)

        return right

    def _save_rights(self, *rights: dict[str, Any]) -> None:
        """Внести только записи прав в временное тестовое хранилище."""

        self.store.commit(ManifestPlan(rights=list(rights)))

    def _check(self) -> None:
        """Проверить план на дату поздней регистрации."""

        ensure_no_conflicting_rights(self.store, self.plan, REGISTERED_AT)

    def test_source_prohibition_cannot_be_overridden_by_narrow_allowance(self) -> None:
        """Более конкретное разрешение в новом плане не скрывает запрет уровня источника."""

        self._save_rights(self._right())
        self.plan.rights.append(self._right(
            rights_record_id="narrow-allowance", status="allowed", scope_type="artifact",
            scope_id=self.plan.artifacts[0]["artifact_record_id"],
        ))

        with self.assertRaisesRegex(ManifestError, "right-acquisition"):
            self._check()

    def test_permission_required_blocks_assumption(self) -> None:
        """Прямое требование разрешения тоже нельзя заменить внутренним допущением."""

        self._save_rights(self._right(status="permission_required", basis_type="unknown"))

        with self.assertRaisesRegex(ManifestError, "permission_required"):
            self._check()

    def test_source_group_prohibition_blocks(self) -> None:
        """Общий запрет группы источников распространяется на входящую в неё работу."""

        self._save_rights(self._right(scope_type="source_group", scope_id=self.fixture.profile.source_group_id))

        with self.assertRaises(ManifestError):
            self._check()

    def test_unrelated_source_does_not_block(self) -> None:
        """Ограничение другого источника не относится к этой партии."""

        self._save_rights(self._right(scope_id="other-source"))
        self._check()

    def test_other_acquisition_modes_do_not_block(self) -> None:
        """Запрет ручного скачивания или API не выдаётся за запрет crawler bulk."""

        self._save_rights(
            self._right(rights_record_id="manual", acquisition_method="manual_download"),
            self._right(rights_record_id="api", acquisition_method="api"),
            self._right(rights_record_id="sample", acquisition_scope="sample"),
        )
        self._check()

    def test_unrelated_training_and_release_restrictions_do_not_block(self) -> None:
        """Новые основания не разрешают обучение и выпуск, поэтому их запреты не снимаются и не мешают хранению."""

        self._save_rights(self._right("ml_training"), self._right("evaluation"), self._right("checkpoint_release"))
        self._check()

    def test_unknown_status_does_not_mean_explicit_prohibition(self) -> None:
        """Отсутствие установленного основания отличается от явного запрета или требования разрешения."""

        self._save_rights(self._right(status="unknown", basis_type="unknown"))
        self._check()

    def test_expired_now_prohibition_still_blocks_historical_acquisition(self) -> None:
        """Истечение запрета после скачивания не делает запрещённое историческое получение допустимым."""

        self._save_rights(self._right(rights_expires_at="2026-08-28"))

        with self.assertRaisesRegex(ManifestError, "2026-08-27"):
            self._check()

    def test_expired_before_acquisition_prohibition_does_not_block(self) -> None:
        """Истёкшее ещё до получения ограничение не применяется к новым файлам."""

        self._save_rights(self._right(
            created_at="2026-08-25T12:00:00+03:00", rights_checked_at="2026-08-25",
            rights_expires_at="2026-08-26",
        ))
        self._check()

    def test_current_storage_restriction_blocks_retention(self) -> None:
        """Запрет хранения, вступивший после получения, учитывается на момент регистрации."""

        self._save_rights(self._right("storage", created_at="2026-08-28T12:00:00+03:00",
                                     rights_checked_at="2026-08-28"))

        with self.assertRaisesRegex(ManifestError, "storage.*2026-08-29"):
            self._check()

    def test_historical_storage_restriction_also_blocks(self) -> None:
        """Прекратившийся запрет хранения не скрывает его действие при получении файла."""

        self._save_rights(self._right("storage", rights_expires_at="2026-08-28"))

        with self.assertRaisesRegex(ManifestError, "storage.*2026-08-27"):
            self._check()

    def test_storage_mode_restriction_is_respected(self) -> None:
        """Уточнение способа получения в запрете хранения не распространяется на другой режим."""

        self._save_rights(self._right("storage", acquisition_method="api", acquisition_scope="bulk"))
        self._check()

    def test_late_recorded_historical_restriction_is_used(self) -> None:
        """Поздно зафиксированный запрет с доказанной датой действия проверяется на дату получения."""

        self._save_rights(self._right(
            created_at="2026-08-28T12:00:00+03:00", rights_checked_at="2026-08-28",
            effective_from="2026-08-26T12:00:00+03:00", effective_from_evidence_sha256="b" * 64,
        ))

        with self.assertRaises(ManifestError):
            self._check()

    def test_later_recorded_unknown_at_cutoff_restriction_is_not_used(self) -> None:
        """Сведения, появившиеся после проверяемой регистрации, не переписывают её срез знания."""

        self._save_rights(self._right(
            created_at="2026-08-30T12:00:00+03:00", rights_checked_at="2026-08-30",
            effective_from="2026-08-26T12:00:00+03:00", effective_from_evidence_sha256="b" * 64,
        ))
        self._check()

    def test_superseded_before_retrieval_restriction_does_not_block(self) -> None:
        """Отменённое до получения ограничение не считается действующим."""

        old = self._right(created_at="2026-08-25T12:00:00+03:00", rights_checked_at="2026-08-25")
        new = self._right(
            rights_record_id="allowed-successor", status="allowed",
            supersedes_rights_record_id=old["rights_record_id"],
            created_at="2026-08-26T12:00:00+03:00", rights_checked_at="2026-08-26",
        )
        self._save_rights(old, new)
        self._check()

    def test_superseded_after_retrieval_restriction_still_blocks(self) -> None:
        """Поздняя отмена запрета не удаляет его из истории более раннего получения."""

        old = self._right()
        new = self._right(
            rights_record_id="allowed-successor", status="allowed",
            supersedes_rights_record_id=old["rights_record_id"],
            created_at="2026-08-28T12:00:00+03:00", rights_checked_at="2026-08-28",
        )
        self._save_rights(old, new)

        with self.assertRaisesRegex(ManifestError, "right-acquisition"):
            self._check()

    def test_future_effective_successor_does_not_cancel_restriction(self) -> None:
        """Будущая дата действия разрешения сохраняет прежнее ограничение до этой даты."""

        old = self._right()
        new = self._right(
            rights_record_id="allowed-successor", status="allowed",
            supersedes_rights_record_id=old["rights_record_id"],
            created_at="2026-08-28T12:00:00+03:00", rights_checked_at="2026-08-28",
            effective_from="2026-08-30T12:00:00+03:00", effective_from_evidence_sha256="b" * 64,
        )
        self._save_rights(old, new)

        with self.assertRaisesRegex(ManifestError, "right-acquisition"):
            self._check()

    def test_source_context_without_owned_artifact_is_checked(self) -> None:
        """Общее оглавление без собственного артефакта также подчиняется запретам источника."""

        self.plan.artifacts.clear()
        self.event.update(request_context_type="source", request_context_id=self.fixture.profile.source_id,
                          source_group_id=self.fixture.profile.source_group_id)
        self._save_rights(self._right("storage", scope_type="source_group",
                                     scope_id=self.fixture.profile.source_group_id))

        with self.assertRaises(ManifestError):
            self._check()

    def test_retrieval_scope_is_checked(self) -> None:
        """Запрет конкретного ответа проверяется и при повторной регистрации его события."""

        right = self._right(scope_type="retrieval", scope_id=self.event["retrieval_id"])
        original_records = self.store.records

        def records(kind: str) -> list[dict[str, Any]]:
            """Представить уже существующее ограничение повторяемого события без записи фикстуры."""

            return [right] if kind == "rights" else original_records(kind)

        with patch.object(self.store, "records", side_effect=records):
            with self.assertRaises(ManifestError):
                self._check()

    def test_artifact_without_new_event_is_checked(self) -> None:
        """Отсутствие нового события при повторном импорте не отключает проверку файла."""

        self.plan.retrieval_events.clear()
        self._save_rights(self._right("storage", scope_type="artifact",
                                     scope_id=self.plan.artifacts[0]["artifact_record_id"]))

        with self.assertRaises(ManifestError):
            self._check()

    def test_check_does_not_mutate_plan_or_registry(self) -> None:
        """Защитная проверка не меняет план, записи прав или рабочие снимки."""

        self._save_rights(self._right(status="unknown", basis_type="unknown"))
        before_plan = copy.deepcopy(self.plan)
        before_rights = self.store.records("rights")
        self._check()
        self.assertEqual(self.plan, before_plan)
        self.assertEqual(self.store.records("rights"), before_rights)

    def test_registration_cannot_precede_retrieval(self) -> None:
        """Нельзя обойти историю ограничений срезом знания раньше самого получения."""

        with self.assertRaisesRegex(ManifestError, "предшествовать"):
            ensure_no_conflicting_rights(self.store, self.plan, "2026-08-26T12:00:00+03:00")

    def test_timezone_is_required(self) -> None:
        """Неопределённое местное время не используется для временного отбора оснований."""

        with self.assertRaisesRegex(ManifestError, "часовой пояс"):
            ensure_no_conflicting_rights(self.store, self.plan, "2026-08-29T12:00:00")


if __name__ == "__main__":
    unittest.main()
