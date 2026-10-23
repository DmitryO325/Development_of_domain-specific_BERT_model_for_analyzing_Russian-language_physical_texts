"""Проверки предварительных ограничений утечек H2 без изменения реестров и split."""

from __future__ import annotations

import copy
import itertools
import unittest

from typing import Any

from src.corpus.h2_candidate_input import CandidateSnapshot
from src.corpus.h2_duplicate_review import build_duplicate_review
from src.corpus.h2_leakage import build_h2_leakage_review


def _uuid(number: int) -> str:
    """Получить воспроизводимый UUID синтетической карточки."""

    return f"11111111-2222-4333-8444-{number:012d}"


def _item(number: int, **fields: list[str]) -> dict[str, Any]:
    """Создать статью с независимыми текстами и явными исходными метаданными."""

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
    """Снабдить исходные карточки свидетельствами происхождения."""

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


def _hold(number: int, **fields: Any) -> dict[str, Any]:
    """Создать исходное резервирование практической карточки с явными флагами."""

    return {
        "candidate_id": f"jinr:{_uuid(number)}",
        "item_uuid": _uuid(number),
        "work_id": None,
        "input_sha256": "c" * 64,
        "status": "reserved_for_practice",
        "seen_by_annotators": None,
        "exclude_from_future_evaluation": True,
        "related_versions_require_check": True,
        "automatic_split_enforcement": False,
        "reason": "Карточка зарезервирована для пробы инструкции H2.",
        **fields,
    }


