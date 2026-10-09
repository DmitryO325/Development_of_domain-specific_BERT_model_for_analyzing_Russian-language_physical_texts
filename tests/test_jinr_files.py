"""Проверки связей публикаций ОИЯИ с файлами без загрузки содержимого."""

from __future__ import annotations

import copy
import unittest

from typing import Any
from unittest.mock import MagicMock

from src.collect.jinr_api import DEFAULT_API_URL, JinrApiError
from src.collect.jinr_files import collect_item_files


ITEM_UUID = "11111111-2222-4333-8444-555555555555"
BUNDLE_UUID = "22222222-2222-4333-8444-555555555555"
OTHER_BUNDLE_UUID = "33333333-2222-4333-8444-555555555555"
BITSTREAM_UUID = "44444444-2222-4333-8444-555555555555"
OTHER_BITSTREAM_UUID = "55555555-2222-4333-8444-555555555555"

ITEM_URL = f"{DEFAULT_API_URL}/core/items/{ITEM_UUID}"
BUNDLES_URL = f"{ITEM_URL}/bundles"
BITSTREAMS_URL = f"{DEFAULT_API_URL}/core/bundles/{BUNDLE_UUID}/bitstreams"
BITSTREAM_URL = f"{DEFAULT_API_URL}/core/bitstreams/{BITSTREAM_UUID}"


def _item() -> dict[str, Any]:
    """Создать карточку публикации с явной ссылкой на её наборы файлов."""

    return {
        "uuid": ITEM_UUID,
        "type": "item",
        "name": "Исследование физических процессов",
        "metadata": {
            "dc.title": [{"value": "Исследование физических процессов"}],
        },
        "_links": {
            "self": {"href": ITEM_URL},
            "bundles": {"href": BUNDLES_URL},
        },
    }


def _bundle(
    bundle_uuid: str = BUNDLE_UUID,
    name: str = "ORIGINAL",
) -> dict[str, Any]:
    """Создать набор файлов, принадлежащий тестовой публикации."""

    bundle_url = f"{DEFAULT_API_URL}/core/bundles/{bundle_uuid}"

    return {
        "uuid": bundle_uuid,
        "type": "bundle",
        "name": name,
        "_links": {
            "self": {"href": bundle_url},
            "bitstreams": {"href": f"{bundle_url}/bitstreams"},
        },
    }


def _bitstream(
    bitstream_uuid: str = BITSTREAM_UUID,
    *,
    details: bool = False,
) -> dict[str, Any]:
    """Создать метаданные файла, отдельно от его двоичного содержимого."""

    bitstream_url = f"{DEFAULT_API_URL}/core/bitstreams/{bitstream_uuid}"
    record: dict[str, Any] = {
        "uuid": bitstream_uuid,
        "type": "bitstream",
        "name": "article.pdf",
        "sizeBytes": 4096,
        "checkSum": {"checkSumAlgorithm": "MD5", "value": "a" * 32},
        "_links": {
            "self": {"href": bitstream_url},
            "content": {"href": f"{bitstream_url}/content"},
        },
    }

    if details:
        record["_links"].update({
            "format": {"href": f"{bitstream_url}/format"},
            "accessStatus": {"href": f"{bitstream_url}/accessStatus"},
        })

    return record


def _page(
    collection: str,
    records: list[dict[str, Any]],
    *,
    number: int = 0,
    total_elements: int | None = None,
    next_url: str | None = None,
) -> dict[str, Any]:
    """Сформировать страницу HAL с согласованным количеством элементов."""

    if total_elements is None:
        total_elements = len(records)

    page_size = max(len(records), 1)
    total_pages = (total_elements + page_size - 1) // page_size
    record: dict[str, Any] = {
        "_embedded": {collection: records},
        "page": {
            "number": number,
            "size": page_size,
            "totalElements": total_elements,
            "totalPages": total_pages,
        },
        "_links": {},
    }

    if next_url is not None:
        record["_links"]["next"] = {"href": next_url}

    return record


