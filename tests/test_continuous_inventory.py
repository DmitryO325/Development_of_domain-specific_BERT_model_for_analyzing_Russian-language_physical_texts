"""Офлайн-проверки непрерывной регистрации, контрольных точек и сохранности допуска."""

from __future__ import annotations

import hashlib
import json
import shutil
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from src.corpus import continuous_inventory as continuous
from src.corpus.journal_import import AmbiguousIssueEntryError, read_collected_article
from src.corpus.journal_inventory_input import prepare_inventory_batch, read_inventory_batch
from src.corpus.journal_inventory_registration import reconcile_inventory_registration
from src.corpus.manifests import ManifestStore, REGISTRY_FILES
from tests import test_journal_inventory_input as input_tests

ROOT = Path(__file__).resolve().parents[1]
MODULE = "src.corpus.continuous_inventory"


class ContinuousInventoryTests(unittest.TestCase):
    """Регистрировать только синтетические карточки во временных реестрах без сети."""

    def setUp(self) -> None:
        """Подключить готовые HTTP-исходники теста и независимые рабочие схемы."""

        self.fixture = input_tests.InventoryInputTests("test_exact_copy_and_original_snapshot_provenance")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.state_dir = self.root / "manifests/imports/continuous_inventory_test"
        shutil.copytree(ROOT / "manifests/schemas", self.root / "manifests/schemas")
        self.store = ManifestStore(project_root=self.root)

    def _run(self, **options: Any) -> dict[str, Any]:
        """Выполнить один настоящий проход без ожидания и фоновых процессов."""

        return continuous.run_inventory(self.root, state_dir=self.state_dir, once=True, **options)

    def _state(self) -> dict[str, Any]:
        """Прочитать контрольную точку выбранного временного запускателя."""

        return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))

    def _registry_bytes(self) -> dict[str, bytes | None]:
        """Зафиксировать все реестры, включая отсутствие ещё не созданных файлов."""

        return {
            kind: self.store.path_for(kind).read_bytes() if self.store.path_for(kind).exists() else None
            for kind in REGISTRY_FILES
        }

    def _batches(self) -> list[Path]:
        """Найти только подготовленные партии внутри временного проекта."""

        return sorted((self.root / "manifests/imports").rglob("batch.json"))

    def _receipt(self, status: dict[str, Any]) -> dict[str, Any]:
        """Прочитать последнюю квитанцию завершённого прохода."""

        return json.loads((self.root / status["last_receipt"]).read_text(encoding="utf-8"))

    def test_real_registration_keeps_unknown_rights_and_quarantine(self) -> None:
        """Новая партия учитывает HTML и PDF, но не выдаёт разрешений на обучение."""

        original, _, _ = self.fixture._fixture()
        before = original.read_bytes()
        status = self._run()

        self.assertEqual(status["completed_batches"], 1)
        self.assertEqual(status["seen_cards"], 1)
        self.assertFalse(status["pending"])
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "quarantined")
        self.assertEqual({item["representation"] for item in self.store.records("artifacts")}, {"html", "pdf"})
        self.assertTrue(all(item["acquisition_status"] == "recorded" for item in self.store.records("artifacts")))
        self.assertTrue(all(item["outcome"] == "recorded" for item in self.store.records("retrieval_events")))
        self.assertEqual(self.store.records("rights"), [])
        self.assertEqual(self.store.records("condition_fulfilments"), [])
        self.assertEqual({item["status"] for item in self.store.records("operation_decisions")}, {"unknown"})
        self.assertTrue(self.store.audit().ok)
        self.assertEqual(original.read_bytes(), before)
        receipt = self._receipt(status)
        self.assertIs(receipt["rights_changed"], False)
        self.assertIs(receipt["training_eligibility_changed"], False)
        self.fixture.client.fetch.assert_not_called()

    def test_unchanged_cards_do_not_create_another_batch_or_registry_records(self) -> None:
        """Пустой повторный проход не растит историю, события и количество партий."""

        self.fixture._fixture()
        first = self._run()
        before = self._registry_bytes()
        batches = self._batches()
        second = self._run()

        self.assertEqual(second["completed_batches"], first["completed_batches"])
        self.assertEqual(second["last_receipt"], first["last_receipt"])
        self.assertEqual(self._batches(), batches)
        self.assertEqual(self._registry_bytes(), before)

    def test_changed_card_is_rechecked_without_duplicate_registration(self) -> None:
        """Новая SHA карточки запускает проверку, а прежнее содержимое не дублируется."""

        original, _, _ = self.fixture._fixture()
        self._run()
        before = self._registry_bytes()
        original.write_bytes(original.read_bytes() + b" ")
        second = self._run()

        self.assertEqual(second["completed_batches"], 2)
        self.assertEqual(second["seen_cards"], 1)
        self.assertEqual(self._registry_bytes(), before)
        self.assertEqual(self._receipt(second)["report"][0]["status"], "already_registered")
        self.assertEqual(self._run()["completed_batches"], 2)

    def test_ufn_legacy_metadata_upgrade_registers_pdf_once_without_rewriting_history(self) -> None:
        """Новая PDF-ссылка обновляет ту же работу, сохраняя прежнюю партию, права и повторяемость."""

        article_url = "https://ufn.ru/ru/articles/2015/5/i/"
        pdf_url = "https://ufn.ru/ufn15/ufn15_5/Russian/r155i.pdf"
        html = input_tests.UFN_HTML.replace(input_tests.UFN_PDF_URL, pdf_url).replace(
            "/ufn2026/ufn2026_8/Russian/r268a.pdf", "/ufn15/ufn15_5/Russian/r155i.pdf",
        ).replace(input_tests.UFN_URL, article_url)

        with patch.dict(input_tests.FIXTURES, {"ufn": (article_url, html)}):
            metadata_path, record, _ = self.fixture._fixture("ufn", with_pdf=False)

        record["document"]["pdf_url"] = None
        self.fixture._save(metadata_path, record)
        original = metadata_path.parent.parent / "articles" / metadata_path.name
        original.parent.mkdir(parents=True)
        metadata_path.rename(original)
        first = self._run()
        work_id = self.store.records("works")[0]["work_id"]
        artifacts_before = {item["artifact_id"] for item in self.store.records("artifacts")}
        registry_before = self._registry_bytes()
        old_batch_path = self._batches()[0]
        frozen_before = {path: path.read_bytes() for path in old_batch_path.parent.rglob("*") if path.is_file()}

        self.assertEqual(first["completed_batches"], 1)
        self.assertEqual(first["seen_cards"], 1)
        self.assertEqual({item["representation"] for item in self.store.records("artifacts")}, {"html"})

        with patch.dict(input_tests.FIXTURES, {"ufn": (article_url, html)}):
            updated_path, updated_record, _ = self.fixture._fixture("ufn", with_pdf=True)

        self.assertEqual(updated_path, original)
        self.assertEqual(updated_record["document"]["pdf_url"], pdf_url)
        second = self._run()
        new_artifacts = [item for item in self.store.records("artifacts") if item["artifact_id"] not in artifacts_before]

        self.assertEqual(second["completed_batches"], 2)
        self.assertEqual(second["seen_cards"], 1)
        self.assertEqual([item["work_id"] for item in self.store.records("works")], [work_id])
        self.assertEqual(len(self.store.records("artifacts")), 2)
        self.assertEqual([item["representation"] for item in new_artifacts], ["pdf"])
        self.assertEqual(new_artifacts[0]["work_id"], work_id)
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "quarantined")

        for kind in ("rights", "condition_fulfilments"):
            self.assertEqual(self._registry_bytes()[kind], registry_before[kind])

        self.assertIs(self._receipt(second)["rights_changed"], False)
        self.assertIs(self._receipt(second)["training_eligibility_changed"], False)
        old_batch = read_inventory_batch(old_batch_path, self.root)
        self.assertEqual(old_batch.records[0].document.to_dict(), record["document"])
        self.assertIsNone(old_batch.records[0].pdf_response)
        self.assertEqual(frozen_before, {
            path: path.read_bytes() for path in old_batch_path.parent.rglob("*") if path.is_file()
        })
        registry_after = self._registry_bytes()
        repeated = self._run()

        self.assertEqual(repeated["completed_batches"], 2)
        self.assertEqual(repeated["last_receipt"], second["last_receipt"])
        self.assertEqual(self._registry_bytes(), registry_after)
        self.assertTrue(self.store.audit().ok)
        self.fixture.client.fetch.assert_not_called()

    def test_later_download_waits_for_the_next_pass(self) -> None:
        """Появившаяся после подготовки карточка не расширяет уже закреплённую партию."""

        self.fixture._fixture()

        def prepare_and_add(project_root: Path, output_dir: Path, *args: Any, **options: Any) -> Path:
            """Добавить синтетическую загрузку только после сохранения исходного batch.json."""

            batch = prepare_inventory_batch(project_root, output_dir, *args, **options)
            self.fixture._fixture("ufn", with_pdf=False)

            return batch

        with patch(f"{MODULE}.prepare_inventory_batch", side_effect=prepare_and_add):
            first = self._run()

        self.assertEqual(first["seen_cards"], 1)
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertEqual(len(read_inventory_batch(self._batches()[0], self.root).records), 1)
        second = self._run()

        self.assertEqual(second["seen_cards"], 2)
        self.assertEqual(second["completed_batches"], 2)
        self.assertEqual(len(self.store.records("works")), 2)

    def test_previously_registered_source_is_checkpointed_without_new_events(self) -> None:
        """Уже учтённая вручную карточка записывается в контрольную точку без дублей."""

        self.fixture._fixture()
        batch_path = prepare_inventory_batch(self.root, self.fixture.output, ("jetp",))
        batch = read_inventory_batch(batch_path, self.root)
        plan, hashes, _ = reconcile_inventory_registration(
            self.store, batch.records, registered_at=input_tests.PREPARED_AT,
        )
        self.store.commit(plan, expected_snapshot_hashes=hashes)
        before = self._registry_bytes()
        status = self._run()

        self.assertEqual(status["seen_cards"], 1)
        self.assertEqual(self._registry_bytes(), before)
        self.assertEqual(self._receipt(status)["report"][0]["status"], "already_registered")

    def test_same_pdf_for_different_cards_is_deferred_and_not_retried_unchanged(self) -> None:
        """Конфликт общего PDF остаётся в квитанции и не превращается в бесконечные партии."""

        self.fixture._fixture("jetp")
        self.fixture._fixture("ioffe")
        status = self._run()
        receipt = self._receipt(status)

        self.assertEqual(status["seen_cards"], 2)
        self.assertEqual(len(receipt["report"]), 2)
        self.assertTrue(all(item["status"] == "deferred" for item in receipt["report"]))
        self.assertEqual(self.store.records("works"), [])
        self.assertEqual(self.store.records("artifacts"), [])
        self.assertEqual(self._run()["completed_batches"], 1)
        self.assertEqual(len(self._batches()), 1)

    def test_non_russian_card_is_recorded_as_skipped_once(self) -> None:
        """Исключённая по языку карточка остаётся проверяемой, но не попадает в реестр работ."""

        html = input_tests.LETTERS_HTML.replace("Исследование свойств", "Study of properties")
        html = html.replace("воды", "water").replace("Изучены свойства", "Properties studied")
        html = html.replace("Получены результаты.", "Results obtained.")
        original, record, _ = self.fixture._fixture("jetp_letters", html=html, with_pdf=False)
        record["status"] = "skipped_non_russian"
        self.fixture._save(original, record)
        status = self._run()

        self.assertEqual(status["seen_cards"], 1)
        self.assertEqual(len(self._receipt(status)["skipped_non_russian"]), 1)
        self.assertEqual(self.store.records("works"), [])
        self.assertEqual(self._run()["completed_batches"], 1)

    def test_ambiguous_source_is_deferred_without_blocking_other_cards(self) -> None:
        """Конфликт оглавления сохраняется отдельно, а однозначная статья регистрируется."""

        self.fixture._fixture()
        original, _, raw_root = self.fixture._ambiguous_fixture()
        before = {path: path.read_bytes() for path in raw_root.rglob("*") if path.is_file()}
        card_bytes = original.read_bytes()
        status = self._run()
        receipt = self._receipt(status)
        batch = read_inventory_batch(self._batches()[0], self.root)

        self.assertEqual(status["seen_cards"], 2)
        self.assertFalse(status["pending"])
        self.assertEqual(len(batch.records), 1)
        self.assertEqual(len(batch.deferred), 1)
        self.assertEqual(receipt["deferred_source"], list(batch.deferred))
        self.assertEqual(receipt["skipped_non_russian"], [])
        self.assertEqual(receipt["report"][0]["status"], "deferred")
        self.assertEqual(len(batch.deferred[0]["details"]["candidates"]), 2)
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertEqual(self.store.records("rights"), [])
        self.assertEqual(self.store.records("condition_fulfilments"), [])
        self.assertEqual(original.read_bytes(), card_bytes)
        self.assertEqual(before, {path: path.read_bytes() for path in raw_root.rglob("*") if path.is_file()})

        registries = self._registry_bytes()
        self.assertEqual(self._run()["completed_batches"], 1)
        self.assertEqual(self._registry_bytes(), registries)

    def test_all_deferred_batch_has_receipt_without_registering_a_work(self) -> None:
        """Партия только из конфликтов не теряется и не повторяется без изменения исходников."""

        self.fixture._ambiguous_fixture()
        before = self._registry_bytes()
        status = self._run()

        self.assertEqual(status["seen_cards"], 1)
        self.assertFalse(status["pending"])
        self.assertEqual(len(self._receipt(status)["deferred_source"]), 1)
        self.assertEqual(self._registry_bytes(), before)
        self.assertEqual(self._run()["completed_batches"], 1)

    def test_resolved_source_version_is_rechecked_after_deferral(self) -> None:
        """Новое подтверждённое оглавление рассматривается снова, старый конфликт сохраняется."""

        self.fixture._ambiguous_fixture()
        first = self._run()
        old_receipt = (self.root / first["last_receipt"]).read_bytes()
        self.fixture._fixture("quantum", with_pdf=False, html=input_tests.ISSUE_HTML, issue_toc=True)
        second = self._run()

        self.assertEqual(second["completed_batches"], 2)
        self.assertEqual(second["seen_cards"], 1)
        self.assertEqual(self._receipt(second)["deferred_source"], [])
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertEqual((self.root / first["last_receipt"]).read_bytes(), old_receipt)

    def test_old_ambiguity_failure_resumes_without_deleting_incomplete_packet(self) -> None:
        """После старой общей ошибки та же партия завершается с отдельным конфликтом."""

        self.fixture._fixture()
        self.fixture._ambiguous_fixture()

        def old_reader(*arguments: Any, **options: Any) -> Any:
            """Воспроизвести прежнюю остановку всей партии на неоднозначном оглавлении."""

            try:
                return read_collected_article(*arguments, **options)

            except AmbiguousIssueEntryError as exception:
                raise ValueError("Оглавление не содержит однозначной записи выбранной статьи") from exception

        with patch("src.corpus.journal_inventory_input.read_collected_article", side_effect=old_reader):
            with self.assertRaisesRegex(ValueError, "однозначной записи"):
                self._run()

        abandoned = self.root / self._state()["pending"]["directory"]
        old_files = {path: path.read_bytes() for path in abandoned.rglob("*") if path.is_file()}
        self.assertTrue(old_files)
        self.assertFalse((abandoned / "batch.json").exists())
        status = self._run()

        self.assertFalse(status["pending"])
        self.assertEqual(status["completed_batches"], 1)
        self.assertEqual(len(self._receipt(status)["deferred_source"]), 1)
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertNotEqual(self._batches()[0].parent, abandoned)
        self.assertEqual(old_files, {path: path.read_bytes() for path in abandoned.rglob("*") if path.is_file()})

    def test_legacy_batch_and_receipt_resume_without_rewriting_them(self) -> None:
        """Завершённые партии v1 и квитанции без deferred_source остаются совместимыми."""

        self.fixture._fixture()
        batch_path = prepare_inventory_batch(self.root, self.fixture.output, ("jetp",))
        packet = json.loads(batch_path.read_text(encoding="utf-8"))
        packet["schema_version"] = "journal-inventory-batch-v1"
        packet.pop("deferred")
        batch_path.write_text(json.dumps(packet), encoding="utf-8")
        batch = read_inventory_batch(batch_path, self.root)
        receipt_path = continuous._register_batch(self.root, batch)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt.pop("deferred_source")
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        before = (batch_path.read_bytes(), receipt_path.read_bytes(), self._registry_bytes())
        status = self._run(seed_batch=batch_path, seed_receipt=receipt_path)

        self.assertEqual(status["completed_batches"], 1)
        self.assertEqual(status["seen_cards"], 1)
        self.assertEqual((batch_path.read_bytes(), receipt_path.read_bytes(), self._registry_bytes()), before)
        self.assertEqual(self._run()["completed_batches"], 1)

    def test_missing_deferred_evidence_or_report_is_not_accepted_as_completion(self) -> None:
        """Конфликт нельзя считать рассмотренным без деталей и адресной записи квитанции."""

        self.fixture._ambiguous_fixture()
        status = self._run()
        batch_path = self._batches()[0]
        receipt_path = self.root / status["last_receipt"]
        original = receipt_path.read_bytes()

        for field in ("deferred_source", "report"):
            receipt = json.loads(original)
            receipt[field] = []
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            with self.subTest(field=field), self.assertRaises(ValueError):
                continuous._completion(self.root, batch_path, receipt_path)

        self.assertEqual(self.store.records("works"), [])

    def test_pending_prepared_batch_is_reused_after_restart(self) -> None:
        """Остановка до записи оставляет намерение и готовую партию для следующего прохода."""

        self.fixture._fixture()

        with patch(f"{MODULE}._register_batch", side_effect=OSError("Тестовый сбой до записи")):
            with self.assertRaises(OSError):
                self._run()

        batches = self._batches()
        self.assertEqual(len(batches), 1)
        self.assertIsNotNone(self._state()["pending"])
        self.assertEqual(self.store.records("works"), [])
        status = self._run()

        self.assertEqual(status["completed_batches"], 1)
        self.assertFalse(status["pending"])
        self.assertEqual(self._batches(), batches)
        self.assertEqual(len(self.store.records("works")), 1)

    def test_commit_without_receipt_resumes_without_duplicate_history(self) -> None:
        """Сбой сохранения квитанции после commit не приводит к повторным событиям."""

        self.fixture._fixture()

        with patch(f"{MODULE}._write_receipt", side_effect=OSError("Нет места для тестовой квитанции")):
            with self.assertRaises(OSError):
                self._run()

        self.assertEqual(len(self.store.records("works")), 1)
        self.assertIsNotNone(self._state()["pending"])
        before = self._registry_bytes()
        batches = self._batches()
        status = self._run()

        self.assertEqual(status["completed_batches"], 1)
        self.assertFalse(status["pending"])
        self.assertEqual(self._batches(), batches)
        self.assertEqual(self._registry_bytes(), before)
        self.assertEqual(self._receipt(status)["report"][0]["status"], "already_registered")

    def test_incomplete_preparation_is_preserved_and_rebuilt_in_another_directory(self) -> None:
        """Частично записанный пакет не удаляется и не выдаётся за готовую партию."""

        self.fixture._fixture()

        def partial_prepare(project_root: Path, output_dir: Path, *args: Any, **options: Any) -> Path:
            """Имитировать остановку после первой записи, но до публикации batch.json."""

            output_dir.mkdir(parents=True)
            (output_dir / "partial.txt").write_text("Незавершённая тестовая копия", encoding="utf-8")
            raise OSError("Остановлена подготовка тестовой партии")

        with patch(f"{MODULE}.prepare_inventory_batch", side_effect=partial_prepare):
            with self.assertRaises(OSError):
                self._run()

        abandoned = self.root / self._state()["pending"]["directory"]
        self.assertEqual(self._batches(), [])
        status = self._run()

        self.assertEqual(status["completed_batches"], 1)
        self.assertNotEqual(self._batches()[0].parent, abandoned)
        self.assertEqual((abandoned / "partial.txt").read_text(encoding="utf-8"), "Незавершённая тестовая копия")

    def test_completed_receipt_is_reused_when_final_checkpoint_was_not_saved(self) -> None:
        """После сбоя контрольной точки готовая квитанция не требует повторной регистрации."""

        self.fixture._fixture()
        save_state = continuous._save_state

        def fail_completed_state(path: Path, state: dict[str, Any]) -> None:
            """Отказать только при последней записи после уже сохранённой квитанции."""

            if state["completed"]:
                raise OSError("Тестовый сбой контрольной точки")

            save_state(path, state)

        with patch(f"{MODULE}._save_state", side_effect=fail_completed_state):
            with self.assertRaises(OSError):
                self._run()

        before = self._registry_bytes()
        batches = self._batches()
        receipts = sorted(self.state_dir.rglob("receipts/*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertIsNotNone(self._state()["pending"])

        with patch.object(ManifestStore, "commit", side_effect=AssertionError("Повторный commit не нужен")):
            status = self._run()

        self.assertEqual(status["completed_batches"], 1)
        self.assertEqual(self._registry_bytes(), before)
        self.assertEqual(self._batches(), batches)
        self.assertEqual(sorted(self.state_dir.rglob("receipts/*.json")), receipts)

    def test_seed_uses_verified_batch_and_receipt_without_recopying(self) -> None:
        """Завершённая ручная партия может стать начальной контрольной точкой."""

        self.fixture._fixture()
        original = self._run()
        batch = self._batches()[0]
        receipt = self.root / original["last_receipt"]
        before = self._registry_bytes()
        status = continuous.run_inventory(
            self.root, state_dir=self.root / "manifests/imports/another_runner", once=True,
            seed_batch=batch, seed_receipt=receipt,
        )

        self.assertEqual(status["seen_cards"], 1)
        self.assertEqual(status["completed_batches"], 1)
        self.assertEqual(self._batches(), [batch])
        self.assertEqual(self._registry_bytes(), before)

    def test_seed_rejects_dry_run_or_changed_rights_receipt(self) -> None:
        """Предварительный расчёт или квитанция с изменением прав не подтверждают регистрацию."""

        self.fixture._fixture()
        status = self._run()
        batch = self._batches()[0]
        receipt_path = self.root / status["last_receipt"]
        original = receipt_path.read_bytes()
        before = self._registry_bytes()

        for field in ("dry_run", "rights_changed", "training_eligibility_changed"):
            receipt = json.loads(original)

            if field == "dry_run":
                receipt["result"]["dry_run"] = True

            else:
                receipt[field] = True

            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            with self.subTest(field=field), self.assertRaises(ValueError):
                continuous.run_inventory(
                    self.root, state_dir=self.root / "manifests/imports/rejected_seed", once=True,
                    seed_batch=batch, seed_receipt=receipt_path,
                )

        self.assertEqual(self._registry_bytes(), before)

    def test_corrupt_checkpoint_fails_without_changing_registries(self) -> None:
        """Повреждённый JSON не сбрасывается в пустую историю успешно обработанных карточек."""

        self.fixture._fixture()
        self._run()
        before = self._registry_bytes()
        state_path = self.state_dir / "state.json"

        for body in ("{broken", "[]", '{"schema_version":"unknown"}'):
            state_path.write_text(body, encoding="utf-8")

            with self.subTest(body=body), self.assertRaises(ValueError):
                self._run()

            self.assertEqual(state_path.read_text(encoding="utf-8"), body)
            self.assertEqual(self._registry_bytes(), before)

    def test_changed_completed_receipt_fails_before_any_new_registration(self) -> None:
        """Контрольная точка привязана к точным байтам завершившей её квитанции."""

        self.fixture._fixture()
        status = self._run()
        before = self._registry_bytes()
        receipt = self.root / status["last_receipt"]
        receipt.write_bytes(receipt.read_bytes() + b" ")

        with self.assertRaises(ValueError):
            self._run()

        self.assertEqual(self._registry_bytes(), before)
        self.assertEqual(len(self._batches()), 1)

    def test_changed_completed_batch_fails_before_any_new_registration(self) -> None:
        """Квитанция не разрешает доверять изменённому описанию обработанной партии."""

        self.fixture._fixture()
        self._run()
        before = self._registry_bytes()
        batch = self._batches()[0]
        batch.write_bytes(batch.read_bytes() + b" ")

        with self.assertRaises(ValueError):
            self._run()

        self.assertEqual(self._registry_bytes(), before)

    def test_corrupt_journal_is_preserved_without_writing_any_registry(self) -> None:
        """Незавершённая строка или повреждённое событие требуют явной проверки журнала."""

        self.fixture._fixture()
        self._run()
        before = self._registry_bytes()
        state = (self.state_dir / "state.json").read_bytes()
        journal = self.state_dir / "events.jsonl"

        for body in (b'{"at":"time","event":"interrupted"}', b"{broken}\n", b"[]\n"):
            journal.write_bytes(body)

            with self.subTest(body=body), self.assertRaises(ValueError):
                self._run()

            self.assertEqual(journal.read_bytes(), body)
            self.assertEqual((self.state_dir / "state.json").read_bytes(), state)
            self.assertEqual(self._registry_bytes(), before)

    def test_missing_checkpoint_is_not_silently_reset_over_existing_journal(self) -> None:
        """Потеря контрольной точки не объявляет ранее работавший запускатель новым."""

        self.fixture._fixture()
        self._run()
        before = self._registry_bytes()
        state = self.state_dir / "state.json"
        state.unlink()

        with self.assertRaises(ValueError):
            self._run()

        self.assertFalse(state.exists())
        self.assertEqual(self._registry_bytes(), before)

    def test_state_directory_symlink_is_rejected_before_registration(self) -> None:
        """Подмена служебного каталога ссылкой не перенаправляет запись реестров и журнала."""

        self.fixture._fixture()
        target = self.root / "manifests/imports/other_state"
        target.mkdir(parents=True)
        self.state_dir.symlink_to(target, target_is_directory=True)

        with self.assertRaises(ValueError):
            self._run()

        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.store.records("works"), [])

    def test_file_in_place_of_source_directory_does_not_hide_downloads(self) -> None:
        """Ошибочный обычный файл вместо каталога сборщика не считается пустым источником."""

        self.fixture._fixture()
        wrong_directory = self.root / "manifests/imports/journals/ufn"
        wrong_directory.write_text("Это не каталог источника", encoding="utf-8")

        with self.assertRaises(ValueError):
            self._run()

        self.assertEqual(self.store.records("works"), [])
        self.assertEqual(self._batches(), [])

    def test_only_one_runner_can_hold_global_registration_lock(self) -> None:
        """Другой каталог состояния не позволяет обойти блокировку второго запускателя."""

        self.fixture._fixture()

        with continuous._runner_lock(self.root):
            with self.assertRaises(ValueError):
                self._run()

            with self.assertRaises(ValueError):
                continuous.run_inventory(
                    self.root, state_dir=self.root / "manifests/imports/second_runner", once=True,
                )

        self.assertEqual(self.store.records("works"), [])
        self.assertEqual(self._run()["completed_batches"], 1)

    def test_interval_must_be_finite_and_positive(self) -> None:
        """Нулевой, отрицательный и бесконечный интервал не запускают цикл."""

        for interval in (True, 0.0, -1.0, float("inf"), float("-inf"), float("nan")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self._run(interval_seconds=interval)

        self.assertFalse((self.state_dir / "state.json").exists())
        self.assertEqual(self._batches(), [])

    def test_repeated_loop_waits_and_stops_without_extra_batch(self) -> None:
        """Два прохода цикла разделены ожиданием, а остановка не запускает третью партию."""

        self.fixture._fixture()
        stop = MagicMock()
        stop.is_set.side_effect = [False, False, True]

        with patch(f"{MODULE}.threading.Event", return_value=stop):
            status = continuous.run_inventory(self.root, state_dir=self.state_dir, interval_seconds=12.5)

        self.assertEqual(status["completed_batches"], 1)
        self.assertEqual(len(self._batches()), 1)
        self.assertEqual(stop.wait.call_count, 2)
        stop.wait.assert_called_with(12.5)

    def test_status_without_state_is_read_only(self) -> None:
        """Просмотр отсутствующего состояния не создаёт каталог, реестры или блокировку."""

        before = {path.relative_to(self.root) for path in self.root.rglob("*")}
        status = continuous.load_inventory_status(self.root, state_dir=self.state_dir)

        self.assertFalse(status["state_exists"])
        self.assertEqual(status["seen_cards"], 0)
        self.assertEqual(status["completed_batches"], 0)
        self.assertEqual({path.relative_to(self.root) for path in self.root.rglob("*")}, before)

    def test_checkpoint_pins_batch_and_receipt_hashes(self) -> None:
        """История хранит проверяемые контрольные суммы обеих частей завершённой партии."""

        self.fixture._fixture()
        self._run()
        completed = self._state()["completed"]
        self.assertEqual(len(completed), 1)

        for name in ("batch", "receipt"):
            body = (self.root / completed[0][f"{name}_path"]).read_bytes()
            self.assertEqual(hashlib.sha256(body).hexdigest(), completed[0][f"{name}_sha256"])


if __name__ == "__main__":
    unittest.main()
