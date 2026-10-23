"""Одиночные запросы с минутной паузой и политикой адресов Иоффе по умолчанию."""

from __future__ import annotations

import fcntl
import http.client
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request

from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .base import HttpResponseSnapshot, SAFE_RESPONSE_HEADERS, USER_AGENT

MIN_DELAY_SECONDS = 60.0
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_FORM_BODY_BYTES = 4096
IOFFE_RESPONSE_HEADERS = SAFE_RESPONSE_HEADERS | {"retry-after"}
ALLOWED_PATH = re.compile(r"/(?:robots\.txt|journals/[1-5]|issues/[0-9]+|articles/(?:viewPDF/)?[0-9]+)?")


class IoffeError(RuntimeError):
    """Ошибка запроса, ограничения частоты или целостности локального архива."""


class IoffeTransientNetworkError(IoffeError):
    """Временный транспортный сбой, повтор которого может назначить внешний запускатель."""


def validate_ioffe_url(url: str) -> None:
    """Разрешить только известные публичные пути journals.ioffe.ru без параметров."""

    if not isinstance(url, str):
        raise TypeError("Адрес Иоффе должен быть строкой")

    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url):
        raise ValueError("Адрес Иоффе не должен содержать пробелы или управляющие символы")

    try:
        parsed = urlsplit(url)
        expected_port = {"http": 80, "https": 443}.get(parsed.scheme)
        allowed_authorities = {"journals.ioffe.ru", f"journals.ioffe.ru:{expected_port}"}

        if (
            expected_port is None or parsed.netloc not in allowed_authorities or
            parsed.port not in {None, expected_port} or "?" in url or "#" in url or
            ALLOWED_PATH.fullmatch(parsed.path) is None
        ):
            raise ValueError("Адрес вне разрешённых публичных страниц Иоффе")

    except ValueError as exception:
        raise ValueError(f"Недопустимый адрес Иоффе: {url}") from exception


