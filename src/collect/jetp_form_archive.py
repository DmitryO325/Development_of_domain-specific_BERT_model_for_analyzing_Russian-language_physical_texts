"""Проверяемый отдельный архив отправки формы выбора тома ЖЭТФ."""

from __future__ import annotations

import hashlib
import json

from pathlib import Path
from typing import Any

from . import jetp
from .base import HttpResponseSnapshot
from .ioffe_archive import (
    MAX_METADATA_BYTES,
    SHA256_PATTERN,
    IoffeArchive,
    _json_bytes,
    _read_bytes,
    _snapshot_record,
    _validate_snapshot,
    _write_bytes,
)
from .ioffe_http import MAX_RESPONSE_BYTES, IoffeError

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
FORMAT_VERSION = "jetp-volume-form-v1"


def _safe_path(archive: IoffeArchive, root: Path, relative_path: str) -> Path:
    """Запретить выход из архива и символические ссылки на любом уровне пути."""

    path = archive._safe_path(root, relative_path)
    current = root

    for part in ("", *Path(relative_path).parts):
        current = current / part

        if current.is_symlink():
            raise IoffeError("Путь архива формы не должен содержать символические ссылки")

    return path


def _request_record(body: bytes) -> dict[str, Any]:
    """Связать идентификатор запроса с методом, адресом, форматом и точными байтами."""

    digest = hashlib.sha256(body).hexdigest()

    return {
        "method": "POST",
        "url": jetp.START_URL,
        "content_type": FORM_CONTENT_TYPE,
        "body_sha256": digest,
        "body_bytes": len(body),
        "body_path": f"form_requests/{digest[:2]}/{digest}.bin",
    }


def _read_json(path: Path) -> dict[str, Any]:
    """Прочитать канонический JSON без незаметной нормализации повреждённого файла."""

    body = _read_bytes(path, MAX_METADATA_BYTES)
    record = json.loads(body)

    if not isinstance(record, dict) or _json_bytes(record) != body:
        raise IoffeError("Ожидалась каноническая запись архива формы")

    return record


def _read_snapshot(archive: IoffeArchive, record: dict[str, Any]) -> HttpResponseSnapshot:
    """Проверить точные пути, размеры и контрольные суммы сохранённого HTTP-снимка."""

    body_digest = record.get("sha256")
    metadata_digest = record.get("response_metadata_sha256")

    if (
        not isinstance(body_digest, str) or SHA256_PATTERN.fullmatch(body_digest) is None or
        not isinstance(metadata_digest, str) or SHA256_PATTERN.fullmatch(metadata_digest) is None
    ):
        raise IoffeError("Некорректная контрольная сумма HTTP-свидетельства формы")

    relative_path = record.get("relative_path")
    metadata_path = f"responses/{body_digest[:2]}/{body_digest}.{metadata_digest}.http.json"
    allowed_paths = {
        f"responses/{body_digest[:2]}/{body_digest}.{suffix}"
        for suffix in ("html", "pdf", "txt", "bin")
    }

    if (
        not isinstance(relative_path, str) or relative_path not in allowed_paths or
        record.get("response_metadata_path") != metadata_path
    ):
        raise IoffeError("Пути HTTP-свидетельства формы не соответствуют контрольным суммам")

    body = _read_bytes(_safe_path(archive, archive.output_dir, relative_path), MAX_RESPONSE_BYTES)
    metadata_bytes = _read_bytes(_safe_path(archive, archive.output_dir, metadata_path), MAX_METADATA_BYTES)

    if (
        hashlib.sha256(body).hexdigest() != body_digest or
        type(record.get("bytes")) is not int or record["bytes"] != len(body) or
        hashlib.sha256(metadata_bytes).hexdigest() != metadata_digest
    ):
        raise IoffeError("Повреждены байты HTTP-свидетельства формы")

    metadata = json.loads(metadata_bytes)
    snapshot = HttpResponseSnapshot(
        requested_url=metadata["requested_url"],
        final_url=metadata["final_url"],
        status_code=metadata["status_code"],
        headers=tuple(tuple(header) for header in metadata["headers"]),
        retrieved_at=metadata["retrieved_at"],
        body=body,
    )
    _validate_snapshot(snapshot, record["requested_url"], archive.validate_url)

    if snapshot.canonical_metadata() != metadata_bytes or _snapshot_record(snapshot) != record:
        raise IoffeError("HTTP-свидетельство формы не является каноническим")

    return snapshot


