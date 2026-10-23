"""Проверки очереди H2 без автоматических меток, потери вариантов и удаления дублей."""

from __future__ import annotations

import copy
import unittest

from typing import Any

from src.corpus.h2_candidate_input import CandidateSnapshot
from src.corpus.h2_candidates import build_candidate_queue


def _item(number: int, **fields: list[str]) -> dict[str, Any]:
    """Создать синтетическую карточку с переопределением исходных полей API."""

    values = {
        "dc.title": ["Исследование ядер"],
        "dc.description.abstract": ["В работе исследованы свойства атомных ядер."],
        "dc.date.issued": ["2024"],
        "dc.relation.ispartof": ["Журнал физики"],
        "dc.type": ["Article"],
        "dc.language.iso": ["ru"],
        **fields,
    }

    return {
        "uuid": f"11111111-2222-4333-8444-{number:012d}",
        "metadata": {
            field: [{"value": value, "language": None} for value in entries]
            for field, entries in values.items()
        },
    }


def _snapshot(*items: dict[str, Any]) -> CandidateSnapshot:
    """Связать синтетические карточки с независимыми свидетельствами."""

    return CandidateSnapshot(
        items=tuple(items),
        sources={item["uuid"]: {"page_path": "data/raw/page.json"} for item in items},
        provenance={"snapshot_sha256": "a" * 64, "total_items": len(items)},
    )