def _constraints(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """Отобрать ограничения по независимому основанию удержания вместе."""

    return [constraint for constraint in result["constraints"] if constraint["kind"] == kind]


class H2LeakageInputTests(unittest.TestCase):
    """Совпадения полного входа модели для всех исходных статей."""

    def test_large_exact_group_is_found_without_any_duplicate_review_pairs(self) -> None:
        """Одиннадцать одинаковых входов образуют группу даже при пустом списке пар."""

        common = {
            "dc.title": ["Общее исследование ядер"],
            "dc.description.abstract": ["Общие результаты эксперимента."],
        }
        snapshot = _snapshot(*(_item(number, **common) for number in range(1, 12)))

        self.assertEqual(build_duplicate_review(snapshot)["pairs"], [])

        result = build_h2_leakage_review(snapshot, [])
        constraints = _constraints(result, "same_h2_input_exact")

        self.assertEqual(len(result["constraints"]), 1)
        self.assertEqual(len(constraints), 1)
        self.assertEqual(constraints[0]["item_uuids"], [_uuid(number) for number in range(1, 12)])
        self.assertEqual(_constraints(result, "same_h2_input_normalized"), [])

    def test_exact_input_does_not_depend_on_publication_year(self) -> None:
        """Одинаковый вход остаётся общим при разных годах и источниках публикации."""

        common = {
            "dc.title": ["Общее исследование ядер"],
            "dc.description.abstract": ["Общие результаты эксперимента."],
        }
        result = build_h2_leakage_review(_snapshot(
            _item(1, **{**common, "dc.date.issued": ["2023"]}),
            _item(2, **{**common, "dc.date.issued": ["2024"]}),
        ), [])

        self.assertEqual(
            [constraint["item_uuids"] for constraint in _constraints(result, "same_h2_input_exact")],
            [[_uuid(1), _uuid(2)]],
        )

    def test_normalized_group_preserves_its_exact_subset(self) -> None:
        """Unicode, регистр и пробелы дополняют точную подгруппу отдельным основанием."""

        exact = {
            "dc.title": ["Исследование Ａ"],
            "dc.description.abstract": ["Свойства  ядер.\nЭксперимент."],
        }
        result = build_h2_leakage_review(_snapshot(
            _item(1, **exact),
            _item(2, **exact),
            _item(3, **{
                "dc.title": ["ИССЛЕДОВАНИЕ A"],
                "dc.description.abstract": ["СВОЙСТВА ядер. Эксперимент."],
            }),
        ), [])

        self.assertEqual(
            [constraint["item_uuids"] for constraint in _constraints(result, "same_h2_input_exact")],
            [[_uuid(1), _uuid(2)]],
        )
        self.assertEqual(
            [constraint["item_uuids"] for constraint in _constraints(result, "same_h2_input_normalized")],
            [[_uuid(1), _uuid(2), _uuid(3)]],
        )

    def test_blank_and_ambiguous_input_fields_cannot_match(self) -> None:
        """Пустое поле или несколько исходных вариантов не становятся общим входом."""

        for field in ("dc.title", "dc.description.abstract"):
            for variants in ([], ["  "], ["Первый вариант", "Второй вариант"]):
                with self.subTest(field=field, variants=variants):
                    common = {
                        "dc.title": ["Общее исследование"],
                        "dc.description.abstract": ["Общие результаты."],
                        field: variants,
                    }
                    result = build_h2_leakage_review(_snapshot(
                        _item(1, **common), _item(2, **common),
                    ), [])

                    self.assertEqual(result["constraints"], [])
                    unavailable = [
                        issue for issue in result["issues"] if issue["code"] == "unavailable_h2_input"
                    ]
                    self.assertEqual(len(unavailable), 2)

                    for issue in unavailable:
                        self.assertEqual(issue["evidence"]["unavailable_fields"], [
                            "title" if field == "dc.title" else "abstract",
                        ])

    def test_joined_ambiguous_values_do_not_match_single_source_value(self) -> None:
        """Совпавший вывод неоднозначной карточки не доказывает единственный вход."""

        for field in ("dc.title", "dc.description.abstract"):
            with self.subTest(field=field):
                common = {
                    "dc.title": ["Общее исследование"],
                    "dc.description.abstract": ["Общие результаты."],
                }
                result = build_h2_leakage_review(_snapshot(
                    _item(1, **{**common, field: ["Первый вариант", "Второй вариант"]}),
                    _item(2, **{**common, field: ["Первый вариант\n\nВторой вариант"]}),
                ), [])

                self.assertEqual(result["constraints"], [])

    def test_duplicate_metadata_values_follow_candidate_queue_input(self) -> None:
        """Повторы единственного непустого значения не создают ложную неоднозначность."""

        result = build_h2_leakage_review(_snapshot(
            _item(1, **{
                "dc.title": ["Общее название", " Общее название ", ""],
                "dc.description.abstract": ["Общие результаты.", "Общие результаты.", " "],
            }),
            _item(2, **{
                "dc.title": ["Общее название"],
                "dc.description.abstract": ["Общие результаты."],
            }),
        ), [])

        self.assertEqual(len(_constraints(result, "same_h2_input_exact")), 1)

    def test_non_article_text_is_not_an_independent_h2_match(self) -> None:
        """Препринт участвует по явной связи, но не по одному совпадению с текстом статьи."""

        common = {
            "dc.title": ["Общее название"],
            "dc.description.abstract": ["Общие результаты."],
        }
        result = build_h2_leakage_review(_snapshot(
            _item(1, **common),
            _item(2, **{**common, "dc.type": ["Preprint"]}),
        ), [_hold(1)])

        self.assertEqual(result["constraints"], [])
        self.assertEqual([hold["item_uuid"] for hold in result["evaluation_holds"]], [_uuid(1)])

    def test_soft_signals_require_review_without_propagating_a_hold(self) -> None:
        """DOI, EDN, название с годом и общая аннотация требуют самостоятельной проверки."""

        cases = (
            ("doi", {"dc.identifier.doi": ["10.1234/shared"]}),
            ("edn", {"local.EDN": ["ABCDEF"]}),
            ("title_year", {"dc.title": ["Общее название"]}),
            ("abstract", {"dc.description.abstract": ["Общая аннотация."]}),
        )

        for basis, common in cases:
            with self.subTest(fields=common):
                result = build_h2_leakage_review(_snapshot(
                    _item(1, **common), _item(2, **common),
                ), [_hold(1)])

                self.assertEqual(result["constraints"], [])
                self.assertTrue(result["issues"])
                self.assertTrue(all(issue["status"] == "review_required" for issue in result["issues"]))
                self.assertEqual([hold["item_uuid"] for hold in result["evaluation_holds"]], [_uuid(1)])
                self.assertEqual(result["issues"][0]["code"], "duplicate_signal")
                self.assertEqual(result["issues"][0]["evidence"]["group"]["basis"], basis)

    def test_annual_reports_with_different_titles_remain_separate(self) -> None:
        """Шаблонная аннотация одиннадцати годовых отчётов не распространяет исключение."""

        result = build_h2_leakage_review(_snapshot(*(
            _item(number, **{
                "dc.title": [f"Отчёт семинара за {2010 + number} год"],
                "dc.description.abstract": ["Годовой отчёт научного семинара."],
                "dc.date.issued": [str(2011 + number)],
            })
            for number in range(1, 12)
        )), [_hold(1)])

        self.assertEqual(result["constraints"], [])
        self.assertTrue(result["issues"])
        self.assertEqual([hold["item_uuid"] for hold in result["evaluation_holds"]], [_uuid(1)])


class H2LeakageRelationTests(unittest.TestCase):
    """Внутренние связи версий дают ограничения, остальные остаются диагностикой."""

    def test_reciprocal_relations_form_one_constraint_for_both_cards(self) -> None:
        """Прямая и обратная связи препринта относятся к одной паре карточек."""

        result = build_h2_leakage_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}),
            _item(2, **{
                "dc.type": ["Preprint"],
                "local.relation.is-preprint-of": [_uuid(1)],
            }),
        ), [])
        constraints = _constraints(result, "source_version_relation")

        self.assertEqual(len(result["constraints"]), 1)
        self.assertEqual(constraints[0]["item_uuids"], [_uuid(1), _uuid(2)])
        evidence = constraints[0]["evidence"]

        self.assertEqual(len(evidence["relations"]), 2)
        self.assertEqual(evidence["relation_ids"], sorted(
            relation["relation_id"] for relation in evidence["relations"]
        ))
        self.assertTrue(all(relation["reciprocal"] for relation in evidence["relations"]))

    def test_one_way_translation_relation_also_forms_constraint(self) -> None:
        """Для явной внутренней версии не требуется наличия обратной ссылки."""

        result = build_h2_leakage_review(_snapshot(
            _item(1, **{
                "local.relation.has-translated-version": [
                    f"https://pubrepo.jinr.ru/entities/publication/{_uuid(2)}",
                ],
            }),
            _item(2),
        ), [])

        self.assertEqual(
            [constraint["item_uuids"] for constraint in _constraints(result, "source_version_relation")],
            [[_uuid(1), _uuid(2)]],
        )

    def test_unresolved_relations_never_create_constraints_or_external_holds(self) -> None:
        """Внешняя цель, самоссылка и неверное поле сохраняются только для проверки."""

        cases = (
            ("local.relation.has-preprint", _uuid(99), "outside_snapshot"),
            ("local.relation.has-preprint", _uuid(1), "self_link"),
            ("local.relation.has-preprint", "", "invalid_target"),
            ("local.relation.has-preprint", "не UUID", "invalid_target"),
            ("local.relation.has-preprint", f"https://example.org/items/{_uuid(2)}", "invalid_target"),
            ("local.relation.unrecognized", _uuid(2), "unsupported_relation"),
        )

        for field, target, expected_status in cases:
            with self.subTest(field=field, target=target):
                result = build_h2_leakage_review(_snapshot(
                    _item(1, **{field: [target]}), _item(2),
                ), [_hold(1)])

                self.assertEqual(result["constraints"], [])
                self.assertEqual(len(result["issues"]), 1)
                self.assertEqual(result["issues"][0]["status"], "review_required")
                self.assertEqual([hold["item_uuid"] for hold in result["evaluation_holds"]], [_uuid(1)])
                issue = result["issues"][0]
                relation = issue["evidence"]["relation"]

                self.assertEqual(issue["code"], expected_status)
                self.assertEqual(issue["item_uuids"], [_uuid(1)])
                self.assertEqual(relation["field"], field)
                self.assertEqual(relation["raw_value"], target)
                self.assertEqual(relation["target_status"], expected_status)
                self.assertEqual(result["evaluation_holds"][0]["unresolved_relation_ids"], [
                    relation["relation_id"],
                ])

    def test_repeated_invalid_values_keep_each_original_relation(self) -> None:
        """Повтор ошибочного значения остаётся отдельным свидетельством с его индексом."""

        result = build_h2_leakage_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": ["ошибка", "ошибка"]}),
        ), [])
        relations = [issue["evidence"]["relation"] for issue in result["issues"]]

        self.assertEqual(len(relations), 2)
        self.assertEqual({relation["value_index"] for relation in relations}, {0, 1})
        self.assertEqual(len({relation["relation_id"] for relation in relations}), 2)


