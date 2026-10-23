"""Проверки сохранения связей версий ОИЯИ без потери исходных значений."""

from __future__ import annotations

import copy
import unittest

from typing import Any

from src.corpus.h2_candidate_input import CandidateSnapshot
from src.corpus.h2_version_relations import extract_version_relations


FIRST_UUID = "aaaaaaaa-2222-4333-8444-000000000001"
SECOND_UUID = "bbbbbbbb-2222-4333-8444-000000000002"
THIRD_UUID = "cccccccc-2222-4333-8444-000000000003"
PREPRINT_FIELD = "local.relation.has-preprint"
ARTICLE_FIELD = "local.relation.is-preprint-of"
TRANSLATION_FIELD = "local.relation.has-translated-version"
ORIGINAL_FIELD = "local.relation.is-translated-version-of"


def _item(item_uuid: str, **fields: list[str]) -> dict[str, Any]:
    """Составить карточку API с точными строковыми значениями метаданных."""

    return {
        "uuid": item_uuid,
        "metadata": {
            field: [{"value": value, "language": None} for value in values]
            for field, values in fields.items()
        },
    }


def _snapshot(*items: dict[str, Any]) -> CandidateSnapshot:
    """Связать карточки с независимыми вложенными сведениями о происхождении."""

    return CandidateSnapshot(
        items=tuple(items),
        sources={
            item["uuid"]: {"page_path": "data/raw/page.json", "nested": {"index": index}}
            for index, item in enumerate(items)
        },
        provenance={"snapshot_sha256": "a" * 64},
    )


