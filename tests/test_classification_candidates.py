"""Проверки офлайн-пакета исходных классификационных кодов без изменения реестров."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import tempfile
import unittest

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import extract_classification_codes
from src.corpus.classification_candidates import (
    build_classification_candidates,
    export_classification_candidates,
)
from src.corpus.h2_candidate_input import CandidateSnapshot

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRST_UUID = "817022d5-88ef-4bf3-b6ea-89a03f0b57bb"
SECOND_UUID = "c1de2fec-3e07-4e15-903c-370df5e5a9c8"
SOURCE_ID = "F04_JINR_ARTICLES_RU"
RETRIEVED_AT = "2026-10-10T12:00:00+03:00"


def _sha256(body: bytes) -> str:
    """Вычислить контрольную сумму независимым от проверяемого модуля способом."""

    return hashlib.sha256(body).hexdigest()


class ClassificationCandidatesTests(unittest.TestCase):
    """Свидетельства и точная связь карточек не превращаются в готовые метки H2."""

    def setUp(self) -> None:
        """Создать минимальный проект с настоящими схемами и синтетическим текстом."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.manifests = self.root / "manifests"
        self.manifests.mkdir()
        shutil.copytree(PROJECT_ROOT / "manifests/schemas", self.manifests / "schemas")

        self.work = json.loads(
            (PROJECT_ROOT / "manifests/templates/works.example.jsonl").read_text(encoding="utf-8"),
        )
        self.work.update({
            "work_id": "work:synthetic:1",
            "work_aliases": [f"source_native_id:{SOURCE_ID}:{FIRST_UUID}"],
            "source_id": SOURCE_ID,
            "source_group_id": "F04_JINR",
            "pacs_codes_raw": [],
            "udc_codes_raw": [],
            "grnti_codes_raw": [],
            "mrnti_codes_raw": [],
        })
        self.artifact = json.loads(
            (PROJECT_ROOT / "manifests/templates/artifacts.example.jsonl").read_text(encoding="utf-8"),
        )
        self.artifact.update({
            "artifact_record_id": "artifact:synthetic:1",
            "work_id": self.work["work_id"],
            "content_role": "full_text",
            "representation": "plain_text",
            "path": "data/extracted/synthetic/article.txt",
            "processing_status": "processed",
            "qa_status": "not_evaluated",
            "h2_input_sha256": None,
            "label_leakage_audit_status": "not_checked",
            "label_leakage_audit_version": None,
            "retrievals": [{
                "retrieval_id": "retrieval:synthetic:1",
                "retrieved_url": "https://example.invalid/paper.pdf",
                "retrieved_at": RETRIEVED_AT,
                "response_metadata_sha256": "f" * 64,
            }],
        })
        self.text_path = self.root / self.artifact["path"]
        self.text_path.parent.mkdir(parents=True)
        self.works = [self.work]
        self.artifacts = [self.artifact]
        self._save_text("Название статьи\nУДК 539.123\nPACS: 14.60.Pq\nОсновной текст.\n")
        self._save_manifests()
        self.output = self.manifests / "results/classification_test_v1"
        self.snapshot_path = self.manifests / "results/snapshot.json"

    def _save_text(self, text: str) -> None:
        """Связать точные байты тестового текста с записью артефакта."""

        body = text.encode("utf-8")
        self.text_path.write_bytes(body)
        digest = _sha256(body)
        self.artifact.update({
            "sha256": digest,
            "artifact_id": f"sha256:{digest}",
            "bytes": len(body),
            "characters": len(text),
        })

    def _save_manifests(self) -> None:
        """Перезаписать только временные реестры после изменения условий теста."""

        for name, rows in (("works", self.works), ("artifacts", self.artifacts)):
            content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
            (self.manifests / f"{name}.jsonl").write_text(content, encoding="utf-8")

    def _tree_bytes(self) -> dict[str, bytes]:
        """Снять содержимое обычных файлов, не переходя по символическим ссылкам."""

        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }

    def _snapshot(self, item_uuid: str = FIRST_UUID) -> CandidateSnapshot:
        """Создать проверенный снимок на границе отдельного читателя метаданных."""

        item = {
            "uuid": item_uuid,
            "metadata": {
                "dc.title": [{"value": self.work["title"]}],
                "dc.identifier.doi": [{"value": "10.1234/synthetic"}],
                "local.udc": [{"value": "539.123"}],
                "local.Rubric": [{"value": "29.15.19"}],
                "dc.subject": [{"value": "PACS 14.60.Pq"}],
            },
        }
        page_path = self.root / "data/raw/jinr_api/synthetic.json"
        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_bytes = json.dumps(item, ensure_ascii=False).encode("utf-8")
        page_path.write_bytes(page_bytes)
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.write_text("{}\n", encoding="utf-8")
        source = {
            "snapshot_path": self.snapshot_path.relative_to(self.root).as_posix(),
            "snapshot_sha256": _sha256(self.snapshot_path.read_bytes()),
            "page_path": page_path.relative_to(self.root).as_posix(),
            "page_sha256": _sha256(page_bytes),
            "requested_url": "https://pubrepo-api.jinr.ru/server/api/discover/search/objects",
            "retrieved_at": RETRIEVED_AT,
            "page_number": 0,
            "item_index": 0,
            "http_evidence_sha256": "a" * 64,
        }

        return CandidateSnapshot(
            items=(item,),
            sources={item_uuid: source},
            provenance={
                "snapshot_path": source["snapshot_path"],
                "snapshot_sha256": source["snapshot_sha256"],
                "total_items": 1,
                "total_pages": 1,
                "source_pages": [{"path": source["page_path"], "sha256": source["page_sha256"]}],
            },
        )

    def _build_with_snapshot(
        self, snapshot: CandidateSnapshot,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Подменить только уже отдельно проверяемое чтение полного снимка API."""

        with patch(
            "src.corpus.classification_candidates.read_candidate_snapshot", return_value=snapshot,
        ):
            return build_classification_candidates(self.root, snapshot_path=self.snapshot_path)

    def test_build_is_offline_and_does_not_change_input_files(self) -> None:
        """Поиск кандидатов не обращается в сеть и не записывает поля рабочих реестров."""

        before = self._tree_bytes()

        with patch("socket.socket", side_effect=AssertionError("Сеть запрещена в тесте")):
            rows, summary = build_classification_candidates(self.root)

        self.assertEqual(self._tree_bytes(), before)
        self.assertEqual(len(rows), 2)
        self.assertEqual(summary["work_count"], 1)
        self.assertEqual(summary["text_artifacts_scanned"], 1)
        self.assertEqual(summary["candidate_count"], len(rows))
        self.assertEqual(summary["works_with_candidates"], 1)

        for row in rows:
            self.assertEqual(row["work_id"], self.work["work_id"])
            self.assertEqual(row["review_status"], "pending")
            self.assertNotIn("label", row)
            self.assertNotIn("split", row)

    def test_text_candidates_keep_exact_origin_and_line_location(self) -> None:
        """К каждому предложенному коду приложены файл, его хеш и исходная строка."""

        rows, _ = build_classification_candidates(self.root)
        row = next(candidate for candidate in rows if candidate["field"] == "udc_codes_raw")

        self.assertEqual(row["codes"], ["539.123"])
        self.assertEqual(row["line_start"], 2)
        self.assertIn("УДК 539.123", row["raw_excerpt"])
        self.assertEqual(row["source"]["kind"], "registered_text")
        self.assertEqual(row["source"]["path"], self.artifact["path"])
        self.assertEqual(row["source"]["sha256"], self.artifact["sha256"])
        self.assertEqual(row["proposed_signals"][0]["code"], "539.123")
        self.assertEqual(row["proposed_signals"][0]["evidence_sha256"], self.artifact["sha256"])

    def test_candidate_identifiers_and_summary_are_reproducible(self) -> None:
        """Повторное чтение неизменных файлов не генерирует новые идентификаторы."""

        first = build_classification_candidates(self.root)
        second = build_classification_candidates(self.root)

        self.assertEqual(first, second)
        identifiers = [row["candidate_id"] for row in first[0]]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_grnti_and_mrnti_are_not_combined(self) -> None:
        """Одинаковые числовые значения разных рубрикаторов остаются разными сигналами."""

        self._save_text("ГРНТИ 29.15.19\nМРНТИ 29.15.19\n")
        self._save_manifests()
        rows, _ = build_classification_candidates(self.root)

        self.assertEqual({row["field"] for row in rows}, {"grnti_codes_raw", "mrnti_codes_raw"})
        self.assertEqual([row["codes"] for row in rows], [["29.15.19"], ["29.15.19"]])

    def test_only_successful_full_text_representations_are_scanned(self) -> None:
        """Аннотации, PDF и незавершённое извлечение не подменяют полный текст статьи."""

        for field, value in (
            ("content_role", "title_abstract"),
            ("representation", "pdf"),
            ("extraction_status", "not_started"),
        ):
            with self.subTest(field=field, value=value):
                original = self.artifact[field]
                self.artifact[field] = value
                self._save_manifests()
                rows, summary = build_classification_candidates(self.root)
                self.assertEqual(rows, [])
                self.assertEqual(summary["text_artifacts_scanned"], 0)
                self.artifact[field] = original

    def test_ocr_text_is_supported(self) -> None:
        """Зарегистрированный результат OCR обрабатывается наравне с текстовым слоем."""

        self.artifact["representation"] = "ocr_text"
        self._save_manifests()
        rows, summary = build_classification_candidates(self.root)

        self.assertEqual(len(rows), 2)
        self.assertEqual(summary["text_artifacts_scanned"], 1)

    def test_changed_text_bytes_fail_before_any_writes(self) -> None:
        """Несовпадающий SHA-256 выявляется до создания пакета кандидатов."""

        self.text_path.write_text("Подменённый текст\nУДК 53\n", encoding="utf-8")
        before = self._tree_bytes()

        with self.assertRaises(ValueError):
            build_classification_candidates(self.root)

        self.assertEqual(self._tree_bytes(), before)
        self.assertFalse(self.output.exists())

    def test_declared_byte_count_is_checked_separately(self) -> None:
        """Правильная контрольная сумма не скрывает неверный размер артефакта."""

        self.artifact["bytes"] += 1
        self._save_manifests()

        with self.assertRaises(ValueError):
            build_classification_candidates(self.root)

    def test_non_utf8_text_is_rejected(self) -> None:
        """Некорректная кодировка не заменяется незаметно на потерю символов."""

        body = b"UDC 539.123\n\xff"
        self.text_path.write_bytes(body)
        self.artifact.update({
            "sha256": _sha256(body), "artifact_id": f"sha256:{_sha256(body)}", "bytes": len(body),
        })
        self._save_manifests()

        with self.assertRaises(ValueError):
            build_classification_candidates(self.root)

    def test_unknown_work_id_is_rejected(self) -> None:
        """Нельзя предложить код статье, отсутствующей в реестре научных работ."""

        self.artifact["work_id"] = "work:unknown"
        self._save_manifests()

        with self.assertRaises(ValueError):
            build_classification_candidates(self.root)

    def test_schema_invalid_work_is_rejected(self) -> None:
        """Чтение карточек использует действующую схему, а не только нужные поля."""

        self.work["eligibility_status"] = "unknown"
        self._save_manifests()

        with self.assertRaises(ValueError):
            build_classification_candidates(self.root)

    def test_text_path_outside_data_is_rejected(self) -> None:
        """Существующий файл вне data не принимается как источник полного текста."""

        outside = self.root / "private.txt"
        outside.write_bytes(self.text_path.read_bytes())
        self.artifact["path"] = outside.relative_to(self.root).as_posix()
        self._save_manifests()
        before = self._tree_bytes()

        with self.assertRaises(ValueError):
            build_classification_candidates(self.root)

        self.assertEqual(self._tree_bytes(), before)

    def test_text_symlink_inside_data_is_rejected(self) -> None:
        """Даже ссылка на внутренний файл не подменяет явно зарегистрированный путь."""

        link = self.text_path.with_name("linked.txt")
        link.symlink_to(self.text_path)
        self.artifact["path"] = link.relative_to(self.root).as_posix()
        self._save_manifests()

        with self.assertRaises(ValueError):
            build_classification_candidates(self.root)

    def test_exact_uuid_alias_adds_metadata_with_separate_provenance(self) -> None:
        """Одинаковый код из PDF и API остаётся двумя независимо проверяемыми находками."""

        snapshot = self._snapshot()
        before = self._tree_bytes()
        rows, summary = self._build_with_snapshot(snapshot)
        metadata_rows = [row for row in rows if row["source"]["kind"] == "jinr_metadata"]

        self.assertEqual(self._tree_bytes(), before)
        self.assertEqual(summary["metadata_items_matched"], 1)
        self.assertEqual(len(metadata_rows), 1)
        row = metadata_rows[0]
        source = snapshot.sources[FIRST_UUID]
        self.assertEqual(row["field"], "udc_codes_raw")
        self.assertEqual(row["codes"], ["539.123"])
        self.assertEqual(row["source"]["sha256"], source["page_sha256"])
        self.assertEqual(row["source"]["path"], source["page_path"])
        self.assertEqual(row["proposed_signals"][0]["evidence_sha256"], source["page_sha256"])
        self.assertEqual(len([row for row in rows if row["field"] == "udc_codes_raw"]), 2)

    def test_matching_title_or_doi_does_not_replace_uuid_alias(self) -> None:
        """Похожее название и тот же DOI не разрешают связать разные карточки API."""

        self.work["doi"] = "10.1234/synthetic"
        self._save_manifests()
        rows, summary = self._build_with_snapshot(self._snapshot(SECOND_UUID))

        self.assertEqual(summary["metadata_items_matched"], 0)
        self.assertFalse(any(row["source"]["kind"] == "jinr_metadata" for row in rows))

    def test_wrong_source_alias_does_not_match_uuid(self) -> None:
        """UUID постороннего источника нельзя считать идентификатором ОИЯИ."""

        self.work["work_aliases"] = [f"source_native_id:OTHER_SOURCE:{FIRST_UUID}"]
        self._save_manifests()
        rows, summary = self._build_with_snapshot(self._snapshot())

        self.assertEqual(summary["metadata_items_matched"], 0)
        self.assertFalse(any(row["source"]["kind"] == "jinr_metadata" for row in rows))

    def test_shared_exact_alias_warns_without_assigning_metadata(self) -> None:
        """Неоднозначный UUID двух работ не разрешается выбором первой карточки."""

        second_work = copy.deepcopy(self.work)
        second_work["work_id"] = "work:synthetic:2"
        self.works.append(second_work)
        self._save_manifests()
        rows, summary = self._build_with_snapshot(self._snapshot())

        self.assertFalse(any(row["source"]["kind"] == "jinr_metadata" for row in rows))
        self.assertTrue(summary["warnings"])

    def test_multiple_uuid_aliases_warn_without_choosing_first(self) -> None:
        """Два идентификатора источника у одной работы требуют проверки человеком."""

        self.work["work_aliases"].append(f"source_native_id:{SOURCE_ID}:{SECOND_UUID}")
        self._save_manifests()
        rows, summary = self._build_with_snapshot(self._snapshot())

        self.assertEqual(summary["metadata_items_matched"], 0)
        self.assertFalse(any(row["source"]["kind"] == "jinr_metadata" for row in rows))
        self.assertTrue(summary["warnings"])

    def test_different_source_codes_remain_visible_as_variants(self) -> None:
        """Расхождение УДК текста и API не разрешается молчаливым выбором одного."""

        snapshot = self._snapshot()
        snapshot.items[0]["metadata"]["local.udc"][0]["value"] = "538.911"
        rows, _ = self._build_with_snapshot(snapshot)
        udc_rows = [row for row in rows if row["field"] == "udc_codes_raw"]

        self.assertEqual({tuple(row["codes"]) for row in udc_rows}, {("539.123",), ("538.911",)})
        self.assertTrue(all("source_variants" in row["flags"] for row in udc_rows))

    def test_all_retrievals_are_preserved_in_proposed_signals(self) -> None:
        """Несколько подтверждённых получений не сводятся к произвольно выбранному URL."""

        other = copy.deepcopy(self.artifact["retrievals"][0])
        other["retrieval_id"] = "retrieval:synthetic:2"
        other["retrieved_url"] = "https://example.invalid/other-copy.pdf"
        self.artifact["retrievals"].append(other)
        self._save_manifests()
        rows, _ = build_classification_candidates(self.root)
        row = next(candidate for candidate in rows if candidate["field"] == "udc_codes_raw")

        self.assertEqual(
            {signal["source_url"] for signal in row["proposed_signals"]},
            {retrieval["retrieved_url"] for retrieval in self.artifact["retrievals"]},
        )

    def test_export_creates_review_files_without_changing_sources(self) -> None:
        """Новый пакет сохраняет русские строки и не затрагивает исходные данные."""

        rows, summary = build_classification_candidates(self.root)
        original_summary = copy.deepcopy(summary)
        before = self._tree_bytes()
        export_classification_candidates(self.root, self.output, rows, summary)
        exported_rows = [
            json.loads(line)
            for line in (self.output / "candidates.jsonl").read_text(encoding="utf-8").splitlines()
        ]

        self.assertEqual(exported_rows, rows)
        self.assertEqual(summary, original_summary)
        self.assertTrue((self.output / "summary.json").is_file())

        for relative, body in before.items():
            self.assertEqual((self.root / relative).read_bytes(), body)

    def test_existing_output_is_never_overwritten(self) -> None:
        """Повторный запуск не стирает пакет или ручные решения пользователя."""

        rows, summary = build_classification_candidates(self.root)
        export_classification_candidates(self.root, self.output, rows, summary)
        (self.output / "review.txt").write_text("Моя проверка", encoding="utf-8")
        before = self._tree_bytes()

        with self.assertRaises(ValueError):
            export_classification_candidates(self.root, self.output, rows, summary)

        self.assertEqual(self._tree_bytes(), before)

    def test_changed_source_after_build_prevents_export(self) -> None:
        """Изменение входа между подготовкой и записью требует нового извлечения."""

        rows, summary = build_classification_candidates(self.root)
        self.text_path.write_bytes(self.text_path.read_bytes() + b"\n")
        before = self._tree_bytes()

        with self.assertRaises(ValueError):
            export_classification_candidates(self.root, self.output, rows, summary)

        self.assertFalse(self.output.exists())
        self.assertEqual(self._tree_bytes(), before)

    def test_changed_metadata_evidence_prevents_export(self) -> None:
        """Изменение страницы API, её HTTP-свидетельства или описи блокирует запись."""

        snapshot = self._snapshot()
        page = snapshot.provenance["source_pages"][0]
        http_path = self.root / "data/raw/jinr_api/synthetic.http.json"
        http_path.write_text('{"status_code": 200}\n', encoding="utf-8")
        page["http_evidence_path"] = http_path.relative_to(self.root).as_posix()
        page["http_evidence_sha256"] = _sha256(http_path.read_bytes())
        rows, summary = self._build_with_snapshot(snapshot)

        for path in (self.root / page["path"], http_path, self.snapshot_path):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                before = self._tree_bytes()

                with self.assertRaises(ValueError):
                    export_classification_candidates(self.root, self.output, rows, summary)

                self.assertFalse(self.output.exists())
                self.assertEqual(self._tree_bytes(), before)
                path.write_bytes(original)

    def test_command_exports_snapshot_candidates_without_network_or_registry_writes(self) -> None:
        """Команда передаёт снимок построителю и создаёт только отдельный пакет просмотра."""

        snapshot = self._snapshot()
        before = self._tree_bytes()
        output = io.StringIO()

        with (
            patch("socket.socket", side_effect=AssertionError("Сеть запрещена в тесте")),
            patch(
                "src.corpus.classification_candidates.read_candidate_snapshot", return_value=snapshot,
            ) as reader,
            redirect_stdout(output),
        ):
            result = extract_classification_codes.main([
                "--snapshot", str(self.snapshot_path), "--output-dir", str(self.output),
            ], project_root=self.root)

        self.assertEqual(result, 0)
        reader.assert_called_once_with(self.root, self.snapshot_path)
        summary = json.loads((self.output / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["candidate_count"], 3)
        self.assertEqual(summary["metadata_items_matched"], 1)
        self.assertIn("Все находки ожидают проверки", output.getvalue())

        for relative, body in before.items():
            self.assertEqual((self.root / relative).read_bytes(), body)

    def test_command_reports_input_and_output_errors_without_partial_files(self) -> None:
        """Ошибки данных и небезопасный путь завершают команду понятным сообщением."""

        for damaged_text in (False, True):
            with self.subTest(damaged_text=damaged_text):
                target = self.output if damaged_text else self.root / "data/unsafe_output"

                if damaged_text:
                    self.text_path.write_bytes(self.text_path.read_bytes() + b"\n")

                before = self._tree_bytes()
                errors = io.StringIO()

                with redirect_stderr(errors), self.assertRaises(SystemExit) as caught:
                    extract_classification_codes.main([
                        "--output-dir", str(target),
                    ], project_root=self.root)

                self.assertEqual(caught.exception.code, 2)
                self.assertIn("Не удалось подготовить классификационные коды", errors.getvalue())
                self.assertEqual(self._tree_bytes(), before)
                self.assertFalse(target.exists())

    def test_export_outside_results_is_rejected_before_writing(self) -> None:
        """Результат нельзя записать поверх реестров либо в исходный корпус."""

        rows, summary = build_classification_candidates(self.root)

        for output in (self.root / "data/new_result", self.manifests / "other", self.root.parent / "outside"):
            with self.subTest(output=output):
                before = self._tree_bytes()

                with self.assertRaises(ValueError):
                    export_classification_candidates(self.root, output, rows, summary)

                self.assertEqual(self._tree_bytes(), before)

    def test_output_symlink_parent_is_rejected(self) -> None:
        """Символическая ссылка в пути вывода не позволяет обойти выбранный каталог."""

        rows, summary = build_classification_candidates(self.root)
        real_directory = self.manifests / "results/real"
        real_directory.mkdir(parents=True)
        link = self.manifests / "results/linked"
        link.symlink_to(real_directory, target_is_directory=True)

        with self.assertRaises(ValueError):
            export_classification_candidates(self.root, link / "output", rows, summary)

        self.assertEqual(list(real_directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
