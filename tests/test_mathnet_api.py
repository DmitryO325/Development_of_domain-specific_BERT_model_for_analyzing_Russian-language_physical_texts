"""Проверки транспорта Math-Net.Ru без сети и настоящих минутных ожиданий."""

from __future__ import annotations

import fcntl
import http.client
import json
import tempfile
import unittest
import urllib.error
import urllib.request

from email.message import Message
from email.utils import formatdate
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from src.collect.base import USER_AGENT
from src.collect.mathnet_api import (
    DEFAULT_API_URL,
    MIN_DELAY_SECONDS,
    MathnetApiClient,
    MathnetApiError,
    _RejectRedirect,
    validate_api_url,
)

LIST_URL = f"{DEFAULT_API_URL}/journals/list"


class _FakeClock:
    """Часы, позволяющие проверить паузы без настоящего ожидания."""

    def __init__(self) -> None:
        """Задать независимые календарные и монотонные часы."""

        self.timestamp = 1000.0
        self.monotonic_timestamp = 10.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        """Получить календарное время теста."""

        return self.timestamp

    def monotonic(self) -> float:
        """Получить время монотонного счётчика."""

        return self.monotonic_timestamp

    def advance(self, seconds: float) -> None:
        """Имитировать длительность передачи или паузы."""

        self.timestamp += seconds
        self.monotonic_timestamp += seconds

    def sleep(self, seconds: float) -> None:
        """Запомнить задержку и немедленно переместить часы."""

        self.sleeps.append(seconds)
        self.advance(seconds)


