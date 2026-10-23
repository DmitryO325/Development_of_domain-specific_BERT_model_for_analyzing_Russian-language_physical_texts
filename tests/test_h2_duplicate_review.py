"""Проверки пакета совпадений и связей версий без автоматического объединения работ."""

from __future__ import annotations

import copy
import unittest

from typing import Any

from src.corpus.h2_candidate_input import CandidateSnapshot
from src.corpus.h2_candidates import build_candidate_queue
from src.corpus.h2_duplicate_review import build_duplicate_review


def _uuid(number: int) -> str:
    """Получить воспроизводимый UUID синтетической карточки."""

    return f"11111111-2222-4333-8444-{number:012d}"


def _item(number: int, **fields: list[str]) -> dict[str, Any]:
    """Создать карточку, которая без переопределения полей не похожа на соседние."""

    values = {
        "dc.title": [f"Исследование ядер: работа {number}"],
        "dc.description.abstract": [f"Получены результаты эксперимента {number}."],
        "dc.date.issued": ["2024"],
        "dc.relation.ispartof": ["Журнал физики"],
        "dc.type": ["Article"],
        "dc.language.iso": ["ru"],
        **fields,
    }

    return {
        "uuid": _uuid(number),
        "metadata": {
            field: [{"value": value, "language": None} for value in entries]
            for field, entries in values.items()
        },
    }


def _snapshot(*items: dict[str, Any]) -> CandidateSnapshot:
    """Снабдить исходные карточки отдельными свидетельствами происхождения."""

    return CandidateSnapshot(
        items=tuple(items),
        sources={
            item["uuid"]: {
                "page_path": "data/raw/page.json",
                "page_sha256": "b" * 64,
            }
            for item in items
        },
        provenance={"snapshot_sha256": "a" * 64, "total_items": len(items)},
    )


def _pair_for(result: dict[str, Any], first: int, second: int) -> dict[str, Any]:
    """Найти единственную проверяемую пару двух синтетических карточек."""

    expected = sorted((_uuid(first), _uuid(second)))
    pairs = [pair for pair in result["pairs"] if pair["item_uuids"] == expected]

    if len(pairs) != 1:
        raise AssertionError(f"Ожидалась одна пара для {expected}, получено {len(pairs)}")

    return pairs[0]


