"""Проверки прав на точное событие получения без расширения области разрешения."""

from __future__ import annotations

import copy
import unittest

from src.corpus.manifests import ManifestError, ManifestPlan, PlannedBlob, sha256_bytes
from src.corpus.registration import resolve_collection_rights
from tests import test_manifest_store as store_fixtures

EVENT_AT = "2026-08-29T12:00:00+03:00"
RECORDED_AT = "2026-08-30T12:00:00+03:00"
CONDITION = "Хранить ответ только в закрытом исследовательском хранилище."


class RetrievalRightsTests(unittest.TestCase):
    """Изолированные проверки событийных прав, условий и сохранения истории."""

    def setUp(self) -> None:
        """Подготовить временный реестр с общими синтетическими данными."""

        self.fixture = store_fixtures.ManifestStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store

    def _plan(self, name: str = "first", *, conditional: bool = False) -> ManifestPlan:
        """Создать сохранённый ответ источника с правами только на его событие."""

        retrieval_id = f"retrieval:test:{name}"
        body = f"Синтетический ответ API: {name}".encode()
        digest = sha256_bytes(body)
        path = f"data/raw/responses/{digest}.json"
        rights = []
        fulfilments = []

        for operation in ("acquisition", "storage"):
            right = self.fixture.right(operation)
            right.update(
                {
                    "rights_record_id": f"right:{name}:{operation}",
                    "scope_type": "retrieval",
                    "scope_id": retrieval_id,
                }
            )

            if conditional:
                right["status"] = "conditional"
                right["rights_conditions"] = [CONDITION]
                fulfilments.append(
                    {
                        "schema_version": "condition-fulfilments-v1",
                        "fulfilment_id": f"fulfilment:{name}:{operation}",
                        "created_at": store_fixtures.TEST_TIMESTAMP,
                        "rights_record_id": right["rights_record_id"],
                        "condition": CONDITION,
                        "subject_type": "retrieval",
                        "subject_id": retrieval_id,
                        "status": "satisfied",
                        "satisfied_at": store_fixtures.TEST_TIMESTAMP,
                        "expires_at": None,
                        "evidence_sha256": "c" * 64,
                        "supersedes_fulfilment_id": None,
                    }
                )

            rights.append(right)

        event = {
            "schema_version": "retrieval-events-v1",
            "retrieval_id": retrieval_id,
            "created_at": RECORDED_AT,
            "request_context_type": "source",
            "request_context_id": self.fixture.profile.source_id,
            "source_group_id": self.fixture.profile.source_group_id,
            "requested_url": f"https://example.invalid/api/{name}",
            "final_url": f"https://example.invalid/api/{name}",
            "retrieved_at": EVENT_AT,
            "acquisition_method": "manual_download",
            "acquisition_scope": "sample",
            "rights_record_ids": [right["rights_record_id"] for right in rights],
            "http_status": 200,
            "response_headers": {},
            "response_metadata_sha256": "b" * 64,
            "response_path": path,
            "response_sha256": digest,
            "response_bytes": len(body),
            "outcome": "succeeded",
            "error_code": None,
            "error_detail": None,
        }

        return ManifestPlan(
            rights=rights,
            retrieval_events=[event],
            condition_fulfilments=fulfilments,
            blobs=[PlannedBlob(path, body, digest)],
        )

    def _registry_bytes(self) -> dict[str, bytes]:
        """Сохранить точные байты существующих журналов для сравнения."""

        manifest_dir = self.fixture.project_root / "manifests"

        return {
            path.name: path.read_bytes()
            for path in manifest_dir.glob("*.jsonl")
        }

    def _resolve_collection(self) -> tuple[str, ...]:
        """Проверить разрешения нового запроса независимо от старых ответов."""

        return resolve_collection_rights(
            self.store,
            self.fixture.profile,
            acquisition_method="manual_download",
            acquisition_scope="sample",
            allowed_rights_record_ids=tuple(
                right["rights_record_id"] for right in self.store.records("rights")
            ),
        )

    def test_source_event_without_work_accepts_exact_rights(self) -> None:
        """Событие источника не должно требовать вымышленной работы или артефакта."""

        plan = self._plan(conditional=True)
        self.store.commit(plan)

        self.assertFalse(self.store.records("works"))
        self.assertFalse(self.store.records("artifacts"))
        decisions = self.store.records("operation_decisions")
        self.assertEqual(len(decisions), 2)

        for decision in decisions:
            operation = decision["operation"]
            self.assertEqual(decision["rights_record_ids"], [f"right:first:{operation}"])
            self.assertEqual(
                decision["condition_fulfilment_ids"],
                [f"fulfilment:first:{operation}"],
            )
            self.assertEqual(decision["status"], "allowed")

        self.assertTrue(self.store.audit().ok)

    def test_exact_rights_override_broad_source_status_only_for_target(self) -> None:
        """Более узкое основание определяет только указанное событие источника."""

        plan = self._plan()
        plan.rights.extend(
            self.fixture.right(operation, status="prohibited")
            for operation in ("acquisition", "storage")
        )
        self.store.commit(plan)

        self.assertTrue(self.store.audit().ok)

        with self.assertRaises(ManifestError):
            self._resolve_collection()

    def test_different_event_of_same_source_cannot_reuse_exact_rights(self) -> None:
        """Одинаковый источник не превращает точечное разрешение в общее."""

        first = self._plan()
        self.store.commit(first)
        original = self._registry_bytes()
        second = self._plan("second")
        second.rights = []
        second.retrieval_events[0]["rights_record_ids"] = first.retrieval_events[0][
            "rights_record_ids"
        ]

        with self.assertRaises(ManifestError):
            self.store.commit(second)

        self.assertEqual(self._registry_bytes(), original)

    def test_missing_retrieval_scope_is_rejected(self) -> None:
        """Область права должна ссылаться на существующее или совместно новое событие."""

        plan = self._plan()

        with self.assertRaisesRegex(ManifestError, "отсутствующее событие получения"):
            self.store.commit(ManifestPlan(rights=plan.rights))

        self.assertFalse(self.store.records("rights"))

    def test_retrieval_right_does_not_apply_to_work_or_pdf_or_text(self) -> None:
        """Право ответа не распространяется на артефакты, даже с тем же retrieval_id."""

        event_plan = self._plan()
        artifact_plan = self.fixture.plan()
        event = event_plan.retrieval_events[0]
        work = artifact_plan.works[0]
        artifact = artifact_plan.artifacts[0]
        artifact["retrievals"][0]["retrieval_id"] = event["retrieval_id"]

        for representation in ("pdf", "plain_text"):
            with self.subTest(representation=representation):
                artifact["representation"] = representation

                for right in event_plan.rights:
                    self.assertFalse(self.store._rights_apply(right, work, artifact))

        for context_type, context_id, context_artifact in (
            ("work", work["work_id"], None),
            ("artifact", artifact["artifact_record_id"], artifact),
        ):
            with self.subTest(context_type=context_type):
                event["request_context_type"] = context_type
                event["request_context_id"] = context_id
                right = event_plan.rights[0]
                self.assertTrue(
                    self.store._right_applies_to_event(right, event, work, context_artifact)
                )
                other_event = {**event, "retrieval_id": "retrieval:another"}
                self.assertFalse(
                    self.store._right_applies_to_event(
                        right, other_event, work, context_artifact
                    )
                )

    def test_artifact_cannot_reference_retrieval_right_as_its_permission(self) -> None:
        """Ссылка на событийное право в правах TXT должна отклоняться целиком."""

        event_plan = self._plan()
        artifact_plan = self.fixture.plan()
        artifact_plan.artifacts[0]["rights_record_ids"] = event_plan.retrieval_events[0][
            "rights_record_ids"
        ]

        with self.assertRaises(ManifestError):
            self.store.preflight([event_plan, artifact_plan])

    def test_new_event_preserves_snapshots_and_existing_history(self) -> None:
        """Добавление отдельного ответа не меняет прежние снимки, права и решения."""

        self.store.commit(self.fixture.plan())
        original = self._registry_bytes()
        self.store.commit(self._plan(conditional=True))
        current = self._registry_bytes()

        for filename, original_bytes in original.items():
            with self.subTest(filename=filename):
                if filename in {"works.jsonl", "artifacts.jsonl"}:
                    self.assertEqual(current[filename], original_bytes)

                else:
                    self.assertTrue(current[filename].startswith(original_bytes))

        self.assertEqual(self._resolve_collection(), ("right-acquisition", "right-storage"))
        self.assertTrue(self.store.audit().ok)

    def test_repeated_registration_is_idempotent(self) -> None:
        """Повтор точного пакета не создаёт новые решения и выполнения условий."""

        plan = self._plan(conditional=True)
        self.store.commit(plan)
        original = self._registry_bytes()
        result = self.store.commit(plan)

        self.assertFalse(any(result.inserted.values()))
        self.assertFalse(any(result.updated.values()))
        self.assertEqual(self._registry_bytes(), original)

    def test_later_exact_acquisition_prohibition_preserves_historical_decision(self) -> None:
        """Позднее знание о событии не переписывает уже принятое решение."""

        plan = self._plan()
        self.store.commit(plan)
        original = self._registry_bytes()
        prohibition = copy.deepcopy(plan.rights[0])
        prohibition.update(
            {
                "rights_record_id": "right:first:late-prohibition",
                "created_at": "2026-08-31T12:00:00+03:00",
                "rights_checked_at": "2026-08-31",
                "effective_from": store_fixtures.TEST_TIMESTAMP,
                "effective_from_evidence_sha256": "e" * 64,
                "status": "prohibited",
            }
        )
        self.store.commit(ManifestPlan(rights=[prohibition]))
        current = self._registry_bytes()

        self.assertEqual(current["operation_decisions.jsonl"], original["operation_decisions.jsonl"])
        self.assertEqual(current["retrieval_events.jsonl"], original["retrieval_events.jsonl"])
        self.assertTrue(current["rights.jsonl"].startswith(original["rights.jsonl"]))
        self.assertTrue(self.store.audit().ok)

    def test_later_exact_storage_prohibition_blocks_current_retention(self) -> None:
        """Историческое разрешение не отменяет новый запрет хранения ответа."""

        plan = self._plan()
        self.store.commit(plan)
        original = self._registry_bytes()
        prohibition = copy.deepcopy(plan.rights[1])
        prohibition.update(
            {
                "rights_record_id": "right:first:storage-prohibition",
                "created_at": "2026-08-31T12:00:00+03:00",
                "rights_checked_at": "2026-08-31",
                "status": "prohibited",
            }
        )

        with self.assertRaises(ManifestError):
            self.store.commit(ManifestPlan(rights=[prohibition]))

        self.assertEqual(self._registry_bytes(), original)

    def test_retrieval_permission_cannot_authorize_new_collection(self) -> None:
        """Даже явно перечисленные ID событийных прав не разрешают новый запрос."""

        self.store.commit(self._plan())

        with self.assertRaises(ManifestError):
            self._resolve_collection()

    def test_incompatible_acquisition_mode_is_rejected(self) -> None:
        """Разрешение другого способа или масштаба получения неприменимо."""

        for field, value in (("acquisition_method", "api"), ("acquisition_scope", "bulk")):
            with self.subTest(field=field):
                plan = self._plan()
                plan.rights[0][field] = value

                with self.assertRaises(ManifestError):
                    self.store.preflight([plan])

    def test_incompatible_storage_mode_is_rejected_for_retrieval_scope(self) -> None:
        """Уточнённое право хранения ответа должно совпадать с режимом события."""

        plan = self._plan()
        plan.rights[1]["acquisition_method"] = "api"
        plan.rights[1]["acquisition_scope"] = "sample"

        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

    def test_missing_or_wrong_event_condition_does_not_allow_acquisition(self) -> None:
        """Условие другого события нельзя использовать вместо точного выполнения."""

        self.store.commit(self._plan("other"))

        for variant in ("missing", "other_event", "wrong_condition", "revoked"):
            with self.subTest(variant=variant):
                plan = self._plan(conditional=True)

                if variant == "missing":
                    plan.condition_fulfilments = []

                elif variant == "other_event":
                    plan.condition_fulfilments[0]["subject_id"] = "retrieval:test:other"

                elif variant == "wrong_condition":
                    plan.condition_fulfilments[0]["condition"] = "Другое условие."

                else:
                    previous = plan.condition_fulfilments[0]
                    revocation = copy.deepcopy(previous)
                    revocation.update(
                        {
                            "fulfilment_id": previous["fulfilment_id"] + ":revoked",
                            "status": "revoked",
                            "created_at": "2026-08-28T12:00:00+03:00",
                            "supersedes_fulfilment_id": previous["fulfilment_id"],
                        }
                    )
                    plan.condition_fulfilments.append(revocation)

                with self.assertRaises(ManifestError):
                    self.store.preflight([plan])

    def test_expired_acquisition_or_current_storage_does_not_allow_response(self) -> None:
        """Истечение права блокирует получение или текущее хранение ответа."""

        for right_index, expires in ((0, "2026-08-28"), (1, "2026-08-31")):
            with self.subTest(right_index=right_index):
                plan = self._plan()
                plan.rights[right_index]["rights_expires_at"] = expires

                with self.assertRaises(ManifestError):
                    self.store.preflight([plan])

    def test_expired_or_late_condition_does_not_allow_response(self) -> None:
        """Событие нельзя подтвердить истёкшим или ещё неизвестным свидетельством."""

        for changes in (
            {"expires_at": "2026-08-28T12:00:00+03:00"},
            {"created_at": "2026-08-31T12:00:00+03:00"},
            {"satisfied_at": RECORDED_AT, "created_at": RECORDED_AT},
        ):
            with self.subTest(changes=changes):
                plan = self._plan(conditional=True)
                plan.condition_fulfilments[0].update(changes)

                with self.assertRaises(ManifestError):
                    self.store.preflight([plan])

    def test_disallowing_status_and_equal_specificity_conflict_are_not_bypassed(self) -> None:
        """Блокирующий статус нельзя обойти разрешением той же специфичности."""

        for status in ("prohibited", "permission_required", "unknown"):
            with self.subTest(status=status):
                plan = self._plan()
                blocking = copy.deepcopy(plan.rights[0])
                blocking["rights_record_id"] += ":blocking"
                blocking["status"] = status
                plan.rights.append(blocking)
                plan.retrieval_events[0]["rights_record_ids"].append(
                    blocking["rights_record_id"]
                )

                with self.assertRaises(ManifestError):
                    self.store.preflight([plan])

    def test_legacy_conditions_keep_exact_retrieval_subject(self) -> None:
        """Перенос старых полей условий не должен расширяться до всего проекта."""

        plan = self._plan(conditional=True)
        plan.condition_fulfilments = []

        for right in plan.rights:
            right["conditions_satisfied_at"] = store_fixtures.TEST_TIMESTAMP
            right["conditions_evidence_sha256"] = "d" * 64

        self.store.commit(plan)
        fulfilments = self.store.records("condition_fulfilments")
        self.assertEqual(len(fulfilments), 2)

        for fulfilment in fulfilments:
            self.assertEqual(fulfilment["subject_type"], "retrieval")
            self.assertEqual(fulfilment["subject_id"], "retrieval:test:first")

        self.assertTrue(self.store.audit().ok)

    def test_late_right_requires_evidence_of_earlier_effective_date(self) -> None:
        """Дата внесения не заменяет доказанное начало действия основания."""

        plan = self._plan()
        right = plan.rights[0]
        right["created_at"] = RECORDED_AT
        right["rights_checked_at"] = "2026-08-30"

        with self.assertRaises(ManifestError):
            self.store.preflight([plan])

        right["effective_from"] = store_fixtures.TEST_TIMESTAMP
        right["effective_from_evidence_sha256"] = "e" * 64
        self.store.commit(plan)
        self.assertTrue(self.store.audit().ok)


if __name__ == "__main__":
    unittest.main()