class MathnetApiClientTests(unittest.TestCase):
    """Ограничения адресов, частоты, размера и поведения при ошибках."""

    def setUp(self) -> None:
        """Подменить транспорт и часы, выделить отдельный каталог состояния."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.state_dir = Path(temporary_directory.name) / "state"
        self.state_path = self.state_dir / "mathnet_api_rate_limit.json"
        self.clock = _FakeClock()
        self.opener = MagicMock()
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.read.return_value = b'{"journals": []}'
        self.response.geturl.return_value = LIST_URL
        self.response.status = 200
        self.response.headers = Message()
        self.response.headers["Content-Type"] = "application/json"
        self.opener.open.return_value = self.response

        patches = (
            patch("src.collect.mathnet_api.time.time", side_effect=self.clock.time),
            patch("src.collect.mathnet_api.time.monotonic", side_effect=self.clock.monotonic),
            patch("src.collect.mathnet_api.time.sleep", side_effect=self.clock.sleep),
            patch("src.collect.mathnet_api.urllib.request.build_opener", return_value=self.opener),
        )

        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _read_state(self) -> dict[str, Any]:
        """Прочитать сохранённую паузу тестового клиента."""

        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_state(self, payload: Any) -> None:
        """Подготовить состояние предыдущего запуска."""

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    def _http_error(self, status: int, retry_after: str | None = None) -> urllib.error.HTTPError:
        """Создать HTTP-ошибку без соединения с сервером."""

        headers = Message()

        if retry_after is not None:
            headers["Retry-After"] = retry_after

        return urllib.error.HTTPError(LIST_URL, status, "test", headers, None)

    def test_constructor_has_no_network_or_filesystem_effects(self) -> None:
        """Создание клиента не создаёт каталог и не вызывает транспорт."""

        MathnetApiClient(self.state_dir)

        self.assertFalse(self.state_dir.exists())
        self.opener.open.assert_not_called()
        self.assertEqual(self.clock.sleeps, [])

    def test_error_constructor_remains_compatible(self) -> None:
        """Старый вызов исключения с одним сообщением сохраняет прежнее поведение."""

        exception = MathnetApiError("Сообщение")

        self.assertIsInstance(exception, RuntimeError)
        self.assertEqual(str(exception), "Сообщение")
        self.assertIsNone(exception.http_status)
        self.assertEqual(exception.error_code, "request_error")

    def test_known_routes_are_allowed(self) -> None:
        """Допустить оба явно указанных в письме варианта маршрутов."""

        for path in (
            "/journals", "/journals/list", "/journals/dan", "/journals/mm1234",
            "/journals/dan/articles", "/journals/dan/articles/dan1234",
            "/texts/mm1234", "/authors/18133",
        ):
            with self.subTest(path=path):
                validate_api_url(DEFAULT_API_URL + path)

        validate_api_url("https://www.mathnet.ru:443/api/texts/mm1234")
        self.opener.open.assert_not_called()

    def test_unsafe_or_unknown_routes_are_rejected_before_state_creation(self) -> None:
        """Запретить другой сервер, авторизацию, обход пути и неподтверждённую пагинацию."""

        urls = (
            "http://www.mathnet.ru/api/journals/list", "file:///api/journals/list",
            "https://mathnet.ru/api/journals/list", "https://www.mathnet.ru.evil/api/journals/list",
            "https://www.mathnet.ru:444/api/journals/list", "https://user@www.mathnet.ru/api/journals/list",
            "https://user:secret@www.mathnet.ru/api/journals/list", LIST_URL + "?",
            LIST_URL + "?page=1", LIST_URL + "#", LIST_URL + "#x", LIST_URL + "/",
            LIST_URL + "\n", " " + LIST_URL, LIST_URL + "\x00",
            DEFAULT_API_URL + "/journals/../list", DEFAULT_API_URL + "/journals/%2e%2e/list",
            DEFAULT_API_URL + "/journals/%64an", DEFAULT_API_URL + "/journals/dan\\articles",
            DEFAULT_API_URL + "/journals/дан", DEFAULT_API_URL + "/journals/dan_123",
            DEFAULT_API_URL + "/journals/" + "a" * 81,
            DEFAULT_API_URL + "/authors/mm1234", DEFAULT_API_URL + "/authors/١٨١٣٣",
            DEFAULT_API_URL + "/admin", DEFAULT_API_URL,
            "https://[broken/api/journals/list", "https://www.mathnet.ru/api//journals/list",
        )
        client = MathnetApiClient(self.state_dir)

        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                client.get_response(url)

        self.opener.open.assert_not_called()
        self.assertFalse(self.state_dir.exists())

    def test_invalid_configuration_is_rejected(self) -> None:
        """Параметры не допускают бесконечность, отрицательные значения и булевы числа."""

        for field, values in (
            ("delay_seconds", (59.9, 0, -1, float("inf"), float("nan"), True, "60")),
            ("timeout", (0, -1, float("inf"), float("nan"), True, "30")),
            ("max_response_bytes", (0, -1, 1.5, True, "10")),
        ):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    # Ошибочные типы намеренно передаются для проверки входных параметров.
                    parameters: dict[str, Any] = {field: value}
                    MathnetApiClient(self.state_dir, **parameters)

        self.opener.open.assert_not_called()

    def test_snapshot_preserves_bytes_and_excludes_credentials(self) -> None:
        """Сохранить точные байты и безопасные заголовки без cookie и авторизации."""

        self.response.headers["Set-Cookie"] = "secret=value"
        self.response.headers["Authorization"] = "secret"
        self.response.headers["ETag"] = '"original"'
        snapshot = MathnetApiClient(self.state_dir, timeout=12).get_response(LIST_URL)

        self.assertEqual(snapshot.body, b'{"journals": []}')
        self.assertEqual(snapshot.requested_url, LIST_URL)
        self.assertEqual(snapshot.final_url, LIST_URL)
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.headers, (("content-type", "application/json"), ("etag", '"original"')))
        self.assertEqual(self._read_state(), {"next_request_at": 1060.0, "in_flight": False})
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 12)
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertNotIn("Cookie", request.headers)
        self.assertNotIn("Authorization", request.headers)

    def test_shared_delay_starts_after_completion(self) -> None:
        """Новый клиент ждёт минуту после полного завершения предыдущего запроса."""

        starts: list[float] = []

        def open_response(*arguments: Any, **keywords: Any) -> MagicMock:
            """Имитировать пять секунд передачи ответа."""

            starts.append(self.clock.time())
            self.clock.advance(5.0)
            return self.response

        self.opener.open.side_effect = open_response
        MathnetApiClient(self.state_dir).get_response(LIST_URL)
        MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.assertEqual(starts, [1000.0, 1065.0])
        self.assertEqual(self.clock.sleeps, [60.0])
        self.assertEqual(self._read_state()["next_request_at"], 1130.0)

    def test_repeated_request_is_not_implicitly_cached(self) -> None:
        """Транспорт всегда делает одиночный запрос; кеш отвечает за отдельный уровень."""

        client = MathnetApiClient(self.state_dir)
        client.get_response(LIST_URL)
        client.get_response(LIST_URL)

        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(self.clock.sleeps, [MIN_DELAY_SECONDS])

    def test_longer_user_delay_is_preserved(self) -> None:
        """Более осторожное ограничение действует и после создания нового клиента."""

        MathnetApiClient(self.state_dir, delay_seconds=90).get_response(LIST_URL)
        MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.assertEqual(self.clock.sleeps, [60.0, 30.0])

    def test_wall_clock_jump_does_not_shorten_live_client_delay(self) -> None:
        """Перевод часов вперёд не обходит монотонную задержку живого клиента."""

        client = MathnetApiClient(self.state_dir)
        client.get_response(LIST_URL)
        self.clock.timestamp += 500
        client.get_response(LIST_URL)

        self.assertEqual(self.clock.sleeps, [60.0])

    def test_process_lock_is_held_during_transport(self) -> None:
        """Отдельный файловый дескриптор не получает блокировку во время запроса."""

        def open_response(*arguments: Any, **keywords: Any) -> MagicMock:
            """Проверить блокировку и сохранённый признак выполняющегося запроса."""

            with (
                (self.state_dir / "mathnet_api_rate_limit.lock").open("a+b") as lock_file,
                self.assertRaises(BlockingIOError),
            ):
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            self.assertEqual(self._read_state(), {"next_request_at": 1090.0, "in_flight": True})
            return self.response

        self.opener.open.side_effect = open_response
        MathnetApiClient(self.state_dir).get_response(LIST_URL)

        with (self.state_dir / "mathnet_api_rate_limit.lock").open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def test_crashed_request_requires_a_fresh_delay(self) -> None:
        """После аварии даже устаревшее состояние добавляет новую минутную паузу."""

        self._write_state({"next_request_at": 900.0, "in_flight": True})
        MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.assertEqual(self.clock.sleeps, [60.0])

    def test_corrupt_state_stops_before_network(self) -> None:
        """Повреждённое состояние нельзя проигнорировать для обхода частоты."""

        payloads = (
            {}, [], None, {"next_request_at": True, "in_flight": False},
            {"next_request_at": -1, "in_flight": False},
            {"next_request_at": float("nan"), "in_flight": False},
            {"next_request_at": float("inf"), "in_flight": False},
            {"next_request_at": "1000", "in_flight": False},
            {"next_request_at": 1000, "in_flight": 1},
            {"next_request_at": 10 ** 400, "in_flight": False},
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                self._write_state(payload)

                with self.assertRaisesRegex(MathnetApiError, "Повреждён файл"):
                    MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.state_path.write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(MathnetApiError, "Повреждён файл"):
            MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.opener.open.assert_not_called()

    def test_state_and_lock_symlinks_are_not_followed(self) -> None:
        """Символьная ссылка не позволяет читать или менять посторонний файл состояния."""

        self.state_dir.mkdir()
        target = self.state_dir.parent / "unrelated.json"
        target.write_text('{"keep": true}', encoding="utf-8")

        for name in ("mathnet_api_rate_limit.json", "mathnet_api_rate_limit.lock"):
            with self.subTest(name=name):
                path = self.state_dir / name
                path.unlink(missing_ok=True)
                path.symlink_to(target)

                try:
                    with self.assertRaises(MathnetApiError):
                        MathnetApiClient(self.state_dir).get_response(LIST_URL)

                finally:
                    path.unlink()

        self.assertEqual(target.read_text(encoding="utf-8"), '{"keep": true}')
        self.opener.open.assert_not_called()

    def test_failed_state_write_prevents_request(self) -> None:
        """Не начинать запрос, если защитную паузу нельзя надёжно записать."""

        with (
            patch("src.collect.mathnet_api.os.replace", side_effect=OSError("disk unavailable")),
            self.assertRaises(MathnetApiError) as context,
        ):
            MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.assertEqual(context.exception.error_code, "state_error")
        self.assertIsNone(context.exception.http_status)
        self.opener.open.assert_not_called()
        self.assertEqual(list(self.state_dir.glob(".mathnet-rate-*")), [])

    def test_http_errors_do_not_retry(self) -> None:
        """Отказ сервера сохраняет задержку и не порождает дополнительных запросов."""

        for status in (301, 302, 401, 403, 404, 500, 503):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = self._http_error(status)

                with self.assertRaisesRegex(MathnetApiError, f"HTTP {status}") as context:
                    MathnetApiClient(self.state_dir).get_response(LIST_URL)

                self.assertEqual(context.exception.http_status, status)
                self.assertEqual(context.exception.error_code, "http_error")
                self.opener.open.assert_called_once()
                self.assertEqual(self._read_state()["next_request_at"], self.clock.time() + 60)

    def test_transport_failures_preserve_delay(self) -> None:
        """Сетевые ошибки и оборванный ответ не повторяются автоматически."""

        for exception, error_code in (
            (urllib.error.URLError("unavailable"), "network_error"),
            (TimeoutError("timeout"), "timeout"),
            (urllib.error.URLError(TimeoutError("timeout")), "timeout"),
            (http.client.IncompleteRead(b"partial"), "network_error"),
        ):
            with self.subTest(exception=exception):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = exception

                with self.assertRaisesRegex(MathnetApiError, "Автоматического повтора нет") as context:
                    MathnetApiClient(self.state_dir).get_response(LIST_URL)

                self.assertEqual(context.exception.error_code, error_code)
                self.assertIsNone(context.exception.http_status)
                self.opener.open.assert_called_once()
                self.assertEqual(self._read_state()["next_request_at"], self.clock.time() + 60)

    def test_retry_after_seconds_persists_across_clients(self) -> None:
        """Ответ 429 откладывает следующий запрос, но не повторяет текущий."""

        self.opener.open.side_effect = self._http_error(429, "180")

        with self.assertRaisesRegex(MathnetApiError, "HTTP 429") as context:
            MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.assertEqual(context.exception.http_status, 429)
        self.assertEqual(context.exception.error_code, "http_error")
        self.assertEqual(self._read_state()["next_request_at"], 1180.0)
        self.opener.open.assert_called_once()
        self.opener.open.side_effect = None
        MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.assertEqual(self.clock.sleeps, [60.0, 60.0, 60.0])

    def test_retry_after_http_date_is_respected(self) -> None:
        """Дата в Retry-After увеличивает сохраняемую паузу."""

        self.opener.open.side_effect = self._http_error(429, formatdate(1180.0, usegmt=True))

        with self.assertRaises(MathnetApiError):
            MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.assertEqual(self._read_state()["next_request_at"], 1180.0)

    def test_invalid_retry_after_keeps_minimum_delay(self) -> None:
        """Некорректные либо короткие задержки сервера не уменьшают минуту."""

        for value in (None, "broken", "-1", "20", "9" * 400):
            with self.subTest(value=value):
                self.opener.open.side_effect = self._http_error(429, value)

                with self.assertRaises(MathnetApiError):
                    MathnetApiClient(self.state_dir).get_response(LIST_URL)

                self.assertEqual(self._read_state()["next_request_at"], self.clock.time() + 60)

    def test_unexpected_success_status_or_final_url_is_rejected(self) -> None:
        """Частичный ответ и изменённый конечный URL не признаются успешной загрузкой."""

        for status in (204, 206, 304):
            with self.subTest(status=status):
                self.response.status = status

                with self.assertRaisesRegex(MathnetApiError, f"HTTP {status}"):
                    MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.response.status = 200
        self.response.geturl.return_value = "https://other.example/file"

        with self.assertRaisesRegex(MathnetApiError, "Конечный URL изменился"):
            MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.response.read.assert_not_called()

    def test_response_size_is_bounded_without_content_length(self) -> None:
        """Даже без заголовка размер ограничен одним дополнительным байтом."""

        self.response.read.return_value = b"12345"

        with self.assertRaisesRegex(MathnetApiError, "превышает лимит") as context:
            MathnetApiClient(self.state_dir, max_response_bytes=4).get_response(LIST_URL)

        self.assertEqual(context.exception.error_code, "invalid_response")
        self.assertIsNone(context.exception.http_status)
        self.response.read.assert_called_once_with(5)

    def test_compressed_response_is_not_misread_as_plain_text(self) -> None:
        """Неподдерживаемое сжатие явно отклоняется до чтения и сохранения ответа."""

        self.response.headers["Content-Encoding"] = "gzip"

        with self.assertRaisesRegex(MathnetApiError, "неподдерживаемое Content-Encoding"):
            MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.response.read.assert_not_called()

    def test_oversized_content_length_stops_before_reading(self) -> None:
        """Заведомо большой ответ не считывается в память."""

        self.response.headers["Content-Length"] = "20"

        with self.assertRaisesRegex(MathnetApiError, "превышает лимит"):
            MathnetApiClient(self.state_dir, max_response_bytes=4).get_response(LIST_URL)

        self.response.read.assert_not_called()

    def test_content_length_must_match_body(self) -> None:
        """Недостающие или лишние байты нельзя сохранить как полный ответ."""

        self.response.headers["Content-Length"] = "20"

        with self.assertRaisesRegex(MathnetApiError, "Неполный или некорректный ответ"):
            MathnetApiClient(self.state_dir).get_response(LIST_URL)

    def test_invalid_or_duplicate_content_length_is_rejected(self) -> None:
        """Некорректный и неоднозначный размер останавливает получение ответа."""

        for values in (("-1",), ("a",), ("+2",), ("2, 2",), ("2", "2")):
            with self.subTest(values=values):
                self.response.headers = Message()

                for value in values:
                    self.response.headers["Content-Length"] = value

                with self.assertRaisesRegex(MathnetApiError, "некорректный Content-Length"):
                    MathnetApiClient(self.state_dir).get_response(LIST_URL)

        self.response.read.assert_not_called()

    def test_redirect_handler_raises_without_followup(self) -> None:
        """Обработчик перенаправления всегда возвращает ошибку вместо нового запроса."""

        # Фиксированный HTTPS-адрес; Request лишь создаётся и не отправляется.
        request = urllib.request.Request(LIST_URL)  # noqa: S310

        with self.assertRaises(urllib.error.HTTPError) as context:
            _RejectRedirect().redirect_request(
                request, None, 302, "moved", Message(), DEFAULT_API_URL + "/texts/mm1234"
            )

        context.exception.close()
        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
