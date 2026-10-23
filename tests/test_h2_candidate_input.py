"""Проверки происхождения и полноты офлайн-снимка кандидатов H2."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest

from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from src.collect.base import HttpResponseSnapshot
from src.corpus.h2_candidate_input import read_candidate_snapshot
from src.corpus.manifests import sha256_bytes

FIRST_UUID = "817022d5-88ef-4bf3-b6ea-89a03f0b57bb"
SECOND_UUID = "c1de2fec-3e07-4e15-903c-370df5e5a9c8"
SEARCH_URL = "https://pubrepo-api.jinr.ru/server/api/discover/search/objects"


class CandidateInputTests(unittest.TestCase):
    """Изолированные свидетельства должны однозначно задавать все карточки снимка."""

    def setUp(self) -> None:
        """Подготовить две страницы с каноническими HTTP-свидетельствами без сети."""

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "manifests/results/analysis.json"
        self.path.parent.mkdir(parents=True)
        self.items = [self.item(FIRST_UUID), self.item(SECOND_UUID)]
        self.payloads = [self.payload([item], page_number) for page_number, item in enumerate(self.items)]
        self.entries = [self.save_page(payload) for payload in self.payloads]
        self.description: dict[str, Any] = {
            "schema_version": "jinr-metadata-analysis-v1",
            "snapshot": {"pages": 2, "items": 2, "unique_uuids": 2, "page_totals": [2]},
            "source_pages": self.entries,
        }
        self.save_description()

    def item(self, item_uuid: str) -> dict[str, Any]:
        """Создать карточку с типичными полями DSpace, не выдавая её за учебную метку."""

        return {
            "type": "item", "uuid": item_uuid, "id": item_uuid,
            "metadata": {
                "dc.title": [{"value": "Физика частиц", "language": "ru", "place": 0}],
                "dc.description.abstract": [{"value": "Изучены свойства частиц."}],
                "dc.type": [{"value": "Article"}],
            },
        }

    def payload(self, items: list[dict[str, Any]], page_number: int) -> dict[str, Any]:
        """Собрать структуру поискового ответа с подтверждённым фильтром языка."""

        return {
            "query": None, "scope": None, "configuration": None,
            "appliedFilters": [{"filter": "Lang", "operator": "equals", "value": "ru", "label": "ru"}],
            "_embedded": {"searchResult": {
                "page": {"number": page_number, "size": 1, "totalPages": 2, "totalElements": 2},
                "_embedded": {"objects": [{"_embedded": {"indexableObject": item}} for item in items]},
            }},
        }

    def save_page(
        self,
        payload: dict[str, Any],
        *,
        url: str | None = None,
        status: int = 200,
        final_url: str | None = None,
    ) -> dict[str, Any]:
        """Сохранить синтетический ответ и вывести запись его SHA-256 в опись."""

        embedded = payload["_embedded"]
        result = embedded.get("searchResult", embedded.get("searchResults"))
        page = result["page"]
        requested_url = url or f"{SEARCH_URL}?{urlencode({
            'dsoType': 'item', 'f.Lang': 'ru,equals', 'page': page['number'], 'size': page['size'],
        })}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        digest = sha256_bytes(body)
        directory = self.root / "data/raw/jinr_api/responses" / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.json"
        path.write_bytes(body)
        snapshot = HttpResponseSnapshot(
            requested_url=requested_url,
            final_url=final_url or requested_url,
            status_code=status,
            headers=(("content-length", str(len(body))), ("content-type", "application/hal+json")),
            retrieved_at="2026-09-21T10:35:49.787749+00:00",
            body=body,
        )
        http_bytes = snapshot.canonical_metadata()
        http_digest = sha256_bytes(http_bytes)
        path.with_name(f"{digest}.{http_digest}.http.json").write_bytes(http_bytes)

        return {
            "page": page["number"], "path": path.relative_to(self.root).as_posix(),
            "sha256": digest, "http_evidence_sha256": http_digest,
            "items": len(result["_embedded"]["objects"]),
        }

    def save_description(self) -> None:
        """Сохранить только тестовую опись после намеренного изменения входных данных."""

        self.path.write_text(json.dumps(self.description, ensure_ascii=False), encoding="utf-8")

    def replace_page(self, page_number: int, **kwargs: Any) -> None:
        """Обновить хеши изменённой страницы, оставив возможность проверять её содержание."""

        self.entries[page_number] = self.save_page(self.payloads[page_number], **kwargs)
        self.save_description()

    def test_reads_all_items_and_hash_bound_sources(self) -> None:
        """Карточки, точные индексы и оба хеша свидетельств сохраняются без регистрации."""

        result = read_candidate_snapshot(self.root, self.path)

        self.assertEqual(result.items, tuple(self.items))
        self.assertEqual(result.provenance["total_items"], 2)
        self.assertEqual(result.provenance["total_pages"], 2)
        self.assertEqual(result.provenance["snapshot_sha256"], sha256_bytes(self.path.read_bytes()))
        self.assertEqual(result.sources[SECOND_UUID]["page_number"], 1)
        self.assertEqual(result.sources[FIRST_UUID]["item_index"], 0)
        self.assertEqual(result.sources[FIRST_UUID]["page_sha256"], self.entries[0]["sha256"])
        self.assertEqual(result.sources[FIRST_UUID]["http_evidence_sha256"], self.entries[0]["http_evidence_sha256"])
        self.assertEqual(read_candidate_snapshot(self.root, self.path), result)

    def test_does_not_select_article_or_require_nonempty_metadata(self) -> None:
        """Reader сохраняет прочие жанры, пустые строки и пустые списки для следующего шага."""

        self.items[0]["metadata"]["dc.type"] = [{"value": "Book chapter"}]
        self.items[0]["metadata"]["dc.title"] = [{"value": "   "}]
        self.items[1]["metadata"] = {}
        self.replace_page(0)
        self.replace_page(1)

        result = read_candidate_snapshot(self.root, self.path)

        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.items[0]["metadata"]["dc.title"][0]["value"], "   ")

    def test_supports_plural_search_results(self) -> None:
        """Вариант searchResults допустим, если единственен."""

        for page_number, payload in enumerate(self.payloads):
            payload["_embedded"]["searchResults"] = payload["_embedded"].pop("searchResult")
            self.replace_page(page_number)

        self.assertEqual(len(read_candidate_snapshot(self.root, self.path).items), 2)

    def test_rejects_ambiguous_result_keys(self) -> None:
        """Два объекта результатов нельзя молча свести к одному."""

        self.payloads[0]["_embedded"]["searchResults"] = copy.deepcopy(self.payloads[0]["_embedded"]["searchResult"])
        self.replace_page(0)

        with self.assertRaisesRegex(ValueError, "ровно один"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_changed_page_bytes(self) -> None:
        """Даже корректный JSON не допустим после изменения байтов вне исходной описи."""

        page_path = self.root / self.entries[0]["path"]
        page_path.write_bytes(page_path.read_bytes() + b" ")

        with self.assertRaisesRegex(ValueError, "SHA-256 тела"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_changed_http_bytes(self) -> None:
        """Подмена HTTP-свидетельства выявляется независимо от сохранности тела."""

        entry = self.entries[0]
        path = (self.root / entry["path"]).with_name(f"{entry['sha256']}.{entry['http_evidence_sha256']}.http.json")
        path.write_bytes(path.read_bytes() + b" ")

        with self.assertRaisesRegex(ValueError, "SHA-256 HTTP"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_noncanonical_http_even_with_updated_hash(self) -> None:
        """Пересчёт хеша не делает произвольное HTTP-свидетельство каноническим."""

        entry = self.entries[0]
        path = (self.root / entry["path"]).with_name(f"{entry['sha256']}.{entry['http_evidence_sha256']}.http.json")
        changed = path.read_bytes() + b"\n"
        digest = sha256_bytes(changed)
        path.with_name(f"{entry['sha256']}.{digest}.http.json").write_bytes(changed)
        entry["http_evidence_sha256"] = digest
        self.save_description()

        with self.assertRaisesRegex(ValueError, "каноническому"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_http_error(self) -> None:
        """Сохранённая ошибка HTTP не является источником карточек."""

        self.replace_page(0, status=403)

        with self.assertRaisesRegex(ValueError, "HTTP 200"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_foreign_origin_and_redirect(self) -> None:
        """Сетевое происхождение проверяется даже при правильных хешах."""

        for kwargs in ({"url": "https://example.org/api"}, {"final_url": "https://example.org/api"}):
            with self.subTest(kwargs=kwargs):
                self.replace_page(0, **kwargs)

                with self.assertRaises(ValueError):
                    read_candidate_snapshot(self.root, self.path)

    def test_rejects_additional_or_missing_query_filters(self) -> None:
        """Частичный поиск нельзя выдать за полный массив русскоязычных метаданных."""

        valid_url = f"{SEARCH_URL}?dsoType=item&f.Lang=ru,equals&page=0&size=1"

        for url in (
            valid_url + "&f.has_content_in_original_bundle=true,equals",
            valid_url + "&query=physics",
            valid_url + "&scope=collection",
            valid_url + "&page=0",
            valid_url.replace("ru,equals", "en,equals"),
            valid_url.replace("&f.Lang=ru,equals", ""),
            valid_url.replace("page=0", "page=1"),
            valid_url.replace("size=1", "size=100"),
        ):
            with self.subTest(url=url):
                self.replace_page(0, url=url)

                with self.assertRaisesRegex(ValueError, "полному"):
                    read_candidate_snapshot(self.root, self.path)

    def test_accepts_first_page_url_without_zero(self) -> None:
        """Отсутствующий page=0 в ссылке DSpace означает первую страницу."""

        self.replace_page(0, url=f"{SEARCH_URL}?dsoType=item&f.Lang=ru,equals&size=1")

        self.assertEqual(len(read_candidate_snapshot(self.root, self.path).items), 2)

    def test_rejects_unconfirmed_filter(self) -> None:
        """Правильный URL недостаточен без подтверждения фильтра в ответе API."""

        for filters in ([], [{"filter": "Lang", "operator": "equals", "value": "en"}]):
            with self.subTest(filters=filters):
                self.payloads[0]["appliedFilters"] = filters
                self.replace_page(0)

                with self.assertRaisesRegex(ValueError, "фильтр"):
                    read_candidate_snapshot(self.root, self.path)

    def test_rejects_missing_and_repeated_page(self) -> None:
        """Пропуски и повторы страниц выявляются до построения очереди."""

        for entries in ([self.entries[0]], [self.entries[1]], [self.entries[0], self.entries[0]]):
            with self.subTest(entries=entries):
                self.description["source_pages"] = entries
                self.save_description()

                with self.assertRaisesRegex(ValueError, "страниц"):
                    read_candidate_snapshot(self.root, self.path)

    def test_sorts_valid_page_entries(self) -> None:
        """Порядок хранения описи не меняет порядок карточек API."""

        self.description["source_pages"] = list(reversed(self.entries))
        self.save_description()

        self.assertEqual(read_candidate_snapshot(self.root, self.path).items, tuple(self.items))

    def test_rejects_incomplete_page(self) -> None:
        """Пустая страница с ненулевым ожидаемым числом карточек недопустима."""

        self.payloads[0]["_embedded"]["searchResult"]["_embedded"]["objects"] = []
        self.replace_page(0)

        with self.assertRaisesRegex(ValueError, "Количество карточек"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_boolean_pagination(self) -> None:
        """Булевы значения нельзя трактовать как числа страниц или карточек."""

        original = copy.deepcopy(self.payloads[0])

        for field in ("number", "size", "totalPages", "totalElements"):
            with self.subTest(field=field):
                self.payloads[0] = copy.deepcopy(original)
                self.payloads[0]["_embedded"]["searchResult"]["page"][field] = True
                self.replace_page(0)

                with self.assertRaisesRegex(ValueError, "целое"):
                    read_candidate_snapshot(self.root, self.path)

    def test_rejects_inconsistent_page_totals(self) -> None:
        """Даже внутренне согласованная страница должна иметь общие итоги снимка."""

        self.payloads[1]["_embedded"]["searchResult"]["page"].update({"totalPages": 3, "totalElements": 3})
        self.replace_page(1)

        with self.assertRaisesRegex(ValueError, "различаются"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_duplicate_uuid(self) -> None:
        """Одинаковая карточка на двух страницах не считается двумя публикациями."""

        self.items[1]["uuid"] = FIRST_UUID
        self.items[1]["id"] = FIRST_UUID
        self.replace_page(1)

        with self.assertRaisesRegex(ValueError, "повторяет UUID"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_malformed_metadata(self) -> None:
        """Скалярные и нестроковые значения метаданных не преобразуются молча в строки."""

        for metadata in (None, [], {"dc.title": "Название"}, {"dc.title": [{"value": 12}]}, {"dc.title": [{}]}):
            with self.subTest(metadata=metadata):
                self.items[0]["metadata"] = metadata
                self.replace_page(0)

                with self.assertRaisesRegex(ValueError, "metadata"):
                    read_candidate_snapshot(self.root, self.path)

    def test_rejects_invalid_uuid(self) -> None:
        """UUID карточки должен иметь однозначную каноническую запись."""

        self.items[0]["uuid"] = FIRST_UUID.upper()
        self.replace_page(0)

        with self.assertRaisesRegex(ValueError, "UUID"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_source_path_escape(self) -> None:
        """Исходный ответ нельзя читать за пределами data даже внутри проекта."""

        destination = self.root / "outside-data.json"
        destination.write_bytes((self.root / self.entries[0]["path"]).read_bytes())
        self.entries[0]["path"] = str(destination)
        self.save_description()

        with self.assertRaisesRegex(ValueError, "разрешённый каталог"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_missing_source_path(self) -> None:
        """Отсутствующее поле пути должно давать объяснимую ошибку входных данных."""

        self.entries[0].pop("path")
        self.save_description()

        with self.assertRaisesRegex(ValueError, "Путь свидетельства"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_missing_page_file(self) -> None:
        """Опись не заменяет фактическое наличие каждого тела ответа."""

        (self.root / self.entries[0]["path"]).unlink()

        with self.assertRaisesRegex(ValueError, "отсутствует"):
            read_candidate_snapshot(self.root, self.path)

    def test_rejects_symlink_escape(self) -> None:
        """Символьная ссылка не позволяет прочитать внешнее свидетельство."""

        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "page.json"
            external.write_bytes((self.root / self.entries[0]["path"]).read_bytes())
            link = self.root / "data/raw/linked.json"
            link.symlink_to(external)
            self.entries[0]["path"] = link.relative_to(self.root).as_posix()
            self.save_description()

            with self.assertRaisesRegex(ValueError, "разрешённый каталог"):
                read_candidate_snapshot(self.root, self.path)

    def test_rejects_snapshot_outside_project(self) -> None:
        """Даже сама опись должна оставаться внутри проверяемого проекта."""

        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "analysis.json"
            external.write_bytes(self.path.read_bytes())

            with self.assertRaisesRegex(ValueError, "разрешённый каталог"):
                read_candidate_snapshot(self.root, external)

    def test_rejects_incorrect_declared_snapshot_count(self) -> None:
        """Сводка анализа не должна противоречить проверенному числу карточек."""

        self.description["snapshot"]["unique_uuids"] = 3
        self.save_description()

        with self.assertRaisesRegex(ValueError, "unique_uuids"):
            read_candidate_snapshot(self.root, self.path)


if __name__ == "__main__":
    unittest.main()