def _retry_after_seconds(value: str | None) -> float:
    """Разобрать серверную паузу в секундах или в виде даты HTTP."""

    if not value:
        return 0.0

    try:
        if value.strip().isdigit():
            seconds = float(value.strip())

        else:
            parsed_date = parsedate_to_datetime(value)

            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=UTC)

            seconds = max(0.0, parsed_date.timestamp() - time.time())

        return seconds if math.isfinite(seconds) else 0.0

    except (ValueError, TypeError, OverflowError):
        return 0.0


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    """Возвращать перенаправление вызывающему коду без второго сетевого запроса."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        """Прервать стандартное автоматическое следование за Location."""

        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class IoffeClient:
    """Обращаться к одному сайту без повторов, авторизации и скрытых переходов."""

    def __init__(
        self,
        state_dir: Path,
        *,
        delay_seconds: float = MIN_DELAY_SECONDS,
        timeout: float = 30.0,
        validate_url: Callable[[str], None] = validate_ioffe_url,
        state_prefix: str = "ioffe",
    ) -> None:
        """Настроить паузу, общую для HTTP, HTTPS и повторных запусков команды."""

        if not math.isfinite(delay_seconds) or delay_seconds < MIN_DELAY_SECONDS:
            raise ValueError("Пауза должна быть конечной и не меньше 60 секунд")

        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout должен быть конечным числом больше нуля")

        if re.fullmatch(r"[a-z][a-z0-9_]*", state_prefix) is None:
            raise ValueError("Некорректное имя состояния ограничителя")

        self.state_dir = Path(state_dir)
        self.validate_url = validate_url
        self.state_prefix = state_prefix
        self.delay_seconds = delay_seconds
        self.timeout = timeout
        self._next_monotonic = 0.0
        self._response_retry_after = 0.0
        self._response_status_code: int | None = None
        # Не используем авторизованные прокси из окружения и не следуем перенаправлениям.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirect())

    def get_response(self, url: str) -> HttpResponseSnapshot:
        """Получить один завершённый ответ, включая HTTP-ошибки, под общей блокировкой."""

        self.validate_url(url)

        return self._response_with_lock(url)

    def post_form_response(self, url: str, body: bytes) -> HttpResponseSnapshot:
        """Отправить проверенную вызывающим кодом форму с общей минутной паузой."""

        self.validate_url(url)

        if not isinstance(body, bytes):
            raise TypeError("Тело формы должно быть байтовой строкой")

        if not body or len(body) > MAX_FORM_BODY_BYTES:
            raise ValueError("Тело формы должно содержать от 1 до 4096 байт")

        return self._response_with_lock(url, form_body=body)

    def _response_with_lock(self, url: str, *, form_body: bytes | None = None) -> HttpResponseSnapshot:
        """Использовать одну межпроцессную блокировку для получения страницы и отправки формы."""

        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)

            with (self.state_dir / f"{self.state_prefix}_rate_limit.lock").open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

                try:
                    if form_body is None:
                        return self._get_locked_response(url)

                    return self._get_locked_response(url, form_body=form_body)

                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        except OSError as exception:
            raise IoffeError(f"Не удалось сохранить ограничение запросов: {exception}") from exception

    def set_minimum_delay(self, delay_seconds: float) -> None:
        """Повысить общую сохраняемую паузу, не сокращая уже полученный Retry-After."""

        if not math.isfinite(delay_seconds) or delay_seconds < MIN_DELAY_SECONDS:
            raise ValueError("Пауза должна быть конечной и не меньше 60 секунд")

        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)

            with (self.state_dir / f"{self.state_prefix}_rate_limit.lock").open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

                try:
                    next_request_at, in_flight = self._read_state()
                    self.delay_seconds = max(self.delay_seconds, delay_seconds)
                    next_request_at = max(next_request_at, time.time() + self.delay_seconds)
                    self._next_monotonic = max(self._next_monotonic, time.monotonic() + self.delay_seconds)
                    self._write_state(next_request_at, in_flight=in_flight)

                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        except OSError as exception:
            raise IoffeError(f"Не удалось повысить паузу запросов Иоффе: {exception}") from exception

    def _get_locked_response(self, url: str, *, form_body: bytes | None = None) -> HttpResponseSnapshot:
        """Записать защитное состояние до запроса и выдержать паузу после любой ошибки."""

        self._wait_until_allowed()
        self._response_retry_after = 0.0
        self._response_status_code = None
        self._write_state(time.time() + self.timeout + self.delay_seconds, in_flight=True)

        try:
            if form_body is None:
                return self._request_once(url)

            return self._post_form_once(url, form_body)

        except (OSError, http.client.HTTPException) as exception:
            reason = exception.reason if isinstance(exception, urllib.error.URLError) else exception
            transient_types = (
                TimeoutError, socket.gaierror, ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError, http.client.RemoteDisconnected, http.client.IncompleteRead,
            )

            # Тип ошибки проверяется только вокруг сетевого вызова, не вокруг записи кеша.
            # Даже обрыв тела ответа с отказом HTTP не разрешает автоматически повторять отказ.
            if (
                not isinstance(exception, urllib.error.HTTPError) and
                (self._response_status_code is None or self._response_status_code < 300) and
                isinstance(reason, transient_types)
            ):
                raise IoffeTransientNetworkError(
                    f"Временный сетевой сбой при получении {url}: {exception}. Автоматического повтора нет."
                ) from exception

            raise IoffeError(f"Не удалось получить {url}: {exception}. Автоматического повтора нет.") from exception

        finally:
            delay_seconds = max(self.delay_seconds, self._response_retry_after)
            self._next_monotonic = time.monotonic() + delay_seconds
            self._write_state(time.time() + delay_seconds, in_flight=False)

    def _request_once(self, url: str) -> HttpResponseSnapshot:
        """Построить обычный GET без изменения существующего интерфейса транспорта."""

        # get_response заранее проверяет точный сервер, порт и публичный путь.
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html, application/pdf, text/plain;q=0.9"},
        )

        return self._response_snapshot(request)

    def _post_form_once(self, url: str, body: bytes) -> HttpResponseSnapshot:
        """Отправить точные байты формы без автоматических перенаправлений и повторов."""

        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html, application/pdf, text/plain;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )

        return self._response_snapshot(request)

    def _response_snapshot(self, request: urllib.request.Request) -> HttpResponseSnapshot:
        """Снять точные байты и безопасные заголовки без распаковки или декодирования."""

        url = request.full_url

        try:
            response = self._opener.open(request, timeout=self.timeout)

        except urllib.error.HTTPError as exception:
            response = exception

        with response:
            status_code = response.getcode()

            if not isinstance(status_code, int) or isinstance(status_code, bool) or not 100 <= status_code <= 599:
                raise IoffeError("Сервер не вернул корректный числовой статус HTTP")

            self._response_status_code = status_code

            if status_code in {429, 503}:
                self._response_retry_after = _retry_after_seconds(response.headers.get("Retry-After"))

            lengths = response.headers.get_all("Content-Length", [])

            if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]+", lengths[0].strip())):
                raise IoffeError("Некорректный или повторяющийся Content-Length")

            expected_length = int(lengths[0]) if lengths else None

            if expected_length is not None and expected_length > MAX_RESPONSE_BYTES:
                raise IoffeError("Ответ источника превышает лимит 128 МиБ")

            body = response.read(MAX_RESPONSE_BYTES + 1)

            if len(body) > MAX_RESPONSE_BYTES:
                raise IoffeError("Ответ источника превышает лимит 128 МиБ")

            if expected_length is not None and len(body) != expected_length:
                raise IoffeError("Получены неполные байты ответа: Content-Length не совпадает")

            if response.geturl() != url:
                raise IoffeError("Конечный адрес изменился: автоматические перенаправления запрещены")

            headers = tuple(sorted(
                (name.lower(), value.strip())
                for name, value in response.headers.items()
                if name.lower() in IOFFE_RESPONSE_HEADERS
            ))

            return HttpResponseSnapshot(
                requested_url=url,
                final_url=url,
                status_code=status_code,
                headers=headers,
                retrieved_at=datetime.now(UTC).isoformat(timespec="microseconds"),
                body=body,
            )

    def _read_state(self) -> tuple[float, bool]:
        """Проверить сохранённое состояние и восстановить повышенную минимальную паузу."""

        state_path = self.state_dir / f"{self.state_prefix}_rate_limit.json"
        next_request_at = 0.0
        in_flight = False

        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                next_request_at = state["next_request_at"]
                in_flight = state["in_flight"]
                minimum_delay = state.get("minimum_delay_seconds", MIN_DELAY_SECONDS)

                if (
                    isinstance(next_request_at, bool) or not isinstance(next_request_at, (int, float)) or
                    not math.isfinite(next_request_at) or next_request_at < 0 or
                    not isinstance(in_flight, bool) or isinstance(minimum_delay, bool) or
                    not isinstance(minimum_delay, (int, float)) or
                    not math.isfinite(minimum_delay) or minimum_delay < MIN_DELAY_SECONDS
                ):
                    raise ValueError("Некорректные поля ограничения запросов")

                self.delay_seconds = max(self.delay_seconds, minimum_delay)

            except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exception:
                raise IoffeError(f"Повреждено ограничение запросов: {state_path}") from exception

        return next_request_at, in_flight

    def _wait_until_allowed(self) -> None:
        """Выдержать сохранённую паузу, не обходя незавершённый запрос прошлого запуска."""

        next_request_at, in_flight = self._read_state()

        if in_flight:
            next_request_at = max(next_request_at, time.time() + self.delay_seconds)

        while True:
            remaining = max(next_request_at - time.time(), self._next_monotonic - time.monotonic())

            if remaining <= 0:
                return

            time.sleep(min(remaining, MIN_DELAY_SECONDS))

    def _write_state(self, next_request_at: float, *, in_flight: bool) -> None:
        """Атомарно записать состояние, удерживая межпроцессную блокировку."""

        state_path = self.state_dir / f"{self.state_prefix}_rate_limit.json"
        temporary_path = state_path.with_suffix(".tmp")

        with temporary_path.open("w", encoding="utf-8") as state_file:
            json.dump({
                "next_request_at": next_request_at,
                "in_flight": in_flight,
                "minimum_delay_seconds": self.delay_seconds,
            }, state_file)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())

        os.replace(temporary_path, state_path)
