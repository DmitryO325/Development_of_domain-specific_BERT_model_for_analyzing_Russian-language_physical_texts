"""Одиночные запросы к API ОИЯИ с общей сохраняемой паузой не менее минуты."""

from __future__ import annotations

import fcntl
import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request

from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .base import HttpResponseSnapshot, SAFE_RESPONSE_HEADERS, USER_AGENT

DEFAULT_API_URL = "https://pubrepo-api.jinr.ru/server/api"
MIN_DELAY_SECONDS = 60.0
MAX_RESPONSE_BYTES = 128 * 1024 * 1024


class JinrApiError(RuntimeError):
    """Ошибка запроса или локального контроля частоты обращений к ОИЯИ."""


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    """Запретить скрытый дополнительный запрос при перенаправлении сервера."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        """Остановить перенаправление, сохранив сигнатуру стандартного обработчика."""

        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _validate_url(url: str) -> None:
    """Разрешить только абсолютный HTTPS-адрес внутри известного API ОИЯИ."""

    if not isinstance(url, str) or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in url
    ):
        raise ValueError("Адрес API должен быть строкой без пробелов и управляющих символов")

    try:
        parsed = urlsplit(url)
        port = parsed.port

    except ValueError as exception:
        raise ValueError("Некорректный адрес API ОИЯИ") from exception

    decoded_path = unquote(parsed.path)
    allowed_path = parsed.path == "/server/api" or parsed.path.startswith("/server/api/")

    if (
        parsed.scheme != "https"
        or parsed.hostname != "pubrepo-api.jinr.ru"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or "#" in url
        or not allowed_path
        or "\\" in decoded_path
        or "%" in decoded_path
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
        or any(part in {".", ".."} for part in decoded_path.split("/"))
    ):
        raise ValueError("Разрешены только HTTPS-адреса API ОИЯИ внутри /server/api")


def _retry_after_seconds(value: str | None) -> float:
    """Разобрать Retry-After в секундах или формате даты HTTP."""

    if not value:
        return 0.0

    if value.strip().isdigit():
        try:
            seconds = float(value.strip())

        except (ValueError, OverflowError):
            return 0.0

        if math.isfinite(seconds):
            return seconds

        return 0.0

    try:
        parsed_date = parsedate_to_datetime(value)

        if parsed_date.tzinfo is None:
            parsed_date = parsed_date.replace(tzinfo=timezone.utc)

        return max(0.0, parsed_date.timestamp() - time.time())

    except (TypeError, ValueError, OverflowError):
        return 0.0


class JinrApiClient:
    """Клиент без повторов и авторизации с общей блокировкой по каталогу состояния."""

    def __init__(
        self,
        state_dir: Path,
        *,
        delay_seconds: float = MIN_DELAY_SECONDS,
        timeout: float = 30.0,
    ) -> None:
        """Подготовить общую для запусков паузу и ограничение сетевого ожидания."""

        if not math.isfinite(delay_seconds) or delay_seconds < MIN_DELAY_SECONDS:
            raise ValueError("Пауза между запросами должна быть конечной и не меньше 60 секунд")

        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout должен быть конечным числом больше нуля")

        self.state_dir = Path(state_dir)
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self._next_monotonic = 0.0
        self._opener = urllib.request.build_opener(_RejectRedirect())

    def get_response(self, url: str) -> HttpResponseSnapshot:
        """Получить один ответ, выдержав общую паузу после предыдущего обращения."""

        _validate_url(url)

        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)

            with (self.state_dir / "jinr_api_rate_limit.lock").open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

                try:
                    return self._get_locked_response(url)

                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        except OSError as exception:
            raise JinrApiError(
                f"Не удалось прочитать или сохранить ограничение запросов для {url}: {exception}"
            ) from exception

    def get_json(self, url: str) -> dict[str, Any]:
        """Прочитать один JSON-объект API без дополнительных запросов."""

        response = self.get_response(url)

        try:
            payload = json.loads(response.body)

        except (json.JSONDecodeError, UnicodeDecodeError) as exception:
            raise JinrApiError(f"Ответ {url} не является корректным JSON") from exception

        if not isinstance(payload, dict):
            raise JinrApiError(f"В ответе {url} ожидался JSON-объект")

        return payload

    def _get_locked_response(self, url: str) -> HttpResponseSnapshot:
        """Выполнить запрос под блокировкой и сохранить паузу даже после ошибки."""

        self._wait_until_allowed()
        next_delay = self.delay_seconds

        # Незавершённый запрос после аварийного выхода добавляет защитную паузу.
        self._write_state(time.time() + self.timeout + next_delay, in_flight=True)

        try:
            return self._request_once(url)

        except urllib.error.HTTPError as exception:
            status_code = exception.code

            if status_code == 429:
                next_delay = max(
                    next_delay,
                    _retry_after_seconds(exception.headers.get("Retry-After")),
                )

            exception.close()
            message = f"Запрос {url} остановлен: HTTP {status_code}. Автоматического повтора нет."

            if 300 <= status_code < 400:
                message += " Перенаправление не выполнено."

            raise JinrApiError(message) from exception

        except (OSError, http.client.HTTPException) as exception:
            raise JinrApiError(
                f"Не удалось получить {url}: {exception}. Автоматического повтора нет."
            ) from exception

        finally:
            self._next_monotonic = time.monotonic() + next_delay
            self._write_state(time.time() + next_delay, in_flight=False)

    def _request_once(self, url: str) -> HttpResponseSnapshot:
        """Прочитать ограниченный по размеру ответ без сохранения секретных заголовков."""

        # Перед вызовом разрешены только HTTPS, один сервер и префикс его API.
        request = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/hal+json, application/json, application/pdf;q=0.9, */*;q=0.1",
            },
        )

        with self._opener.open(request, timeout=self.timeout) as response:
            expected_length = _content_length(response.headers, url)

            if expected_length is not None and expected_length > MAX_RESPONSE_BYTES:
                raise JinrApiError(f"Ответ {url} превышает лимит {MAX_RESPONSE_BYTES} байт")

            body = response.read(MAX_RESPONSE_BYTES + 1)

            if len(body) > MAX_RESPONSE_BYTES:
                raise JinrApiError(f"Ответ {url} превышает лимит {MAX_RESPONSE_BYTES} байт")

            if expected_length is not None and len(body) != expected_length:
                raise JinrApiError(
                    f"Неполный или некорректный ответ {url}: получено {len(body)} байт, "
                    f"Content-Length указывает {expected_length}"
                )

            safe_headers = tuple(sorted(
                (name.lower(), value.strip())
                for name, value in response.headers.items()
                if name.lower() in SAFE_RESPONSE_HEADERS
            ))

            return HttpResponseSnapshot(
                requested_url=url,
                final_url=response.geturl(),
                status_code=response.status,
                headers=safe_headers,
                retrieved_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                body=body,
            )

    def _wait_until_allowed(self) -> None:
        """Прочитать сохранённое ограничение, не игнорируя повреждённое состояние."""

        state_path = self.state_dir / "jinr_api_rate_limit.json"
        next_request_at = 0.0

        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                next_request_at = state["next_request_at"]

                if (
                    isinstance(next_request_at, bool)
                    or not isinstance(next_request_at, (float, int))
                    or not math.isfinite(next_request_at)
                    or next_request_at < 0
                    or not isinstance(state["in_flight"], bool)
                ):
                    raise ValueError("Некорректные поля ограничения запросов")

                if state["in_flight"]:
                    next_request_at = max(next_request_at, time.time() + self.delay_seconds)

            except (ValueError, KeyError, TypeError, OverflowError) as exception:
                raise JinrApiError(
                    f"Повреждён файл ограничения запросов {state_path}; загрузка остановлена"
                ) from exception

        while True:
            remaining = max(
                next_request_at - time.time(),
                self._next_monotonic - time.monotonic(),
            )

            if remaining <= 0:
                return

            time.sleep(min(remaining, MIN_DELAY_SECONDS))

    def _write_state(self, next_request_at: float, *, in_flight: bool) -> None:
        """Атомарно сохранить время следующего обращения, пока удерживается блокировка."""

        state_path = self.state_dir / "jinr_api_rate_limit.json"
        temporary_path = state_path.with_suffix(".tmp")
        payload = {"next_request_at": next_request_at, "in_flight": in_flight}

        with temporary_path.open("w", encoding="utf-8") as state_file:
            json.dump(payload, state_file, ensure_ascii=False)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())

        os.replace(temporary_path, state_path)


def _content_length(headers: Message, url: str) -> int | None:
    """Проверить заявленный размер ответа, если сервер прислал Content-Length."""

    raw_length = headers.get("Content-Length")

    if raw_length is None:
        return

    try:
        expected_length = int(raw_length)

    except ValueError as exception:
        raise JinrApiError(f"Ответ {url} содержит некорректный Content-Length") from exception

    if expected_length < 0:
        raise JinrApiError(f"Ответ {url} содержит отрицательный Content-Length")

    return expected_length
