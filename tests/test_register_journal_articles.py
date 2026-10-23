"""Интеграционные проверки команды импорта журналов во временные реестры."""

from __future__ import annotations

import io
import json
import tempfile
import unittest

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from scripts import register_journal_articles
from src.corpus.manifests import ManifestConcurrencyError, ManifestPlan, ManifestStore
from src.corpus.registration import reconcile_document_plan
from tests.test_journal_registration import RIGHTS_IDS, JournalFixture


class RegisterJournalArticlesTests(unittest.TestCase):
    """Проверки записи только после полной проверки исходников и явно выбранных прав."""

    def setUp(self) -> None:
        """Изолировать исходники, реестры и вывод команды от рабочего корпуса."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.fixture = JournalFixture(Path(temporary_directory.name))
        self.store = self.fixture.store()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.stdout = self.patches.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        self.stderr = self.patches.enter_context(patch("sys.stderr", new_callable=io.StringIO))
        self.patches.enter_context(patch.object(register_journal_articles, "PROJECT_ROOT", self.fixture.root))
        self.store_factory = self.patches.enter_context(
            patch.object(register_journal_articles, "ManifestStore", side_effect=self._store_factory)
        )

    def _store_factory(self, **arguments: Any) -> ManifestStore:
        """Подставить рабочие схемы, сохранив временные проект и каталог реестров."""

        self.assertEqual(arguments["project_root"], self.fixture.root)
        self.assertEqual(arguments["manifest_dir"], self.fixture.manifest_dir)

        return self.store

    def _arguments(self, command: str = "register") -> list[str]:
        """Составить команду с явными путями, чтобы исключить доступ к рабочим партиям."""

        arguments = [
            command, "jetp",
            "--input-dir", str(self.fixture.input_dir),
            "--raw-dir", str(self.fixture.raw_root),
            "--manifest-dir", str(self.fixture.manifest_dir),
        ]

        if command == "register":
            for record_id in RIGHTS_IDS:
                arguments.extend(("--rights-record-id", record_id))

        return arguments

    def _seed_rights(self, *, status: str = "allowed") -> None:
        """Добавить только временные синтетические права для интеграционного теста."""

        self.store.commit(ManifestPlan(rights=self.fixture.rights(status=status)))

    def test_check_does_not_open_store_or_write_files(self) -> None:
        """Проверка исходников работает без прав, реестров и любой записи."""

        before = self.fixture.snapshot()

        result = register_journal_articles.main(self._arguments("check"))

        self.assertEqual(result, 0)
        self.store_factory.assert_not_called()
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertIn("с PDF: 1", self.stdout.getvalue())
        self.assertIn("Файлы не изменены", self.stdout.getvalue())

    def test_register_requires_explicit_right_ids(self) -> None:
        """Само наличие скачанных файлов не разрешает подразумевать права на импорт."""

        arguments = self._arguments("check")
        arguments[0] = "register"
        before = self.fixture.snapshot()

        with self.assertRaises(SystemExit) as exception:
            register_journal_articles.main(arguments)

        self.assertEqual(exception.exception.code, 2)
        self.store_factory.assert_not_called()
        self.assertEqual(self.fixture.snapshot(), before)

    def test_unknown_right_ids_do_not_create_registry_or_artifacts(self) -> None:
        """Отсутствующие основания останавливают импорт без подделки rights.jsonl."""

        before = self.fixture.snapshot()

        result = register_journal_articles.main(self._arguments())

        self.assertEqual(result, 1)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertFalse(self.store.records("works"))
        self.assertIn("отсутствуют", self.stderr.getvalue())

    def test_unknown_permission_status_is_not_treated_as_allowed(self) -> None:
        """Неизвестный допуск не превращается в разрешение при регистрации локальных PDF."""

        self._seed_rights(status="unknown")
        before = self.fixture.snapshot()

        result = register_journal_articles.main(self._arguments())

        self.assertEqual(result, 1)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.store.records("works"), [])
        self.assertIn("не разрешена", self.stderr.getvalue())

    def test_dry_run_does_not_change_any_bytes(self) -> None:
        """Явный сухой прогон выполняет проверку, но не создаёт даже артефакты."""

        self._seed_rights()
        before = self.fixture.snapshot()

        result = register_journal_articles.main(self._arguments() + ["--dry-run"])

        self.assertEqual(result, 0)
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.store.records("works"), [])
        self.assertIn("Предварительная проверка регистрации", self.stdout.getvalue())

    def test_register_and_repeat_do_not_change_source_cards_or_create_duplicates(self) -> None:
        """Команда атомарно записывает план, а повтор сохраняет исходники и pending."""

        self._seed_rights()
        source_record = self.fixture.record_path.read_bytes()

        self.assertEqual(register_journal_articles.main(self._arguments()), 0)
        registered = self.fixture.snapshot()
        self.assertEqual(register_journal_articles.main(self._arguments()), 0)

        self.assertEqual(self.fixture.snapshot(), registered)
        self.assertEqual(self.fixture.record_path.read_bytes(), source_record)
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertEqual(len(self.store.records("artifacts")), 2)
        self.assertEqual(len(self.store.records("retrieval_events")), 2)
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "pending")
        self.assertLessEqual(
            {record["operation"] for record in self.store.records("operation_decisions")},
            {"acquisition", "storage"},
        )

    def test_corrupt_response_stops_before_store_is_opened(self) -> None:
        """Повреждение PDF выявляется до прав, планирования и любого частичного коммита."""

        self._seed_rights()
        evidence = self.fixture.record["pdf_response"]
        (self.fixture.raw_root / evidence["relative_path"]).write_bytes(b"damaged")
        before = self.fixture.snapshot()

        result = register_journal_articles.main(self._arguments())

        self.assertEqual(result, 1)
        self.store_factory.assert_not_called()
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertIn("Контрольная сумма", self.stderr.getvalue())

    def test_corrupt_later_card_does_not_allow_prefix_commit(self) -> None:
        """Нельзя зарегистрировать начало партии, если следующий JSON повреждён."""

        self._seed_rights()
        (self.fixture.record_path.parent / "zzz-invalid.json").write_text("{", encoding="utf-8")
        before = self.fixture.snapshot()

        result = register_journal_articles.main(self._arguments())

        self.assertEqual(result, 1)
        self.store_factory.assert_not_called()
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertEqual(self.store.records("works"), [])

    def test_article_limit_does_not_hide_corrupt_duplicate_or_later_card(self) -> None:
        """Ограничение размера плана не подменяет проверку целостности входной партии."""

        self._seed_rights()
        (self.fixture.record_path.parent / "zzz-invalid.json").write_text("{", encoding="utf-8")
        before = self.fixture.snapshot()

        result = register_journal_articles.main(self._arguments() + ["--max-articles", "1"])

        self.assertEqual(result, 1)
        self.store_factory.assert_not_called()
        self.assertEqual(self.fixture.snapshot(), before)

    def test_negative_limit_is_rejected_without_store(self) -> None:
        """Отрицательный лимит не запускает обработку партии или запись."""

        before = self.fixture.snapshot()

        result = register_journal_articles.main(self._arguments("check") + ["--max-articles", "-1"])

        self.assertEqual(result, 1)
        self.store_factory.assert_not_called()
        self.assertEqual(self.fixture.snapshot(), before)

    def test_commit_concurrency_conflict_reconciles_again_before_retry(self) -> None:
        """После смены снимка команда не повторяет старые CAS-хеши без новой сверки."""

        self._seed_rights()
        commit = self.store.commit
        attempts = 0

        def retry_commit(*arguments: Any, **keywords: Any) -> Any:
            """Сымитировать одну конкурентную запись, затем выполнить обычный коммит."""

            nonlocal attempts
            attempts += 1

            if attempts == 1:
                raise ManifestConcurrencyError("Тестовая конкурентная запись")

            return commit(*arguments, **keywords)

        with patch.object(self.store, "commit", side_effect=retry_commit):
            with patch.object(register_journal_articles, "reconcile_document_plan",
                              wraps=reconcile_document_plan) as reconcile:
                result = register_journal_articles.main(self._arguments())

        self.assertEqual(result, 0)
        self.assertEqual(attempts, 2)
        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(len(self.store.records("works")), 1)

    def test_concurrency_retries_are_bounded_and_do_not_write(self) -> None:
        """Три последовательных конфликта завершают запуск вместо бесконечного цикла."""

        self._seed_rights()
        before = self.fixture.snapshot()

        with patch.object(self.store, "commit", side_effect=ManifestConcurrencyError("Изменился снимок")) as commit:
            result = register_journal_articles.main(self._arguments())

        self.assertEqual(result, 1)
        self.assertEqual(commit.call_count, 3)
        self.assertEqual(self.fixture.snapshot(), before)

    def test_check_default_paths_match_each_collectors_layout(self) -> None:
        """Для каждого источника проверка читает собственные исходники и каталог карточек."""

        for source in ("ioffe", "jetp", "jetp_letters", "quantum", "ufn"):
            with self.subTest(source=source):
                suffix = Path("ioffe") if source == "ioffe" else Path("journals") / source

                with patch.object(register_journal_articles, "load_articles", return_value=[]) as load:
                    result = register_journal_articles.main(["check", source])

                self.assertEqual(result, 0)
                load.assert_called_once_with(
                    self.fixture.root / "manifests/imports" / suffix,
                    source=source,
                    raw_root=self.fixture.root / "data/raw" / suffix,
                    max_articles=0,
                )

        self.store_factory.assert_not_called()

    def _inventory_batch(self) -> SimpleNamespace:
        """Подставить проверенные снимки временной партии без сетевого доступа."""

        return SimpleNamespace(
            path=self.fixture.root / "batch.json",
            sha256="a" * 64,
            records=(self.fixture.article(),),
            skipped=(),
            deferred=(),
        )

    def test_inventory_dry_run_without_rights_does_not_write(self) -> None:
        """Предварительная инвентаризация не создаёт ни реестров, ни квитанций."""

        batch = self._inventory_batch()
        before = self.fixture.snapshot()

        with patch.object(register_journal_articles, "read_inventory_batch", return_value=batch):
            result = register_journal_articles.main(["register-inventory", str(batch.path), "--dry-run"])

        self.assertEqual(result, 0, self.stderr.getvalue())
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertIn("Новые материалы учитываются в карантине", self.stdout.getvalue())

    def test_inventory_commit_and_repeat_preserve_rights_and_source(self) -> None:
        """Учёт записывает факты без прав, а повтор не создаёт новые строки реестров."""

        batch = self._inventory_batch()
        source_bytes = self.fixture.record_path.read_bytes()

        with patch.object(register_journal_articles, "read_inventory_batch", return_value=batch):
            self.assertEqual(register_journal_articles.main(["register-inventory", str(batch.path)]), 0,
                             self.stderr.getvalue())
            hashes = self.store.snapshot_hashes()
            self.assertEqual(register_journal_articles.main(["register-inventory", str(batch.path)]), 0,
                             self.stderr.getvalue())

        self.assertEqual(self.store.snapshot_hashes(), hashes)
        self.assertEqual(self.fixture.record_path.read_bytes(), source_bytes)
        self.assertEqual(self.store.records("rights"), [])
        self.assertEqual(self.store.records("works")[0]["eligibility_status"], "quarantined")
        self.assertEqual(len(list((self.fixture.root / "receipts").glob("*.json"))), 2)

    def test_inventory_receipt_error_does_not_claim_rollback(self) -> None:
        """Ошибка квитанции явно сообщает об уже совершённой записи реестров."""

        batch = self._inventory_batch()

        with (
            patch.object(register_journal_articles, "read_inventory_batch", return_value=batch),
            patch.object(register_journal_articles, "_write_inventory_receipt", side_effect=OSError("Диск")),
        ):
            result = register_journal_articles.main(["register-inventory", str(batch.path)])

        self.assertEqual(result, 1)
        self.assertEqual(len(self.store.records("works")), 1)
        self.assertIn("реестры записаны, но квитанция не сохранена", self.stderr.getvalue())

    def test_inventory_source_conflict_is_reported_without_registration(self) -> None:
        """Команда сохраняет конфликт отдельно от нерусских карточек и реестров."""

        batch = self._inventory_batch()
        batch.records = ()
        batch.deferred = ({
            "source": "quantum",
            "record_path": self.fixture.record_path.relative_to(self.fixture.root).as_posix(),
            "reason": "В оглавлении одна ссылка относится к двум статьям",
            "details": {
                "article_url": "https://quantum-electronics.ru/article.pdf",
                "issue_url": "https://quantum-electronics.ru/2003_33_6/",
                "candidates": [{"title": "Первая статья"}, {"title": "Вторая статья"}],
            },
        },)
        before = self.store.snapshot_hashes()

        with patch.object(register_journal_articles, "read_inventory_batch", return_value=batch):
            result = register_journal_articles.main(["register-inventory", str(batch.path)])

        self.assertEqual(result, 0, self.stderr.getvalue())
        self.assertEqual(self.store.snapshot_hashes(), before)
        receipt_path, = (self.fixture.root / "receipts").glob("*.json")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["deferred_source"], list(batch.deferred))
        self.assertEqual(receipt["skipped_non_russian"], [])
        self.assertEqual(len(receipt["report"]), 1)
        self.assertEqual(receipt["report"][0]["status"], "deferred")
        self.assertEqual(receipt["result"]["inserted"]["works"], 0)
        self.assertFalse(receipt["rights_changed"])
        self.assertFalse(receipt["training_eligibility_changed"])
        self.assertIn("отложено конфликтов исходников: 1", self.stdout.getvalue())

    def test_inventory_bad_batch_stops_before_store(self) -> None:
        """Повреждённая зафиксированная партия не открывает рабочие реестры."""

        before = self.fixture.snapshot()

        with patch.object(register_journal_articles, "read_inventory_batch", side_effect=ValueError("SHA-256")):
            result = register_journal_articles.main(["register-inventory", str(self.fixture.root / "batch.json")])

        self.assertEqual(result, 1)
        self.store_factory.assert_not_called()
        self.assertEqual(self.fixture.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
