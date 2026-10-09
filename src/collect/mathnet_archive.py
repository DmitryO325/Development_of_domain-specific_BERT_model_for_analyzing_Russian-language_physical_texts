"""Неизменяемые ответы Math-Net, проверяемый кеш и журнал отдельных запросов."""

from __future__ import annotations

import codecs
import fcntl
import hashlib
import json
import os
import re
import tempfile

from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any
from uuid import uuid4

from .base import HttpResponseSnapshot, SAFE_RESPONSE_HEADERS
from .mathnet_api import MathnetApiClient, MathnetApiError, validate_api_url

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
HTML_PATTERN = re.compile(r"\s*(?:<!doctype\s+html\b|<html\b|<head\b|<body\b)", re.IGNORECASE)


@dataclass(frozen=True)
class MathnetResult:
    """Результат одного запроса или проверки локальной копии, не запись корпуса."""

    body_path: Path
    metadata_path: Path
    from_cache: bool
    data: dict[str, Any] | list[Any] | str


def _json_bytes(record: dict[str, Any]) -> bytes:
    """Сериализовать служебную запись независимо от формата исходного ответа."""

    return (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_bytes(path: Path, content: bytes, *, immutable: bool = False) -> None:
    """Атомарно опубликовать файл, сохраняя уже существующие исходные байты."""

    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)

        try:
            temporary.write(content)
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
                if path.is_symlink() or path.read_bytes() != content:
                    raise ValueError(f"Нельзя заменить исходные байты: {path}") from exception

        else:
            os.replace(temporary_path, path)

    finally:
        temporary_path.unlink(missing_ok=True)


def _read_bytes(path: Path, limit: int) -> bytes:
    """Прочитать локальный файл с ограничением размера до разбора содержимого."""

    with path.open("rb") as source_file:
        content = source_file.read(limit + 1)

    if len(content) > limit:
        raise ValueError(f"Локальный файл превышает допустимый размер: {path}")

    return content


def _reject_constant(value: str) -> None:
    """Отвергнуть нечисловые константы, не входящие в стандарт JSON."""

    raise ValueError(f"Недопустимое значение JSON: {value}")


def _load_json(body: bytes) -> Any:
    """Разобрать JSON, не принимая некорректную кодировку и чрезмерную вложенность."""

    try:
        return json.loads(body, parse_constant=_reject_constant)

    except (ValueError, UnicodeError, RecursionError) as exception:
        raise ValueError("Не удалось разобрать JSON; исходные байты не изменены") from exception


def _validate_snapshot(snapshot: HttpResponseSnapshot, url: str) -> None:
    """Проверить происхождение и безопасный состав HTTP-свидетельства."""

    if (
        snapshot.requested_url != url or snapshot.final_url != url or
        type(snapshot.status_code) is not int or snapshot.status_code != 200
    ):
        raise ValueError("Ожидался HTTP 200 с исходного адреса без перенаправлений")

    if len(snapshot.body) > MAX_ARCHIVE_BYTES:
        raise ValueError("Ответ превышает предельный размер локального архива")

    try:
        retrieved_at = datetime.fromisoformat(snapshot.retrieved_at)

    except (ValueError, TypeError) as exception:
        raise ValueError("Некорректное время HTTP-свидетельства") from exception

    if retrieved_at.utcoffset() is None:
        raise ValueError("Время HTTP-свидетельства должно содержать часовой пояс")

    if any(
        not isinstance(name, str) or not isinstance(value, str) or
        name not in SAFE_RESPONSE_HEADERS
        for name, value in snapshot.headers
    ):
        raise ValueError("HTTP-свидетельство содержит недопустимые заголовки")


def _parse_response(
    snapshot: HttpResponseSnapshot,
    kind: str,
    encoding: str | None,
) -> dict[str, Any] | list[Any] | str:
    """Принять только явный JSON или обычный текст, не угадывая неизвестную схему."""

    content_types = [value for name, value in snapshot.headers if name == "content-type"]

    if len(content_types) != 1:
        raise ValueError("Ожидался единственный Content-Type; формат ответа нужно проверить")

    message = Message()
    message["Content-Type"] = content_types[0]
    content_type = message.get_content_type()

    if kind == "json":
        if content_type != "application/json" and not (
            content_type.startswith("application/") and content_type.endswith("+json")
        ):
            raise ValueError("Ожидался MIME-тип JSON; неожиданный ответ не принят в кеш")

        data = _load_json(snapshot.body)

        if not isinstance(data, (dict, list)):
            raise ValueError("Ожидался JSON-объект или список, схема полей пока не подтверждена")

        if isinstance(data, dict) and ("error" in data or "errors" in data):
            raise ValueError("JSON содержит поле ошибки; ответ требует проверки и не принят в кеш")

        return data

    if content_type != "text/plain":
        raise ValueError("Ожидался text/plain; другой формат текста требует отдельной проверки")

    selected_encoding = encoding or message.get_content_charset() or "utf-8"

    try:
        text = snapshot.body.decode(selected_encoding)

    except (LookupError, UnicodeError, ValueError) as exception:
        raise ValueError("Не удалось декодировать текст; проверьте charset или задайте --encoding") from exception

    if not text.strip("\ufeff \t\r\n") or "\x00" in text or HTML_PATTERN.match(text.lstrip("\ufeff")):
        raise ValueError("Вместо обычного текста получен пустой ответ, HTML или двоичные данные")

    return text


