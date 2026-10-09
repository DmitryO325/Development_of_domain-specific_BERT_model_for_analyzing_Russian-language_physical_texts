"""Проверки офлайн-пакета Article и защиты от подмены локальных свидетельств."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest

from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.corpus.jinr_article_input import (
    ADJACENT_COMPUTATIONAL_METHODS_GROUP,
    ADJACENT_COMPUTING_GROUP,
    _corpus_group,
    read_article_batch,
)
from src.corpus.manifests import sha256_bytes
from tests.jinr_article_fixtures import ITEM_UUID, RETRIEVED_AT, ArticleFixture


class ArticleInputTests(unittest.TestCase):
    """Сопоставление пакета с исходными файлами и каноническими HTTP-данными."""

    def setUp(self) -> None:
        """Создать независимый синтетический пакет с двумя ручными загрузками."""

        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.fixture = ArticleFixture(self.root, aliases=2)

    def tearDown(self) -> None:
        """Удалить только временный каталог текущего теста."""

        self.temporary_directory.cleanup()

    def _set_titles(self, titles: list[dict[str, Any]], selected_title: str) -> None:
        """Сохранить языковые варианты и независимо назначенное название тестового пакета."""

        self.fixture.item["metadata"]["dc.title"] = copy.deepcopy(titles)
        self.fixture.save_metadata(sync_copies=False)
        self.fixture.entry["titles_as_reported"] = copy.deepcopy(titles)
        self.fixture.entry["title"] = selected_title
        self.fixture.selection[0]["title"] = selected_title

        for match in self.fixture.matches:
            match["matched_item"]["title"] = selected_title

        self.fixture.save_sources()

    def test_russian_title_does_not_depend_on_order_or_missing_language(self) -> None:
        """Два языка сохраняются полностью, а русское название не выбирается по позиции."""

        russian_title = "Нейтронные свойства\nвещества"

        for russian_language, english_language in (
            (None, None), ("ru", "en"), ("ru-RU", "en-US"),
            ("rus", "eng"), ("", ""), ("RU_ru", "EN_us"),
        ):
            for reverse in (False, True):
                with self.subTest(language=russian_language, reverse=reverse):
                    titles = [
                        {"value": russian_title, "language": russian_language, "place": 0},
                        {"value": "Neutron properties of matter", "language": english_language, "place": 1},
                    ]

                    if reverse:
                        titles.reverse()

                    self._set_titles(titles, russian_title)
                    before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                    article = read_article_batch(self.root, self.fixture.path).entries[0]

                    self.assertEqual(article.entry["title"], russian_title)
                    self.assertEqual(article.entry["titles_as_reported"], titles)
                    self.assertEqual(article.item["metadata"]["dc.title"], titles)
                    self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_ambiguous_titles_and_conflicting_language_labels_are_rejected(self) -> None:
        """Разные русские варианты и противоречивые метки нельзя разрешать порядком строк."""

        russian_title = "Нейтронные свойства вещества"
        english_title = "Neutron properties of matter"

        for titles in (
            [{"value": russian_title}, {"value": "Другое русское название"}],
            [{"value": russian_title, "language": "ru"}, {"value": "Другой вариант", "language": "ru"}],
            [{"value": russian_title, "language": "ru"}, {"value": "Другой вариант"}],
            [{"value": russian_title, "language": "en"}, {"value": english_title, "language": "en"}],
            [{"value": russian_title, "language": "ru"}, {"value": russian_title, "language": "en"}],
            [{"value": english_title}, {"value": "Another English title"}],
            [{"value": russian_title}, {"value": "12345"}],
            [{"value": russian_title, "language": ["ru"]}, {"value": english_title}],
            [{"value": russian_title, "language": 1}, {"value": english_title}],
        ):
            with self.subTest(titles=titles):
                self._set_titles(titles, russian_title)

                with self.assertRaisesRegex(ValueError, "dc.title"):
                    read_article_batch(self.root, self.fixture.path)

    def test_all_bilingual_metadata_copies_are_checked(self) -> None:
        """Название каждой производной записи и полный исходный массив сверяются с API."""

        russian_title = "Нейтронные свойства вещества"
        titles = [
            {"value": "Neutron properties of matter", "language": None, "place": 0},
            {"value": russian_title, "language": None, "place": 1},
        ]

        for target in ("entry", "selection", "matched_item", "titles_value", "titles_language", "titles_missing"):
            with self.subTest(target=target):
                self._set_titles(titles, russian_title)

                if target == "entry":
                    self.fixture.entry["title"] = titles[0]["value"]

                elif target == "selection":
                    self.fixture.selection[0]["title"] = titles[0]["value"]

                elif target == "matched_item":
                    self.fixture.matches[0]["matched_item"]["title"] = titles[0]["value"]

                elif target == "titles_value":
                    self.fixture.entry["titles_as_reported"][0]["value"] = "Другой перевод"

                elif target == "titles_language":
                    self.fixture.entry["titles_as_reported"][0]["language"] = "en"

                else:
                    self.fixture.entry.pop("titles_as_reported")

                self.fixture.save_sources()

                with self.assertRaises(ValueError):
                    read_article_batch(self.root, self.fixture.path)

    def test_corpus_group_must_be_explicit_known_and_consistent(self) -> None:
        """Смежная группа не выводится из темы и не подменяется в одной копии отбора."""

        for entry_group, selection_group in (
            (ADJACENT_COMPUTING_GROUP, ADJACENT_COMPUTING_GROUP),
            (ADJACENT_COMPUTATIONAL_METHODS_GROUP, ADJACENT_COMPUTATIONAL_METHODS_GROUP),
            (None, None),
            (ADJACENT_COMPUTING_GROUP, None), (None, ADJACENT_COMPUTING_GROUP),
            (ADJACENT_COMPUTATIONAL_METHODS_GROUP, None), (None, ADJACENT_COMPUTATIONAL_METHODS_GROUP),
            (ADJACENT_COMPUTING_GROUP, ADJACENT_COMPUTATIONAL_METHODS_GROUP),
            (ADJACENT_COMPUTATIONAL_METHODS_GROUP, ADJACENT_COMPUTING_GROUP),
            ("unknown", "unknown"),
        ):
            with self.subTest(entry=entry_group, selection=selection_group):
                for record, group in ((self.fixture.entry, entry_group), (self.fixture.selection[0], selection_group)):
                    record.pop("corpus_group_id", None)

                    if group is not None:
                        record["corpus_group_id"] = group

                self.fixture.save_sources()

                if entry_group == selection_group and entry_group != "unknown":
                    article = read_article_batch(self.root, self.fixture.path).entries[0]

                    if entry_group is None:
                        self.assertNotIn("corpus_group_id", article.entry)

                    else:
                        self.assertEqual(article.entry["corpus_group_id"], entry_group)

                else:
                    with self.assertRaisesRegex(ValueError, "corpus_group_id"):
                        read_article_batch(self.root, self.fixture.path)

    def test_invalid_group_values_raise_clear_value_errors(self) -> None:
        """Явный null, числа и контейнеры не превращаются в отсутствие группы или TypeError."""

        for value in (None, "", "physics_core", 1, 1.5, True, [], {}):
            for target in ("entry", "selection", "both"):
                with self.subTest(value=value, target=target):
                    self.fixture.entry.pop("corpus_group_id", None)
                    self.fixture.selection[0].pop("corpus_group_id", None)

                    if target in {"entry", "both"}:
                        self.fixture.entry["corpus_group_id"] = value

                    if target in {"selection", "both"}:
                        self.fixture.selection[0]["corpus_group_id"] = value

                    self.fixture.save_sources()

                    with self.assertRaisesRegex(ValueError, "corpus_group_id"):
                        read_article_batch(self.root, self.fixture.path)

    def test_corpus_groups_are_only_applicable_to_articles(self) -> None:
        """Ни прежняя, ни новая смежная группа не расширяют поддерживаемые типы публикаций."""

        for group in (ADJACENT_COMPUTING_GROUP, ADJACENT_COMPUTATIONAL_METHODS_GROUP):
            for document_type in ("Preprint", "Book chapter", "Book", "Other", "article", ""):
                with self.subTest(group=group, document_type=document_type):
                    with self.assertRaisesRegex(ValueError, "corpus_group_id"):
                        _corpus_group({"corpus_group_id": group}, document_type)

    def test_reader_preserves_original_article_and_both_acquisitions(self) -> None:
        """API остаётся оригинальным, а одинаковые PDF сохраняют два происхождения."""

        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

        with patch("urllib.request.urlopen", side_effect=AssertionError("Сеть запрещена")):
            batch = read_article_batch(self.root, self.fixture.path)

        article = batch.entries[0]
        self.assertEqual(batch.path, "manifests/imports/articles.json")
        self.assertEqual(article.item, self.fixture.item)
        self.assertEqual(article.pdf_bytes, len(self.fixture.pdf_bytes))
        self.assertEqual(article.pdf_path, self.fixture.entry["pdf"]["path"])
        self.assertEqual(article.pdf_sha256, sha256_bytes(self.fixture.pdf_bytes))
        self.assertEqual(article.metadata_response.snapshot.retrieved_at, RETRIEVED_AT)
        self.assertEqual(article.acquisitions, tuple(self.fixture.inventory))
        self.assertIsNone(article.entry["genre"])
        self.assertIsNone(article.entry["journal_id"])
        self.assertIs(article.entry["identity_evidence"]["bitstream_item_relation_verified"], False)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_derived_metadata_cannot_override_api(self) -> None:
        """Название, DOI, журнал и дата пакета должны соответствовать карточке API."""

        original = copy.deepcopy(self.fixture.entry)

        for field, value in (
            ("title", "Подменённое название"),
            ("doi_as_reported", ["10.1000/other"]),
            ("journal_title_as_reported", ["Другой журнал"]),
            ("issued_as_reported", ["1999"]),
            ("metadata_document_type_as_reported", ["Preprint"]),
            ("item_uuid", "00000000-0000-0000-0000-000000000099"),
        ):
            with self.subTest(field=field):
                self.fixture.entry.clear()
                self.fixture.entry.update(copy.deepcopy(original))
                self.fixture.entry[field] = value
                self.fixture.save_batch()

                with self.assertRaises(ValueError):
                    read_article_batch(self.root, self.fixture.path)

    def test_api_type_language_own_link_and_date_are_checked(self) -> None:
        """Согласованные производные записи не заменяют семантическую проверку API."""

        original = copy.deepcopy(self.fixture.item)

        for field, value in (
            ("dc.type", "Preprint"), ("dc.language.iso", "en"),
            ("dc.date.issued", "2024-02-31"), ("dc.identifier.doi", "не DOI"),
        ):
            with self.subTest(field=field):
                self.fixture.item = copy.deepcopy(original)
                self.fixture.item["metadata"][field] = [{"value": value}]
                self.fixture.save_metadata()

                with self.assertRaises(ValueError):
                    read_article_batch(self.root, self.fixture.path)

        self.fixture.item = original
        self.fixture.item["_links"]["self"]["href"] = "https://example.org/item"
        self.fixture.save_metadata()

        with self.assertRaisesRegex(ValueError, "ссылка API"):
            read_article_batch(self.root, self.fixture.path)

    def test_all_source_hashes_are_checked(self) -> None:
        """Любое изменение JSONL должно выявляться до чтения производных записей."""

        for source in self.fixture.batch["source_files"]:
            with self.subTest(source=source["path"]):
                path = self.root / source["path"]
                original = path.read_bytes()
                path.write_bytes(original + b"\n")

                with self.assertRaisesRegex(ValueError, "sha256"):
                    read_article_batch(self.root, self.fixture.path)

                path.write_bytes(original)

    def test_nonselected_and_missing_selection_entries_are_rejected(self) -> None:
        """Пакет не может включать отложенные или исключённые решения selection."""

        for decision in ("needs_review", "other_field"):
            with self.subTest(decision=decision):
                self.fixture.selection[0]["decision"] = decision
                self.fixture.save_sources()

                with self.assertRaisesRegex(ValueError, "physics_candidate"):
                    read_article_batch(self.root, self.fixture.path)

    def test_selection_and_match_claims_are_checked(self) -> None:
        """Подмена пути, типа сопоставления или методов не должна подтверждать статью."""

        original = copy.deepcopy(self.fixture.entry)

        for field, value in (
            ("matches_line", 1), ("matches_line", True),
            ("match_methods", ["несуществующее свидетельство"]),
            ("bitstream_item_relation_verified", True),
            ("matches_path", self.fixture.inventory_path),
        ):
            with self.subTest(field=field, value=value):
                self.fixture.entry.clear()
                self.fixture.entry.update(copy.deepcopy(original))
                self.fixture.entry["identity_evidence"][field] = value
                self.fixture.save_batch()

                with self.assertRaises(ValueError):
                    read_article_batch(self.root, self.fixture.path)

    def test_match_cannot_claim_a_server_relation(self) -> None:
        """Даже согласованное изменение производных флагов не создаёт серверную связь."""

        self.fixture.entry["identity_evidence"]["bitstream_item_relation_verified"] = True

        for match in self.fixture.matches:
            match["bitstream_item_relation_verified"] = True

        self.fixture.save_sources()

        with self.assertRaisesRegex(ValueError, "bitstream_item_relation_verified"):
            read_article_batch(self.root, self.fixture.path)

    def test_match_metadata_and_inventory_references_are_checked(self) -> None:
        """Каждый alias должен ссылаться на собственную строку описи и тот же API."""

        original = copy.deepcopy(self.fixture.matches)

        for section, field, value in (
            ("matched_item", "item_uuid", "00000000-0000-0000-0000-000000000099"),
            ("matched_item", "doi", ["10.1000/other"]),
            ("inventory_source", "line", 2),
            ("inventory_source", "path", self.fixture.matches_path),
            ("metadata_source", "api_response_sha256", "0" * 64),
        ):
            with self.subTest(section=section, field=field):
                self.fixture.matches = copy.deepcopy(original)
                self.fixture.matches[0][section][field] = value
                self.fixture.save_sources()

                with self.assertRaises(ValueError):
                    read_article_batch(self.root, self.fixture.path)

    def test_multiple_search_candidates_do_not_replace_selected_identity(self) -> None:
        """Предварительный поиск может дать несколько кандидатов до точного сопоставления."""

        self.fixture.matches[0]["candidate_item_ids"].append("00000000-0000-0000-0000-000000000099")
        self.fixture.save_sources()
        self.assertEqual(read_article_batch(self.root, self.fixture.path).entries[0].item["uuid"], ITEM_UUID)

        self.fixture.matches[0]["candidate_item_ids"] = ["00000000-0000-0000-0000-000000000099"]
        self.fixture.save_sources()

        with self.assertRaisesRegex(ValueError, "среди кандидатов"):
            read_article_batch(self.root, self.fixture.path)

    def test_manual_history_cannot_be_rewritten(self) -> None:
        """Агент, время, способ и охват получения сверяются с исходной описью."""

        original = copy.deepcopy(self.fixture.entry["acquisition_evidence"])

        for field, value in (
            ("inventory_line", 2), ("inventory_line", 0),
            ("acquisition_method", "crawler"), ("acquisition_scope", "individual"),
            ("acquisition_agent_as_recorded", "curl"),
            ("retrieved_at_as_recorded", "2026-10-05T00:00:00+00:00"),
            ("source_url", "https://example.org/file.pdf"),
        ):
            with self.subTest(field=field):
                self.fixture.entry["acquisition_evidence"] = copy.deepcopy(original)
                self.fixture.entry["acquisition_evidence"][0][field] = value
                self.fixture.save_batch()

                with self.assertRaises(ValueError):
                    read_article_batch(self.root, self.fixture.path)

    def test_inventory_size_and_manual_method_are_independently_checked(self) -> None:
        """Согласованные копии пакета не разрешают неверный размер и способ получения."""

        self.fixture.inventory[0]["bytes"] += 1
        self.fixture.save_sources()

        with self.assertRaisesRegex(ValueError, "bytes"):
            read_article_batch(self.root, self.fixture.path)

        self.fixture.inventory[0]["bytes"] -= 1
        self.fixture.inventory[0]["acquisition_method"] = "crawler"
        self.fixture.entry["acquisition_evidence"][0]["acquisition_method"] = "crawler"
        self.fixture.save_sources()

        with self.assertRaisesRegex(ValueError, "acquisition_method"):
            read_article_batch(self.root, self.fixture.path)

    def test_every_alias_and_original_download_are_required(self) -> None:
        """Нельзя потерять один путь или одну ручную загрузку одинаковых байтов."""

        self.fixture.entry["acquisition_evidence"].pop(0)
        self.fixture.save_batch()

        with self.assertRaisesRegex(ValueError, "все PDF aliases"):
            read_article_batch(self.root, self.fixture.path)

        self.fixture.entry["pdf"]["aliases"].pop(0)
        self.fixture.selection[0]["pdf_aliases"].pop(0)
        self.fixture.batch["pdf_path_count"] = 1
        self.fixture.save_sources()

        with self.assertRaisesRegex(ValueError, "все известные"):
            read_article_batch(self.root, self.fixture.path)

    def test_alias_bytes_and_pdf_signature_are_checked(self) -> None:
        """Один изменённый alias должен отклонять весь пакет."""

        path = self.root / self.fixture.entry["pdf"]["aliases"][0]
        path.write_bytes(b"not a PDF")

        with self.assertRaisesRegex(ValueError, "PDF alias"):
            read_article_batch(self.root, self.fixture.path)

    def test_duplicate_entries_sources_rows_and_aliases_are_rejected(self) -> None:
        """Повторные строки не должны теряться при преобразовании в словари или множества."""

        self.fixture.batch["entries"].append(copy.deepcopy(self.fixture.entry))
        self.fixture.save_batch()

        with self.assertRaisesRegex(ValueError, "Повторное"):
            read_article_batch(self.root, self.fixture.path)

        self.fixture.batch["entries"].pop()
        self.fixture.batch["source_files"].append(copy.deepcopy(self.fixture.batch["source_files"][0]))
        self.fixture.save_batch()

        with self.assertRaisesRegex(ValueError, "повторяет путь"):
            read_article_batch(self.root, self.fixture.path)

        self.fixture.inventory.append(copy.deepcopy(self.fixture.inventory[0]))
        self.fixture.save_sources()

        with self.assertRaisesRegex(ValueError, "Повторное"):
            read_article_batch(self.root, self.fixture.path)

    def test_source_role_cannot_be_reassigned(self) -> None:
        """Ссылка на существующую строку другой роли не должна приниматься."""

        self.fixture.entry["acquisition_evidence"][0]["inventory_path"] = self.fixture.matches_path
        self.fixture.save_batch()

        with self.assertRaisesRegex(ValueError, "роль"):
            read_article_batch(self.root, self.fixture.path)

    def test_counts_and_boolean_counts_are_checked(self) -> None:
        """Числа статей и путей должны совпадать с содержимым и быть целыми числами."""

        for field, value in (("candidate_count", 2), ("candidate_count", True), ("pdf_path_count", 1)):
            with self.subTest(field=field, value=value):
                self.fixture.batch.update({"candidate_count": 1, "pdf_path_count": 2})
                self.fixture.batch[field] = value
                self.fixture.save_batch()

                with self.assertRaisesRegex(ValueError, field):
                    read_article_batch(self.root, self.fixture.path)

    def test_duplicate_json_keys_are_rejected_in_batch_and_sources(self) -> None:
        """Повторные JSON-ключи недопустимы даже при верной контрольной сумме."""

        self.fixture.path.write_text('{"entries": [], "entries": []}', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "Повторный ключ"):
            read_article_batch(self.root, self.fixture.path)

        self.fixture.save_batch()
        source = self.fixture.batch["source_files"][0]
        body = b'{"duplicate": 1, "duplicate": 2}\n'
        (self.root / source["path"]).write_bytes(body)
        source["sha256"] = sha256_bytes(body)
        self.fixture.save_batch()

        with self.assertRaisesRegex(ValueError, "Повторный ключ"):
            read_article_batch(self.root, self.fixture.path)

    def test_data_and_manifest_path_escape_are_rejected(self) -> None:
        """Пути и символьные ссылки не могут вывести PDF и описи из разрешённых каталогов."""

        outside_pdf = self.root / "outside.pdf"
        outside_pdf.write_bytes(self.fixture.pdf_bytes)
        symlink = self.root / "data/escape.pdf"
        symlink.symlink_to(outside_pdf)

        for path in (outside_pdf, symlink):
            with self.subTest(path=path):
                self.fixture.entry["pdf"]["path"] = str(path)
                self.fixture.save_batch()

                with self.assertRaisesRegex(ValueError, "каталог"):
                    read_article_batch(self.root, self.fixture.path)

        outside_manifest = self.root / "outside.json"
        outside_manifest.write_bytes(self.fixture.path.read_bytes())

        with self.assertRaisesRegex(ValueError, "manifests"):
            read_article_batch(self.root, outside_manifest)

    def test_cache_hash_api_body_and_http_metadata_are_checked(self) -> None:
        """Кеш, исходные байты и HTTP-снимок проверяются независимо."""

        source = self.fixture.entry["metadata_source"]
        cache_path = self.root / source["cache_record_path"]
        original_cache = cache_path.read_bytes()
        cache_path.write_bytes(original_cache + b" ")

        with self.assertRaisesRegex(ValueError, "cache_record_sha256"):
            read_article_batch(self.root, self.fixture.path)

        cache_path.write_bytes(original_cache)
        body_path = self.root / source["api_response_path"]
        original_body = body_path.read_bytes()
        body_path.write_bytes(original_body + b" ")

        with self.assertRaisesRegex(ValueError, "SHA-256 тела"):
            read_article_batch(self.root, self.fixture.path)

        body_path.write_bytes(original_body)
        cache = json.loads(original_cache)
        metadata_path = self.root / "data/raw/jinr_api" / cache["response_metadata_path"]
        metadata_path.write_bytes(metadata_path.read_bytes() + b" ")

        with self.assertRaisesRegex(ValueError, "SHA-256 HTTP"):
            read_article_batch(self.root, self.fixture.path)

    def test_http_semantics_are_checked_even_with_recomputed_hashes(self) -> None:
        """Верные хеши не разрешают ошибочный статус, MIME, перенаправление и неканонический JSON."""

        for field, value in (
            ("status_code", 403), ("final_url", "https://example.org/wrong"),
            ("headers", [["content-type", "text/html"]]),
            ("retrieved_at", "2026-09-21T12:00:00"), ("extra", "неизвестное поле"),
        ):
            with self.subTest(field=field):
                self.fixture.save_metadata()
                source = self.fixture.entry["metadata_source"]
                cache_path = self.root / source["cache_record_path"]
                cache = json.loads(cache_path.read_bytes())
                metadata_path = self.root / "data/raw/jinr_api" / cache["response_metadata_path"]
                metadata = json.loads(metadata_path.read_bytes())
                metadata[field] = value
                metadata_body = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                metadata_path.write_bytes(metadata_body)
                cache["response_metadata_sha256"] = sha256_bytes(metadata_body)
                cache_body = json.dumps(cache, ensure_ascii=False).encode()
                cache_path.write_bytes(cache_body)
                source["cache_record_sha256"] = sha256_bytes(cache_body)

                for match in self.fixture.matches:
                    match["metadata_source"] = copy.deepcopy(source)

                self.fixture.save_sources()

                with self.assertRaises(ValueError):
                    read_article_batch(self.root, self.fixture.path)

    def test_metadata_paths_are_confined_to_data_and_manifests(self) -> None:
        """Ни тело API, ни запись кеша не могут находиться за разрешёнными границами."""

        source = self.fixture.entry["metadata_source"]
        outside = self.root / "outside.json"
        outside.write_bytes((self.root / source["api_response_path"]).read_bytes())
        source["api_response_path"] = str(outside)
        self.fixture.selection[0]["metadata_source"]["api_response_path"] = str(outside)
        self.fixture.save_sources()

        with self.assertRaisesRegex(ValueError, "каталог"):
            read_article_batch(self.root, self.fixture.path)

        self.fixture.save_metadata()
        source = self.fixture.entry["metadata_source"]
        outside.write_bytes((self.root / source["cache_record_path"]).read_bytes())
        source["cache_record_path"] = str(outside)
        self.fixture.save_batch()

        with self.assertRaisesRegex(ValueError, "manifests"):
            read_article_batch(self.root, self.fixture.path)


if __name__ == "__main__":
    unittest.main()
