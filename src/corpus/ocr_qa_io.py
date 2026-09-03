"""Загрузка и файловая проверка машинных форм контроля качества OCR."""

from __future__ import annotations

import hashlib
import json

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .schema_validation import SchemaCatalog, SchemaValidationError

OCR_QA_SCHEMA_FILES = {
    "run": "ocr_qa_run.schema.json",
    "frame": "ocr_qa_frame.schema.json",
    "page": "ocr_qa_page.schema.json",
    "formula": "ocr_qa_formula.schema.json",
    "summary": "ocr_qa_summary.schema.json",
}
MAX_SCHEMA_ERRORS = 8


class _DuplicateJsonKeyError(ValueError):
    """JSON-объект содержит один ключ более одного раза."""


@dataclass(frozen=True, slots=True)
class _OcrQaBundle:
    """Загруженные машинные формы одного запуска OCR QA."""

    run: dict[str, Any]
    frame: list[dict[str, Any]]
    pages: list[dict[str, Any]]
    formulas: list[dict[str, Any]]
    summary: dict[str, Any]
    paths: dict[str, Path]


class _OcrQaIoMixin:
    """Операции загрузки, проверки схем и ссылочных файлов OCR QA."""

    project_root: Path
    schema_dir: Path
    check_files: bool
    _file_hashes: dict[Path, str]

    def _load_bundle(
        self,
        *,
        run_path: Path,
        summary_path: Path,
        frame_path: Path | None,
        page_results_path: Path | None,
        formula_results_path: Path | None,
        errors: list[str],
    ) -> _OcrQaBundle | None:
        """Загрузить пять форм и разрешить записанные в них пути."""

        resolved_run_path = self._resolve_input_path(run_path)
        resolved_summary_path = self._resolve_input_path(summary_path)
        run = self._read_json_object(resolved_run_path, "паспорт запуска", errors)
        summary = self._read_json_object(
            resolved_summary_path,
            "сводку запуска",
            errors,
        )

        if run is None or summary is None:
            return

        resolved_frame_path = self._select_path(
            frame_path,
            run.get("sample_frame_path"),
            field_name="sample_frame_path",
            errors=errors,
        )
        resolved_pages_path = self._select_path(
            page_results_path,
            summary.get("page_results_path"),
            field_name="page_results_path",
            errors=errors,
        )
        embedded_formula_path = summary.get("formula_results_path")

        if formula_results_path is not None and embedded_formula_path is None:
            errors.append(
                "Путь formula_results_path передан валидатору, но в сводке "
                "это поле равно null."
            )

        resolved_formulas_path = self._select_path(
            formula_results_path,
            embedded_formula_path,
            field_name="formula_results_path",
            errors=errors,
            allow_missing=True,
        )

        if resolved_frame_path is None or resolved_pages_path is None:
            return

        frame = self._read_jsonl(resolved_frame_path, "выборку страниц", errors)
        pages = self._read_jsonl(resolved_pages_path, "результаты страниц", errors)
        formulas = (
            self._read_jsonl(
                resolved_formulas_path,
                "результаты формул",
                errors,
            )
            if resolved_formulas_path is not None
            else []
        )

        if frame is None or pages is None or formulas is None:
            return

        paths = {
            "run": resolved_run_path,
            "summary": resolved_summary_path,
            "frame": resolved_frame_path,
            "pages": resolved_pages_path,
        }

        if resolved_formulas_path is not None:
            paths["formulas"] = resolved_formulas_path

        self._check_embedded_input_path(
            summary.get("run_manifest_path"),
            resolved_run_path,
            "run_manifest_path",
            errors,
        )
        self._check_embedded_input_path(
            run.get("sample_frame_path"),
            resolved_frame_path,
            "sample_frame_path",
            errors,
        )
        self._check_embedded_input_path(
            summary.get("page_results_path"),
            resolved_pages_path,
            "page_results_path",
            errors,
        )

        if resolved_formulas_path is not None:
            self._check_embedded_input_path(
                embedded_formula_path,
                resolved_formulas_path,
                "formula_results_path",
                errors,
            )

        return _OcrQaBundle(run, frame, pages, formulas, summary, paths)

    def _resolve_input_path(self, path: Path) -> Path:
        """Разрешить явный путь CLI или API относительно корня проекта."""

        candidate = Path(path)

        if not candidate.is_absolute():
            candidate = self.project_root / candidate

        return candidate.resolve()

    def _select_path(
        self,
        explicit_path: Path | None,
        embedded_path: Any,
        *,
        field_name: str,
        errors: list[str],
        allow_missing: bool = False,
    ) -> Path | None:
        """Выбрать явный путь или безопасно разрешить путь из формы."""

        if explicit_path is not None:
            return self._resolve_input_path(explicit_path)

        if embedded_path is None and allow_missing:
            return

        if not isinstance(embedded_path, str) or not embedded_path.strip():
            errors.append(f"Поле {field_name} не содержит пригодного пути.")
            return

        return self._resolve_embedded_path(embedded_path, field_name, errors)

    def _resolve_embedded_path(
        self,
        value: str,
        field_name: str,
        errors: list[str],
    ) -> Path | None:
        """Разрешить вложенный относительный путь без выхода из проекта."""

        relative_path = Path(value)

        if relative_path.is_absolute():
            errors.append(f"Поле {field_name} должно содержать относительный путь.")
            return

        resolved_path = (self.project_root / relative_path).resolve()

        if not resolved_path.is_relative_to(self.project_root):
            errors.append(f"Поле {field_name} указывает за пределы каталога проекта.")
            return

        return resolved_path

    def _check_embedded_input_path(
        self,
        embedded_path: Any,
        actual_path: Path,
        field_name: str,
        errors: list[str],
    ) -> None:
        """Проверить, что вложенный путь именует фактически проверяемый файл."""

        if not isinstance(embedded_path, str):
            return

        expected_path = self._resolve_embedded_path(
            embedded_path,
            field_name,
            errors,
        )

        if expected_path is not None and expected_path != actual_path:
            errors.append(
                f"Поле {field_name} указывает на {embedded_path!r}, но валидатору "
                "передан другой файл."
            )

    def _read_json_object(
        self,
        path: Path,
        label: str,
        errors: list[str],
    ) -> dict[str, Any] | None:
        """Прочитать один JSON-объект и отклонить повторные ключи."""

        try:
            value = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_object_without_duplicate_keys,
            )

        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exception:
            errors.append(f"Не удалось прочитать {label} {path}: {exception}")
            return

        if not isinstance(value, dict):
            errors.append(f"Файл {path} должен содержать один JSON-объект.")
            return

        return value

    def _read_jsonl(
        self,
        path: Path,
        label: str,
        errors: list[str],
    ) -> list[dict[str, Any]] | None:
        """Прочитать JSONL и отклонить пустые строки и повторные ключи."""

        try:
            lines = path.read_text(encoding="utf-8").splitlines()

        except (OSError, UnicodeDecodeError) as exception:
            errors.append(f"Не удалось прочитать {label} {path}: {exception}")
            return

        records: list[dict[str, Any]] = []

        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                errors.append(
                    f"Файл {path}, строка {line_number}: "
                    "пустая строка недопустима."
                )
                continue

            try:
                value = json.loads(
                    line,
                    object_pairs_hook=_object_without_duplicate_keys,
                )

            except (json.JSONDecodeError, ValueError) as exception:
                errors.append(f"Файл {path}, строка {line_number}: {exception}")
                continue

            if not isinstance(value, dict):
                errors.append(
                    f"Файл {path}, строка {line_number}: ожидался JSON-объект."
                )
                continue

            records.append(value)

        return records

    def _validate_schemas(
        self,
        bundle: _OcrQaBundle,
        errors: list[str],
    ) -> None:
        """Проверить каждую форму по контракту JSON Schema."""

        try:
            from jsonschema import Draft202012Validator, FormatChecker
            from jsonschema.exceptions import SchemaError

        except ImportError:
            errors.append("Для проверки OCR QA необходим пакет jsonschema.")
            return

        records_by_kind = {
            "run": [bundle.run],
            "frame": bundle.frame,
            "page": bundle.pages,
            "formula": bundle.formulas,
            "summary": [bundle.summary],
        }

        for kind, records in records_by_kind.items():
            schema_path = self.schema_dir / OCR_QA_SCHEMA_FILES[kind]

            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))

            except (OSError, json.JSONDecodeError) as exception:
                errors.append(
                    f"Не удалось прочитать схему {schema_path}: {exception}"
                )
                continue

            try:
                Draft202012Validator.check_schema(schema)

            except SchemaError as exception:
                errors.append(f"Некорректная схема {schema_path}: {exception}")
                continue

            validator = Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
            )

            for record_index, record in enumerate(records, start=1):
                schema_errors = sorted(
                    validator.iter_errors(record),
                    key=lambda error: tuple(
                        str(part) for part in error.absolute_path
                    ),
                )

                for schema_error in schema_errors[:MAX_SCHEMA_ERRORS]:
                    location = ".".join(
                        str(part) for part in schema_error.absolute_path
                    ) or "<root>"
                    errors.append(
                        f"Схема {kind}, запись {record_index}, {location}: "
                        f"{schema_error.message}"
                    )

                if len(schema_errors) > MAX_SCHEMA_ERRORS:
                    hidden_count = len(schema_errors) - MAX_SCHEMA_ERRORS
                    errors.append(
                        f"Схема {kind}, запись {record_index}: скрыто "
                        f"ещё ошибок: {hidden_count}."
                    )

    def _validate_files(
        self,
        bundle: _OcrQaBundle,
        pages: list[dict[str, Any]],
        formulas: list[dict[str, Any]],
        errors: list[str],
    ) -> None:
        """Проверить наличие и SHA-256 всех доступных ссылочных файлов."""

        run = bundle.run
        summary = bundle.summary
        checks: list[tuple[Any, Any, str]] = [
            (
                run.get("source_artifacts_manifest_path"),
                run.get("source_artifacts_manifest_sha256"),
                "реестр исходных артефактов",
            ),
            (
                run.get("sample_frame_path"),
                run.get("sample_frame_sha256"),
                "кадр выборки",
            ),
            (
                run.get("annotation_guide_path"),
                run.get("annotation_guide_sha256"),
                "руководство разметки",
            ),
            (
                summary.get("run_manifest_path"),
                summary.get("run_manifest_sha256"),
                "паспорт запуска",
            ),
            (
                summary.get("page_results_path"),
                summary.get("page_results_sha256"),
                "результаты страниц",
            ),
        ]

        if summary.get("formula_results_path") is not None:
            checks.append(
                (
                    summary.get("formula_results_path"),
                    summary.get("formula_results_sha256"),
                    "результаты формул",
                )
            )

        for variant in run.get("variants", []):
            if variant.get("configuration_path") is not None:
                checks.append(
                    (
                        variant.get("configuration_path"),
                        variant.get("configuration_sha256"),
                        f"конфигурация варианта {variant.get('variant_id')!r}",
                    )
                )

        for frame in bundle.frame:
            sample_id = frame.get("page_sample_id")
            checks.extend(
                [
                    (
                        frame.get("source_pdf_path"),
                        _artifact_digest(frame.get("source_pdf_artifact_id")),
                        f"исходный PDF страницы {sample_id!r}",
                    ),
                    (
                        frame.get("page_render_path"),
                        frame.get("page_render_sha256"),
                        f"изображение страницы {sample_id!r}",
                    ),
                ]
            )

        for page in pages:
            record_id = page.get("page_qa_record_id")
            checks.extend(
                [
                    (
                        page.get("gold_prose_text_path"),
                        page.get("gold_prose_text_sha256"),
                        f"эталонный текст результата страницы {record_id!r}",
                    ),
                    (
                        page.get("candidate_prose_text_path"),
                        page.get("candidate_prose_text_sha256"),
                        f"текст-кандидат прозы результата страницы {record_id!r}",
                    ),
                    (
                        page.get("candidate_page_text_path"),
                        page.get("candidate_page_text_sha256"),
                        f"текст-кандидат результата страницы {record_id!r}",
                    ),
                ]
            )

        for formula in formulas:
            record_id = formula.get("formula_qa_record_id")
            checks.append(
                (
                    formula.get("candidate_page_text_path"),
                    formula.get("candidate_page_text_sha256"),
                    f"текст-кандидат результата формулы {record_id!r}",
                )
            )

        for path_value, digest, label in checks:
            self._check_file(path_value, digest, label, errors)

        self._validate_source_artifact_links(bundle, errors)

    def _check_file(
        self,
        path_value: Any,
        expected_sha256: Any,
        label: str,
        errors: list[str],
    ) -> None:
        """Проверить один файл внутри проекта и его SHA-256."""

        if not isinstance(path_value, str) or not isinstance(expected_sha256, str):
            return

        path = self._resolve_embedded_path(path_value, label, errors)

        if path is None:
            return

        if not path.is_file():
            errors.append(
                f"Ссылочный файл отсутствует: {label} — {path_value}"
            )
            return

        actual_sha256 = self._file_hashes.get(path)

        if actual_sha256 is None:
            try:
                actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

            except OSError as exception:
                errors.append(
                    f"Не удалось прочитать ссылочный файл: {label} — "
                    f"{path_value}: {exception}"
                )
                return

            self._file_hashes[path] = actual_sha256

        if actual_sha256 != expected_sha256:
            errors.append(
                f"Не совпадает SHA-256: {label}, {path_value!r}; "
                f"ожидалось {expected_sha256}, получено {actual_sha256}."
            )

    def _validate_source_artifact_links(
        self,
        bundle: _OcrQaBundle,
        errors: list[str],
    ) -> None:
        """Сверить PDF-ссылки выборки с реестром исходных артефактов."""

        path_value = bundle.run.get("source_artifacts_manifest_path")

        if not isinstance(path_value, str):
            return

        path = self._resolve_embedded_path(
            path_value,
            "source_artifacts_manifest_path",
            errors,
        )

        if path is None or not path.is_file():
            return

        artifacts = self._read_jsonl(path, "реестр исходных артефактов", errors)

        if artifacts is None:
            return

        schema_catalog = SchemaCatalog(self.schema_dir)
        valid_artifacts: list[dict[str, Any]] = []

        for record_index, artifact in enumerate(artifacts, start=1):
            try:
                schema_catalog.validate("artifacts", artifact)

            except (SchemaValidationError, KeyError, RuntimeError) as exception:
                errors.append(
                    "Реестр исходных артефактов, запись "
                    f"{record_index}: ошибка схемы artifacts: {exception}"
                )
                continue

            valid_artifacts.append(artifact)

        artifacts_by_id: dict[Any, list[dict[str, Any]]] = defaultdict(list)

        for artifact in valid_artifacts:
            artifacts_by_id[artifact.get("artifact_id")].append(artifact)

        for frame in bundle.frame:
            artifact_id = frame.get("source_pdf_artifact_id")
            candidates = artifacts_by_id.get(artifact_id, [])
            sample_id = frame.get("page_sample_id")

            if len(candidates) != 1:
                errors.append(
                    f"Страница выборки {sample_id!r}: ожидался ровно один "
                    f"исходный артефакт {artifact_id!r}, найдено {len(candidates)}."
                )
                continue

            artifact = candidates[0]

            for artifact_field, frame_field in (
                ("work_id", "work_id"),
                ("path", "source_pdf_path"),
            ):
                if artifact.get(artifact_field) != frame.get(frame_field):
                    errors.append(
                        f"Страница выборки {sample_id!r}: {frame_field} не совпадает "
                        "с реестром исходных артефактов."
                    )

            if artifact.get("representation") != "pdf":
                errors.append(
                    f"Страница выборки {sample_id!r}: исходный артефакт не является PDF."
                )


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    """Собрать JSON-объект и отклонить повторный ключ."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"повторный ключ {key!r}")

        result[key] = value

    return result


def _artifact_digest(value: Any) -> str | None:
    """Извлечь шестнадцатеричный SHA-256 из artifact_id."""

    if not isinstance(value, str) or not value.startswith("sha256:"):
        return

    digest = value.removeprefix("sha256:")
    return digest if len(digest) == 64 else None
