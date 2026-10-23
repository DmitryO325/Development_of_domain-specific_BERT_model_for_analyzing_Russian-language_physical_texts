"""Проверки точного состава подготовленных партий журналов без сети и импорта."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.corpus.journal_batch_input import read_frozen_journal_batch
from src.corpus.journal_import import CollectedArticleError, read_collected_article
from tests import test_journal_import as import_fixtures


class FrozenJournalBatchTests(unittest.TestCase):
    """Проверять пакет по реальным локальным карточкам и неизменяемым HTTP-снимкам."""

    def setUp(self) -> None:
        """Создать отдельную карточку ЖЭТФ и зафиксировать её контрольную сумму."""

        fixture = import_fixtures.JournalImportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        self.record_path, self.record, _ = fixture._fixture()
        self.packet_path = self.root / "manifests/imports/test_batch_v1/prepared_batch.json"
        self.packet_path.parent.mkdir(parents=True)
        self.packet: dict[str, Any] = {
            "schema_version": "prepared-journal-registration-batch-v1",
            "prepared_at": "2026-10-14T10:00:00+00:00",
            "status": "prepared_not_registered",
            "policy": {
                "path": "docs/DECISIONS.md", "file_sha256": "a" * 64,
                "dec012_snapshot": "## DEC-012. Основание\n\n**Состояние:** принято 19.08.2026.\n"
                "basis_type=lawful_access_research_assumption",
            },
            "raw_roots": {"jetp": "raw"},
            "counts": {"jetp": {"cards": 1, "pdfs": 1, "skipped_non_russian": 0}},
            "records": [self._descriptor(self.record_path)],
            "skipped": [],
        }
        self._save()

    def _descriptor(self, path: Path, *, has_pdf: bool = True) -> dict[str, Any]:
        """Составить описание точных байтов одной тестовой карточки."""

        return {
            "source": "jetp", "record_path": path.relative_to(self.root).as_posix(),
            "record_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "has_pdf": has_pdf,
        }

    def _save(self) -> None:
        """Записать тестовый пакет после изменения одного проверяемого поля."""

        self.packet_path.write_text(json.dumps(self.packet, ensure_ascii=False), encoding="utf-8")

    def _read(self) -> Any:
        """Прочитать тестовую партию без обращения к рабочему проекту."""

        return read_frozen_journal_batch(self.packet_path, project_root=self.root)

    def test_valid_batch_keeps_exact_packet_and_response(self) -> None:
        """Пакет возвращает исходную карточку, порядок источников и собственный SHA."""

        batch = self._read()
        self.assertEqual(batch.batch_id, "test_batch_v1")
        self.assertEqual(batch.path, self.packet_path)
        self.assertEqual(batch.record_sources, ("jetp",))
        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.records[0].document.to_dict(), self.record["document"])
        self.assertEqual(batch.sha256, hashlib.sha256(self.packet_path.read_bytes()).hexdigest())
        self.assertEqual(batch.raw_packet, self.packet)

    def test_relative_packet_path_is_supported(self) -> None:
        """Относительный путь пакета разрешается от явно выбранного корня проекта."""

        batch = read_frozen_journal_batch(self.packet_path.relative_to(self.root), project_root=self.root)
        self.assertEqual(batch.path, self.packet_path)

    def test_new_files_are_not_discovered_or_imported(self) -> None:
        """Новые файлы живого каталога, даже повреждённые, не расширяют замороженный состав."""

        (self.record_path.parent / "new.json").write_text("not-json", encoding="utf-8")
        self.assertEqual(len(self._read().records), 1)

    def test_reading_does_not_write_or_call_network(self) -> None:
        """Чтение оставляет все локальные байты неизменными и не использует HTTP-клиент."""

        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self._read()
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.fixture.client.get_response.assert_not_called()

    def test_current_policy_file_is_not_required(self) -> None:
        """Принятая политика берётся из пакета, даже если текущей документации рядом нет."""

        self.assertFalse((self.root / "docs/DECISIONS.md").exists())
        self._read()

    def test_unaccepted_policy_is_rejected(self) -> None:
        """Черновик DEC-012 не считается сохранённым принятым основанием партии."""

        self.packet["policy"]["dec012_snapshot"] = "## DEC-012.\n**Состояние:** черновик"
        self._save()

        with self.assertRaisesRegex(ValueError, "принятии"):
            self._read()

    def test_wrong_packet_digest_is_rejected(self) -> None:
        """Квитанция может закрепить точные байты самого пакета, а не только карточек."""

        with self.assertRaisesRegex(ValueError, "сумма пакета"):
            read_frozen_journal_batch(self.packet_path, project_root=self.root, expected_sha256="0" * 64)

        expected = hashlib.sha256(self.packet_path.read_bytes()).hexdigest()
        read_frozen_journal_batch(self.packet_path, project_root=self.root, expected_sha256=expected)

    def test_changed_card_is_rejected(self) -> None:
        """Даже безобидное изменение сериализации карточки требует новой фиксации партии."""

        with self.record_path.open("a", encoding="utf-8") as output_file:
            output_file.write("\n")

        with self.assertRaisesRegex(ValueError, "сумма закреплённой"):
            self._read()

    def test_corrupted_http_body_is_rejected(self) -> None:
        """Правильный SHA карточки не заменяет проверку байтов её HTTP-исходников."""

        (self.fixture.raw / self.record["pdf_response"]["relative_path"]).write_bytes(b"changed")

        with self.assertRaises(CollectedArticleError):
            self._read()

    def test_empty_or_wrong_status_batch_is_rejected(self) -> None:
        """Пустая партия и пакет с изменённым статусом не являются исходным снимком."""

        original = copy.deepcopy(self.packet)

        for key, value in (("records", []), ("status", "registered"), ("schema_version", "other")):
            with self.subTest(key=key):
                self.packet = copy.deepcopy(original)
                self.packet[key] = value
                self._save()

                with self.assertRaises(ValueError):
                    self._read()

    def test_wrong_source_and_has_pdf_are_rejected(self) -> None:
        """Источник и наличие PDF должны точно соответствовать проверенной карточке."""

        original = copy.deepcopy(self.packet)

        for key, value in (("source", "unknown"), ("has_pdf", False), ("has_pdf", 1)):
            with self.subTest(key=key, value=value):
                self.packet = copy.deepcopy(original)
                self.packet["records"][0][key] = value
                self._save()

                with self.assertRaises(ValueError):
                    self._read()

    def test_record_path_escape_is_rejected(self) -> None:
        """Абсолютные пути, обход через родителя и неоднозначное написание запрещены."""

        for value in (str(self.record_path), "../outside.json", "state/../state/articles/article.json",
                      "state//articles/article.json", "state\\articles\\article.json"):
            with self.subTest(path=value):
                self.packet["records"][0]["record_path"] = value
                self._save()

                with self.assertRaisesRegex(ValueError, "путь"):
                    self._read()

    def test_symlink_record_or_parent_is_rejected(self) -> None:
        """Символическая ссылка внутри проекта запрещена даже при сохранении SHA."""

        link = self.record_path.parent / "alias.json"
        link.symlink_to(self.record_path)
        self.packet["records"][0]["record_path"] = link.relative_to(self.root).as_posix()
        self._save()

        with self.assertRaisesRegex(ValueError, "символическую ссылку"):
            self._read()

        parent = self.root / "linked_cards"
        parent.symlink_to(self.record_path.parent, target_is_directory=True)
        self.packet["records"][0]["record_path"] = "linked_cards/article.json"
        self._save()

        with self.assertRaisesRegex(ValueError, "символическую ссылку"):
            self._read()

    def test_symlink_raw_root_and_packet_are_rejected(self) -> None:
        """Ссылки запрещены также в корне HTTP-исходников и пути самого пакета."""

        link = self.root / "linked_raw"
        link.symlink_to(self.fixture.raw, target_is_directory=True)
        self.packet["raw_roots"]["jetp"] = "linked_raw"
        self._save()

        with self.assertRaisesRegex(ValueError, "символическую ссылку"):
            self._read()

        packet_link = self.root / "packet.json"
        packet_link.symlink_to(self.packet_path)

        with self.assertRaisesRegex(ValueError, "символическую ссылку"):
            read_frozen_journal_batch(packet_link, project_root=self.root)

    def test_duplicate_paths_and_urls_are_rejected(self) -> None:
        """Нельзя учесть одну карточку дважды или спрятать её копию под другим именем."""

        self.packet["records"].append(copy.deepcopy(self.packet["records"][0]))
        self._save()

        with self.assertRaisesRegex(ValueError, "повторяется"):
            self._read()

        copy_path = self.record_path.parent / "copy.json"
        copy_path.write_bytes(self.record_path.read_bytes())
        self.packet["records"][1] = self._descriptor(copy_path)
        self._save()

        with self.assertRaisesRegex(ValueError, "Статья повторяется"):
            self._read()

    def test_declared_counts_are_checked_strictly(self) -> None:
        """Числа карточек и PDF проверяются; true не считается целым числом 1."""

        for value in (2, True):
            self.packet["counts"]["jetp"]["cards"] = value
            self._save()

            with self.assertRaisesRegex(ValueError, "Итоги партии"):
                self._read()

    def test_preparation_must_follow_retrieval_and_have_timezone(self) -> None:
        """Партия не может включать ещё не полученный снимок или неопределённое местное время."""

        for value in ("2026-10-14T08:00:00+00:00", "2026-10-14T10:00:00"):
            self.packet["prepared_at"] = value
            self._save()

            with self.assertRaises(ValueError):
                self._read()

    def test_skipped_cards_never_enter_reader_or_result(self) -> None:
        """Пропущенная карточка проверяется по SHA, но не становится работой для импорта."""

        skipped_path = self.record_path.parent / "skipped.json"
        skipped_path.write_text('{"status":"skipped_non_russian"}', encoding="utf-8")
        descriptor = self._descriptor(skipped_path)
        descriptor.pop("has_pdf")
        self.packet["skipped"].append(descriptor)
        self.packet["counts"]["jetp"]["skipped_non_russian"] = 1
        self._save()

        with patch("src.corpus.journal_batch_input.read_collected_article", wraps=read_collected_article) as reader:
            batch = self._read()

        self.assertEqual(len(batch.records), 1)
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(reader.call_args.args[0], self.record_path)

    def test_json_duplicate_keys_are_rejected(self) -> None:
        """Неоднозначный JSON нельзя принять даже при корректном последнем значении."""

        body = self.packet_path.read_text(encoding="utf-8")
        self.packet_path.write_text('{"status":"wrong",' + body[1:], encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Поле пакета повторяется"):
            self._read()

    def test_live_card_change_during_read_is_rejected(self) -> None:
        """Изменение карточки между проверкой SHA и разбором обнаруживается повторной проверкой."""

        article = read_collected_article(self.record_path, source="jetp", raw_root=self.fixture.raw)

        def mutate_card(*arguments: Any, **keywords: Any) -> Any:
            """Воспроизвести запись сборщика одновременно с чтением партии."""

            self.record_path.write_bytes(self.record_path.read_bytes() + b"\n")
            return article

        with patch("src.corpus.journal_batch_input.read_collected_article", side_effect=mutate_card):
            with self.assertRaisesRegex(ValueError, "изменилась во время"):
                self._read()

    def test_quantum_metadata_only_constraint_is_enforced(self) -> None:
        """Даже корректный PDF Quantum не расширяет утверждённый состав этой партии."""

        path, _, _ = self.fixture._fixture("quantum")
        descriptor = self._descriptor(path)
        descriptor["source"] = "quantum"
        self.packet["records"] = [descriptor]
        self.packet["raw_roots"] = {"quantum": "raw"}
        self.packet["counts"] = {"quantum": {"cards": 1, "pdfs": 1, "skipped_non_russian": 0}}
        self._save()

        with self.assertRaisesRegex(ValueError, "Quantum"):
            self._read()


if __name__ == "__main__":
    unittest.main()