class MathnetArchive:
    """Хранить отдельные ответы; без явного разрешения читать только локальный кеш."""

    def __init__(
        self,
        output_dir: Path,
        state_dir: Path,
        client: MathnetApiClient | None = None,
    ) -> None:
        """Запомнить каталоги без создания файлов и сетевых обращений."""

        self.output_dir = Path(output_dir).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.client = client

    def fetch(
        self,
        url: str,
        *,
        kind: str,
        allow_network: bool = False,
        refresh: bool = False,
        encoding: str | None = None,
    ) -> MathnetResult:
        """Проверить кеш либо получить ровно один ответ без обхода ссылок."""

        validate_api_url(url)

        if kind not in {"json", "text"}:
            raise ValueError("Вид ответа должен быть json или text")

        if refresh and not allow_network:
            raise ValueError("Обновление кеша требует явного --allow-network")

        if encoding is not None:
            if kind != "text" or not isinstance(encoding, str) or not encoding.strip():
                raise ValueError("Кодировку можно явно задать только для текстового ответа")

            try:
                codecs.lookup(encoding)

            except LookupError as exception:
                raise ValueError("Неизвестная кодировка текста") from exception

        self.state_dir.mkdir(parents=True, exist_ok=True)

        with (self.state_dir / "mathnet_archive.lock").open("a+b") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            except BlockingIOError as exception:
                raise MathnetApiError("Другой процесс уже работает с архивом Math-Net") from exception

            try:
                return self._fetch_locked(url, kind, allow_network, refresh, encoding)

            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _cache_path(self, url: str, *, unverified: bool = False) -> Path:
        """Получить индекс успешного или ещё не разобранного ответа по хешу URL."""

        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        directory = "unverified" if unverified else "cache"

        return self.state_dir / directory / f"{digest}.json"

    def _content_path(self, digest: object, *, metadata: bool = False) -> Path:
        """Построить проверенный путь к телу или HTTP-свидетельству по SHA-256."""

        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            raise ValueError("В кеше содержится некорректный SHA-256")

        directory, suffix = ("http", "json") if metadata else ("responses", "bin")
        path = self.output_dir / directory / digest[:2] / f"{digest}.{suffix}"

        if path.is_symlink() or not path.resolve().is_relative_to(self.output_dir):
            raise ValueError("Путь ответа выходит за пределы архива или является символической ссылкой")

        return path

    def _read_cached(
        self,
        url: str,
        kind: str,
        *,
        unverified: bool = False,
    ) -> tuple[HttpResponseSnapshot, Path, Path, dict[str, Any]] | None:
        """Перепроверить происхождение, размеры и хеши кеша без запроса к серверу."""

        cache_path = self._cache_path(url, unverified=unverified)

        if not cache_path.exists():
            return

        record = _load_json(_read_bytes(cache_path, MAX_METADATA_BYTES))

        if (
            not isinstance(record, dict) or record.get("version") != 1 or
            record.get("requested_url") != url or record.get("kind") != kind
        ):
            raise ValueError("Запись кеша повреждена или относится к другому запросу")

        if record.get("text_encoding") is not None and not isinstance(record["text_encoding"], str):
            raise ValueError("В кеше указана некорректная кодировка текста")

        body_path = self._content_path(record.get("body_sha256"))
        metadata_path = self._content_path(record.get("metadata_sha256"), metadata=True)
        body = _read_bytes(body_path, MAX_ARCHIVE_BYTES)
        metadata_bytes = _read_bytes(metadata_path, MAX_METADATA_BYTES)

        if (
            type(record.get("bytes")) is not int or record["bytes"] != len(body) or
            hashlib.sha256(body).hexdigest() != record["body_sha256"] or
            hashlib.sha256(metadata_bytes).hexdigest() != record["metadata_sha256"]
        ):
            raise ValueError("Нарушена целостность тела ответа или HTTP-свидетельства")

        metadata = _load_json(metadata_bytes)

        if not isinstance(metadata, dict):
            raise ValueError("Некорректное HTTP-свидетельство в кеше")

        if any(
            not isinstance(metadata.get(name), str)
            for name in ("requested_url", "final_url", "retrieved_at")
        ) or type(metadata.get("status_code")) is not int:
            raise ValueError("Некорректные поля HTTP-свидетельства в кеше")

        headers = metadata.get("headers")

        if not isinstance(headers, list) or any(
            not isinstance(header, list) or len(header) != 2 or
            any(not isinstance(value, str) for value in header)
            for header in headers
        ):
            raise ValueError("Некорректные заголовки HTTP-свидетельства в кеше")

        snapshot = HttpResponseSnapshot(
            requested_url=metadata["requested_url"],
            final_url=metadata["final_url"],
            status_code=metadata["status_code"],
            headers=tuple((name, value) for name, value in headers),
            retrieved_at=metadata["retrieved_at"],
            body=body,
        )
        _validate_snapshot(snapshot, url)

        return snapshot, body_path, metadata_path, record

    def _save_response(self, snapshot: HttpResponseSnapshot) -> tuple[Path, Path, dict[str, Any]]:
        """Сохранить точные байты до разбора, чтобы неожиданный формат можно было исследовать."""

        body_digest = hashlib.sha256(snapshot.body).hexdigest()
        metadata_bytes = snapshot.canonical_metadata()
        metadata_digest = hashlib.sha256(metadata_bytes).hexdigest()
        body_path = self._content_path(body_digest)
        metadata_path = self._content_path(metadata_digest, metadata=True)

        if len(metadata_bytes) > MAX_METADATA_BYTES:
            raise ValueError("HTTP-свидетельство превышает допустимый размер")

        _write_bytes(body_path, snapshot.body, immutable=True)
        _write_bytes(metadata_path, metadata_bytes, immutable=True)
        record = {
            "version": 1,
            "requested_url": snapshot.requested_url,
            "body_sha256": body_digest,
            "metadata_sha256": metadata_digest,
            "bytes": len(snapshot.body),
        }

        return body_path, metadata_path, record

    def _record_attempt(self, record: dict[str, Any]) -> None:
        """Зафиксировать исход отдельного сетевого обращения в локальном журнале."""

        event = {
            **record,
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        }
        path = self.state_dir / "attempts" / f"{uuid4()}.json"
        _write_bytes(path, _json_bytes(event), immutable=True)

    def _fetch_locked(
        self,
        url: str,
        kind: str,
        allow_network: bool,
        refresh: bool,
        encoding: str | None,
    ) -> MathnetResult:
        """Выполнить операцию под блокировкой без подмены ошибки старым успешным ответом."""

        cached = self._read_cached(url, kind)
        using_unverified = False

        if cached is None and not refresh:
            cached = self._read_cached(url, kind, unverified=True)
            using_unverified = cached is not None

        if cached is not None and not refresh:
            snapshot, body_path, metadata_path, record = cached
            selected_encoding = encoding or record.get("text_encoding")
            data = _parse_response(snapshot, kind, selected_encoding)

            if using_unverified or encoding is not None:
                record["text_encoding"] = selected_encoding
                _write_bytes(self._cache_path(url), _json_bytes(record))

            return MathnetResult(body_path, metadata_path, True, data)

        if not allow_network:
            raise ValueError("Проверенного ответа нет в кеше; сеть выключена. Для запроса нужен --allow-network")

        client = self.client if self.client is not None else MathnetApiClient(self.state_dir)
        attempt: dict[str, Any] = {"requested_url": url, "kind": kind}

        try:
            snapshot = client.get_response(url)

        except (MathnetApiError, OSError, ValueError) as exception:
            failure = {**attempt, "status": "request_failed", "error_type": type(exception).__name__}

            if isinstance(exception, MathnetApiError):
                failure["http_status"] = exception.http_status
                failure["error_code"] = exception.error_code

            self._record_attempt(failure)
            raise

        try:
            _validate_snapshot(snapshot, url)
            body_path, metadata_path, record = self._save_response(snapshot)
            record.update({"kind": kind, "text_encoding": encoding})
            attempt.update(record)
            # Ошибку декодирования можно исправить офлайн, не выдавая raw за успешный кеш.
            _write_bytes(self._cache_path(url, unverified=True), _json_bytes(record))
            data = _parse_response(snapshot, kind, encoding)

        except ValueError:
            self._record_attempt({**attempt, "status": "response_rejected"})
            raise

        self._record_attempt({**attempt, "status": "accepted"})
        _write_bytes(self._cache_path(url), _json_bytes(record))

        return MathnetResult(body_path, metadata_path, False, data)