class VersionRelationsTests(unittest.TestCase):
    """Проверки ссылок, взаимности, воспроизводимости и неизменности снимка."""

    def test_plain_uuid_and_target_presence(self) -> None:
        """Известная и отсутствующая карточки получают разные диагностические статусы."""

        records = extract_version_relations(_snapshot(
            _item(FIRST_UUID, **{PREPRINT_FIELD: [SECOND_UUID, THIRD_UUID]}),
            _item(SECOND_UUID),
        ))

        self.assertEqual([record["target_status"] for record in records], [
            "in_snapshot", "outside_snapshot",
        ])
        self.assertEqual([record["target_item_uuid"] for record in records], [
            SECOND_UUID, THIRD_UUID,
        ])
        self.assertTrue(all(not record["reciprocal"] for record in records))

    def test_uppercase_target_and_outer_spaces_keep_original(self) -> None:
        """Регистр цели и крайние пробелы нормализуются без изменения исходной строки."""

        raw_value = f"  {SECOND_UUID.upper()}  "
        records = extract_version_relations(_snapshot(_item(
            FIRST_UUID, **{PREPRINT_FIELD: [raw_value]},
        )))

        self.assertEqual(records[0]["target_item_uuid"], SECOND_UUID)
        self.assertEqual(records[0]["raw_value"], raw_value)

    def test_known_publication_urls(self) -> None:
        """Поддерживаются только адреса карточек известных сайтов репозитория."""

        addresses = [
            f"https://pubrepo.jinr.ru/items/{SECOND_UUID}",
            f"http://pubrepo.jinr.ru/entities/publication/{SECOND_UUID}/",
            f"https://pubrepo-api.jinr.ru/server/api/core/items/{SECOND_UUID}",
            f"https://PUBREPO.JINR.RU:443/items/{SECOND_UUID.upper()}",
            f"http://pubrepo.jinr.ru:80/items/{SECOND_UUID}",
        ]
        records = extract_version_relations(_snapshot(_item(
            FIRST_UUID, **{PREPRINT_FIELD: addresses},
        )))

        self.assertEqual([record["raw_value"] for record in records], addresses)
        self.assertTrue(all(record["target_item_uuid"] == SECOND_UUID for record in records))

    def test_unsafe_or_ambiguous_urls_remain_visible(self) -> None:
        """Посторонние хосты, хвосты и неоднозначные адреса не становятся UUID ОИЯИ."""

        addresses = [
            f"https://example.org/items/{SECOND_UUID}",
            f"https://pubrepo.jinr.ru.example.org/items/{SECOND_UUID}",
            f"https://pubrepo.jinr.ru@evil.org/items/{SECOND_UUID}",
            f"https://user@pubrepo.jinr.ru/items/{SECOND_UUID}",
            f"https://@pubrepo.jinr.ru/items/{SECOND_UUID}",
            f"https://pubrepo.jinr.ru:444/items/{SECOND_UUID}",
            f"https://pubrepo.jinr.ru:bad/items/{SECOND_UUID}",
            f"https://[pubrepo.jinr.ru/items/{SECOND_UUID}",
            f"https://pubrepo.jinr.ru/items/{SECOND_UUID}?version=2",
            f"https://pubrepo.jinr.ru/items/{SECOND_UUID}#other",
            f"https://pubrepo.jinr.ru/items/{SECOND_UUID}/bundles",
            f"https://pubrepo.jinr.ru/other/{SECOND_UUID}",
            f"https://pubrepo-api.jinr.ru/items/{SECOND_UUID}",
            f"https://pubrepo.jinr.ru/server/api/core/items/{SECOND_UUID}",
            f"ftp://pubrepo.jinr.ru/items/{SECOND_UUID}",
            f"//pubrepo.jinr.ru/items/{SECOND_UUID}",
            f"https://pubrepo.jinr.ru/items/%62{SECOND_UUID[1:]}",
            f"https://pubrepo.jinr.ru/ite\nms/{SECOND_UUID}",
            f"\n{SECOND_UUID}",
            f"https://pubrepo.jinr.ru/items/{SECOND_UUID}\x7f",
        ]

        for address in addresses:
            with self.subTest(address=address):
                record = extract_version_relations(_snapshot(_item(
                    FIRST_UUID, **{PREPRINT_FIELD: [address]},
                )))[0]

                self.assertEqual(record["raw_value"], address)
                self.assertIsNone(record["target_item_uuid"])
                self.assertEqual(record["target_status"], "invalid_target")

    def test_malformed_targets_are_not_lost(self) -> None:
        """Пустые строки, произвольный текст и нестандартная запись UUID сохраняются."""

        values = ["", "   ", "не UUID", SECOND_UUID.replace("-", ""), f"{{{SECOND_UUID}}}"]
        records = extract_version_relations(_snapshot(_item(
            FIRST_UUID, **{PREPRINT_FIELD: values},
        )))

        self.assertEqual([record["raw_value"] for record in records], values)
        self.assertTrue(all(record["target_status"] == "invalid_target" for record in records))

    def test_self_links_are_not_reciprocal(self) -> None:
        """Две встречные ссылки карточки на саму себя не подтверждают связь версий."""

        records = extract_version_relations(_snapshot(_item(FIRST_UUID, **{
            PREPRINT_FIELD: [FIRST_UUID], ARTICLE_FIELD: [FIRST_UUID],
        })))

        self.assertTrue(all(record["target_status"] == "self_link" for record in records))
        self.assertTrue(all(not record["reciprocal"] for record in records))

    def test_inverse_preprint_links_are_reciprocal(self) -> None:
        """Взаимность требует двух разных карточек и обратного типа связи."""

        records = extract_version_relations(_snapshot(
            _item(FIRST_UUID, **{PREPRINT_FIELD: [SECOND_UUID]}),
            _item(SECOND_UUID, **{ARTICLE_FIELD: [FIRST_UUID]}),
        ))

        self.assertTrue(all(record["reciprocal"] for record in records))

    def test_inverse_translation_links_are_reciprocal(self) -> None:
        """Перевод и оригинал образуют отдельную пару обратных типов связи."""

        records = extract_version_relations(_snapshot(
            _item(FIRST_UUID, **{TRANSLATION_FIELD: [SECOND_UUID]}),
            _item(SECOND_UUID, **{ORIGINAL_FIELD: [FIRST_UUID]}),
        ))

        self.assertTrue(all(record["reciprocal"] for record in records))
        self.assertEqual({record["kind"] for record in records}, {
            "has_translated_version", "is_translated_version_of",
        })

    def test_same_or_unrelated_kinds_do_not_prove_reciprocity(self) -> None:
        """Обратный адрес без правильного обратного отношения не считается взаимностью."""

        for reverse_field in (PREPRINT_FIELD, TRANSLATION_FIELD, ORIGINAL_FIELD):
            with self.subTest(reverse_field=reverse_field):
                records = extract_version_relations(_snapshot(
                    _item(FIRST_UUID, **{PREPRINT_FIELD: [SECOND_UUID]}),
                    _item(SECOND_UUID, **{reverse_field: [FIRST_UUID]}),
                ))

                self.assertTrue(all(not record["reciprocal"] for record in records))

    def test_repeated_entries_have_distinct_ids_and_original_indexes(self) -> None:
        """Повтор одного значения не исчезает и различается исходным индексом."""

        records = extract_version_relations(_snapshot(_item(
            FIRST_UUID, **{PREPRINT_FIELD: [SECOND_UUID, SECOND_UUID, ""]},
        )))

        self.assertEqual([record["value_index"] for record in records], [0, 1, 2])
        self.assertEqual(len({record["relation_id"] for record in records}), 3)
        self.assertEqual([record["raw_value"] for record in records], [
            SECOND_UUID, SECOND_UUID, "",
        ])

    def test_unknown_relations_are_retained_without_guessing(self) -> None:
        """Неподдерживаемое отношение остаётся видимым даже при корректном UUID."""

        records = extract_version_relations(_snapshot(
            _item(FIRST_UUID, **{"local.relation.related": [SECOND_UUID, ""]}),
            _item(SECOND_UUID),
        ))

        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["kind"] == "unsupported" for record in records))
        self.assertTrue(all(record["target_status"] == "unsupported_relation" for record in records))
        self.assertTrue(all(record["target_item_uuid"] is None for record in records))
        self.assertTrue(all(not record["reciprocal"] for record in records))

    def test_nonarticle_cards_are_included(self) -> None:
        """Препринты и отчёты не исключаются фильтром журнальных статей."""

        records = extract_version_relations(_snapshot(
            _item(FIRST_UUID, **{"dc.type": ["Preprint"], ARTICLE_FIELD: [SECOND_UUID]}),
            _item(SECOND_UUID, **{"dc.type": ["Report"], PREPRINT_FIELD: [FIRST_UUID]}),
        ))

        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["reciprocal"] for record in records))

    def test_other_fields_and_empty_lists_do_not_create_links(self) -> None:
        """Обычные поля и список без исходных значений не создают фиктивной связи."""

        records = extract_version_relations(_snapshot(_item(FIRST_UUID, **{
            "dc.relation.ispartof": [SECOND_UUID],
            "dc.identifier.uri": [SECOND_UUID],
            "local.relation": [SECOND_UUID],
            PREPRINT_FIELD: [],
        })))

        self.assertEqual(records, [])

    def test_empty_snapshot_has_no_relations(self) -> None:
        """Корректный пустой снимок возвращает пустой перечень."""

        self.assertEqual(extract_version_relations(_snapshot()), [])

    def test_output_does_not_mutate_snapshot_or_share_provenance(self) -> None:
        """Изменение результата не затрагивает ни исходник, ни соседнее свидетельство."""

        snapshot = _snapshot(_item(FIRST_UUID, **{PREPRINT_FIELD: [SECOND_UUID, THIRD_UUID]}))
        before = copy.deepcopy(snapshot)
        records = extract_version_relations(snapshot)
        records[0]["source"]["nested"]["index"] = 99

        self.assertEqual(snapshot, before)
        self.assertEqual(records[1]["source"]["nested"]["index"], 0)

    def test_order_and_identifiers_are_stable(self) -> None:
        """Порядок карточек и ключей не влияет на порядок и идентификаторы результата."""

        first = _item(FIRST_UUID, **{PREPRINT_FIELD: [SECOND_UUID], TRANSLATION_FIELD: [THIRD_UUID]})
        second = _item(SECOND_UUID, **{ARTICLE_FIELD: [FIRST_UUID]})
        snapshot = _snapshot(first, second)
        reordered_first = copy.deepcopy(first)
        reordered_first["metadata"] = dict(reversed(list(first["metadata"].items())))
        reordered = CandidateSnapshot(
            items=(second, reordered_first), sources=snapshot.sources, provenance=snapshot.provenance,
        )

        self.assertEqual(extract_version_relations(snapshot), extract_version_relations(reordered))

    def test_relation_identifier_depends_on_exact_raw_value(self) -> None:
        """Изменение исходной строки меняет идентификатор при том же нормализованном UUID."""

        plain = extract_version_relations(_snapshot(_item(
            FIRST_UUID, **{PREPRINT_FIELD: [SECOND_UUID]},
        )))[0]
        uppercase = extract_version_relations(_snapshot(_item(
            FIRST_UUID, **{PREPRINT_FIELD: [SECOND_UUID.upper()]},
        )))[0]

        self.assertEqual(plain["target_item_uuid"], uppercase["target_item_uuid"])
        self.assertNotEqual(plain["relation_id"], uppercase["relation_id"])

    def test_duplicate_source_uuid_is_rejected(self) -> None:
        """Повтор UUID карточки не скрывается при построении внутреннего указателя."""

        with self.assertRaisesRegex(ValueError, "повторяет UUID"):
            extract_version_relations(_snapshot(_item(FIRST_UUID), _item(FIRST_UUID)))

    def test_noncanonical_source_uuid_is_rejected(self) -> None:
        """UUID исходной карточки подчиняется строгим правилам загрузчика снимка."""

        for item_uuid in (FIRST_UUID.upper(), FIRST_UUID.replace("-", ""), "не UUID"):
            with self.subTest(item_uuid=item_uuid), self.assertRaises(ValueError):
                extract_version_relations(_snapshot(_item(item_uuid)))

    def test_missing_or_invalid_provenance_is_rejected(self) -> None:
        """Нельзя представить связь проверенной без объекта происхождения её карточки."""

        invalid_sources: list[dict[str, Any]] = [{}, {FIRST_UUID: "не объект"}]

        for sources in invalid_sources:
            with self.subTest(sources=sources), self.assertRaisesRegex(ValueError, "происхождения"):
                extract_version_relations(CandidateSnapshot(
                    items=(_item(FIRST_UUID),), sources=sources, provenance={},
                ))

    def test_malformed_metadata_is_rejected(self) -> None:
        """Неверная структура метаданных не превращается в частичный перечень связей."""

        malformed_metadata = [
            None,
            [],
            {PREPRINT_FIELD: SECOND_UUID},
            {PREPRINT_FIELD: [SECOND_UUID]},
            {PREPRINT_FIELD: [{}]},
            {PREPRINT_FIELD: [{"value": None}]},
            {PREPRINT_FIELD: [{"value": 123}]},
            {"dc.title": [{}]},
            {"": []},
            {42: []},
        ]

        for metadata in malformed_metadata:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                item = _item(FIRST_UUID)
                item["metadata"] = metadata
                extract_version_relations(_snapshot(item))

    def test_no_merge_label_or_split_is_assigned(self) -> None:
        """Запись связи не выдаёт разрешения сливать карточки или назначать выборку."""

        record = extract_version_relations(_snapshot(_item(
            FIRST_UUID, **{PREPRINT_FIELD: [SECOND_UUID]},
        )))[0]

        for field in ("work_id", "family_id", "label", "split", "confirmed", "merge"):
            self.assertNotIn(field, record)


if __name__ == "__main__":
    unittest.main()
