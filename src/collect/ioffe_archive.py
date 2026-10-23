"""Неизменяемые ответы журналов и проверяемый кеш с политикой адресов Иоффе по умолчанию."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile

from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import HttpResponseSnapshot
from .ioffe_http import (
    IOFFE_RESPONSE_HEADERS,
    MAX_RESPONSE_BYTES,
    IoffeClient,
    IoffeError,
    validate_ioffe_url,
)

MAX_METADATA_BYTES = 128 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def _json_bytes(record: dict[str, Any]) -> bytes:
    """Сериализовать служебную запись, не меняя точное тело ответа."""

    return (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _read_bytes(path: Path, limit: int) -> bytes:
    """Прочитать локальный файл с ограничением размера."""

    with path.open("rb") as source_file:
        body = source_file.read(limit + 1)

    if len(body) > limit:
        raise IoffeError(f"Превышен допустимый размер локального файла: {path}")

    return body


def _write_bytes(path: Path, body: bytes, *, immutable: bool = False) -> None:
    """Атомарно записать служебный файл или неизменяемые исходные байты."""

    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)

        try:
            temporary.write(body)
            temporary.flush()
            os.fsync(temporary.fileno())

        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

    try:
        if immutable:
            try:
                os.link(temporary_path, path)

            except FileExistsError as exception:
                if path.is_symlink() or _read_bytes(path, MAX_RESPONSE_BYTES) != body:
                    raise IoffeError(f"Нельзя перезаписать исходный файл: {path}") from exception

        else:
            os.replace(temporary_path, path)

    finally:
        temporary_path.unlink(missing_ok=True)


def _validate_snapshot(
    snapshot: HttpResponseSnapshot,
    url: str,
    validate_url: Callable[[str], None] = validate_ioffe_url,
) -> None:
    """Проверить полный ответ, происхождение, время и безопасные заголовки."""

    validate_url(url)

    if (
        snapshot.requested_url != url or snapshot.final_url != url or
        type(snapshot.status_code) is not int or not 100 <= snapshot.status_code <= 599
    ):
        raise IoffeError("HTTP-свидетельство не соответствует исходному адресу или статусу")

    if not isinstance(snapshot.body, bytes) or len(snapshot.body) > MAX_RESPONSE_BYTES:
        raise IoffeError("Недопустимое тело HTTP-ответа")

    try:
        retrieved_at = datetime.fromisoformat(snapshot.retrieved_at)

        if retrieved_at.utcoffset() is None:
            raise ValueError("Нет часового пояса")

    except (ValueError, TypeError) as exception:
        raise IoffeError("Некорректное время HTTP-свидетельства") from exception

    if not isinstance(snapshot.headers, tuple) or any(
        not isinstance(header, tuple) or len(header) != 2 or
        not all(isinstance(value, str) for value in header) or
        header[0] not in IOFFE_RESPONSE_HEADERS or
        any(ord(character) < 32 or ord(character) == 127 for character in header[1])
        for header in snapshot.headers
    ):
        raise IoffeError("Недопустимые заголовки HTTP-свидетельства")

    lengths = [value for name, value in snapshot.headers if name == "content-length"]

    if lengths and (
        len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None or
        int(lengths[0]) != len(snapshot.body)
    ):
        raise IoffeError("Content-Length не соответствует полному телу ответа")


def write_json(path: Path, record: dict[str, Any]) -> None:
    """Атомарно сохранить производный JSON, пропуская уже совпадающее содержимое."""

    body = _json_bytes(record)

    try:
        if path.is_symlink():
            raise IoffeError(f"Нельзя записывать JSON через символическую ссылку: {path}")

        if path.exists() and path.stat().st_size == len(body) and _read_bytes(path, len(body)) == body:
            return

        _write_bytes(path, body)

    except OSError as exception:
        raise IoffeError(f"Не удалось сохранить JSON: {path}: {exception}") from exception


def _response_suffix(snapshot: HttpResponseSnapshot) -> str:
    """Выбрать понятное расширение, не выдавая страницу ошибки за PDF."""

    content_types = [value.split(";", 1)[0].strip().lower() for name, value in snapshot.headers if name == "content-type"]

    if content_types == ["application/pdf"] and snapshot.body[:1024].lstrip().startswith(b"%PDF-"):
        return "pdf"

    if content_types == ["text/html"]:
        return "html"

    if content_types == ["text/plain"]:
        return "txt"

    return "bin"


def _snapshot_record(snapshot: HttpResponseSnapshot) -> dict[str, Any]:
    """Определить неизменяемые пути ответа по хешам точного тела и метаданных."""

    body_digest = hashlib.sha256(snapshot.body).hexdigest()
    metadata_digest = snapshot.metadata_sha256()

    return {
        "requested_url": snapshot.requested_url,
        "relative_path": (
            f"responses/{body_digest[:2]}/{body_digest}.{_response_suffix(snapshot)}"
        ),
        "sha256": body_digest,
        "bytes": len(snapshot.body),
        "response_metadata_path": (
            f"responses/{body_digest[:2]}/{body_digest}.{metadata_digest}.http.json"
        ),
        "response_metadata_sha256": metadata_digest,
    }


class IoffeArchive:
    """Хранить ответы до разбора содержимого; сеть включается только явным флагом."""

    def __init__(
        self,
        output_dir: Path,
        state_dir: Path,
        client: IoffeClient,
        *,
        allow_network: bool = False,
        validate_url: Callable[[str], None] = validate_ioffe_url,
        accepted_statuses: Callable[[str], frozenset[int]] | None = None,
    ) -> None:
        """Разделить неизменяемые тела, HTTP-свидетельства и изменяемые указатели кеша."""

        self.output_dir = Path(output_dir).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.client = client
        self.allow_network = allow_network
        # Другие журналы используют тот же формат снимков и проверку целостности.
        self.validate_url = validate_url
        self.accepted_statuses = accepted_statuses

    def _accepts(self, url: str, status_code: int) -> bool:
        """Разрешить особые статусы только по явной политике конкретного адреса."""

        allowed = self.accepted_statuses(url) if self.accepted_statuses else frozenset({200})

        return status_code in allowed

    @contextmanager
    def _locked_archive(self) -> Generator[None]:
        """Сериализовать проверку кеша, загрузку и публикацию ответа между процессами."""

        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self._safe_path(self.state_dir, "ioffe_archive.lock")

            with lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

                try:
                    yield

                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        except OSError as exception:
            raise IoffeError(f"Не удалось использовать локальный архив: {exception}") from exception

    def _safe_path(self, root: Path, relative_path: str) -> Path:
        """Проверить, что локальная ссылка не выходит из своего каталога."""

        path = root / relative_path

        if Path(relative_path).is_absolute() or ".." in Path(relative_path).parts or not path.resolve().is_relative_to(root):
            raise IoffeError("Недопустимый путь в локальном архиве")

        return path

    def _record_path(self, url: str, directory: str = "cache") -> Path:
        """Составить путь по SHA-256 адреса без использования URL как имени файла."""

        url_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()

        return self._safe_path(self.state_dir, f"{directory}/{url_digest}.json")

    def _cached_response(self, url: str) -> tuple[HttpResponseSnapshot, dict[str, Any]] | None:
        """Проверить связь URL, двух SHA-256, размера и канонического HTTP-свидетельства."""

        cache_path = self._record_path(url)

        if not cache_path.exists():
            return

        try:
            record = json.loads(_read_bytes(cache_path, MAX_METADATA_BYTES))

            if not isinstance(record, dict) or record.get("requested_url") != url:
                raise ValueError("Кеш не соответствует адресу")

            body_digest = record.get("sha256")
            metadata_digest = record.get("response_metadata_sha256")

            if (
                not isinstance(body_digest, str) or SHA256_PATTERN.fullmatch(body_digest) is None or
                not isinstance(metadata_digest, str) or SHA256_PATTERN.fullmatch(metadata_digest) is None
            ):
                raise ValueError("Некорректные контрольные суммы")

            relative_path = record.get("relative_path")
            allowed_paths = {f"responses/{body_digest[:2]}/{body_digest}.{suffix}" for suffix in ("pdf", "html", "txt", "bin")}
            metadata_relative_path = f"responses/{body_digest[:2]}/{body_digest}.{metadata_digest}.http.json"

            if (
                not isinstance(relative_path, str) or relative_path not in allowed_paths or
                record.get("response_metadata_path") != metadata_relative_path
            ):
                raise ValueError("Пути не соответствуют контрольным суммам")

            body = _read_bytes(self._safe_path(self.output_dir, relative_path), MAX_RESPONSE_BYTES)
            metadata_bytes = _read_bytes(self._safe_path(self.output_dir, metadata_relative_path), MAX_METADATA_BYTES)

            if (
                hashlib.sha256(body).hexdigest() != body_digest or
                type(record.get("bytes")) is not int or record["bytes"] != len(body) or
                hashlib.sha256(metadata_bytes).hexdigest() != metadata_digest
            ):
                raise ValueError("Контрольные суммы или размер не совпадают")

            metadata = json.loads(metadata_bytes)
            snapshot = HttpResponseSnapshot(
                requested_url=metadata["requested_url"],
                final_url=metadata["final_url"],
                status_code=metadata["status_code"],
                headers=tuple(tuple(header) for header in metadata["headers"]),
                retrieved_at=metadata["retrieved_at"],
                body=body,
            )
            _validate_snapshot(snapshot, url, self.validate_url)

            if not self._accepts(url, snapshot.status_code) or snapshot.canonical_metadata() != metadata_bytes:
                raise ValueError("Ожидался канонический снимок допустимого HTTP-ответа")

            return snapshot, record

        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exception:
            raise IoffeError(f"Повреждён кеш: {cache_path}: {exception}") from exception

    def _save(self, snapshot: HttpResponseSnapshot) -> dict[str, Any]:
        """Сохранить точный ответ; только явно допустимый статус становится кешем."""

        record = _snapshot_record(snapshot)
        metadata_bytes = snapshot.canonical_metadata()

        _write_bytes(self._safe_path(self.output_dir, record["relative_path"]), snapshot.body, immutable=True)
        _write_bytes(self._safe_path(self.output_dir, record["response_metadata_path"]), metadata_bytes, immutable=True)
        _write_bytes(self._record_path(snapshot.requested_url, "attempts"), _json_bytes(record))

        if self._accepts(snapshot.requested_url, snapshot.status_code):
            _write_bytes(self._record_path(snapshot.requested_url), _json_bytes(record))

        return record

    def _import_response(self, snapshot: HttpResponseSnapshot) -> HttpResponseSnapshot:
        """Сохранить готовый снимок, когда вызывающий код уже удерживает блокировку."""

        _validate_snapshot(snapshot, snapshot.requested_url, self.validate_url)

        try:
            record = self._save(snapshot)

        except OSError as exception:
            raise IoffeError(f"Не удалось сохранить ответ источника: {exception}") from exception

        if not self._accepts(snapshot.requested_url, snapshot.status_code):
            evidence_path = self.output_dir / record["response_metadata_path"]
            raise IoffeError(f"HTTP {snapshot.status_code}; повторов и перенаправлений нет. Свидетельство: {evidence_path}")

        return snapshot

    def import_response(self, snapshot: HttpResponseSnapshot) -> HttpResponseSnapshot:
        """Импортировать готовый точный снимок без сети и допуска материала к обучению."""

        _validate_snapshot(snapshot, snapshot.requested_url, self.validate_url)

        with self._locked_archive():
            return self._import_response(snapshot)

    def get_response(self, url: str, *, refresh: bool = False) -> HttpResponseSnapshot:
        """Вернуть проверенный кеш или явно разрешённый новый ответ, без скрытого повтора."""

        self.validate_url(url)

        # Порядок захвата всегда archive → rate; второй процесс повторно проверит кеш.
        with self._locked_archive():
            cached = self._cached_response(url)

            if cached is not None and not refresh:
                return cached[0]

            if not self.allow_network:
                raise IoffeError(f"Нет разрешения на сетевой запрос {url}; нужен allow_network=True")

            snapshot = self.client.get_response(url)
            _validate_snapshot(snapshot, url, self.validate_url)

            return self._import_response(snapshot)

    def evidence(self, url: str) -> dict[str, Any]:
        """Получить проверенные пути и контрольные суммы последнего успешного ответа."""

        self.validate_url(url)

        with self._locked_archive():
            cached = self._cached_response(url)

            if cached is None:
                raise IoffeError(f"Нет успешного сохранённого ответа: {url}")

            return dict(cached[1])

    def evidence_for(self, snapshot: HttpResponseSnapshot) -> dict[str, Any]:
        """Проверить свидетельство конкретного снимка, не читая и не меняя latest-кеш."""

        if not isinstance(snapshot, HttpResponseSnapshot):
            raise IoffeError("Ожидается сохранённый HTTP-снимок")

        _validate_snapshot(snapshot, snapshot.requested_url, self.validate_url)

        if not self._accepts(snapshot.requested_url, snapshot.status_code):
            raise IoffeError("Нельзя использовать свидетельство недопустимого HTTP-ответа")

        record = _snapshot_record(snapshot)
        body_path = self._safe_path(self.output_dir, record["relative_path"])
        metadata_path = self._safe_path(self.output_dir, record["response_metadata_path"])

        # Исходные файлы публикуются неизменяемо. Проверка не зависит от указателя
        # кеша, который другой процесс мог обновить после получения снимка.
        try:
            if body_path.is_symlink() or metadata_path.is_symlink():
                raise IoffeError("Свидетельство снимка не должно быть символической ссылкой")

            body = _read_bytes(body_path, MAX_RESPONSE_BYTES)
            metadata_bytes = _read_bytes(metadata_path, MAX_METADATA_BYTES)

        except OSError as exception:
            raise IoffeError(
                f"Не найдено сохранённое свидетельство снимка: {snapshot.requested_url}"
            ) from exception

        if body != snapshot.body or metadata_bytes != snapshot.canonical_metadata():
            raise IoffeError(
                f"Повреждено свидетельство HTTP-снимка: {snapshot.requested_url}"
            )

        return record