class H2LeakageEvaluationHoldTests(unittest.TestCase):
    """Консервативное исключение связанных практических карточек из будущей оценки."""

    def test_hold_follows_exact_group_and_preprint_relation_transitively(self) -> None:
        """Цепочка практическая статья, её текстовый повтор, препринт исключается целиком."""

        common = {
            "dc.title": ["Общее исследование"],
            "dc.description.abstract": ["Общие результаты эксперимента."],
        }
        result = build_h2_leakage_review(_snapshot(
            _item(1, **common),
            _item(2, **{**common, "local.relation.has-preprint": [_uuid(3)]}),
            _item(3, **{"dc.type": ["Preprint"]}),
            _item(4),
        ), [_hold(1, work_id="work:practice", seen_by_annotators=True)])
        holds = {hold["item_uuid"]: hold for hold in result["evaluation_holds"]}
        constraint_ids = sorted(constraint["constraint_id"] for constraint in result["constraints"])

        self.assertEqual(set(holds), {_uuid(1), _uuid(2), _uuid(3)})
        self.assertEqual(holds[_uuid(1)]["work_id"], "work:practice")
        self.assertEqual(holds[_uuid(1)]["reason"], "reserved_for_practice")
        self.assertTrue(holds[_uuid(1)]["seen_by_annotators"])

        for number in (2, 3):
            self.assertIsNone(holds[_uuid(number)]["work_id"])
            self.assertEqual(holds[_uuid(number)]["reason"], "related_to_practice")
            self.assertIsNone(holds[_uuid(number)]["seen_by_annotators"])

        for item_uuid, hold in holds.items():
            self.assertEqual(hold["candidate_id"], f"jinr:{item_uuid}")
            self.assertEqual(hold["origin_item_uuids"], [_uuid(1)])
            self.assertEqual(hold["constraint_ids"], constraint_ids)
            self.assertTrue(hold["exclude_from_future_evaluation"])
            self.assertFalse(hold["automatic_split_enforcement"])
            self.assertFalse(hold["applied"])

    def test_normalized_text_match_also_propagates_hold(self) -> None:
        """Различия регистра не позволяют повторному входу попасть в будущую оценку."""

        result = build_h2_leakage_review(_snapshot(
            _item(1, **{
                "dc.title": ["Общее исследование"],
                "dc.description.abstract": ["Общие результаты эксперимента."],
            }),
            _item(2, **{
                "dc.title": ["ОБЩЕЕ ИССЛЕДОВАНИЕ"],
                "dc.description.abstract": ["ОБЩИЕ РЕЗУЛЬТАТЫ ЭКСПЕРИМЕНТА."],
            }),
        ), [_hold(1)])

        self.assertEqual([hold["item_uuid"] for hold in result["evaluation_holds"]], [_uuid(1), _uuid(2)])
        self.assertEqual(len(_constraints(result, "same_h2_input_normalized")), 1)

    def test_multiple_original_holds_preserve_both_anchors_without_identity_merge(self) -> None:
        """Связная компонента хранит все основания и индивидуальные work_id исходных карточек."""

        result = build_h2_leakage_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}),
            _item(2, **{
                "dc.type": ["Preprint"],
                "local.relation.has-translated-version": [_uuid(3)],
            }),
            _item(3),
        ), [
            _hold(1, work_id="work:first", seen_by_annotators=False),
            _hold(3, work_id="work:third", seen_by_annotators=True),
        ])
        holds = {hold["item_uuid"]: hold for hold in result["evaluation_holds"]}

        self.assertEqual(len(holds), 3)

        for hold in holds.values():
            self.assertEqual(hold["origin_item_uuids"], [_uuid(1), _uuid(3)])

        self.assertEqual(holds[_uuid(1)]["work_id"], "work:first")
        self.assertEqual(holds[_uuid(3)]["work_id"], "work:third")
        self.assertEqual(holds[_uuid(1)]["reason"], "reserved_for_practice")
        self.assertEqual(holds[_uuid(3)]["reason"], "reserved_for_practice")
        self.assertFalse(holds[_uuid(1)]["seen_by_annotators"])
        self.assertTrue(holds[_uuid(3)]["seen_by_annotators"])
        self.assertIsNone(holds[_uuid(2)]["work_id"])
        self.assertIsNone(holds[_uuid(2)]["seen_by_annotators"])

    def test_isolated_seed_stays_reserved_with_no_constraint_evidence(self) -> None:
        """Практическая карточка исключается и без совпадений с другими карточками."""

        result = build_h2_leakage_review(_snapshot(_item(1), _item(2)), [_hold(1)])
        hold = result["evaluation_holds"][0]

        self.assertEqual(len(result["evaluation_holds"]), 1)
        self.assertEqual(hold["item_uuid"], _uuid(1))
        self.assertEqual(hold["origin_item_uuids"], [_uuid(1)])
        self.assertEqual(hold["constraint_ids"], [])
        self.assertEqual(hold["reason"], "reserved_for_practice")

    def test_no_original_holds_means_no_evaluation_exclusions(self) -> None:
        """Ограничения сами по себе не назначают практику и не исключают компоненту."""

        result = build_h2_leakage_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}), _item(2),
        ), [])

        self.assertTrue(result["constraints"])
        self.assertEqual(result["evaluation_holds"], [])

    def test_external_relation_on_propagated_member_remains_unresolved(self) -> None:
        """Внешняя версия соседа видна во всей компоненте без выдуманного резерва цели."""

        result = build_h2_leakage_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}),
            _item(2, **{"local.relation.has-translated-version": [_uuid(99)]}),
        ), [_hold(1)])
        relation_id = result["issues"][0]["evidence"]["relation"]["relation_id"]

        self.assertEqual([hold["item_uuid"] for hold in result["evaluation_holds"]], [_uuid(1), _uuid(2)])

        for hold in result["evaluation_holds"]:
            self.assertEqual(hold["unresolved_relation_ids"], [relation_id])