class DuplicateReviewGroupTests(unittest.TestCase):
    """Проверки независимых оснований совпадения без слияния карточек."""

    def test_multiple_signals_produce_one_pair_with_all_group_ids(self) -> None:
        """Совпадения DOI, EDN, названия с годом и аннотации не размножают пару."""

        fields = {
            "dc.title": ["Общее исследование"],
            "dc.description.abstract": ["Одинаковое описание эксперимента."],
            "dc.identifier.doi": ["10.1234/example"],
            "local.EDN": ["ABCDEF"],
        }
        result = build_duplicate_review(_snapshot(_item(1, **fields), _item(2, **fields)))
        pair = _pair_for(result, 1, 2)

        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual({group["basis"] for group in result["groups"]}, {
            "doi", "edn", "title_year", "abstract",
        })
        self.assertEqual(set(pair["duplicate_group_ids"]), {
            group["group_id"] for group in result["groups"]
        })
        self.assertEqual(pair["relation_ids"], [])
        self.assertEqual(result["summary"]["duplicate_two_card_pair_count"], 1)
        self.assertEqual(result["summary"]["duplicate_card_count"], 2)

    def test_existing_duplicate_group_identifiers_are_preserved(self) -> None:
        """Идентификаторы оснований совпадения остаются совместимыми с очередью H2."""

        snapshot = _snapshot(
            _item(1, **{"local.EDN": ["ABCDEF"]}),
            _item(2, **{"local.EDN": ["abcdef"]}),
        )
        _, queue_summary = build_candidate_queue(snapshot)
        result = build_duplicate_review(snapshot)

        self.assertEqual(
            {group["group_id"] for group in result["groups"]},
            {group["group_id"] for group in queue_summary["duplicate_groups"]},
        )

    def test_annual_reports_remain_one_group_without_fifty_five_pairs(self) -> None:
        """Общая аннотация одиннадцати ежегодных отчётов не означает 55 дублей."""

        snapshot = _snapshot(*(
            _item(number, **{
                "dc.title": [f"Отчёт семинара за {2010 + number} год"],
                "dc.description.abstract": ["Годовой отчёт научного семинара."],
                "dc.date.issued": [str(2011 + number)],
            })
            for number in range(1, 12)
        ))
        result = build_duplicate_review(snapshot)
        group = result["groups"][0]

        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(group["basis"], "abstract")
        self.assertEqual(group["member_count"], 11)
        self.assertEqual(group["item_uuids"], [_uuid(number) for number in range(1, 12)])
        self.assertEqual(len(group["candidate_ids"]), 11)
        self.assertIsNone(group["review_decision"])
        self.assertEqual(result["pairs"], [])
        self.assertEqual(result["summary"]["large_duplicate_group_count"], 1)
        self.assertEqual(result["summary"]["duplicate_card_count"], 11)

    def test_three_member_group_is_not_expanded_either(self) -> None:
        """Ограничение разворачивания действует уже для группы из трёх карточек."""

        result = build_duplicate_review(_snapshot(*(
            _item(number, **{"local.EDN": ["ABCDEF"]}) for number in range(1, 4)
        )))

        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(result["groups"][0]["member_count"], 3)
        self.assertEqual(result["pairs"], [])
        self.assertEqual(result["summary"]["duplicate_two_card_pair_count"], 0)

    def test_ambiguous_shared_identifiers_are_visible_without_merging(self) -> None:
        """Общий вариант неоднозначного DOI или EDN остаётся лишь сигналом проверки."""

        cases = (
            ("local.EDN", "edn", ["ABCDEF", "GHIJKL"], "ABCDEF"),
            ("dc.identifier.doi", "doi", ["10.1234/first", "10.1234/second"], "10.1234/first"),
        )

        for field, basis, variants, shared_value in cases:
            with self.subTest(basis=basis):
                result = build_duplicate_review(_snapshot(
                    _item(1, **{field: variants}),
                    _item(2, **{field: [shared_value]}),
                ))
                group = result["groups"][0]
                pair = _pair_for(result, 1, 2)

                self.assertEqual(group["basis"], basis)
                self.assertEqual(group["ambiguous_identifier_items"], [_uuid(1)])
                self.assertIn(f"shared_ambiguous_{basis}", group["review_flags"])
                self.assertIn(f"shared_ambiguous_{basis}", pair["review_flags"])
                self.assertIsNone(group["review_decision"])
                self.assertEqual(len(result["cards"]), 2)

    def test_same_abstract_with_different_titles_requires_review(self) -> None:
        """Чужая аннотация не превращает статьи о разных установках в один документ."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{
                "dc.title": ["Оптимизация гексапольных магнитов"],
                "dc.description.abstract": ["Расчёт магнитного поля установки."],
                "local.EDN": ["ABCDEF"],
            }),
            _item(2, **{
                "dc.title": ["Лазерно-искровой источник ионов"],
                "dc.description.abstract": ["Расчёт магнитного поля установки."],
                "local.EDN": ["GHIJKL"],
            }),
        ))
        pair = _pair_for(result, 1, 2)

        self.assertIn("same_abstract_different_title", pair["review_flags"])
        self.assertTrue(pair["comparison"]["same_abstract_exact"])
        self.assertFalse(pair["comparison"]["same_title_normalized"])
        self.assertFalse(pair["comparison"]["same_h2_input_exact"])
        self.assertIsNone(pair["review_decision"])

    def test_seeded_pair_keeps_the_large_group_it_also_belongs_to(self) -> None:
        """Независимая пара сохраняет общую большую группу без её разворачивания."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{
                "dc.description.abstract": ["Общая аннотация."],
                "local.EDN": ["ABCDEF"],
            }),
            _item(2, **{
                "dc.description.abstract": ["Общая аннотация."],
                "local.EDN": ["ABCDEF"],
            }),
            _item(3, **{"dc.description.abstract": ["Общая аннотация."]}),
        ))
        pair = _pair_for(result, 1, 2)

        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual(set(pair["duplicate_group_ids"]), {
            group["group_id"] for group in result["groups"]
        })
        self.assertEqual(result["summary"]["large_duplicate_group_count"], 1)

    def test_overlapping_signals_do_not_create_a_transitive_pair(self) -> None:
        """Связи первой со второй и второй с третьей не доказывают совпадение крайних."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.EDN": ["ABCDEF"]}),
            _item(2, **{
                "local.EDN": ["ABCDEF"],
                "dc.identifier.doi": ["10.1234/related"],
            }),
            _item(3, **{"dc.identifier.doi": ["10.1234/related"]}),
        ))

        self.assertEqual(len(result["pairs"]), 2)
        self.assertEqual({tuple(pair["item_uuids"]) for pair in result["pairs"]}, {
            (_uuid(1), _uuid(2)), (_uuid(2), _uuid(3)),
        })


class DuplicateReviewComparisonTests(unittest.TestCase):
    """Различие точного входа H2 и мягких признаков совпадения."""

    def test_exact_h2_input_ignores_differing_years_but_keeps_warning(self) -> None:
        """Одинаковые название и аннотация остаются точным совпадением входа модели."""

        common = {
            "dc.title": ["Исследование ядер"],
            "dc.description.abstract": ["Одинаковое описание результатов."],
        }
        result = build_duplicate_review(_snapshot(
            _item(1, **{**common, "dc.date.issued": ["2023"]}),
            _item(2, **{**common, "dc.date.issued": ["2024"]}),
        ))
        pair = _pair_for(result, 1, 2)

        self.assertTrue(pair["comparison"]["same_h2_input_exact"])
        self.assertTrue(pair["comparison"]["same_h2_input_normalized"])
        self.assertTrue(pair["comparison"]["differing_years"])
        self.assertIn("same_h2_input_exact", pair["review_flags"])

    def test_normalized_h2_input_is_not_reported_as_exact(self) -> None:
        """Unicode, регистр и внутренние пробелы дают мягкое, но не точное совпадение."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{
                "dc.title": ["Исследование Ａ"],
                "dc.description.abstract": ["Свойства  ядер.\nЭксперимент."],
            }),
            _item(2, **{
                "dc.title": ["ИССЛЕДОВАНИЕ A"],
                "dc.description.abstract": ["СВОЙСТВА ядер. Эксперимент."],
            }),
        ))
        pair = _pair_for(result, 1, 2)
        comparison = pair["comparison"]

        self.assertFalse(comparison["same_title_exact"])
        self.assertFalse(comparison["same_abstract_exact"])
        self.assertFalse(comparison["same_h2_input_exact"])
        self.assertTrue(comparison["same_title_normalized"])
        self.assertTrue(comparison["same_abstract_normalized"])
        self.assertTrue(comparison["same_h2_input_normalized"])
        self.assertIn("same_h2_input_normalized", pair["review_flags"])
        self.assertNotIn("same_h2_input_exact", pair["review_flags"])

    def test_missing_title_or_abstract_cannot_be_equal_h2_input(self) -> None:
        """Отсутствующие тексты не считаются общим содержанием двух входов H2."""

        for missing_field in ("dc.title", "dc.description.abstract"):
            with self.subTest(field=missing_field):
                common = {
                    "dc.title": ["Исследование ядер"],
                    "dc.description.abstract": ["Общие результаты эксперимента."],
                    "local.EDN": ["ABCDEF"],
                    missing_field: ["  "],
                }
                result = build_duplicate_review(_snapshot(
                    _item(1, **common), _item(2, **common),
                ))
                comparison = _pair_for(result, 1, 2)["comparison"]

                self.assertFalse(comparison["same_h2_input_exact"])
                self.assertFalse(comparison["same_h2_input_normalized"])

    def test_absent_abstracts_do_not_create_abstract_group(self) -> None:
        """Пара по EDN не получает дополнительного основания из двух пустых полей."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.EDN": ["ABCDEF"], "dc.description.abstract": []}),
            _item(2, **{"local.EDN": ["ABCDEF"], "dc.description.abstract": [" "]}),
        ))

        self.assertEqual([group["basis"] for group in result["groups"]], ["edn"])

    def test_year_comparison_is_unknown_if_year_is_unresolved(self) -> None:
        """Неоднозначный или отсутствующий год не превращается в установленное отличие."""

        for years in ([], ["2023", "2024"], ["не указан"]):
            with self.subTest(years=years):
                result = build_duplicate_review(_snapshot(
                    _item(1, **{"local.EDN": ["ABCDEF"], "dc.date.issued": years}),
                    _item(2, **{"local.EDN": ["ABCDEF"]}),
                ))

                self.assertIsNone(_pair_for(result, 1, 2)["comparison"]["differing_years"])


class DuplicateReviewRelationTests(unittest.TestCase):
    """Проверки исходных связей статей, препринтов и переводов."""

    def test_reciprocal_preprint_relations_keep_two_records_and_one_pair(self) -> None:
        """Два направления исходной связи не становятся двумя независимыми парами."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}),
            _item(2, **{
                "dc.type": ["Preprint"],
                "local.relation.is-preprint-of": [_uuid(1)],
            }),
        ))
        pair = _pair_for(result, 1, 2)

        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual(len(result["relations"]), 2)
        self.assertEqual(set(pair["relation_ids"]), {
            relation["relation_id"] for relation in result["relations"]
        })
        self.assertEqual({relation["kind"] for relation in result["relations"]}, {
            "has_preprint", "is_preprint_of",
        })
        self.assertTrue(all(relation["reciprocal"] for relation in result["relations"]))
        self.assertIn("source_version_relation", pair["review_flags"])
        self.assertEqual(result["summary"]["relation_status_counts"], {"in_snapshot": 2})

    def test_linked_preprint_is_preserved_without_becoming_h2_candidate(self) -> None:
        """Не-Article в исходной связи остаётся доступным для проверки как отдельная версия."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}),
            _item(2, **{"dc.type": ["Preprint"]}),
        ))
        cards = {card["item_uuid"]: card for card in result["cards"]}

        self.assertEqual(set(cards), {_uuid(1), _uuid(2)})
        self.assertTrue(cards[_uuid(1)]["is_h2_candidate"])
        self.assertFalse(cards[_uuid(2)]["is_h2_candidate"])
        self.assertEqual(result["summary"]["candidate_count"], 1)
        self.assertEqual(result["summary"]["source_item_count"], 2)

    def test_relation_uuid_and_trusted_card_url_resolve_to_same_pair(self) -> None:
        """Полный адрес карточки репозитория поддерживается вместе с чистым UUID."""

        target_url = f"https://pubrepo.jinr.ru/entities/publication/{_uuid(2)}"
        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [target_url]}),
            _item(2, **{"dc.type": ["Preprint"]}),
        ))
        relation = result["relations"][0]

        self.assertEqual(relation["raw_value"], target_url)
        self.assertEqual(relation["target_item_uuid"], _uuid(2))
        self.assertEqual(relation["target_status"], "in_snapshot")
        self.assertEqual(len(_pair_for(result, 1, 2)["relation_ids"]), 1)

    def test_outside_target_keeps_unresolved_pair_without_inventing_a_card(self) -> None:
        """Ссылка на отсутствующую в снимке карточку остаётся нерешённой диагностикой."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.relation.has-translated-version": [_uuid(99)]}),
        ))
        relation = result["relations"][0]

        self.assertEqual(relation["target_item_uuid"], _uuid(99))
        self.assertEqual(relation["target_status"], "outside_snapshot")
        self.assertNotIn(_uuid(99), {card["item_uuid"] for card in result["cards"]})
        self.assertEqual(result["summary"]["relation_status_counts"], {"outside_snapshot": 1})

        pair = _pair_for(result, 1, 99)

        self.assertEqual(len(result["pairs"]), 1)
        self.assertIn("target_outside_snapshot", pair["review_flags"])
        self.assertEqual(pair["relation_ids"], [relation["relation_id"]])
        self.assertTrue(all(value is None for value in pair["comparison"].values()))

    def test_self_link_is_not_an_independent_version(self) -> None:
        """Связь публикации с самой собой учитывается как ошибка исходного поля."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(1)]}),
        ))

        self.assertEqual(result["pairs"], [])
        self.assertEqual(result["relations"][0]["target_status"], "self_link")
        self.assertFalse(result["relations"][0]["reciprocal"])

    def test_invalid_targets_remain_visible_without_creating_pairs(self) -> None:
        """Пустая строка, посторонний сайт и ошибочный UUID не подставляют карточку."""

        targets = (
            "", "не UUID", f"https://example.org/items/{_uuid(2)}",
            f"https://pubrepo.jinr.ru.evil.example/items/{_uuid(2)}",
        )

        for target in targets:
            with self.subTest(target=target):
                result = build_duplicate_review(_snapshot(
                    _item(1, **{"local.relation.has-preprint": [target]}),
                    _item(2),
                ))
                relation = result["relations"][0]

                self.assertEqual(relation["raw_value"], target)
                self.assertEqual(relation["target_status"], "invalid_target")
                self.assertIsNone(relation["target_item_uuid"])
                self.assertEqual(result["pairs"], [])

    def test_unknown_relation_field_is_preserved_but_not_interpreted(self) -> None:
        """Неизвестный вид связи нельзя произвольно принять за версию статьи."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.relation.unrecognized": [_uuid(2)]}),
            _item(2),
        ))
        relation = result["relations"][0]

        self.assertEqual(relation["field"], "local.relation.unrecognized")
        self.assertEqual(relation["target_status"], "unsupported_relation")
        self.assertIsNone(relation["target_item_uuid"])
        self.assertEqual(result["pairs"], [])

    def test_same_direction_links_are_not_marked_reciprocal(self) -> None:
        """Две связи has-preprint в противоположные стороны не подтверждают семантику."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}),
            _item(2, **{"local.relation.has-preprint": [_uuid(1)]}),
        ))

        self.assertEqual(len(result["pairs"]), 1)
        self.assertFalse(any(relation["reciprocal"] for relation in result["relations"]))

    def test_translation_relations_are_also_explicit_version_signals(self) -> None:
        """Перевод регистрируется как исходная связь, а не совпадение русских текстов."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.relation.has-translated-version": [_uuid(2)]}),
            _item(2, **{"local.relation.is-translated-version-of": [_uuid(1)]}),
        ))

        self.assertEqual(len(result["pairs"]), 1)
        self.assertTrue(all(relation["reciprocal"] for relation in result["relations"]))
        self.assertIn("source_version_relation", _pair_for(result, 1, 2)["review_flags"])

    def test_duplicate_and_version_signals_share_one_pair(self) -> None:
        """Совпадение названия с годом и явная связь сохраняются как разные основания."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{
                "dc.title": ["Эволюционные уравнения кинематики"],
                "local.relation.has-preprint": [_uuid(2)],
            }),
            _item(2, **{
                "dc.title": ["Эволюционные уравнения кинематики"],
                "local.relation.is-preprint-of": [_uuid(1)],
            }),
        ))
        pair = _pair_for(result, 1, 2)

        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual(len(pair["duplicate_group_ids"]), 1)
        self.assertEqual(len(pair["relation_ids"]), 2)


class DuplicateReviewSafetyTests(unittest.TestCase):
    """Воспроизводимость, происхождение и отсутствие научных решений за человека."""

    def test_output_is_deterministic_for_different_item_order(self) -> None:
        """Порядок исходных карточек не меняет идентификаторы и сводку пакета."""

        items = [
            _item(1, **{"local.EDN": ["ABCDEF"]}),
            _item(2, **{
                "local.EDN": ["ABCDEF"],
                "local.relation.has-preprint": [_uuid(3)],
            }),
            _item(3, **{"dc.type": ["Preprint"]}),
        ]

        self.assertEqual(
            build_duplicate_review(_snapshot(*items)),
            build_duplicate_review(_snapshot(*reversed(items))),
        )

    def test_source_objects_are_not_mutated_or_shared_with_output(self) -> None:
        """Изменение результата не переписывает метаданные и свидетельства источника."""

        snapshot = _snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}),
            _item(2, **{"dc.type": ["Preprint"]}),
        )
        before = copy.deepcopy(snapshot)
        result = build_duplicate_review(snapshot)

        self.assertEqual(snapshot, before)
        result["cards"][0]["metadata_as_reported"]["dc.title"][0]["value"] = "Изменённая копия"
        result["cards"][0]["source"]["page_path"] = "другой путь"
        result["relations"][0]["source"]["page_path"] = "путь связи"
        result["summary"]["provenance"]["snapshot_sha256"] = "c" * 64
        self.assertEqual(snapshot, before)

    def test_all_decisions_remain_empty_and_no_family_is_assigned(self) -> None:
        """Пакет не назначает классы, выборки, рабочие идентификаторы или семейства версий."""

        result = build_duplicate_review(_snapshot(
            _item(1, **{"local.EDN": ["ABCDEF"]}),
            _item(2, **{"local.EDN": ["ABCDEF"]}),
        ))

        for row in result["cards"] + result["groups"] + result["pairs"] + result["relations"]:
            for forbidden in ("work_id", "family_id", "version_group", "eligibility_status"):
                self.assertNotIn(forbidden, row)

            self.assertIsNone(row.get("label"))
            self.assertIsNone(row.get("split"))

        for row in result["groups"] + result["pairs"]:
            self.assertIsNone(row["review_decision"])

        for pair in result["pairs"]:
            self.assertEqual(pair["comment"], "")

        self.assertEqual(result["summary"]["status"], "review_required")
        self.assertFalse(result["summary"]["training_ready"])

    def test_summary_counts_match_exported_records(self) -> None:
        """Сводка раздельно считает источники, кандидатов, группы, пары и связи."""

        snapshot = _snapshot(
            _item(1, **{"local.EDN": ["ABCDEF"]}),
            _item(2, **{
                "local.EDN": ["ABCDEF"],
                "local.relation.has-preprint": [_uuid(3)],
            }),
            _item(3, **{"dc.type": ["Preprint"]}),
            _item(4),
        )
        result = build_duplicate_review(snapshot)
        summary = result["summary"]

        self.assertEqual(summary["source_item_count"], 4)
        self.assertEqual(summary["candidate_count"], 3)
        self.assertEqual(summary["duplicate_group_count"], len(result["groups"]))
        self.assertEqual(summary["relation_record_count"], len(result["relations"]))
        self.assertEqual(summary["pair_count"], len(result["pairs"]))
        self.assertEqual(summary["card_count"], len(result["cards"]))
        self.assertEqual(summary["provenance"], snapshot.provenance)

    def test_adding_unrelated_card_does_not_change_pair_or_group_ids(self) -> None:
        """Стабильные идентификаторы не зависят от количества посторонних публикаций."""

        first = _item(1, **{"local.EDN": ["ABCDEF"]})
        second = _item(2, **{"local.EDN": ["ABCDEF"]})
        initial = build_duplicate_review(_snapshot(first, second))
        extended = build_duplicate_review(_snapshot(first, second, _item(3)))

        self.assertEqual(initial["groups"], extended["groups"])
        self.assertEqual(initial["pairs"], extended["pairs"])

    def test_repeated_uuid_in_source_is_rejected(self) -> None:
        """Повтор одной карточки в снимке нельзя превратить в сигнал двух публикаций."""

        with self.assertRaises(ValueError):
            build_duplicate_review(_snapshot(_item(1), _item(1)))

    def test_missing_source_evidence_is_rejected(self) -> None:
        """Участник проверки должен сохранять привязку к проверенному исходному снимку."""

        snapshot = CandidateSnapshot(
            items=(_item(1),),
            sources={},
            provenance={"snapshot_sha256": "a" * 64},
        )

        with self.assertRaises(ValueError):
            build_duplicate_review(snapshot)

    def test_noncanonical_source_uuid_is_rejected(self) -> None:
        """Некорректный идентификатор источника не становится ссылкой на карточку."""

        item = _item(1)
        item["uuid"] = "не UUID"

        with self.assertRaises(ValueError):
            build_duplicate_review(_snapshot(item))


if __name__ == "__main__":
    unittest.main()