def _responses(*, details: bool = False) -> dict[str, dict[str, Any]]:
    """Описать один набор ORIGINAL и один файл без настоящих запросов."""

    responses = {
        BUNDLES_URL: _page("bundles", [_bundle()]),
        BITSTREAMS_URL: _page("bitstreams", [_bitstream(details=details)]),
    }

    if details:
        responses[f"{BITSTREAM_URL}/format"] = {
            "type": "bitstreamformat",
            "id": 2,
            "mimetype": "application/pdf",
        }
        responses[f"{BITSTREAM_URL}/accessStatus"] = {
            "type": "accessStatus",
            "status": "open.access",
            "embargoDate": None,
        }

    return responses


class JinrFileTraversalTests(unittest.TestCase):
    """Проверки корректного обхода только исходных файлов публикации."""

    def test_original_file_preserves_metadata_without_content_request(self) -> None:
        """Связь, размер, MD5, формат и доступ сохраняются без скачивания PDF."""

        responses = _responses(details=True)
        get_json = MagicMock(side_effect=responses.__getitem__)

        result = collect_item_files(_item(), get_json)
        file_record = result["files"][0]

        self.assertEqual(result["item_uuid"], ITEM_UUID)
        self.assertEqual(result["item_title"], "Исследование физических процессов")
        self.assertEqual(result["scope"], "ORIGINAL")
        self.assertTrue(result["listing_complete"])
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(result["original_bundle_count"], 1)
        self.assertEqual(file_record["bitstream_uuid"], BITSTREAM_UUID)
        self.assertEqual(file_record["bundle_uuid"], BUNDLE_UUID)
        self.assertEqual(file_record["name"], "article.pdf")
        self.assertEqual(file_record["size_bytes"], 4096)
        self.assertEqual(file_record["server_checksum"], {
            "algorithm": "MD5", "value": "a" * 32,
        })
        self.assertEqual(file_record["content_url"], f"{BITSTREAM_URL}/content")
        self.assertEqual(file_record["mime_type"], "application/pdf")
        self.assertEqual(file_record["access_status"], "open.access")
        self.assertIsNone(file_record["embargo_date"])
        self.assertEqual(set(result["source_urls"]), set(responses))
        self.assertEqual(get_json.call_count, 4)

    def test_non_original_bundles_are_not_followed(self) -> None:
        """LICENSE, THUMBNAIL и TEXT пропускаются даже без ссылок на файлы."""

        for bundle_name in ("LICENSE", "THUMBNAIL", "TEXT"):
            with self.subTest(bundle_name=bundle_name):
                responses = _responses()
                other_bundle = _bundle(OTHER_BUNDLE_UUID, bundle_name)
                del other_bundle["_links"]["bitstreams"]
                responses[BUNDLES_URL] = _page("bundles", [
                    other_bundle, _bundle(),
                ])
                get_json = MagicMock(side_effect=responses.__getitem__)

                result = collect_item_files(_item(), get_json)

                self.assertEqual(len(result["files"]), 1)
                self.assertEqual(get_json.call_count, 2)

    def test_empty_bundles_without_embedded_are_complete(self) -> None:
        """Нулевая коллекция HAL может не содержать блока _embedded."""

        response = _page("bundles", [])
        del response["_embedded"]
        get_json = MagicMock(return_value=response)

        result = collect_item_files(_item(), get_json)

        self.assertEqual(result["files"], [])
        self.assertEqual(result["original_bundle_count"], 0)
        self.assertTrue(result["listing_complete"])
        self.assertEqual(result["diagnostics"], [])
        get_json.assert_called_once_with(BUNDLES_URL)

    def test_original_bundle_can_have_no_files(self) -> None:
        """Пустой ORIGINAL не означает ошибку обхода и не создаёт файл."""

        responses = _responses()
        responses[BITSTREAMS_URL] = _page("bitstreams", [])
        get_json = MagicMock(side_effect=responses.__getitem__)

        result = collect_item_files(_item(), get_json)

        self.assertEqual(result["files"], [])
        self.assertEqual(result["original_bundle_count"], 1)
        self.assertTrue(result["listing_complete"])

    def test_absent_optional_metadata_remains_unknown(self) -> None:
        """Имя с .pdf не заменяет MIME, а наличие файла не открывает доступ."""

        responses = _responses()
        bitstream = responses[BITSTREAMS_URL]["_embedded"]["bitstreams"][0]
        del bitstream["checkSum"]
        del bitstream["sizeBytes"]
        del bitstream["_links"]["content"]
        get_json = MagicMock(side_effect=responses.__getitem__)

        file_record = collect_item_files(_item(), get_json)["files"][0]

        self.assertIsNone(file_record["size_bytes"])
        self.assertIsNone(file_record["server_checksum"])
        self.assertIsNone(file_record["content_url"])
        self.assertIsNone(file_record["mime_type"])
        self.assertEqual(file_record["access_status"], "unknown")
        self.assertIsNone(file_record["embargo_date"])
        self.assertEqual(get_json.call_count, 2)

    def test_closed_file_is_listed_without_downloading(self) -> None:
        """Закрытый файл остаётся в перечне со статусом, но не скачивается."""

        responses = _responses(details=True)
        responses[f"{BITSTREAM_URL}/accessStatus"].update({
            "status": "embargo",
            "embargoDate": "2027-01-01",
        })
        get_json = MagicMock(side_effect=responses.__getitem__)

        file_record = collect_item_files(_item(), get_json)["files"][0]

        self.assertEqual(file_record["access_status"], "embargo")
        self.assertEqual(file_record["embargo_date"], "2027-01-01")
        self.assertNotIn(f"{BITSTREAM_URL}/content", [
            call.args[0] for call in get_json.call_args_list
        ])

    def test_bundles_and_bitstreams_follow_all_pages(self) -> None:
        """Пагинация наборов и исходных файлов проверяется независимо."""

        next_bundles = f"{BUNDLES_URL}?page=1&size=1"
        next_bitstreams = f"{BITSTREAMS_URL}?page=1&size=1"
        responses = {
            BUNDLES_URL: _page("bundles", [_bundle(OTHER_BUNDLE_UUID, "LICENSE")],
                              total_elements=2, next_url=next_bundles),
            next_bundles: _page("bundles", [_bundle()], number=1, total_elements=2),
            BITSTREAMS_URL: _page("bitstreams", [_bitstream()],
                                 total_elements=2, next_url=next_bitstreams),
            next_bitstreams: _page("bitstreams", [_bitstream(OTHER_BITSTREAM_UUID)],
                                  number=1, total_elements=2),
        }
        get_json = MagicMock(side_effect=responses.__getitem__)

        result = collect_item_files(_item(), get_json)

        self.assertEqual([record["bitstream_uuid"] for record in result["files"]], [
            BITSTREAM_UUID, OTHER_BITSTREAM_UUID,
        ])
        self.assertEqual(set(result["source_urls"]), set(responses))
        self.assertEqual(get_json.call_count, 4)

    def test_relative_pagination_link_is_resolved(self) -> None:
        """Относительная ссылка next остаётся в той же коллекции файлов."""

        responses = _responses()
        responses[BITSTREAMS_URL] = _page(
            "bitstreams", [_bitstream()], total_elements=2,
            next_url="?page=1&size=1",
        )
        responses[f"{BITSTREAMS_URL}?page=1&size=1"] = _page(
            "bitstreams", [_bitstream(OTHER_BITSTREAM_UUID)],
            number=1, total_elements=2,
        )
        get_json = MagicMock(side_effect=responses.__getitem__)

        result = collect_item_files(_item(), get_json)

        self.assertEqual(len(result["files"]), 2)