class H2LeakageSafetyTests(unittest.TestCase):
    """Проверка входов, воспроизводимости и отсутствия автоматических решений."""

    def test_empty_snapshot_remains_explicitly_preliminary(self) -> None:
        """Пустой результат не объявляет корпус готовым к обучению или проверенным по split."""

        result = build_h2_leakage_review(_snapshot(), [])

        self.assertEqual(result["constraints"], [])
        self.assertEqual(result["evaluation_holds"], [])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["summary"]["status"], "preliminary_review")

        for field in (
            "training_ready", "splits_checked", "automatic_split_enforcement", "registries_changed",
        ):
            self.assertIs(result["summary"][field], False)

    def test_constraints_are_only_explicit_unapplied_proposals(self) -> None:
        """Ограничение содержит свидетельства и не превращается в исполненное назначение split."""

        result = build_h2_leakage_review(_snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}), _item(2),
        ), [_hold(1)])

        for constraint in result["constraints"]:
            self.assertIsInstance(constraint["constraint_id"], str)
            self.assertTrue(constraint["constraint_id"])
            self.assertEqual(constraint["item_uuids"], sorted(set(constraint["item_uuids"])))
            self.assertIsInstance(constraint["evidence"], dict)
            self.assertTrue(constraint["evidence"])
            self.assertEqual(constraint["proposed_action"], "keep_together")
            self.assertEqual(constraint["status"], "proposed")
            self.assertIs(constraint["applied"], False)

    def test_permutations_of_items_and_holds_do_not_change_results(self) -> None:
        """Порядок входных карточек и практических оснований не влияет на пакет."""

        items = (
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}),
            _item(2, **{
                "local.relation.has-translated-version": [_uuid(3)],
                "local.EDN": ["ABCDEF"],
            }),
            _item(3, **{"local.EDN": ["ABCDEF"]}),
        )
        holds = [_hold(1), _hold(3)]
        expected = build_h2_leakage_review(_snapshot(*items), holds)

        for ordered_items in itertools.permutations(items):
            for ordered_holds in (holds, list(reversed(holds))):
                with self.subTest(items=[item["uuid"] for item in ordered_items]):
                    self.assertEqual(
                        build_h2_leakage_review(_snapshot(*ordered_items), ordered_holds), expected,
                    )

    def test_snapshot_and_original_holds_are_not_mutated_or_shared(self) -> None:
        """Изменения результата не могут переписать снимок и резервирование практики."""

        snapshot = _snapshot(
            _item(1, **{"local.relation.has-preprint": [_uuid(2)]}), _item(2),
        )
        holds = [_hold(1, seen_by_annotators=True)]
        original_snapshot = copy.deepcopy(snapshot)
        original_holds = copy.deepcopy(holds)
        result = build_h2_leakage_review(snapshot, holds)

        self.assertEqual(snapshot, original_snapshot)
        self.assertEqual(holds, original_holds)

        result["constraints"][0]["item_uuids"].append(_uuid(99))
        result["constraints"][0]["evidence"]["relations"][0]["source"]["page_path"] = "изменённая копия"
        result["constraints"][0]["evidence"].clear()
        result["evaluation_holds"][0]["origin_item_uuids"].clear()
        result["evaluation_holds"][0]["seen_by_annotators"] = False

        self.assertEqual(snapshot, original_snapshot)
        self.assertEqual(holds, original_holds)

    def test_duplicate_or_absent_practice_uuid_is_rejected(self) -> None:
        """Повторное основание и ссылка на отсутствующую карточку не принимаются молча."""

        for holds in ([_hold(1), _hold(1)], [_hold(99)]):
            with self.subTest(holds=holds), self.assertRaises(ValueError):
                build_h2_leakage_review(_snapshot(_item(1)), holds)

    def test_invalid_practice_fields_are_rejected(self) -> None:
        """Неверные идентификаторы, типы и защитные флаги останавливают сборку пакета."""

        cases = (
            ("item_uuid", "не UUID"),
            ("candidate_id", f"jinr:{_uuid(2)}"),
            ("candidate_id", 1),
            ("work_id", 1),
            ("input_sha256", "g" * 64),
            ("input_sha256", "a" * 63),
            ("input_sha256", None),
            ("status", "released"),
            ("seen_by_annotators", 1),
            ("seen_by_annotators", "false"),
            ("exclude_from_future_evaluation", False),
            ("exclude_from_future_evaluation", 1),
            ("related_versions_require_check", False),
            ("related_versions_require_check", "true"),
            ("automatic_split_enforcement", True),
            ("automatic_split_enforcement", 0),
            ("reason", 1),
        )

        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                build_h2_leakage_review(_snapshot(_item(1)), [_hold(1, **{field: value})])

    def test_practice_hold_must_have_every_required_field(self) -> None:
        """Отсутствующие защитные поля и исходные идентификаторы нельзя додумывать."""

        for field in _hold(1):
            with self.subTest(field=field):
                hold = _hold(1)
                del hold[field]

                with self.assertRaises(ValueError):
                    build_h2_leakage_review(_snapshot(_item(1)), [hold])

    def test_invalid_practice_container_types_are_rejected(self) -> None:
        """Вместо списка объектов нельзя передать словарь, null или произвольную запись."""

        cases: list[Any] = [None, {}, (), [None], [1], ["карточка"]]

        for holds in cases:
            with self.subTest(holds=holds), self.assertRaises(TypeError):
                build_h2_leakage_review(_snapshot(_item(1)), holds)


if __name__ == "__main__":
    unittest.main()
