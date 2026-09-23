"""Проверки клиента ОИЯИ без настоящих запросов и ожидания времени."""

from __future__ import annotations

import fcntl
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

from src.collect.jinr_api import (
    DEFAULT_API_URL,
    JinrApiClient,
    JinrApiError,
    _RejectRedirect,
)


class _FakeClock:
    """Часы, в которых сон мгновенно перемещает оба счётчика вперёд."""

    def __init__(self) -> None:
        """Начать с ненулевого календарного и монотонного времени."""

        self.timestamp = 1000.0
        self.monotonic_timestamp = 10.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        """Вернуть календарное время теста."""

        return self.timestamp

    def monotonic(self) -> float:
        """Вернуть монотонное время теста."""

        return self.monotonic_timestamp

    def advance(self, seconds: float) -> None:
        """Переместить часы вперёд, имитируя длительность операции."""

        self.timestamp += seconds
        self.monotonic_timestamp += seconds

    def sleep(self, seconds: float) -> None:
        """Запомнить паузу и выполнить её без реального ожидания."""

        self.sleeps.append(seconds)
        self.advance(seconds)


class JinrApiClientTests(unittest.TestCase):
    """Проверки частоты, безопасности адресов и остановки при сетевой ошибке."""

    def setUp(self) -> None:
        """Подготовить отдельное состояние, поддельный транспорт и часы."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.state_dir = Path(temporary_directory.name)
        self.clock = _FakeClock()
        self.opener = MagicMock()
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.read.return_value = b'{"name": "JINR"}'
        self.response.geturl.return_value = DEFAULT_API_URL
        self.response.status = 200
        self.response.headers = Message()
        self.response.headers["Content-Type"] = "application/hal+json"
        self.opener.open.return_value = self.response

        patches = (
            patch("src.collect.jinr_api.time.time", side_effect=self.clock.time),
            patch("src.collect.jinr_api.time.monotonic", side_effect=self.clock.monotonic),
            patch("src.collect.jinr_api.time.sleep", side_effect=self.clock.sleep),
            patch("src.collect.jinr_api.urllib.request.build_opener", return_value=self.opener),
        )

        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _read_state(self) -> dict[str, Any]:
        """Прочитать состояние клиента, сохранённое после обращения."""

        return json.loads((self.state_dir / "jinr_api_rate_limit.json").read_text())

    def _http_error(self, status: int, retry_after: str | None = None) -> urllib.error.HTTPError:
        """Создать HTTP-ошибку без соединения с сервером."""

        headers = Message()

        if retry_after is not None:
            headers["Retry-After"] = retry_after

        return urllib.error.HTTPError(DEFAULT_API_URL, status, "test", headers, None)

    def test_first_request_is_immediate_and_snapshot_excludes_secrets(self) -> None:
        """Первый запрос не ждёт, а снимок ответа не сохраняет cookie и токены."""

        self.response.headers["Set-Cookie"] = "session=secret"
        self.response.headers["Authorization"] = "secret"
        snapshot = JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(snapshot.body, b'{"name": "JINR"}')
        self.assertEqual(snapshot.headers, (("content-type", "application/hal+json"),))
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(snapshot.requested_url, DEFAULT_API_URL)
        self.assertEqual(self._read_state(), {"next_request_at": 1060.0, "in_flight": False})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.get_header("Accept"),
            "application/hal+json, application/json, application/pdf;q=0.9, */*;q=0.1",
        )
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)

    def test_delay_starts_after_response_and_persists_across_instances(self) -> None:
        """Новому экземпляру необходимо ждать минуту после завершения старого запроса."""

        starts: list[float] = []

        def open_response(*arguments: Any, **keywords: Any) -> MagicMock:
            """Запомнить начало запроса и имитировать пять секунд передачи."""

            starts.append(self.clock.time())
            self.clock.advance(5.0)
            return self.response

        self.opener.open.side_effect = open_response
        JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)
        JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(starts, [1000.0, 1065.0])
        self.assertEqual(self.clock.sleeps, [60.0])
        self.assertEqual(self._read_state()["next_request_at"], 1130.0)

    def test_multiple_calls_on_one_client_share_delay(self) -> None:
        """Повторный вызов того же клиента также выдерживает минуту."""

        client = JinrApiClient(self.state_dir)
        client.get_response(DEFAULT_API_URL)
        client.get_response(DEFAULT_API_URL)

        self.assertEqual(self.clock.sleeps, [60.0])
        self.assertEqual(self.opener.open.call_count, 2)

    def test_clock_jump_does_not_remove_in_process_delay(self) -> None:
        """Перевод календарных часов вперёд не сокращает паузу живого клиента."""

        client = JinrApiClient(self.state_dir)
        client.get_response(DEFAULT_API_URL)
        self.clock.timestamp += 500.0
        client.get_response(DEFAULT_API_URL)

        self.assertEqual(self.clock.sleeps, [60.0])

    def test_in_flight_state_requires_a_fresh_delay(self) -> None:
        """После аварийного завершения клиент добавляет минуту перед новым запросом."""

        state_path = self.state_dir / "jinr_api_rate_limit.json"
        state_path.write_text(json.dumps({"next_request_at": 900.0, "in_flight": True}))
        JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(self.clock.sleeps, [60.0])

    def test_http_errors_do_not_retry_and_preserve_delay(self) -> None:
        """Ответы 401, 403 и 503 останавливают вызов, сохраняя общую паузу."""

        for status in (401, 403, 503):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = self._http_error(status)

                with self.assertRaisesRegex(JinrApiError, f"HTTP {status}"):
                    JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

                self.assertEqual(self.opener.open.call_count, 1)
                self.assertEqual(self._read_state()["next_request_at"], self.clock.time() + 60.0)

    def test_network_failure_also_preserves_delay(self) -> None:
        """Ошибка DNS не вызывает повтор и учитывается в ограничении частоты."""

        self.opener.open.side_effect = urllib.error.URLError("DNS unavailable")

        with self.assertRaisesRegex(JinrApiError, "Автоматического повтора нет"):
            JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self._read_state()["next_request_at"], 1060.0)

    def test_retry_after_seconds_survives_restart(self) -> None:
        """HTTP 429 откладывает следующий запуск на указанное сервером число секунд."""

        self.opener.open.side_effect = self._http_error(429, "180")

        with self.assertRaisesRegex(JinrApiError, "HTTP 429"):
            JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(self._read_state()["next_request_at"], 1180.0)
        self.opener.open.side_effect = None
        JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(self.clock.sleeps, [60.0, 60.0, 60.0])

    def test_retry_after_http_date_is_respected(self) -> None:
        """Retry-After в виде HTTP-даты продлевает обычную минутную паузу."""

        self.opener.open.side_effect = self._http_error(429, formatdate(1180.0, usegmt=True))

        with self.assertRaises(JinrApiError):
            JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(self._read_state()["next_request_at"], 1180.0)

    def test_invalid_retry_after_keeps_minimum_delay(self) -> None:
        """Некорректный Retry-After не отменяет минутное ограничение."""

        self.opener.open.side_effect = self._http_error(429, "not a date")

        with self.assertRaises(JinrApiError):
            JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(self._read_state()["next_request_at"], 1060.0)

    def test_redirect_is_never_followed(self) -> None:
        """Обработчик перенаправления возвращает ошибку вместо нового Request."""

        # Фиксированный HTTPS-адрес; Request создаётся без отправки в сеть.
        request = urllib.request.Request(DEFAULT_API_URL)  # noqa: S310

        with self.assertRaises(urllib.error.HTTPError) as captured:
            _RejectRedirect().redirect_request(
                request, None, 302, "Found", Message(), "https://other.example/"
            )

        self.assertEqual(captured.exception.code, 302)
        self.opener.open.side_effect = captured.exception

        with self.assertRaisesRegex(JinrApiError, "Перенаправление не выполнено"):
            JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.opener.open.assert_called_once()

    def test_unsafe_urls_are_rejected_before_transport(self) -> None:
        """Чужой сервер, иная схема, выход из API и пользовательские данные запрещены."""

        unsafe_urls = (
            "http://pubrepo-api.jinr.ru/server/api",
            "file:///server/api",
            "https://other.example/server/api",
            "https://pubrepo-api.jinr.ru.evil.example/server/api",
            "https://user:password@pubrepo-api.jinr.ru/server/api",
            "https://pubrepo-api.jinr.ru:8443/server/api",
            "https://pubrepo-api.jinr.ru:bad/server/api",
            "https://pubrepo-api.jinr.ru/server/apix",
            "https://pubrepo-api.jinr.ru/server%2fapi",
            DEFAULT_API_URL + "#fragment",
            DEFAULT_API_URL + "/../login",
            DEFAULT_API_URL + "/%2e%2e/login",
            DEFAULT_API_URL + "/%252e%252e/login",
            DEFAULT_API_URL + "/%00login",
            DEFAULT_API_URL + "/\\login",
            DEFAULT_API_URL + "\n",
        )
        client = JinrApiClient(self.state_dir)

        for url in unsafe_urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                client.get_response(url)

        self.opener.open.assert_not_called()

    def test_invalid_delay_and_timeout_are_rejected(self) -> None:
        """Настройки с короткой паузой, бесконечностью и NaN недопустимы."""

        for delay in (0.0, -1.0, 59.9, float("nan"), float("inf")):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                JinrApiClient(self.state_dir, delay_seconds=delay)

        for timeout in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                JinrApiClient(self.state_dir, timeout=timeout)

    def test_corrupted_state_stops_before_transport(self) -> None:
        """Повреждённое ограничение частоты нельзя молча сбросить."""

        state_path = self.state_dir / "jinr_api_rate_limit.json"

        for text in ('{', '[]', '{"next_request_at": "bad", "in_flight": false}'):
            with self.subTest(text=text):
                state_path.write_text(text)

                with self.assertRaisesRegex(JinrApiError, "Повреждён файл"):
                    JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.opener.open.assert_not_called()

    def test_response_size_is_limited(self) -> None:
        """Слишком большой ответ обрывает обработку и сохраняет минутную паузу."""

        self.response.read.return_value = b"12345"

        with (
            patch("src.collect.jinr_api.MAX_RESPONSE_BYTES", 4),
            self.assertRaisesRegex(JinrApiError, "превышает лимит"),
        ):
            JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.response.read.assert_called_once_with(5)
        self.assertEqual(self._read_state()["next_request_at"], 1060.0)

    def test_get_json_requires_an_object(self) -> None:
        """JSON-массив и HTML вместо JSON не принимаются за объект API."""

        client = JinrApiClient(self.state_dir)
        self.assertEqual(client.get_json(DEFAULT_API_URL), {"name": "JINR"})

        for body in (b"[]", b"<html>error</html>", b"\xff"):
            with self.subTest(body=body):
                self.response.read.return_value = body

                with self.assertRaises(JinrApiError):
                    client.get_json(DEFAULT_API_URL)

    def test_truncated_json_and_pdf_are_rejected(self) -> None:
        """Усечённые ответы не становятся снимками даже при синтаксически верном JSON."""

        client = JinrApiClient(self.state_dir)

        for body in (b'{"ok": true}', b"%PDF-1.7\nshort"):
            with self.subTest(body=body):
                self.response.read.return_value = body
                self.response.headers["Content-Length"] = str(len(body) + 10)

                with self.assertRaisesRegex(JinrApiError, "Неполный или некорректный ответ"):
                    client.get_response(DEFAULT_API_URL)

                del self.response.headers["Content-Length"]
                self.assertEqual(self._read_state()["next_request_at"], self.clock.time() + 60.0)

    def test_matching_content_length_is_accepted(self) -> None:
        """Совпадение фактического и заявленного размера позволяет сохранить ответ."""

        body = b"%PDF-1.7\ncomplete"
        self.response.read.return_value = body
        self.response.headers["Content-Length"] = str(len(body))
        snapshot = JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(snapshot.body, body)
        self.assertIn(("content-length", str(len(body))), snapshot.headers)

    def test_invalid_content_length_is_rejected(self) -> None:
        """Нечисловой и отрицательный размер останавливает чтение ответа."""

        client = JinrApiClient(self.state_dir)

        for raw_length in ("bad", "-1", "1.5"):
            with self.subTest(raw_length=raw_length):
                self.response.headers["Content-Length"] = raw_length

                with self.assertRaisesRegex(JinrApiError, "Content-Length"):
                    client.get_response(DEFAULT_API_URL)

                del self.response.headers["Content-Length"]

        self.response.read.assert_not_called()

    def test_excessive_content_length_is_rejected_before_reading(self) -> None:
        """Заранее известный слишком большой ответ не читается в память."""

        self.response.headers["Content-Length"] = "5"

        with (
            patch("src.collect.jinr_api.MAX_RESPONSE_BYTES", 4),
            self.assertRaisesRegex(JinrApiError, "превышает лимит"),
        ):
            JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.response.read.assert_not_called()

    def test_request_holds_and_releases_interprocess_lock(self) -> None:
        """Каждый GET выполняется внутри исключительной файловой блокировки."""

        with patch("src.collect.jinr_api.fcntl.flock") as flock:
            JinrApiClient(self.state_dir).get_response(DEFAULT_API_URL)

        self.assertEqual(
            [call.args[1] for call in flock.call_args_list],
            [fcntl.LOCK_EX, fcntl.LOCK_UN],
        )


if __name__ == "__main__":
    unittest.main()