class JinrFileValidationTests(unittest.TestCase):
    """Проверки повреждённого HAL и границ разрешённых запросов."""

    def test_invalid_item_is_rejected_before_requests(self) -> None:
        """Неправильные UUID, тип и отсутствие связи не запускают запросы."""

        for field, value in (("uuid", "wrong"), ("type", "bundle"), ("_links", {})):
            with self.subTest(field=field):
                item = _item()
                item[field] = value
                get_json = MagicMock()

                with self.assertRaises(ValueError):
                    collect_item_files(item, get_json)

                get_json.assert_not_called()

    def test_invalid_collection_entries_are_rejected(self) -> None:
        """Элемент должен иметь корректные UUID и тип своей коллекции."""

        for collection, url in (("bundles", BUNDLES_URL), ("bitstreams", BITSTREAMS_URL)):
            for field, value in (("uuid", "invalid"), ("type", "item")):
                with self.subTest(collection=collection, field=field):
                    responses = _responses()
                    responses[url]["_embedded"][collection][0][field] = value
                    get_json = MagicMock(side_effect=responses.__getitem__)

                    with self.assertRaises(ValueError):
                        collect_item_files(_item(), get_json)

    def test_invalid_embedded_collection_is_rejected(self) -> None:
        """Ненулевая коллекция не может отсутствовать или быть объектом."""

        for embedded in ({}, {"bundles": {}}, {"bundles": [None]}):
            with self.subTest(embedded=embedded):
                response = _page("bundles", [_bundle()])
                response["_embedded"] = embedded
                get_json = MagicMock(return_value=response)

                with self.assertRaises(ValueError):
                    collect_item_files(_item(), get_json)

                get_json.assert_called_once_with(BUNDLES_URL)

    def test_invalid_page_counters_are_rejected(self) -> None:
        """Счётчики страниц должны быть целыми, неотрицательными и согласованными."""

        for field, value in (
            ("number", 1), ("number", False), ("size", 0), ("size", "1"),
            ("totalElements", -1), ("totalElements", 0), ("totalPages", 0),
        ):
            with self.subTest(field=field, value=value):
                response = _page("bundles", [_bundle()])
                response["page"][field] = value
                get_json = MagicMock(return_value=response)

                with self.assertRaises(ValueError):
                    collect_item_files(_item(), get_json)

    def test_missing_next_page_is_rejected(self) -> None:
        """Нельзя объявить неполный перечень завершённым при исчезнувшей next."""

        response = _page("bundles", [_bundle()], total_elements=2)
        get_json = MagicMock(return_value=response)

        with self.assertRaises(ValueError):
            collect_item_files(_item(), get_json)

    def test_next_after_last_page_is_rejected(self) -> None:
        """Лишняя следующая страница противоречит заявленному числу страниц."""

        response = _page("bundles", [_bundle()], next_url=f"{BUNDLES_URL}?page=1")
        get_json = MagicMock(return_value=response)

        with self.assertRaises(ValueError):
            collect_item_files(_item(), get_json)

    def test_repeated_next_link_is_rejected(self) -> None:
        """Циклическая ссылка не должна бесконечно перечитывать одну страницу."""

        response = _page("bundles", [_bundle()], total_elements=2, next_url=BUNDLES_URL)
        get_json = MagicMock(return_value=response)

        with self.assertRaises(ValueError):
            collect_item_files(_item(), get_json)

        self.assertEqual([call.args[0] for call in get_json.call_args_list].count(BUNDLES_URL), 1)

    def test_repeated_uuid_on_next_page_is_rejected(self) -> None:
        """Повтор UUID на другой странице нельзя принять за ещё один файл."""

        responses = _responses()
        next_url = f"{BITSTREAMS_URL}?page=1&size=1"
        responses[BITSTREAMS_URL] = _page(
            "bitstreams", [_bitstream()], total_elements=2, next_url=next_url,
        )
        responses[next_url] = _page("bitstreams", [_bitstream()], number=1, total_elements=2)
        get_json = MagicMock(side_effect=responses.__getitem__)

        with self.assertRaises(ValueError):
            collect_item_files(_item(), get_json)

    def test_changing_total_during_pagination_is_rejected(self) -> None:
        """Изменение общего числа элементов не даёт подтвердить полноту перечня."""

        responses = _responses()
        next_url = f"{BITSTREAMS_URL}?page=1&size=1"
        responses[BITSTREAMS_URL] = _page(
            "bitstreams", [_bitstream()], total_elements=2, next_url=next_url,
        )
        responses[next_url] = _page(
            "bitstreams", [_bitstream(OTHER_BITSTREAM_UUID)], number=1, total_elements=3,
            next_url=f"{BITSTREAMS_URL}?page=2&size=1",
        )
        get_json = MagicMock(side_effect=responses.__getitem__)

        with self.assertRaises(ValueError):
            collect_item_files(_item(), get_json)

        self.assertEqual(get_json.call_count, 3)

    def test_unsafe_bundle_links_are_rejected_before_requests(self) -> None:
        """Начальная ссылка не может вести на чужой сервер, объект или PDF."""

        for unsafe_url in (
            "https://example.org/server/api/core/items/item/bundles",
            f"{DEFAULT_API_URL}/core/items/{OTHER_BUNDLE_UUID}/bundles",
            f"{BITSTREAM_URL}/content",
            BUNDLES_URL.replace("https://", "http://"),
        ):
            with self.subTest(url=unsafe_url):
                item = _item()
                item["_links"]["bundles"]["href"] = unsafe_url
                get_json = MagicMock()

                with self.assertRaises(ValueError):
                    collect_item_files(item, get_json)

                get_json.assert_not_called()

    def test_next_link_cannot_leave_parent_collection(self) -> None:
        """Пагинация не должна незаметно переходить к чужим наборам файлов."""

        for unsafe_url in (
            "https://example.org/server/api/core/items/item/bundles?page=1",
            f"{DEFAULT_API_URL}/core/items/{OTHER_BUNDLE_UUID}/bundles?page=1",
            f"{BITSTREAM_URL}/content?page=1",
            f"{BUNDLES_URL}?page=1&authentication-token=unexpected",
        ):
            with self.subTest(url=unsafe_url):
                response = _page("bundles", [_bundle()], total_elements=2, next_url=unsafe_url)
                get_json = MagicMock(return_value=response)

                with self.assertRaises(ValueError):
                    collect_item_files(_item(), get_json)

                self.assertNotIn(unsafe_url, [call.args[0] for call in get_json.call_args_list])

    def test_collection_self_link_cannot_change_parent(self) -> None:
        """Собственная ссылка страницы не должна указывать на другую публикацию."""

        response = _page("bundles", [_bundle()])
        response["_links"]["self"] = {
            "href": f"{DEFAULT_API_URL}/core/items/{OTHER_BUNDLE_UUID}/bundles",
        }
        get_json = MagicMock(return_value=response)

        with self.assertRaises(ValueError):
            collect_item_files(_item(), get_json)

        get_json.assert_called_once_with(BUNDLES_URL)

    def test_wrong_bitstream_collection_link_is_not_requested(self) -> None:
        """Набор ORIGINAL обязан ссылаться на собственную коллекцию файлов."""

        responses = _responses()
        bundle = responses[BUNDLES_URL]["_embedded"]["bundles"][0]
        unsafe_url = f"{DEFAULT_API_URL}/core/bundles/{OTHER_BUNDLE_UUID}/bitstreams"
        bundle["_links"]["bitstreams"]["href"] = unsafe_url
        get_json = MagicMock(side_effect=responses.__getitem__)

        with self.assertRaises(ValueError):
            collect_item_files(_item(), get_json)

        get_json.assert_called_once_with(BUNDLES_URL)

    def test_optional_link_cannot_request_content(self) -> None:
        """Ссылка формата или статуса не может заставить скачать содержимое."""

        for relation in ("format", "accessStatus"):
            with self.subTest(relation=relation):
                responses = _responses(details=True)
                bitstream = responses[BITSTREAMS_URL]["_embedded"]["bitstreams"][0]
                unsafe_url = f"{BITSTREAM_URL}/content"
                bitstream["_links"][relation]["href"] = unsafe_url
                get_json = MagicMock(side_effect=responses.__getitem__)

                with self.assertRaises(ValueError):
                    collect_item_files(_item(), get_json)

                self.assertNotIn(unsafe_url, [call.args[0] for call in get_json.call_args_list])

    def test_self_link_cannot_identify_another_bitstream(self) -> None:
        """UUID файла и его собственная ссылка не должны противоречить друг другу."""

        responses = _responses()
        bitstream = responses[BITSTREAMS_URL]["_embedded"]["bitstreams"][0]
        bitstream["_links"]["self"]["href"] = (
            f"{DEFAULT_API_URL}/core/bitstreams/{OTHER_BITSTREAM_UUID}"
        )
        get_json = MagicMock(side_effect=responses.__getitem__)

        with self.assertRaises(ValueError):
            collect_item_files(_item(), get_json)

    def test_invalid_optional_response_type_is_rejected(self) -> None:
        """Ответ другой сущности не подменяет сведения о формате или доступе."""

        for relation in ("format", "accessStatus"):
            with self.subTest(relation=relation):
                responses = _responses(details=True)
                responses[f"{BITSTREAM_URL}/{relation}"]["type"] = "item"
                get_json = MagicMock(side_effect=responses.__getitem__)

                with self.assertRaises(ValueError):
                    collect_item_files(_item(), get_json)

    def test_source_failure_propagates_without_partial_result(self) -> None:
        """Ошибка доступа не превращается в успешный пустой список файлов."""

        exception = JinrApiError("Временная недоступность API")
        get_json = MagicMock(side_effect=exception)

        with self.assertRaises(JinrApiError) as caught:
            collect_item_files(_item(), get_json)

        self.assertIs(caught.exception, exception)
        get_json.assert_called_once_with(BUNDLES_URL)

    def test_input_item_is_not_modified(self) -> None:
        """Построение перечня не должно изменять сохранённую карточку публикации."""

        item = _item()
        original = copy.deepcopy(item)
        responses = _responses()
        get_json = MagicMock(side_effect=responses.__getitem__)

        collect_item_files(item, get_json)

        self.assertEqual(item, original)


