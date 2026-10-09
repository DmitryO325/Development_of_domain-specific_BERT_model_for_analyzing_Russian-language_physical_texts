"""Проверки дат действия оснований и неизменяемой истории решений."""

from __future__ import annotations

import copy
import unittest
from typing import Any

from src.corpus.manifests import (
    ManifestError,
    ManifestPlan,
    PlannedBlob,
    canonical_json,
    sha256_bytes,
)
from src.corpus.registration import resolve_collection_rights
from src.corpus.schema_validation import SchemaValidationError
from tests import test_manifest_store as store_fixtures

EFFECTIVE_AT = "2026-08-26T12:00:00+03:00"
RETRIEVED_AT = "2026-08-27T12:00:00+03:00"
RIGHT_RECORDED_AT = "2026-08-28T12:00:00+03:00"
REGISTERED_AT = "2026-08-29T12:00:00+03:00"
LATER_AT = "2026-08-30T12:00:00+03:00"
CONDITION = "Использовать материал только в закрытом исследовании."


class TemporalRightsTests(unittest.TestCase):
    """Проверки позднего внесения достоверных фактов без задних дат."""

    def setUp(self) -> None:
        """Подготовить отдельное хранилище, не повторяя чужие тесты."""

        self.fixture = store_fixtures.ManifestStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store

    def _right(
        self,
        operation: str,
        *,
        conditional: bool = False,
    ) -> dict[str, Any]:
        """Создать позднюю запись с доказанной более ранней датой действия."""

        right = self.fixture.right(operation)
        right.update(
            {
                "created_at": RIGHT_RECORDED_AT,
                "rights_checked_at": "2026-08-28",
                "effective_from": EFFECTIVE_AT,
                "effective_from_evidence_sha256": "c" * 64,
            }
        )

        if conditional:
            right["status"] = "conditional"
            right["rights_conditions"] = [CONDITION]

        return right

    def _fulfilment(self) -> dict[str, Any]:
        """Отделить фактическое выполнение условия от его поздней записи."""

        return {
            "schema_version": "condition-fulfilments-v1",
            "fulfilment_id": "fulfilment:temporal:closed-research",
            "created_at": RIGHT_RECORDED_AT,
            "rights_record_id": "right-acquisition",
            "condition": CONDITION,
            "subject_type": "source",
            "subject_id": self.fixture.profile.source_id,
            "status": "satisfied",
            "satisfied_at": EFFECTIVE_AT,
            "expires_at": None,
            "evidence_sha256": "d" * 64,
            "supersedes_fulfilment_id": None,
        }

    def _plan(self, *, conditional: bool = False) -> ManifestPlan:
        """Подготовить регистрацию ранее полученного настоящего HTTP-ответа."""

        body = b"historical response for temporal tests"
        digest = sha256_bytes(body)
        path = f"data/raw/responses/{digest}.bin"
        rights = [
            self._right("acquisition", conditional=conditional),
            self._right("storage"),
        ]
        event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": "retrieval:temporal:recorded-later",
            "created_at": REGISTERED_AT,
            "request_context_type": "source",
            "request_context_id": self.fixture.profile.source_id,
            "source_group_id": self.fixture.profile.source_group_id,
            "requested_url": "https://example.invalid/historical.pdf",
            "final_url": "https://example.invalid/historical.pdf",
            "retrieved_at": RETRIEVED_AT,
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "rights_record_ids": [right["rights_record_id"] for right in rights],
            "http_status": 200,
            "response_headers": {},
            "response_metadata_sha256": "e" * 64,
            "response_path": path,
            "response_sha256": digest,
            "response_bytes": len(body),
            "outcome": "succeeded",
            "error_code": None,
            "error_detail": None,
        }
        return ManifestPlan(
            rights=rights,
            condition_fulfilments=[self._fulfilment()] if conditional else [],
            retrieval_events=[event],
            blobs=[PlannedBlob(path, body, digest)],
        )

    def _decision(
        self,
        plan: ManifestPlan,
        cutoff: str | None,
    ) -> dict[str, Any]:
        """Создать отдельное решение для проверки ограничений даты знания."""

        context = {
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
        }
        right = plan.rights[0]
        decision = {
            "schema_version": "operation-decisions-v1",
            "decision_id": "decision:temporal:explicit",
            "created_at": REGISTERED_AT,
            "decision_key": "temporal:explicit:acquisition",
            "operation": "acquisition",
            "derivative_scope": None,
            "subject_type": "retrieval",
            "subject_id": plan.retrieval_events[0]["retrieval_id"],
            "decision_at": RETRIEVED_AT,
            "context": context,
            "context_sha256": sha256_bytes(canonical_json(context).encode("utf-8")),
            "rights_record_ids": [right["rights_record_id"]],
            "rights_snapshot_sha256": sha256_bytes(
                f"{canonical_json(right)}\n".encode("utf-8")
            ),
            "condition_fulfilment_ids": [
                item["fulfilment_id"] for item in plan.condition_fulfilments
            ],
            "supersedes_decision_id": None,
            "status": "allowed",
        }

        if cutoff is not None:
            decision["knowledge_cutoff_at"] = cutoff

        return decision

    def _decision_bytes(self) -> bytes:
        """Прочитать журнал решений для побайтового сравнения истории."""

        path = self.fixture.project_root / "manifests" / "operation_decisions.jsonl"
        return path.read_bytes()

    def _resolve_current_rights(self) -> tuple[str, ...]:
        """Проверить допустимость нового сбора по текущим сведениям."""

        return resolve_collection_rights(
            self.store,
            self.fixture.profile,
            acquisition_method="manual_download",
            acquisition_scope="sample",
            allowed_rights_record_ids=tuple(
                right["rights_record_id"] for right in self.store.records("rights")
            ),
        )

    def test_late_right_and_fulfilment_allow_historical_response(self) -> None:
        """Доказанные ранние факты должны разрешать позднюю регистрацию."""

        plan = self._plan(conditional=True)
        result = self.store.commit(plan)

        self.assertEqual(result.inserted["retrieval_events"], 1)
        decisions = self.store.records("operation_decisions")
        acquisition = next(
            item for item in decisions if item["operation"] == "acquisition"
        )
        self.assertEqual(acquisition["decision_at"], RETRIEVED_AT)
        self.assertEqual(acquisition["knowledge_cutoff_at"], REGISTERED_AT)
        self.assertEqual(acquisition["created_at"], REGISTERED_AT)
        self.assertEqual(acquisition["status"], "allowed")
        self.assertEqual(
            acquisition["condition_fulfilment_ids"],
            [self._fulfilment()["fulfilment_id"]],
        )
        self.assertEqual(
            self.store.records("rights")[0]["created_at"], RIGHT_RECORDED_AT
        )
        self.assertTrue(self.store.audit().ok)

    def test_late_right_without_effective_date_does_not_allow_old_response(self) -> None:
        """Отсутствие новой даты должно сохранять прежнюю строгую семантику."""

        plan = self._plan()
        del plan.rights[0]["effective_from"]
        del plan.rights[0]["effective_from_evidence_sha256"]

        with self.assertRaises(ManifestError):
            self.store.commit(plan)

        self.assertFalse(self.store.records("retrieval_events"))

    def test_effective_date_after_acquisition_does_not_allow_response(self) -> None:
        """Разрешение, начавшее действовать позже, не должно покрывать загрузку."""

        plan = self._plan()
        plan.rights[0]["effective_from"] = RIGHT_RECORDED_AT

        with self.assertRaises(ManifestError):
            self.store.commit(plan)

    def test_effective_date_requires_evidence(self) -> None:
        """Раннюю дату нельзя вносить без самостоятельного свидетельства."""

        plan = self._plan()
        del plan.rights[0]["effective_from_evidence_sha256"]

        with self.assertRaises((SchemaValidationError, ManifestError)):
            self.store.preflight([plan])

    def test_later_retroactive_prohibition_preserves_decision_bytes(self) -> None:
        """Новое знание о запрете не должно переписывать принятое решение."""

        self.store.commit(self._plan())
        original = self._decision_bytes()
        prohibition = self._right("acquisition")
        prohibition.update(
            {
                "rights_record_id": "right-acquisition-later-prohibition",
                "created_at": LATER_AT,
                "rights_checked_at": "2026-08-30",
                "status": "prohibited",
            }
        )
        self.store.commit(ManifestPlan(rights=[prohibition]))

        self.assertEqual(self._decision_bytes(), original)
        self.assertTrue(self.store.audit().ok)

    def test_later_condition_revocation_preserves_decision_bytes(self) -> None:
        """Поздно записанный отзыв не должен изменять историческое решение."""

        self.store.commit(self._plan(conditional=True))
        original = self._decision_bytes()
        revocation = self._fulfilment()
        revocation.update(
            {
                "fulfilment_id": "fulfilment:temporal:later-revocation",
                "created_at": LATER_AT,
                "status": "revoked",
                "supersedes_fulfilment_id": self._fulfilment()["fulfilment_id"],
            }
        )
        self.store.commit(ManifestPlan(condition_fulfilments=[revocation]))

        self.assertEqual(self._decision_bytes(), original)
        self.assertTrue(self.store.audit().ok)

    def test_current_storage_prohibition_blocks_retained_response(self) -> None:
        """Сохранённая история не должна разрешать текущее запрещённое хранение."""

        self.store.commit(self._plan())
        original = self._decision_bytes()
        prohibition = self._right("storage")
        prohibition.update(
            {
                "rights_record_id": "right-storage-later-prohibition",
                "created_at": LATER_AT,
                "rights_checked_at": "2026-08-30",
                "effective_from": LATER_AT,
                "status": "prohibited",
            }
        )

        with self.assertRaises(ManifestError):
            self.store.commit(ManifestPlan(rights=[prohibition]))

        self.assertEqual(self._decision_bytes(), original)
        self.assertNotIn(prohibition, self.store.records("rights"))
        self.assertTrue(self.store.audit().ok)

    def test_cutoff_cannot_exceed_recording_date_or_reach_future(self) -> None:
        """Решение не может ссылаться на знание, полученное после его записи."""

        for cutoff in (LATER_AT, "2099-01-01T00:00:00+00:00"):
            with self.subTest(cutoff=cutoff):
                plan = self._plan()
                plan.operation_decisions.append(self._decision(plan, cutoff))

                with self.assertRaises(ManifestError):
                    self.store.preflight([plan])

    def test_cutoff_excludes_later_recorded_condition(self) -> None:
        """Позднее свидетельство условия нельзя подставить в раннее знание."""

        plan = self._plan(conditional=True)
        plan.rights[0]["created_at"] = EFFECTIVE_AT
        plan.rights[0]["rights_checked_at"] = "2026-08-26"
        plan.operation_decisions.append(self._decision(plan, RETRIEVED_AT))

        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_future_effective_successor_does_not_replace_earlier_right(self) -> None:
        """Будущая дата преемника не должна отменять право до этой даты."""

        plan = self._plan()
        successor = copy.deepcopy(plan.rights[0])
        successor.update(
            {
                "rights_record_id": "right-acquisition-future-successor",
                "created_at": REGISTERED_AT,
                "rights_checked_at": "2026-08-29",
                "effective_from": LATER_AT,
                "status": "prohibited",
                "supersedes_rights_record_id": plan.rights[0]["rights_record_id"],
            }
        )
        plan.rights.append(successor)
        self.store.commit(plan)

        acquisition = next(
            item for item in self.store.records("operation_decisions")
            if item["operation"] == "acquisition"
        )
        self.assertEqual(acquisition["rights_record_ids"], ["right-acquisition"])
        self.assertTrue(self.store.audit().ok)

    def test_legacy_decision_without_cutoff_remains_unchanged(self) -> None:
        """Старая запись без даты знания должна оставаться допустимой и неизменной."""

        plan = self._plan()
        plan.rights = [
            self.fixture.right("acquisition"),
            self.fixture.right("storage"),
        ]
        legacy = self._decision(plan, None)
        plan.operation_decisions.append(legacy)
        self.store.commit(plan)
        original = self._decision_bytes()

        self.store.commit(ManifestPlan())

        self.assertEqual(self._decision_bytes(), original)
        saved = next(
            item for item in self.store.records("operation_decisions")
            if item["decision_id"] == legacy["decision_id"]
        )
        self.assertNotIn("knowledge_cutoff_at", saved)
        self.assertTrue(self.store.audit().ok)

    def test_cutoff_excludes_later_recorded_right(self) -> None:
        """Даже доказанное раннее право нельзя прочесть раньше его внесения."""

        plan = self._plan()
        plan.operation_decisions.append(self._decision(plan, RETRIEVED_AT))

        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_current_collection_rejects_future_effective_permission(self) -> None:
        """Предварительная проверка не должна принимать ещё не действующее право."""

        acquisition = self._right("acquisition")
        acquisition["effective_from"] = "2099-01-01T00:00:00+00:00"
        self.store.commit(
            ManifestPlan(rights=[acquisition, self._right("storage")])
        )

        with self.assertRaises(ManifestError):
            self._resolve_current_rights()

    def test_current_collection_keeps_permission_before_future_prohibition(self) -> None:
        """Будущий запрет-преемник не должен заранее отменять разрешение."""

        acquisition = self._right("acquisition")
        successor = copy.deepcopy(acquisition)
        successor.update(
            {
                "rights_record_id": "right-acquisition-future-prohibition",
                "created_at": REGISTERED_AT,
                "rights_checked_at": "2026-08-29",
                "effective_from": "2099-01-01T00:00:00+00:00",
                "status": "prohibited",
                "supersedes_rights_record_id": acquisition["rights_record_id"],
            }
        )
        self.store.commit(
            ManifestPlan(rights=[acquisition, successor, self._right("storage")])
        )

        self.assertEqual(
            self._resolve_current_rights(), ("right-acquisition", "right-storage")
        )

    def test_current_collection_keeps_prohibition_before_future_permission(self) -> None:
        """Будущее разрешение-преемник не должно преждевременно снимать запрет."""

        acquisition = self._right("acquisition")
        acquisition["status"] = "prohibited"
        successor = copy.deepcopy(acquisition)
        successor.update(
            {
                "rights_record_id": "right-acquisition-future-permission",
                "created_at": REGISTERED_AT,
                "rights_checked_at": "2026-08-29",
                "effective_from": "2099-01-01T00:00:00+00:00",
                "status": "allowed",
                "supersedes_rights_record_id": acquisition["rights_record_id"],
            }
        )
        self.store.commit(
            ManifestPlan(rights=[acquisition, successor, self._right("storage")])
        )

        with self.assertRaises(ManifestError):
            self._resolve_current_rights()

    def test_current_collection_respects_revocation_of_legacy_condition(self) -> None:
        """Старое поле о выполнении не должно перекрывать последующий отзыв."""

        acquisition = self._right("acquisition", conditional=True)
        del acquisition["effective_from"]
        del acquisition["effective_from_evidence_sha256"]
        acquisition["conditions_satisfied_at"] = EFFECTIVE_AT
        acquisition["conditions_evidence_sha256"] = "d" * 64
        self.store.commit(
            ManifestPlan(rights=[acquisition, self._right("storage")])
        )
        self.assertEqual(
            self._resolve_current_rights(), ("right-acquisition", "right-storage")
        )
        fulfilment = self.store.records("condition_fulfilments")[0]
        revocation = copy.deepcopy(fulfilment)
        revocation.update(
            {
                "fulfilment_id": "fulfilment:temporal:legacy-revoked",
                "created_at": LATER_AT,
                "satisfied_at": LATER_AT,
                "status": "revoked",
                "supersedes_fulfilment_id": fulfilment["fulfilment_id"],
            }
        )
        self.store.commit(ManifestPlan(condition_fulfilments=[revocation]))

        with self.assertRaises(ManifestError):
            self._resolve_current_rights()


if __name__ == "__main__":
    unittest.main()
