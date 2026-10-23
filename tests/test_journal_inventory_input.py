"""Офлайн-проверки неизменяемого состава журнальных исходников без изменения реестров."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pymupdf

from src.collect.base import HttpResponseSnapshot
from src.collect.ioffe import parse_article as parse_ioffe_article
from src.collect.ioffe_archive import IoffeArchive
from src.collect.ioffe_http import validate_ioffe_url
from src.collect.journal_sources import get_source
from src.corpus import journal_inventory_input as inventory
from src.corpus.journal_import import CollectedArticleError
from tests.test_ioffe import ARTICLE_HTML as IOFFE_HTML, ARTICLE_URL as IOFFE_URL
from tests.test_jetp import ARTICLE_HTML as JETP_HTML, ARTICLE_URL as JETP_URL
from tests.test_jetp_letters import ARTICLE_HTML as LETTERS_HTML, ARTICLE_URL as LETTERS_URL
from tests.test_journal_import import ISSUE_HTML, ISSUE_URL
from tests.test_quantum_electronics import ARTICLE_HTML as QUANTUM_HTML, ARTICLE_URL as QUANTUM_URL
from tests.test_ufn_journal import ARTICLE_HTML as UFN_HTML, ARTICLE_URL as UFN_URL, PDF_URL as UFN_PDF_URL

PREPARED_AT = "2026-10-17T12:00:00+00:00"
MODULE = "src.corpus.journal_inventory_input"
FIXTURES = {
    "ioffe": (IOFFE_URL, IOFFE_HTML), "jetp": (JETP_URL, JETP_HTML),
    "jetp_letters": (LETTERS_URL, LETTERS_HTML), "quantum": (QUANTUM_URL, QUANTUM_HTML),
    "ufn": (UFN_URL, UFN_HTML),
}


class InventoryInputTests(unittest.TestCase):
    """Проверять байты, время, состав и явные исключения только во временном проекте."""

    def setUp(self) -> None:
        """Создать независимый проект и запретить случайное обращение к HTTP."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "manifests/imports/inventory_test"
        self.client = MagicMock()
        self.client.fetch.side_effect = AssertionError("Сетевой запрос запрещён в тесте")
        clock_patch = patch(f"{MODULE}._now", return_value=PREPARED_AT)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        network_patch = patch("urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена"))
        network_patch.start()
        self.addCleanup(network_patch.stop)

        with pymupdf.open() as document:
            document.new_page()
            self.pdf_bytes = document.tobytes()

    def _fixture(
        self, source: str = "jetp", *, with_pdf: bool = True, html: str | None = None,
        issue_toc: bool = False,
    ) -> tuple[Path, dict[str, Any], Path]:
        """Сохранить синтетическую карточку в штатных каталогах выбранного сборщика."""

        suffix = Path("ioffe") if source == "ioffe" else Path("journals") / source
        raw_root = self.root / "data/raw" / suffix
        input_root = self.root / "manifests/imports" / suffix
        url, fixture_html = FIXTURES[source]
        validator = validate_ioffe_url if source == "ioffe" else get_source(source).validate_url
        archive = IoffeArchive(raw_root, input_root, self.client, validate_url=validator)
        if issue_toc:
            url = ISSUE_URL
            html = ISSUE_HTML if html is None else html
            document = get_source(source).parse_issue_articles(html, url)[0]

        elif source == "ioffe":
            html = fixture_html if html is None else html
            document = parse_ioffe_article(html, url)

        else:
            html = fixture_html if html is None else html
            document = get_source(source).parse_article(html, url)

        snapshot = HttpResponseSnapshot(
            url, url, 200, (("content-type", "text/html; charset=utf-8"),),
            "2026-10-14T09:00:00+00:00", html.encode("utf-8"),
        )
        archive.import_response(snapshot)
        record: dict[str, Any] = {
            "schema_version": "ioffe-collected-article-v1" if source == "ioffe" else "journal-collected-article-v1",
            "document": document.to_dict(), "card_response": archive.evidence_for(snapshot),
            "status": "metadata_only", "registered": False, "training_eligibility": "not_assessed",
        }

        if source == "ioffe":
            record["article_id"] = document.extra["article_id"]

        else:
            record.update(source_id=source, metadata_page_url=url)

        if with_pdf and document.pdf_url is not None:
            pdf = replace(
                snapshot, requested_url=document.pdf_url, final_url=document.pdf_url,
                retrieved_at="2026-10-14T09:01:00+00:00",
                headers=(("content-type", "application/pdf"),), body=self.pdf_bytes,
            )
            archive.import_response(pdf)
            record.update(pdf_response=archive.evidence_for(pdf), page_count=1, status="pdf_downloaded")

        directory = "articles" if "pdf_response" in record else "metadata"
        path = input_root / directory / "article.json"
        self._save(path, record)

        return path, record, raw_root

    def _ambiguous_fixture(self) -> tuple[Path, dict[str, Any], Path]:
        """Создать подлинное оглавление с одной PDF-ссылкой для двух разных статей."""

        html = ISSUE_HTML.replace("/2024/01/other.pdf", "/2024/01/test_article.pdf")

        return self._fixture("quantum", with_pdf=False, html=html, issue_toc=True)

    def _save(self, path: Path, value: dict[str, Any]) -> None:
        """Изменить только синтетический JSON для проверки защиты от повреждений."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _prepare(self, sources: tuple[str, ...] = ("jetp",)) -> Path:
        """Подготовить пакет выбранных синтетических источников."""

        return inventory.prepare_inventory_batch(self.root, self.output, sources)

    def _load_packet(self, path: Path) -> dict[str, Any]:
        """Прочитать описание тестового пакета для контролируемой подмены."""

        return json.loads(path.read_text(encoding="utf-8"))

    def test_exact_copy_and_original_snapshot_provenance(self) -> None:
        """Готовый пакет содержит исходные байты, реальные даты и никаких решений о правах."""

        original, _, _ = self._fixture()
        original_bytes = original.read_bytes()
        path = self._prepare()
        packet = self._load_packet(path)
        batch = inventory.read_inventory_batch(path, self.root)
        descriptor = packet["records"][0]

        self.assertEqual((self.root / descriptor["record_path"]).read_bytes(), original_bytes)
        self.assertEqual(descriptor["original_path"], original.relative_to(self.root).as_posix())
        self.assertEqual(batch.records[0].record_path, self.root / descriptor["record_path"])
        self.assertEqual(batch.records[0].card_response.retrieved_at, "2026-10-14T09:00:00+00:00")
        self.assertEqual(batch.prepared_at, PREPARED_AT)
        self.assertEqual(batch.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(batch.sources, ("jetp",))
        self.assertEqual(batch.skipped, ())
        self.assertEqual(batch.deferred, ())
        self.assertEqual(packet["schema_version"], "journal-inventory-batch-v2")
        self.assertNotIn("rights", packet)
        self.assertNotIn("training_eligibility", packet)
        self.client.fetch.assert_not_called()

    def test_ufn_legacy_pdf_record_remains_readable_in_frozen_batch(self) -> None:
        """Повторный сбор старого PDF не меняет прежний документ или байты закреплённой партии."""

        article_url = "https://ufn.ru/ru/articles/2015/5/i/"
        pdf_url = "https://ufn.ru/ufn15/ufn15_5/Russian/r155i.pdf"
        html = UFN_HTML.replace(UFN_PDF_URL, pdf_url).replace(
            "/ufn2026/ufn2026_8/Russian/r268a.pdf", "/ufn15/ufn15_5/Russian/r155i.pdf",
        ).replace(UFN_URL, article_url)

        with patch.dict(FIXTURES, {"ufn": (article_url, html)}):
            original, record, _ = self._fixture("ufn", with_pdf=False)

        self.assertEqual(record["document"]["pdf_url"], pdf_url)
        record["document"]["pdf_url"] = None
        self._save(original, record)
        original_bytes = original.read_bytes()
        packet_path = self._prepare(("ufn",))
        packet = self._load_packet(packet_path)
        frozen = self.root / packet["records"][0]["record_path"]
        batch_bytes = packet_path.read_bytes()

        with patch.dict(FIXTURES, {"ufn": (article_url, html)}):
            self._fixture("ufn", with_pdf=False)

        self.assertNotEqual(original.read_bytes(), original_bytes)
        batch = inventory.read_inventory_batch(packet_path, self.root)

        self.assertEqual(batch.records[0].document.to_dict(), record["document"])
        self.assertIsNone(batch.records[0].pdf_response)
        self.assertEqual(frozen.read_bytes(), original_bytes)
        self.assertEqual(packet_path.read_bytes(), batch_bytes)
        self.client.fetch.assert_not_called()

    def test_all_five_source_paths_and_metadata_only_quantum(self) -> None:
        """Иоффе берётся из особого каталога, Quantum остаётся без PDF, УФН проверяется штатно."""

        for source in inventory.INVENTORY_SOURCES:
            self._fixture(source, with_pdf=source != "quantum")

        path = inventory.prepare_inventory_batch(self.root, self.output)
        batch = inventory.read_inventory_batch(path, self.root)

        self.assertEqual(batch.sources, inventory.INVENTORY_SOURCES)
        self.assertEqual(len(batch.records), 5)
        self.assertIsNone(batch.records[3].pdf_response)
        self.assertEqual(batch.raw_packet["raw_roots"]["ioffe"], "data/raw/ioffe")
        self.assertEqual(batch.raw_packet["raw_roots"]["ufn"], "data/raw/journals/ufn")

    def test_registered_flags_do_not_remove_source_records(self) -> None:
        """Подготовка не решает повторную регистрацию и сохраняет все исходные свидетельства."""

        original, record, _ = self._fixture()
        record["registered"] = True
        self._save(original, record)
        path = self._prepare()

        self.assertEqual(len(inventory.read_inventory_batch(path, self.root).records), 1)

    def test_read_uses_frozen_copy_after_original_changes(self) -> None:
        """Обновление или удаление рабочей карточки не подменяет готовую партию."""

        original, _, _ = self._fixture()
        path = self._prepare()
        original.write_text("повреждённая новая карточка", encoding="utf-8")
        self.assertEqual(len(inventory.read_inventory_batch(path, self.root).records), 1)
        original.unlink()
        self.assertEqual(len(inventory.read_inventory_batch(path, self.root).records), 1)

    def test_existing_output_is_never_overwritten(self) -> None:
        """Повторная подготовка не меняет даже корректный существующий пакет."""

        self._fixture()
        path = self._prepare()
        before = path.read_bytes()

        with self.assertRaisesRegex(ValueError, "уже существует"):
            self._prepare()

        self.assertEqual(path.read_bytes(), before)

    def test_symlink_source_and_destination_are_rejected(self) -> None:
        """Ссылки не позволяют читать или перезаписывать другие каталоги."""

        original, _, _ = self._fixture()
        copy_path = original.with_suffix(".saved")
        original.rename(copy_path)
        original.symlink_to(copy_path)

        with self.assertRaisesRegex(ValueError, "Символические ссылки"):
            self._prepare()

        original.unlink()
        copy_path.rename(original)
        self.output.symlink_to(self.root / "missing-output", target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "Символические ссылки"):
            self._prepare()

    def test_empty_unknown_and_repeated_sources_are_rejected(self) -> None:
        """Набор источников должен быть непустым, известным и однозначным."""

        for sources in ((), ("unknown",), ("jetp", "jetp")):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                self._prepare(sources)

    def test_missing_source_and_empty_source_are_rejected(self) -> None:
        """Отсутствие исходников не превращается в успешно подготовленную пустую партию."""

        with self.assertRaisesRegex(ValueError, "Нет штатного"):
            self._prepare()

        (self.root / "manifests/imports/journals/jetp").mkdir(parents=True)
        (self.root / "data/raw/journals/jetp").mkdir(parents=True)

        with self.assertRaisesRegex(ValueError, "нет сохранённых карточек"):
            self._prepare()

    def test_parent_escape_is_rejected(self) -> None:
        """Пакет нельзя подготовить за пределами проекта."""

        self._fixture()

        with self.assertRaises(ValueError):
            inventory.prepare_inventory_batch(self.root, self.root / "../outside", ("jetp",))

    def test_changed_source_aborts_before_publication(self) -> None:
        """Параллельное изменение карточки после копирования не попадает в готовую партию."""

        original, _, _ = self._fixture()
        writer = inventory._write_bytes

        def write_and_change(path: Path, body: bytes, *, immutable: bool = False) -> None:
            """Имитировать обновление карточки сборщиком после её копирования."""

            writer(path, body, immutable=immutable)
            original.write_bytes(original.read_bytes() + b" ")

        with (
            patch(f"{MODULE}._write_bytes", side_effect=write_and_change),
            self.assertRaisesRegex(ValueError, "изменилась при подготовке"),
        ):
            self._prepare()

        self.assertFalse((self.output / "batch.json").exists())

    def test_new_card_after_enumeration_is_not_added(self) -> None:
        """Новая карточка во время копирования остаётся для следующей партии."""

        original, _, _ = self._fixture()
        writer = inventory._write_bytes

        def write_and_add(path: Path, body: bytes, *, immutable: bool = False) -> None:
            """Добавить отдельную карточку после фиксации списка исходников."""

            writer(path, body, immutable=immutable)
            original.with_name("late.json").write_bytes(original.read_bytes())

        with patch(f"{MODULE}._write_bytes", side_effect=write_and_add):
            path = self._prepare()

        self.assertEqual(len(inventory.read_inventory_batch(path, self.root).records), 1)

    def test_selected_paths_freeze_only_requested_cards(self) -> None:
        """Частичная партия сохраняет только выбранные карточки, не меняя остальные."""

        selected, _, _ = self._fixture()
        other = selected.with_name("other.json")
        other.write_bytes(selected.read_bytes())
        before = other.read_bytes()
        path = inventory.prepare_inventory_batch(
            self.root, self.output, ("jetp",), selected_paths=(selected,),
        )
        batch = inventory.read_inventory_batch(path, self.root)

        self.assertEqual(len(batch.records), 1)
        self.assertEqual(
            batch.raw_packet["records"][0]["original_path"], selected.relative_to(self.root).as_posix(),
        )
        self.assertEqual(other.read_bytes(), before)

    def test_empty_duplicate_and_unknown_selected_paths_are_rejected(self) -> None:
        """Отбор не допускает пустой состав, повторы и несуществующие исходники."""

        selected, _, _ = self._fixture()

        for paths in ((), (selected, selected), (selected.with_name("missing.json"),)):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                inventory.prepare_inventory_batch(self.root, self.output, ("jetp",), selected_paths=paths)

            self.assertFalse(self.output.exists())

    def test_selected_paths_cannot_escape_source_or_project(self) -> None:
        """Существующий файл вне выбранного сборщика или проекта не становится карточкой."""

        selected, _, _ = self._fixture()
        other_source, _, _ = self._fixture("ufn")
        arbitrary = self.root / "unrelated.json"
        arbitrary.write_bytes(selected.read_bytes())

        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / "article.json"
            outside.write_bytes(selected.read_bytes())

            for candidate in (other_source, arbitrary, outside):
                with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                    inventory.prepare_inventory_batch(
                        self.root, self.output, ("jetp",), selected_paths=(candidate,),
                    )

                self.assertFalse(self.output.exists())

    def test_selected_paths_cannot_bypass_source_symlink_check(self) -> None:
        """Явный выбор карточки не разрешает символические ссылки вместо исходника."""

        selected, _, _ = self._fixture()
        real_path = selected.with_suffix(".saved")
        selected.rename(real_path)
        selected.symlink_to(real_path)

        with self.assertRaisesRegex(ValueError, "Символические ссылки"):
            inventory.prepare_inventory_batch(
                self.root, self.output, ("jetp",), selected_paths=(selected,),
            )

        self.assertFalse(self.output.exists())

    def test_non_russian_is_explicitly_skipped(self) -> None:
        """Только подтверждённая нерусская карточка сохраняется в явном списке исключений."""

        html = LETTERS_HTML.replace("Исследование свойств", "Study of properties").replace("воды", "water")
        html = html.replace("Изучены свойства", "Properties studied").replace("Получены результаты.", "Results obtained.")
        original, record, _ = self._fixture("jetp_letters", html=html, with_pdf=False)
        record["status"] = "skipped_non_russian"
        self._save(original, record)
        path = self._prepare(("jetp_letters",))
        batch = inventory.read_inventory_batch(path, self.root)

        self.assertEqual(batch.records, ())
        self.assertEqual(batch.sources, ())
        self.assertEqual(len(batch.skipped), 1)
        self.assertIn("нерусскоязычная", batch.skipped[0]["reason"])
        self.assertEqual(batch.deferred, ())

    def test_legacy_packet_remains_readable_without_rewrite(self) -> None:
        """Старый пакет v1 читается без новых обязательных полей и без перезаписи байтов."""

        self._fixture()
        path = self._prepare()
        packet = self._load_packet(path)
        packet["schema_version"] = inventory.LEGACY_INVENTORY_SCHEMA
        packet.pop("deferred")
        self._save(path, packet)
        before = path.read_bytes()
        batch = inventory.read_inventory_batch(path, self.root)

        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.deferred, ())
        self.assertEqual(batch.raw_packet["schema_version"], inventory.LEGACY_INVENTORY_SCHEMA)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(batch.sha256, hashlib.sha256(before).hexdigest())

    def test_packet_fields_are_strict_for_each_version(self) -> None:
        """v1 не принимает deferred, а v2 требует его даже при отсутствии конфликтов."""

        self._fixture()
        path = self._prepare()
        packet = self._load_packet(path)
        legacy_with_new_field = dict(packet, schema_version=inventory.LEGACY_INVENTORY_SCHEMA)
        missing_new_field = dict(packet)
        missing_new_field.pop("deferred")
        unknown_version = dict(packet, schema_version="journal-inventory-batch-v3")
        invalid_category = dict(packet, deferred={})

        for altered in (legacy_with_new_field, missing_new_field, unknown_version, invalid_category):
            with self.subTest(packet=altered):
                self._save(path, altered)

                with self.assertRaises(ValueError):
                    inventory.read_inventory_batch(path, self.root)

    def test_ambiguous_issue_is_separate_from_records_and_skipped(self) -> None:
        """Корректная статья остаётся к регистрации, неоднозначная сохраняет всех кандидатов."""

        self._fixture()
        original, record, _ = self._ambiguous_fixture()
        before = original.read_bytes()
        path = self._prepare(("jetp", "quantum"))
        batch = inventory.read_inventory_batch(path, self.root)
        descriptor = batch.deferred[0]
        expected_candidates = get_source("quantum").parse_issue_articles(
            ISSUE_HTML.replace("/2024/01/other.pdf", "/2024/01/test_article.pdf"), ISSUE_URL,
        )

        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.sources, ("jetp",))
        self.assertEqual(batch.skipped, ())
        self.assertEqual(len(batch.deferred), 1)
        self.assertFalse(descriptor["has_pdf"])
        self.assertEqual(descriptor["source"], "quantum")
        self.assertEqual(descriptor["details"], {
            "issue_url": ISSUE_URL,
            "article_url": record["document"]["url"],
            "candidates": [document.to_dict() for document in expected_candidates],
        })
        self.assertIn("ручная проверка", descriptor["reason"])
        self.assertEqual(set(descriptor), inventory.RECORD_FIELDS | {"reason", "details"})
        self.assertEqual((self.root / descriptor["record_path"]).read_bytes(), before)
        self.assertEqual(original.read_bytes(), before)
        self.assertFalse((self.root / "manifests/works.jsonl").exists())
        self.client.fetch.assert_not_called()

    def test_packet_can_contain_only_deferred_cards(self) -> None:
        """Одна подтверждённая неоднозначность образует непустой пакет без допуска к регистрации."""

        self._ambiguous_fixture()
        path = self._prepare(("quantum",))
        batch = inventory.read_inventory_batch(path, self.root)

        self.assertEqual(batch.records, ())
        self.assertEqual(batch.sources, ())
        self.assertEqual(batch.skipped, ())
        self.assertEqual(len(batch.deferred), 1)

    def test_deferred_reason_details_and_pdf_flag_are_reproduced(self) -> None:
        """Ручная подмена причины, кандидатов, адресов и наличия PDF отклоняется."""

        self._ambiguous_fixture()
        path = self._prepare(("quantum",))
        original = path.read_bytes()

        for alteration in (
            "reason", "article_url", "issue_url", "candidates", "empty_candidates", "candidate_title", "has_pdf",
        ):
            with self.subTest(alteration=alteration):
                path.write_bytes(original)
                packet = self._load_packet(path)
                descriptor = packet["deferred"][0]

                if alteration == "reason":
                    descriptor["reason"] = "Другая причина"

                elif alteration == "has_pdf":
                    descriptor["has_pdf"] = True

                elif alteration == "candidates":
                    descriptor["details"]["candidates"].pop()

                elif alteration == "empty_candidates":
                    descriptor["details"]["candidates"] = []

                elif alteration == "candidate_title":
                    descriptor["details"]["candidates"][0]["title"] = "Подменённое название"

                else:
                    descriptor["details"][alteration] = "https://quantum-electronics.ru/other/"

                self._save(path, packet)

                with self.assertRaisesRegex(ValueError, "Причина отложения"):
                    inventory.read_inventory_batch(path, self.root)

    def test_deferred_details_preserve_json_scalar_types(self) -> None:
        """Числовое равенство в Python не позволяет подменить целую страницу на true или 1.0."""

        html = ISSUE_HTML.replace("/2024/01/other.pdf", "/2024/01/test_article.pdf").replace("Петров 23", "Петров 1")
        self._fixture("quantum", with_pdf=False, html=html, issue_toc=True)
        path = self._prepare(("quantum",))
        before = path.read_bytes()

        for replacement in (True, 1.0):
            with self.subTest(replacement=replacement):
                path.write_bytes(before)
                packet = self._load_packet(path)
                candidate = packet["deferred"][0]["details"]["candidates"][0]
                self.assertEqual(candidate["extra"]["first_page"], 1)
                candidate["extra"]["first_page"] = replacement
                self._save(path, packet)

                with self.assertRaisesRegex(ValueError, "Причина отложения"):
                    inventory.read_inventory_batch(path, self.root)

    def test_deferred_hash_and_source_are_checked(self) -> None:
        """Отложенный статус не ослабляет проверку контрольных сумм и источника карточки."""

        self._ambiguous_fixture()
        path = self._prepare(("quantum",))
        packet = self._load_packet(path)
        frozen = self.root / packet["deferred"][0]["record_path"]
        before = frozen.read_bytes()
        frozen.write_bytes(before + b" ")

        with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
            inventory.read_inventory_batch(path, self.root)

        frozen.write_bytes(before)
        packet["deferred"][0]["source"] = "ufn"
        self._save(path, packet)

        with self.assertRaisesRegex(ValueError, "Источник карточки"):
            inventory.read_inventory_batch(path, self.root)

    def test_deferred_http_evidence_and_time_are_rechecked(self) -> None:
        """Отложенная карточка требует неизменённых HTTP-байтов и существования до даты пакета."""

        _, record, raw_root = self._ambiguous_fixture()
        path = self._prepare(("quantum",))
        packet = self._load_packet(path)
        body_path = raw_root / record["card_response"]["relative_path"]
        before = body_path.read_bytes()
        body_path.write_bytes(before + b" ")

        with self.assertRaises(CollectedArticleError):
            inventory.read_inventory_batch(path, self.root)

        body_path.write_bytes(before)
        packet["prepared_at"] = "2026-10-14T08:59:00+00:00"
        self._save(path, packet)

        with self.assertRaisesRegex(ValueError, "позднее"):
            inventory.read_inventory_batch(path, self.root)

    def test_regular_card_cannot_be_moved_to_deferred(self) -> None:
        """Полностью однозначную карточку нельзя исключить фиктивной причиной отложения."""

        self._fixture("quantum", with_pdf=False)
        path = self._prepare(("quantum",))
        packet = self._load_packet(path)
        descriptor = packet["records"].pop()
        descriptor.update(reason="Требуется проверка", details={})
        packet["deferred"].append(descriptor)
        self._save(path, packet)

        with self.assertRaisesRegex(ValueError, "Причина отложения"):
            inventory.read_inventory_batch(path, self.root)

    def test_deferred_card_cannot_be_moved_to_registration_or_skipped(self) -> None:
        """Неоднозначность нельзя зарегистрировать либо объявить нерусскоязычной статьёй."""

        self._ambiguous_fixture()
        path = self._prepare(("quantum",))
        before = path.read_bytes()

        for category in ("records", "skipped"):
            with self.subTest(category=category):
                path.write_bytes(before)
                packet = self._load_packet(path)
                descriptor = packet["deferred"].pop()
                descriptor.pop("details")

                if category == "records":
                    descriptor.pop("reason")

                packet[category].append(descriptor)
                self._save(path, packet)

                with self.assertRaises(ValueError):
                    inventory.read_inventory_batch(path, self.root)

    def test_corrupt_ambiguous_card_still_blocks_preparation(self) -> None:
        """Даже при повторной PDF-ссылке подмена названия не превращается в допустимое отложение."""

        original, record, _ = self._ambiguous_fixture()
        record["document"]["title"] = "Отсутствующая в оглавлении статья"
        self._save(original, record)

        with self.assertRaises(CollectedArticleError):
            self._prepare(("quantum",))

        self.assertFalse((self.output / "batch.json").exists())

    def test_other_invalid_card_blocks_entire_batch(self) -> None:
        """Несовпадение заголовка с HTML не маскируется пропуском статьи."""

        original, record, _ = self._fixture()
        record["document"]["title"] = "Подменённое название"
        self._save(original, record)

        with self.assertRaises(CollectedArticleError):
            self._prepare()

        self.assertFalse((self.output / "batch.json").exists())

    def test_http_time_later_than_preparation_blocks_batch(self) -> None:
        """Нельзя утверждать, что поздний HTTP-ответ существовал при фиксации состава."""

        self._fixture()

        with (
            patch(f"{MODULE}._now", return_value="2026-10-14T09:00:30+00:00"),
            self.assertRaisesRegex(ValueError, "позднее"),
        ):
            self._prepare()

        self.assertFalse((self.output / "batch.json").exists())

    def test_reader_checks_pdf_time_and_requires_timezone(self) -> None:
        """Дата пакета проверяется и при чтении; локальное время без пояса не принимается."""

        self._fixture()
        path = self._prepare()
        packet = self._load_packet(path)

        for timestamp in ("2026-10-14T09:00:30+00:00", "2026-10-17T12:00:00"):
            packet["prepared_at"] = timestamp
            self._save(path, packet)

            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                inventory.read_inventory_batch(path, self.root)

    def test_frozen_card_hash_is_checked(self) -> None:
        """Даже несущественный пробел в закреплённой карточке меняет её контрольную сумму."""

        self._fixture()
        path = self._prepare()
        frozen = self.root / self._load_packet(path)["records"][0]["record_path"]
        frozen.write_bytes(frozen.read_bytes() + b" ")

        with self.assertRaisesRegex(ValueError, "Контрольная сумма"):
            inventory.read_inventory_batch(path, self.root)

    def test_http_body_and_metadata_hashes_are_rechecked(self) -> None:
        """Читатель не доверяет одной лишь ранее успешной подготовке пакета."""

        _, record, raw_root = self._fixture()
        path = self._prepare()
        metadata_path = raw_root / record["card_response"]["response_metadata_path"]
        before = metadata_path.read_bytes()
        metadata_path.write_bytes(before + b" ")

        with self.assertRaises(CollectedArticleError):
            inventory.read_inventory_batch(path, self.root)

        metadata_path.write_bytes(before)
        body_path = raw_root / record["card_response"]["relative_path"]
        body_path.write_bytes(body_path.read_bytes() + b" ")

        with self.assertRaises(CollectedArticleError):
            inventory.read_inventory_batch(path, self.root)

    def test_duplicate_descriptors_are_rejected(self) -> None:
        """Повтор одной закреплённой копии не увеличивает состав партии."""

        self._fixture()
        path = self._prepare()
        packet = self._load_packet(path)
        packet["records"].append(packet["records"][0].copy())
        self._save(path, packet)

        with self.assertRaisesRegex(ValueError, "повторяется"):
            inventory.read_inventory_batch(path, self.root)

    def test_copies_must_stay_under_packet_and_raw_roots_are_fixed(self) -> None:
        """Описание партии не может перенаправить читатель на произвольные исходники."""

        self._fixture()
        path = self._prepare()
        original_packet = path.read_bytes()
        packet = self._load_packet(path)
        packet["records"][0]["record_path"] = packet["records"][0]["original_path"]
        self._save(path, packet)

        with self.assertRaisesRegex(ValueError, "вне пакета"):
            inventory.read_inventory_batch(path, self.root)

        path.write_bytes(original_packet)
        packet = self._load_packet(path)
        packet["raw_roots"]["jetp"] = "data/raw/ioffe"
        self._save(path, packet)

        with self.assertRaisesRegex(ValueError, "Корни исходников"):
            inventory.read_inventory_batch(path, self.root)

    def test_false_skip_and_pdf_flags_are_rejected(self) -> None:
        """Ручное перемещение русской статьи в skipped или отрицание PDF не проходит проверку."""

        self._fixture()
        path = self._prepare()
        packet = self._load_packet(path)
        packet["records"][0]["has_pdf"] = False
        self._save(path, packet)

        with self.assertRaisesRegex(ValueError, "Отметка PDF"):
            inventory.read_inventory_batch(path, self.root)

        descriptor = packet["records"].pop()
        descriptor["reason"] = "Нельзя тихо пропустить"
        packet["skipped"].append(descriptor)
        self._save(path, packet)

        with self.assertRaisesRegex(ValueError, "Причина исключения"):
            inventory.read_inventory_batch(path, self.root)

    def test_packet_duplicate_json_keys_are_rejected(self) -> None:
        """Одинаково названные JSON-поля не допускают неоднозначной интерпретации пакета."""

        self._fixture()
        path = self._prepare()
        body = path.read_text(encoding="utf-8")
        path.write_text(body.replace("{", '{"schema_version":"wrong",', 1), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "JSON пакета"):
            inventory.read_inventory_batch(path, self.root)


if __name__ == "__main__":
    unittest.main()