class JinrPartialFileListingTests(unittest.TestCase):
    """Проверки видимых файлов при расхождении счётчиков без ослабления проверок."""

    def test_empty_declared_collection_has_explicit_shortage(self) -> None:
        """Пустой список при ненулевом счётчике не подтверждает отсутствие файлов."""

        for relation, url in (("bundles", BUNDLES_URL), ("bitstreams", BITSTREAMS_URL)):
            with self.subTest(relation=relation):
                responses = _responses(details=True)
                responses[url] = _page(relation, [], total_elements=1)
                get_json = MagicMock(side_effect=responses.__getitem__)

                result = collect_item_files(_item(), get_json)

                self.assertEqual(result["files"], [])
                self.assertFalse(result["listing_complete"])
                self.assertEqual(len(result["diagnostics"]), 1)
                diagnostic = result["diagnostics"][0]
                self.assertIsInstance(diagnostic["message"], str)
                self.assertIn("Причина расхождения не установлена", diagnostic["message"])
                self.assertEqual(
                    {key: value for key, value in diagnostic.items() if key != "message"},
                    {
                        "kind": "count_mismatch",
                        "url": url,
                        "relation": relation,
                        "page_number": 0,
                        "expected_count": 1,
                        "actual_count": 0,
                        "total_elements": 1,
                    },
                )
                expected_urls = [BUNDLES_URL] if relation == "bundles" else [
                    BUNDLES_URL, BITSTREAMS_URL,
                ]
                self.assertEqual([call.args[0] for call in get_json.call_args_list], expected_urls)

    def test_partial_collection_preserves_visible_original_files(self) -> None:
        """Расхождение счётчика наборов или файлов не теряет видимый исходный файл."""

        for relation, url in (("bundles", BUNDLES_URL), ("bitstreams", BITSTREAMS_URL)):
            with self.subTest(relation=relation):
                responses = _responses(details=True)
                responses[url]["page"].update({"size": 2, "totalElements": 2})
                get_json = MagicMock(side_effect=responses.__getitem__)

                result = collect_item_files(_item(), get_json)

                self.assertFalse(result["listing_complete"])
                self.assertEqual(len(result["files"]), 1)
                self.assertEqual(result["files"][0]["bitstream_uuid"], BITSTREAM_UUID)
                self.assertEqual(result["files"][0]["access_status"], "open.access")
                self.assertEqual(len(result["diagnostics"]), 1)
                diagnostic = result["diagnostics"][0]
                self.assertEqual(diagnostic["url"], url)
                self.assertEqual(diagnostic["expected_count"], 2)
                self.assertEqual(diagnostic["actual_count"], 1)
                self.assertEqual([call.args[0] for call in get_json.call_args_list], [
                    BUNDLES_URL, BITSTREAMS_URL,
                    f"{BITSTREAM_URL}/format", f"{BITSTREAM_URL}/accessStatus",
                ])

    def test_empty_first_page_does_not_hide_later_resources(self) -> None:
        """Пустая первая страница не мешает прочитать видимый ресурс на следующей."""

        for relation, url, resource in (
            ("bundles", BUNDLES_URL, _bundle()),
            ("bitstreams", BITSTREAMS_URL, _bitstream()),
        ):
            with self.subTest(relation=relation):
                responses = _responses()
                next_url = f"{url}?page=1&size=1"
                responses[url] = _page(relation, [], total_elements=2, next_url=next_url)
                responses[next_url] = _page(relation, [resource], number=1, total_elements=2)
                get_json = MagicMock(side_effect=responses.__getitem__)

                result = collect_item_files(_item(), get_json)

                self.assertEqual(len(result["files"]), 1)
                self.assertEqual(result["files"][0]["bitstream_uuid"], BITSTREAM_UUID)
                self.assertFalse(result["listing_complete"])
                self.assertEqual(len(result["diagnostics"]), 1)
                self.assertEqual(result["diagnostics"][0]["url"], url)
                expected_urls = [BUNDLES_URL, next_url, BITSTREAMS_URL] if relation == "bundles" else [
                    BUNDLES_URL, BITSTREAMS_URL, next_url,
                ]
                self.assertEqual([call.args[0] for call in get_json.call_args_list], expected_urls)

    def test_multiple_partial_collections_accumulate_diagnostics_and_files(self) -> None:
        """Замечания по всем страницам и наборам сохраняются вместе с видимыми файлами."""

        next_bundles = f"{BUNDLES_URL}?page=1&size=2"
        other_bitstreams = f"{DEFAULT_API_URL}/core/bundles/{OTHER_BUNDLE_UUID}/bitstreams"
        next_bitstreams = f"{other_bitstreams}?page=1&size=1"
        responses = _responses()
        responses[BUNDLES_URL] = _page("bundles", [_bundle()], total_elements=4, next_url=next_bundles)
        responses[BUNDLES_URL]["page"].update({"size": 2, "totalPages": 2})
        responses[next_bundles] = _page("bundles", [_bundle(OTHER_BUNDLE_UUID)], number=1, total_elements=4)
        responses[next_bundles]["page"].update({"size": 2, "totalPages": 2})
        responses[BITSTREAMS_URL]["page"].update({"size": 2, "totalElements": 2})
        responses[other_bitstreams] = _page(
            "bitstreams", [], total_elements=2, next_url=next_bitstreams,
        )
        responses[next_bitstreams] = _page(
            "bitstreams", [_bitstream(OTHER_BITSTREAM_UUID)], number=1, total_elements=2,
        )
        get_json = MagicMock(side_effect=responses.__getitem__)

        result = collect_item_files(_item(), get_json)

        self.assertFalse(result["listing_complete"])
        self.assertEqual(result["original_bundle_count"], 2)
        self.assertEqual([record["bitstream_uuid"] for record in result["files"]], [
            BITSTREAM_UUID, OTHER_BITSTREAM_UUID,
        ])
        self.assertEqual([diagnostic["url"] for diagnostic in result["diagnostics"]], [
            BUNDLES_URL, next_bundles, BITSTREAMS_URL, other_bitstreams,
        ])
        self.assertEqual([diagnostic["page_number"] for diagnostic in result["diagnostics"]], [0, 1, 0, 0])
        self.assertEqual([call.args[0] for call in get_json.call_args_list], result["source_urls"])

    def test_shortage_on_later_page_preserves_page_context(self) -> None:
        """Успешная первая страница не превращает неполную вторую в готовый перечень."""

        next_url = f"{BITSTREAMS_URL}?page=1&size=1"
        responses = _responses(details=True)
        responses[BITSTREAMS_URL] = _page(
            "bitstreams", [_bitstream(details=True)], total_elements=2, next_url=next_url,
        )
        responses[next_url] = _page("bitstreams", [], number=1, total_elements=2)
        get_json = MagicMock(side_effect=responses.__getitem__)

        result = collect_item_files(_item(), get_json)

        self.assertFalse(result["listing_complete"])
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["bitstream_uuid"], BITSTREAM_UUID)
        self.assertEqual(len(result["diagnostics"]), 1)
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["url"], next_url)
        self.assertEqual(diagnostic["page_number"], 1)
        self.assertEqual(diagnostic["total_elements"], 2)
        self.assertEqual([call.args[0] for call in get_json.call_args_list], [
            BUNDLES_URL, BITSTREAMS_URL, next_url,
            f"{BITSTREAM_URL}/format", f"{BITSTREAM_URL}/accessStatus",
        ])

    def test_malformed_short_collection_remains_hard_failure(self) -> None:
        """Дефицит не скрывает неверную структуру, UUID, тип или повторы ресурсов."""

        for relation, url, resource in (
            ("bundles", BUNDLES_URL, _bundle()),
            ("bitstreams", BITSTREAMS_URL, _bitstream()),
        ):
            for case in ("embedded", "missing", "list", "entry", "uuid", "type", "duplicate"):
                with self.subTest(relation=relation, case=case):
                    responses = _responses()
                    response = _page(relation, [copy.deepcopy(resource)])
                    response["page"].update({"size": 3, "totalElements": 3})

                    if case == "embedded":
                        response["_embedded"] = []

                    elif case == "missing":
                        response["_embedded"] = {}

                    elif case == "list":
                        response["_embedded"][relation] = {}

                    elif case == "entry":
                        response["_embedded"][relation] = [None]

                    elif case == "duplicate":
                        response["_embedded"][relation].append(copy.deepcopy(resource))

                    else:
                        response["_embedded"][relation][0][case] = "invalid"

                    responses[url] = response
                    get_json = MagicMock(side_effect=responses.__getitem__)

                    with self.assertRaises(ValueError):
                        collect_item_files(_item(), get_json)

    def test_unsafe_links_remain_hard_failure_during_shortage(self) -> None:
        """Небезопасные self и next проверяются прежде регистрации дефицита."""

        for relation, url in (("bundles", BUNDLES_URL), ("bitstreams", BITSTREAMS_URL)):
            for link_name in ("self", "next", "resource_self"):
                with self.subTest(relation=relation, link_name=link_name):
                    responses = _responses()
                    response = responses[url]
                    response["page"].update({"size": 2, "totalElements": 2})
                    unsafe_url = "https://example.org/forbidden"

                    if link_name == "resource_self":
                        response["_embedded"][relation][0]["_links"]["self"] = {
                            "href": unsafe_url,
                        }

                    else:
                        response["_links"][link_name] = {"href": unsafe_url}

                    get_json = MagicMock(side_effect=responses.__getitem__)

                    with self.assertRaises(ValueError):
                        collect_item_files(_item(), get_json)
                    self.assertNotIn(unsafe_url, [call.args[0] for call in get_json.call_args_list])

    def test_invalid_next_links_remain_hard_failure_during_shortage(self) -> None:
        """Расхождение счётчика не позволяет пропустить, повторить или забыть страницу."""

        for relation, url in (("bundles", BUNDLES_URL), ("bitstreams", BITSTREAMS_URL)):
            for next_url in (None, f"{url}?page=0&size=1", f"{url}?page=2&size=1"):
                with self.subTest(relation=relation, next_url=next_url):
                    responses = _responses()
                    responses[url] = _page(relation, [], total_elements=2, next_url=next_url)
                    get_json = MagicMock(side_effect=responses.__getitem__)

                    with self.assertRaises(ValueError):
                        collect_item_files(_item(), get_json)

                    expected_urls = [BUNDLES_URL] if relation == "bundles" else [
                        BUNDLES_URL, BITSTREAMS_URL,
                    ]
                    self.assertEqual([call.args[0] for call in get_json.call_args_list], expected_urls)

    def test_surplus_resources_remain_hard_failure(self) -> None:
        """Лишние элементы не являются случаем неполного ответа сервера."""

        for relation, url, extra_resource in (
            ("bundles", BUNDLES_URL, _bundle(OTHER_BUNDLE_UUID)),
            ("bitstreams", BITSTREAMS_URL, _bitstream(OTHER_BITSTREAM_UUID)),
        ):
            with self.subTest(relation=relation):
                responses = _responses()
                responses[url]["_embedded"][relation].append(extra_resource)
                get_json = MagicMock(side_effect=responses.__getitem__)

                with self.assertRaises(ValueError):
                    collect_item_files(_item(), get_json)


if __name__ == "__main__":
    unittest.main()