class CandidateQueueTests(unittest.TestCase):
    """Проверки отбора по исходному типу и диагностик для ручного просмотра."""

    def test_all_articles_including_missing_text_are_retained(self) -> None:
        """Неполная карточка остаётся видимой, остальные жанры учитываются отдельно."""

        snapshot = _snapshot(
            _item(1),
            _item(2, **{"dc.description.abstract": ["  ", ""]}),
            _item(3, **{"dc.type": ["Preprint", "Report"]}),
        )
        rows, summary = build_candidate_queue(snapshot)

        self.assertEqual(len(rows), 2)
        self.assertEqual(summary["source_item_count"], 3)
        self.assertEqual(summary["with_title_abstract"], 1)
        self.assertEqual(summary["other_document_types"], {"Preprint + Report": 1})
        self.assertIn("missing_abstract", rows[1]["review_flags"])

    def test_no_labels_splits_or_work_identifiers_are_assigned(self) -> None:
        """Даже похожие на код рубрики не становятся эталонной меткой."""

        rows, summary = build_candidate_queue(_snapshot(_item(1, **{
            "local.Rubric": ["Ядерная физика"],
            "local.udc": ["29.15.01"],
            "local.grnti": ["29.15"],
        })))
        row = rows[0]

        self.assertIsNone(row["label"])
        self.assertIsNone(row["split"])
        self.assertNotIn("work_id", row)
        self.assertNotIn("eligibility_status", row)
        self.assertEqual(row["review_status"], "not_reviewed")
        self.assertEqual(row["udc_as_reported"], ["29.15.01"])
        self.assertEqual(summary["labelled_count"], 0)
        self.assertFalse(summary["training_ready"])

    def test_physics_signal_only_changes_review_order(self) -> None:
        """Другие рубрики не исключаются, заполненные физические карточки идут первыми."""

        rows, _ = build_candidate_queue(_snapshot(
            _item(1, **{"local.Rubric": ["Биология"]}),
            _item(2),
            _item(3, **{"local.Rubric": ["Физика"]}),
            _item(4, **{"local.Rubric": ["Физика"], "dc.title": []}),
        ))

        self.assertEqual([row["item_uuid"][-1] for row in rows], ["3", "1", "2", "4"])
        self.assertEqual(rows[0]["physics_signal"], "physics_rubric")
        self.assertEqual(rows[1]["physics_signal"], "other_rubric")
        self.assertEqual(rows[2]["physics_signal"], "missing_rubric")

    def test_unknown_rubric_is_not_classified_by_substring(self) -> None:
        """Слова в названии не превращают постороннюю рубрику в физическую метку."""

        rows, _ = build_candidate_queue(_snapshot(_item(1, **{
            "local.Rubric": ["Методика преподавания физики"],
        })))

        self.assertEqual(rows[0]["physics_signal"], "other_rubric")

    def test_variants_and_exact_metadata_are_preserved(self) -> None:
        """Оригинальные языковые варианты, пробелы и служебные поля остаются в JSONL."""

        item = _item(1, **{
            "dc.title": ["  Исследование ядер\n", "Nuclear study"],
            "dc.description.abstract": ["Первая аннотация.", "Другая аннотация."],
        })
        snapshot = _snapshot(item)
        before = copy.deepcopy(snapshot)
        rows, _ = build_candidate_queue(snapshot)

        self.assertEqual(snapshot, before)
        self.assertEqual(rows[0]["metadata_as_reported"], item["metadata"])
        self.assertEqual(rows[0]["title"], "Исследование ядер\n\nNuclear study")
        self.assertIn("multiple_title_variants", rows[0]["review_flags"])
        self.assertIn("multiple_abstract_variants", rows[0]["review_flags"])
        rows[0]["metadata_as_reported"]["dc.title"][0]["value"] = "Изменение копии"
        rows[0]["source"]["page_path"] = "другой путь"
        self.assertEqual(snapshot, before)

    def test_ambiguous_edn_is_left_empty_and_all_values_remain(self) -> None:
        """Два разных EDN нельзя разрешить порядком значений."""

        rows, _ = build_candidate_queue(_snapshot(_item(1, **{
            "local.EDN": ["abcdef", "GHIJKL", "ABCDEF"],
        })))

        self.assertIsNone(rows[0]["edn"])
        self.assertEqual(rows[0]["edn_values"], ["ABCDEF", "GHIJKL"])
        self.assertIn("ambiguous_edn", rows[0]["review_flags"])

    def test_ambiguous_doi_is_left_empty(self) -> None:
        """Различные корректные DOI сохраняются как конфликтующие исходные варианты."""

        rows, _ = build_candidate_queue(_snapshot(_item(1, **{
            "dc.identifier.doi": ["10.1234/a", "https://doi.org/10.1234/B"],
        })))

        self.assertIsNone(rows[0]["doi"])
        self.assertEqual(rows[0]["doi_values"], ["10.1234/a", "10.1234/b"])
        self.assertIn("ambiguous_doi", rows[0]["review_flags"])

    def test_invalid_identifiers_are_flags_not_silent_losses(self) -> None:
        """Ошибочные идентификаторы не мешают просмотру и сохраняются в исходных полях."""

        item = _item(1, **{
            "dc.identifier.doi": ["https://[wrong", "не DOI"],
            "local.EDN": ["123456"],
        })
        rows, _ = build_candidate_queue(_snapshot(item))

        self.assertIsNone(rows[0]["doi"])
        self.assertIsNone(rows[0]["edn"])
        self.assertIn("invalid_doi", rows[0]["review_flags"])
        self.assertIn("invalid_edn", rows[0]["review_flags"])
        self.assertEqual(rows[0]["metadata_as_reported"], item["metadata"])

    def test_duplicate_signals_do_not_merge_or_drop_cards(self) -> None:
        """Каждое основание совпадения даёт отдельную группу с двумя исходными UUID."""

        fields = {"dc.identifier.doi": ["10.1234/a"], "local.EDN": ["ABCDEF"]}
        rows, summary = build_candidate_queue(_snapshot(_item(1, **fields), _item(2, **fields)))

        self.assertEqual(len(rows), 2)
        self.assertEqual(summary["rows_with_duplicate_signals"], 2)
        self.assertEqual({group["basis"] for group in summary["duplicate_groups"]}, {
            "doi", "edn", "title_year", "abstract",
        })
        self.assertEqual(rows[0]["duplicate_groups"], rows[1]["duplicate_groups"])

    def test_doi_forms_share_group(self) -> None:
        """Регистр и стандартный DOI URL не скрывают возможное совпадение."""

        _, summary = build_candidate_queue(_snapshot(
            _item(1, **{"dc.identifier.doi": ["doi:10.1234/Test"]}),
            _item(2, **{"dc.identifier.doi": ["https://doi.org/10.1234/test"]}),
        ))

        self.assertEqual(sum(group["basis"] == "doi" for group in summary["duplicate_groups"]), 1)

    def test_blank_abstracts_are_not_duplicate_group(self) -> None:
        """Отсутствие аннотаций не является совпадением их содержания."""

        _, summary = build_candidate_queue(_snapshot(
            _item(1, **{"dc.description.abstract": []}),
            _item(2, **{"dc.description.abstract": [" "]}),
        ))

        self.assertNotIn("abstract", {group["basis"] for group in summary["duplicate_groups"]})

    def test_language_and_label_hints_are_only_diagnostics(self) -> None:
        """Некириллические поля и признаки классификатора остаются доступными для проверки."""

        rows, _ = build_candidate_queue(_snapshot(_item(1, **{
            "dc.title": ["Nuclear physics"],
            "dc.description.abstract": ["PACS 21.60. Nuclear calculations."],
        })))
        flags = rows[0]["review_flags"]

        self.assertIn("check_title_language", flags)
        self.assertIn("check_abstract_language", flags)
        self.assertIn("possible_label_hint_in_text", flags)
        self.assertTrue(rows[0]["has_title_abstract"])
        self.assertIsNone(rows[0]["label"])

    def test_journal_key_normalizes_only_unicode_case_and_spaces(self) -> None:
        """Сокращение названия не объединяется с полным названием автоматически."""

        rows, _ = build_candidate_queue(_snapshot(
            _item(1, **{"dc.relation.ispartof": [" ЖУРНАЛ\n физики "]}),
            _item(2, **{"dc.relation.ispartof": ["Журнал физики"]}),
            _item(3, **{"dc.relation.ispartof": ["Ж. физики"]}),
        ))

        self.assertEqual(rows[0]["journal_key"], rows[1]["journal_key"])
        self.assertNotEqual(rows[0]["journal_key"], rows[2]["journal_key"])

    def test_missing_and_multiple_years_and_journals_are_visible(self) -> None:
        """Неоднозначный год и название издания не выбираются по первой строке."""

        rows, _ = build_candidate_queue(_snapshot(_item(1, **{
            "dc.date.issued": ["2023", "2024"],
            "dc.relation.ispartof": ["Журнал А", "Журнал Б"],
        })))

        self.assertIsNone(rows[0]["year"])
        self.assertIsNone(rows[0]["journal_key"])
        self.assertIn("unresolved_year", rows[0]["review_flags"])
        self.assertIn("multiple_journal_variants", rows[0]["review_flags"])

    def test_mixed_article_type_is_retained_with_warning(self) -> None:
        """Смешанная жанровая запись остаётся в очереди с явной диагностикой."""

        rows, _ = build_candidate_queue(_snapshot(_item(1, **{"dc.type": ["Article", "Preprint"]})))

        self.assertEqual(len(rows), 1)
        self.assertIn("mixed_document_types", rows[0]["review_flags"])

    def test_order_is_independent_of_input_item_order(self) -> None:
        """Очередь и группы совпадений воспроизводятся независимо от обхода карточек."""

        items = [_item(2), _item(1)]

        self.assertEqual(
            build_candidate_queue(_snapshot(*items)),
            build_candidate_queue(_snapshot(*reversed(items))),
        )

    def test_duplicate_uuid_or_missing_source_is_rejected(self) -> None:
        """Ошибки связи источника не превращаются в две самостоятельные работы."""

        item = _item(1)

        with self.assertRaises(ValueError):
            build_candidate_queue(_snapshot(item, item))

        with self.assertRaises(ValueError):
            build_candidate_queue(CandidateSnapshot(items=(item,), sources={}, provenance={}))

    def test_no_article_snapshot_has_empty_queue_and_valid_summary(self) -> None:
        """Наличие одних препринтов не создаёт фиктивные журнальные примеры H2."""

        rows, summary = build_candidate_queue(_snapshot(_item(1, **{"dc.type": ["Preprint"]})))

        self.assertEqual(rows, [])
        self.assertEqual(summary["candidate_count"], 0)
        self.assertFalse(summary["training_ready"])


if __name__ == "__main__":
    unittest.main()