def _cached_redirect(
    archive: IoffeArchive,
    request: dict[str, Any],
    request_digest: str,
    body: bytes,
    volume: int,
) -> str | None:
    """Проверить старую форму и ответ без обращения к изменяемому GET-кешу."""

    from .journal_collection import decode_html

    cache_path = _safe_path(archive, archive.state_dir, f"form_cache/{request_digest}.json")

    if not cache_path.exists():
        return

    pointer = _read_json(cache_path)
    envelope_digest = pointer.get("envelope_sha256")

    if not isinstance(envelope_digest, str) or SHA256_PATTERN.fullmatch(envelope_digest) is None:
        raise IoffeError("Некорректная контрольная сумма конверта формы")

    envelope_path = f"form_submissions/{request_digest}/{envelope_digest}.json"

    if pointer != {
        "request_sha256": request_digest,
        "envelope_path": envelope_path,
        "envelope_sha256": envelope_digest,
    }:
        raise IoffeError("Указатель кеша формы не соответствует запросу")

    envelope = _read_json(_safe_path(archive, archive.output_dir, envelope_path))

    if (
        hashlib.sha256(_json_bytes(envelope)).hexdigest() != envelope_digest or
        set(envelope) != {"format", "volume", "request", "form", "response"} or
        envelope["format"] != FORMAT_VERSION or
        type(envelope["volume"]) is not int or envelope["volume"] != volume or
        envelope["request"] != request or
        not isinstance(envelope["form"], dict) or not isinstance(envelope["response"], dict)
    ):
        raise IoffeError("Конверт формы не соответствует сохранённому запросу")

    saved_body = _read_bytes(_safe_path(archive, archive.output_dir, request["body_path"]), 4096)

    if saved_body != body:
        raise IoffeError("Точные байты запроса формы не совпадают")

    saved_form = _read_snapshot(archive, envelope["form"])
    archive.evidence_for(saved_form)

    if jetp.build_volume_form(decode_html(saved_form, "koi8-r"), saved_form.requested_url, volume) != body:
        raise IoffeError("Сохранённая форма не подтверждает параметры запроса")

    response = _read_snapshot(archive, envelope["response"])

    return jetp.volume_redirect(response, volume)


def _save_submission(
    archive: IoffeArchive,
    request: dict[str, Any],
    body: bytes,
    request_digest: str,
    form_evidence: dict[str, Any],
    response: HttpResponseSnapshot,
    volume: int,
) -> dict[str, str]:
    """Сохранить запрос и любой полный ответ до решения о допустимости перенаправления."""

    response_record = _snapshot_record(response)
    envelope = {
        "format": FORMAT_VERSION,
        "volume": volume,
        "request": request,
        "form": form_evidence,
        "response": response_record,
    }
    envelope_bytes = _json_bytes(envelope)
    envelope_digest = hashlib.sha256(envelope_bytes).hexdigest()
    envelope_path = f"form_submissions/{request_digest}/{envelope_digest}.json"

    for relative_path, exact_bytes in (
        (request["body_path"], body),
        (response_record["relative_path"], response.body),
        (response_record["response_metadata_path"], response.canonical_metadata()),
        (envelope_path, envelope_bytes),
    ):
        _write_bytes(_safe_path(archive, archive.output_dir, relative_path), exact_bytes, immutable=True)

    pointer = {
        "request_sha256": request_digest,
        "envelope_path": envelope_path,
        "envelope_sha256": envelope_digest,
    }
    attempt_path = _safe_path(archive, archive.state_dir, f"form_attempts/{request_digest}.json")
    _write_bytes(attempt_path, _json_bytes(pointer))

    return pointer


def select_volume(
    archive: IoffeArchive,
    form_snapshot: HttpResponseSnapshot,
    volume: int,
    *,
    refresh: bool = False,
) -> str:
    """Получить подтверждённый формой адрес тома, не меняя GET-кеш и реестры корпуса."""

    # Локальный импорт разрывает цикл: сборщик использует этот архив формы.
    from .journal_collection import decode_html

    if not isinstance(form_snapshot, HttpResponseSnapshot):
        raise IoffeError("Для выбора тома нужен сохранённый HTTP-снимок формы")

    if type(volume) is not int or volume <= 0:
        raise ValueError("Номер тома должен быть положительным целым числом")

    try:
        with archive._locked_archive():
            form_evidence = archive.evidence_for(form_snapshot)
            _read_snapshot(archive, form_evidence)
            body = jetp.build_volume_form(decode_html(form_snapshot, "koi8-r"), form_snapshot.requested_url, volume)
            request = _request_record(body)
            request_digest = hashlib.sha256(_json_bytes(request)).hexdigest()
            cached_url = _cached_redirect(archive, request, request_digest, body, volume)

            if cached_url is not None and not refresh:
                return cached_url

            if not archive.allow_network:
                raise IoffeError("Нет разрешения на отправку формы ЖЭТФ; нужен allow_network=True")

            response = archive.client.post_form_response(jetp.START_URL, body)
            _validate_snapshot(response, jetp.START_URL, archive.validate_url)
            pointer = _save_submission(archive, request, body, request_digest, form_evidence, response, volume)

            try:
                redirect_url = jetp.volume_redirect(response, volume)

            except ValueError as exception:
                evidence_path = archive.output_dir / pointer["envelope_path"]
                raise IoffeError(f"{exception}. Свидетельство формы: {evidence_path}") from exception

            cache_path = _safe_path(archive, archive.state_dir, f"form_cache/{request_digest}.json")
            _write_bytes(cache_path, _json_bytes(pointer))

            return redirect_url

    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exception:
        raise IoffeError(f"Не удалось проверить архив формы ЖЭТФ: {exception}") from exception
