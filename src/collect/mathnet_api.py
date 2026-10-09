"""Одиночные запросы Math-Net.Ru с сохраняемой паузой и без скрытых повторов."""

from __future__ import annotations

import fcntl
import http.client
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.request

from datetime import datetime, timezone
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .base import HttpResponseSnapshot, SAFE_RESPONSE_HEADERS, USER_AGENT

DEFAULT_API_URL = "https://www.mathnet.ru/api"
MIN_DELAY_SECONDS = 60.0
_IDENTIFIER = r"[A-Za-z0-9]{1,80}"
_API_PATH = re.compile(
    rf"/api/(?:journals(?:/{_IDENTIFIER}(?:/articles(?:/{_IDENTIFIER})?)?)?"
    rf"|texts/{_IDENTIFIER}|authors/[0-9]{{1,80}})"
)


class MathnetApiError(RuntimeError):
    """Ошибка обращения к Math-Net.Ru или сохраняемого ограничения частоты."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        error_code: str = "request_error",
    ) -> None:
        """Сохранить безопасный код причины отдельно от диагностического сообщения."""

        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    """Запретить перенаправление, которое иначе создаёт дополнительный запрос."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        """Остановить переход, сохранив сигнатуру стандартного обработчика."""

        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def validate_api_url(url: str) -> None:
    """Разрешить только известные маршруты HTTPS API без параметров и авторизации."""

    if not isinstance(url, str) or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in url
    ):
        raise ValueError("Адрес API должен быть строкой без пробелов и управляющих символов")

    try:
        parsed = urlsplit(url)

    except ValueError as exception:
        raise ValueError("Некорректный адрес API Math-Net.Ru") from exception

    if (
        not url.startswith("https://")
        or parsed.netloc not in {"www.mathnet.ru", "www.mathnet.ru:443"}
        or "?" in url
        or "#" in url
        or _API_PATH.fullmatch(parsed.path) is None
    ):
        raise ValueError("Разрешены только документированные HTTPS-адреса API www.mathnet.ru")


def _retry_after_seconds(value: str | None) -> float:
    """Прочитать серверную задержку в секундах либо в формате даты HTTP."""

    if not value:
        return 0.0

    try:
        if re.fullmatch(r"[0-9]+", value.strip()):
            seconds = float(value.strip())

        else:
            parsed_date = parsedate_to_datetime(value)

            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)

            seconds = max(0.0, parsed_date.timestamp() - time.time())

        return seconds if math.isfinite(seconds) else 0.0

    except (TypeError, ValueError, OverflowError):
        return 0.0


def _content_length(headers: Message, url: str) -> int | None:
    """Проверить единственное неотрицательное значение Content-Length."""

    values = headers.get_all("Content-Length", [])

    if not values:
        return

    if len(values) != 1 or re.fullmatch(r"[0-9]+", values[0].strip()) is None:
        raise MathnetApiError(
            f"Ответ {url} содержит некорректный Content-Length", error_code="invalid_response"
        )

    try:
        return int(values[0].strip())

    except ValueError as exception:
        raise MathnetApiError(
            f"Ответ {url} содержит некорректный Content-Length", error_code="invalid_response"
        ) from exception


class MathnetApiClient:
    """Клиент с общей для процессов паузой, без авторизации и автоматических повторов."""

    def __init__(
        self,
        state_dir: Path,
        *,
        timeout: float = 30.0,
        delay_seconds: float = MIN_DELAY_SECONDS,
        max_response_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        """Проверить параметры без создания файлов и обращения к серверу."""

        for name, value, minimum in (
            ("delay_seconds", delay_seconds, MIN_DELAY_SECONDS),
            ("timeout", timeout, 0.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < minimum
                or (name == "timeout" and value == 0)
            ):
                raise ValueError(
                    f"Некорректный {name}: пауза должна быть не меньше 60 секунд, "
                    "timeout — больше нуля; оба значения должны быть конечными"
                )

        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes < 1
        ):
            raise ValueError("max_response_bytes должен быть положительным целым числом")

        self.state_dir = Path(state_dir)
        self.timeout = timeout
        self.delay_seconds = delay_seconds
        self.max_response_bytes = max_response_bytes
        self._next_monotonic = 0.0
        self._opener = urllib.request.build_opener(_RejectRedirect())

    def get_response(self, url: str) -> HttpResponseSnapshot:
        """Получить один ответ по разрешённому адресу под межпроцессной блокировкой."""

        validate_api_url(url)

        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self.state_dir / "mathnet_api_rate_limit.lock"
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)

            with os.fdopen(descriptor, "a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

                try:
                    return self._get_locked_response(url)

                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        except OSError as exception:
            raise MathnetApiError(
                f"Не удалось прочитать или сохранить ограничение запросов Math-Net.Ru: {exception}",
                error_code="state_error",
            ) from exception

    def _get_locked_response(self, url: str) -> HttpResponseSnapshot:
        """Сохранить защитную паузу перед запросом и отсчитать её заново после завершения."""

        self._wait_until_allowed()
        next_delay = self.delay_seconds

        # Незавершённый запрос оставляет признак аварии для следующего запуска.
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

            raise MathnetApiError(
                message, http_status=status_code, error_code="http_error"
            ) from exception

        except (OSError, http.client.HTTPException) as exception:
            timed_out = isinstance(exception, TimeoutError) or (
                isinstance(exception, urllib.error.URLError)
                and isinstance(exception.reason, TimeoutError)
            )

            raise MathnetApiError(
                f"Не удалось получить {url}: {exception}. Автоматического повтора нет.",
                error_code="timeout" if timed_out else "network_error",
            ) from exception

        finally:
            self._next_monotonic = time.monotonic() + next_delay
            self._write_state(time.time() + next_delay, in_flight=False)

    def _request_once(self, url: str) -> HttpResponseSnapshot:
        """Прочитать ограниченный ответ 200 без сохранения cookie и других секретов."""

        # get_response проверяет точный HTTPS-сервер и разрешённый маршрут.
        request = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain;q=0.9",
                "Accept-Encoding": "identity",
            },
        )

        with self._opener.open(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise urllib.error.HTTPError(
                    url, response.status, "Неожиданный статус", response.headers, None
                )

            if response.geturl() != url:
                raise MathnetApiError(
                    "Конечный URL изменился; перенаправления Math-Net.Ru запрещены",
                    error_code="invalid_response",
                )

            content_encoding = response.headers.get("Content-Encoding", "identity").strip().lower()

            if content_encoding != "identity":
                raise MathnetApiError(
                    f"Ответ {url} использует неподдерживаемое Content-Encoding",
                    error_code="invalid_response",
                )

            expected_length = _content_length(response.headers, url)

            if expected_length is not None and expected_length > self.max_response_bytes:
                raise MathnetApiError(
                    f"Ответ {url} превышает лимит {self.max_response_bytes} байт",
                    error_code="invalid_response",
                )

            body = response.read(self.max_response_bytes + 1)

            if len(body) > self.max_response_bytes:
                raise MathnetApiError(
                    f"Ответ {url} превышает лимит {self.max_response_bytes} байт",
                    error_code="invalid_response",
                )

            if expected_length is not None and len(body) != expected_length:
                raise MathnetApiError(
                    f"Неполный или некорректный ответ {url}: получено {len(body)} байт, "
                    f"Content-Length указывает {expected_length}",
                    error_code="invalid_response",
                )

            safe_headers = tuple(sorted(
                (name.lower(), value.strip())
                for name, value in response.headers.items()
                if name.lower() in SAFE_RESPONSE_HEADERS
            ))

            return HttpResponseSnapshot(
                requested_url=url,
                final_url=url,
                status_code=200,
                headers=safe_headers,
                retrieved_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                body=body,
            )

    def _wait_until_allowed(self) -> None:
        """Прочитать сохранённую паузу и остановиться при повреждённом состоянии."""

        state_path = self.state_dir / "mathnet_api_rate_limit.json"
        next_request_at = 0.0

        try:
            descriptor = os.open(state_path, os.O_RDONLY | os.O_NOFOLLOW)

        except FileNotFoundError:
            descriptor = None

        if descriptor is not None:
            try:
                with os.fdopen(descriptor, "r", encoding="utf-8") as state_file:
                    state = json.load(state_file)

                next_request_at = state["next_request_at"]

                if (
                    isinstance(next_request_at, bool)
                    or not isinstance(next_request_at, (int, float))
                    or not math.isfinite(next_request_at)
                    or next_request_at < 0
                    or not isinstance(state["in_flight"], bool)
                ):
                    raise ValueError("Некорректные поля ограничения запросов")

                if state["in_flight"]:
                    next_request_at = max(next_request_at, time.time() + self.delay_seconds)

            except (ValueError, KeyError, TypeError, OverflowError) as exception:
                raise MathnetApiError(
                    f"Повреждён файл ограничения запросов {state_path}; загрузка остановлена",
                    error_code="state_error",
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
        """Атомарно заменить файл состояния, пока удерживается общая блокировка."""

        state_path = self.state_dir / "mathnet_api_rate_limit.json"
        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".mathnet-rate-", dir=self.state_dir, delete=False
            ) as state_file:
                temporary_path = Path(state_file.name)
                json.dump(
                    {"next_request_at": next_request_at, "in_flight": in_flight},
                    state_file,
                    ensure_ascii=False,
                    allow_nan=False,
                )
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())

            os.replace(temporary_path, state_path)

        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
