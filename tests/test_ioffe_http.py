"""Проверки транспорта Иоффе без сети и настоящих минутных ожиданий."""

from __future__ import annotations

import fcntl
import http.client
import io
import json
import socket
import ssl
import tempfile
import unittest
import urllib.error
import urllib.request

from email.message import Message
from email.utils import formatdate
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from src.collect.ioffe_http import (
    MAX_FORM_BODY_BYTES, IoffeClient, IoffeError, IoffeTransientNetworkError,
    _RejectRedirect, validate_ioffe_url,
)

URL = "https://journals.ioffe.ru/articles/51681"


class IoffeHttpTests(unittest.TestCase):
    """Проверять ограничения адресов, частоту и сохранение ошибок HTTP."""

    def setUp(self) -> None:
        """Подготовить поддельные часы, HTTP-ответ и временное состояние."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name)
        self.timestamp = 1000.0
        self.sleeps: list[float] = []
        self.opener = MagicMock()
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.getcode.return_value = 200
        self.response.geturl.return_value = URL
        self.response.read.return_value = b"article"
        self.response.headers = Message()
        self.response.headers["Content-Type"] = "text/html"
        self.opener.open.return_value = self.response

        for patcher in (
            patch("src.collect.ioffe_http.urllib.request.build_opener", return_value=self.opener),
            patch("src.collect.ioffe_http.time.time", side_effect=self._now),
            patch("src.collect.ioffe_http.time.monotonic", side_effect=self._now),
            patch("src.collect.ioffe_http.time.sleep", side_effect=self._sleep),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _now(self) -> float:
        """Вернуть единое время поддельных часов."""

        return self.timestamp

    def _sleep(self, seconds: float) -> None:
        """Продвинуть часы без блокировки тестового процесса."""

        self.sleeps.append(seconds)
        self.timestamp += seconds

    def _state(self) -> dict[str, object]:
        """Прочитать сохранённое состояние ограничения частоты."""

        return json.loads((self.state_dir / "ioffe_rate_limit.json").read_text())

    def test_public_paths_and_ports(self) -> None:
        """Известные страницы разрешены для HTTP и HTTPS на стандартных портах."""

        for authority in ("http://journals.ioffe.ru", "https://journals.ioffe.ru:443", "http://journals.ioffe.ru:80"):
            for path in ("/", "/robots.txt", "/journals/5", "/issues/223", "/articles/12", "/articles/viewPDF/12"):
                with self.subTest(url=authority + path):
                    validate_ioffe_url(authority + path)

    def test_unsafe_urls_fail_before_network(self) -> None:
        """Сторонние серверы, служебные пути и неоднозначные URL не доходят до транспорта."""

        for url in (
            "https://journals.ioffe.ru.evil.org/", "https://user@journals.ioffe.ru/",
            "https://journals.ioffe.ru:80/", "ftp://journals.ioffe.ru/", "https://journals.ioffe.ru/../",
            "https://journals.ioffe.ru/%2f", "https://journals.ioffe.ru/articles/12?", URL + "#",
            "https://journals.ioffe.ru/journals/6", "https://journals.ioffe.ru/ajax/index",
            "https://journals.ioffe.ru/articles/12/", "\n" + URL, URL + "\\",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                IoffeClient(self.state_dir).get_response(url)

        self.opener.open.assert_not_called()

    def test_snapshot_and_shared_lock(self) -> None:
        """Ответ сохраняет безопасные заголовки, а запрос честно называет сборщик."""

        self.response.headers["Set-Cookie"] = "secret"
        self.response.headers["Content-Length"] = "7"

        with patch("src.collect.ioffe_http.fcntl.flock") as flock:
            snapshot = IoffeClient(self.state_dir).get_response(URL)

        self.assertEqual(snapshot.body, b"article")
        self.assertNotIn(("set-cookie", "secret"), snapshot.headers)
        self.assertEqual([call.args[1] for call in flock.call_args_list], [fcntl.LOCK_EX, fcntl.LOCK_UN])
        self.assertEqual(self._state(), {"next_request_at": 1060.0, "in_flight": False, "minimum_delay_seconds": 60.0})
        request = self.opener.open.call_args.args[0]
        self.assertTrue(request.get_header("User-agent").startswith("NIR-corpus-bot/"))
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertIsNone(request.get_header("Content-type"))
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)

    def test_form_request_preserves_exact_body_and_shared_lock(self) -> None:
        """Форма передаётся методом POST без изменения байтов и под общей блокировкой."""

        body = b"a=select&issues_lang=r&volume=169"

        with patch("src.collect.ioffe_http.fcntl.flock") as flock:
            snapshot = IoffeClient(self.state_dir).post_form_response(URL, body)

        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, body)
        self.assertEqual(request.get_header("Content-type"), "application/x-www-form-urlencoded")
        self.assertTrue(request.get_header("User-agent").startswith("NIR-corpus-bot/"))
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)
        self.assertEqual(snapshot.body, b"article")
        self.assertEqual(snapshot.requested_url, URL)
        self.assertEqual([call.args[1] for call in flock.call_args_list], [fcntl.LOCK_EX, fcntl.LOCK_UN])
        self.assertEqual(self._state()["next_request_at"], 1060.0)

    def test_form_validates_body_and_url_before_lock_or_network(self) -> None:
        """Пустая, слишком большая или нетипизированная форма не меняет состояние запросов."""

        client = IoffeClient(self.state_dir)

        with patch("src.collect.ioffe_http.fcntl.flock") as flock:
            for body in (b"", b"x" * (MAX_FORM_BODY_BYTES + 1)):
                with self.subTest(body_length=len(body)), self.assertRaises(ValueError):
                    client.post_form_response(URL, body)

            for invalid_body in (None, "a=select", bytearray(b"a=select")):
                with self.subTest(body_type=type(invalid_body)), self.assertRaises(TypeError):
                    client.post_form_response(URL, cast(bytes, invalid_body))

            with self.assertRaises(ValueError):
                client.post_form_response("https://example.com/", b"a=select")

        flock.assert_not_called()
        self.opener.open.assert_not_called()
        self.assertEqual(list(self.state_dir.iterdir()), [])

    def test_form_and_get_share_persistent_pause(self) -> None:
        """GET и POST разных экземпляров используют одну паузу без сброса состояния."""

        IoffeClient(self.state_dir).get_response(URL)
        IoffeClient(self.state_dir).post_form_response(URL, b"a=select&volume=169")
        IoffeClient(self.state_dir).get_response(URL)

        self.assertEqual(self.sleeps, [60.0, 60.0])
        self.assertEqual(self._state()["next_request_at"], 1180.0)
        self.assertEqual(
            [call.args[0].get_method() for call in self.opener.open.call_args_list],
            ["GET", "POST", "GET"],
        )

    def test_form_redirect_returns_snapshot_without_following(self) -> None:
        """HTTP 302 на форму сохраняется как ответ без автоматического GET по Location."""

        headers = Message()
        headers["Location"] = "https://example.com/"
        self.opener.open.side_effect = urllib.error.HTTPError(URL, 302, "test", headers, io.BytesIO(b"redirect"))

        snapshot = IoffeClient(self.state_dir).post_form_response(URL, b"a=select")

        self.assertEqual(snapshot.status_code, 302)
        self.assertEqual(snapshot.body, b"redirect")
        self.assertIn(("location", "https://example.com/"), snapshot.headers)
        self.assertEqual(snapshot.final_url, URL)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_form_network_error_reserves_pause_without_retry(self) -> None:
        """Сетевая ошибка POST оставляет минутную паузу и не повторяет отправку формы."""

        self.opener.open.side_effect = urllib.error.URLError("unavailable")

        with self.assertRaisesRegex(IoffeError, "Автоматического повтора нет"):
            IoffeClient(self.state_dir).post_form_response(URL, b"a=select")

        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self._state()["next_request_at"], 1060.0)

    def test_form_error_preserves_retry_after(self) -> None:
        """Retry-After при отказе на POST сохраняется даже при неполном теле ответа."""

        self.response.getcode.return_value = 503
        self.response.headers["Retry-After"] = "180"
        self.response.headers["Content-Length"] = "100"

        with self.assertRaisesRegex(IoffeError, "неполные"):
            IoffeClient(self.state_dir).post_form_response(URL, b"a=select")

        self.assertEqual(self._state()["next_request_at"], 1180.0)

    def test_form_rejects_oversized_response(self) -> None:
        """Для ответа на форму действует тот же предел размера, что для GET."""

        with patch("src.collect.ioffe_http.MAX_RESPONSE_BYTES", 3), self.assertRaisesRegex(IoffeError, "лимит"):
            IoffeClient(self.state_dir).post_form_response(URL, b"a=select")

    def test_pause_shared_by_new_clients_and_both_schemes(self) -> None:
        """Новый клиент и переход на HTTP не сбрасывают минутную паузу."""

        IoffeClient(self.state_dir).get_response(URL)
        http_url = URL.replace("https:", "http:")
        self.response.geturl.return_value = http_url
        IoffeClient(self.state_dir).get_response(http_url)

        self.assertEqual(self.sleeps, [60.0])

    def test_error_reserves_pause_without_retry(self) -> None:
        """Сетевая ошибка не вызывает повтор и сохраняет задержку следующего запуска."""

        self.opener.open.side_effect = urllib.error.URLError("unavailable")

        with self.assertRaisesRegex(IoffeError, "Автоматического повтора нет"):
            IoffeClient(self.state_dir).get_response(URL)

        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(self._state()["next_request_at"], 1060.0)

    def test_typed_transient_network_errors_are_distinguished(self) -> None:
        """Только явно временные типы сетевых ошибок дают внешнему запускателю право повтора."""

        failures = (
            TimeoutError("timeout"), socket.gaierror(-2, "DNS"),
            ConnectionResetError("reset"), ConnectionAbortedError("aborted"),
            BrokenPipeError("pipe"), http.client.RemoteDisconnected("remote"),
            http.client.IncompleteRead(b"part", 10),
        )

        for failure in failures:
            for wrapped in (False, True):
                with self.subTest(failure=type(failure).__name__, wrapped=wrapped):
                    self.opener.open.reset_mock()
                    self.opener.open.side_effect = urllib.error.URLError(failure) if wrapped else failure

                    with self.assertRaises(IoffeTransientNetworkError):
                        IoffeClient(self.state_dir).get_response(URL)

                    self.opener.open.assert_called_once()
                    self.assertFalse(self._state()["in_flight"])
                    self.assertEqual(self._state()["next_request_at"], self.timestamp + 60)

    def test_tls_generic_network_errors_and_error_strings_are_not_retryable(self) -> None:
        """Сертификат, отказ соединения и похожий на timeout текст не разрешают повтор."""

        failures = (
            ssl.SSLCertVerificationError("certificate"), ssl.SSLError("TLS"),
            ConnectionRefusedError("refused"), OSError("socket error"),
            http.client.BadStatusLine("invalid"), urllib.error.URLError("timed out"),
        )

        for failure in failures:
            for wrapped in (False, True):
                with self.subTest(failure=type(failure).__name__, wrapped=wrapped):
                    self.opener.open.side_effect = urllib.error.URLError(failure) if wrapped else failure

                    with self.assertRaises(IoffeError) as captured:
                        IoffeClient(self.state_dir).get_response(URL)

                    self.assertNotIsInstance(captured.exception, IoffeTransientNetworkError)

    def test_timeout_while_writing_rate_state_is_not_network_retry(self) -> None:
        """Типизированная ошибка файловой системы не должна выглядеть сетевым сбоем."""

        client = IoffeClient(self.state_dir)

        with (
            patch.object(client, "_write_state", side_effect=TimeoutError("disk")),
            self.assertRaises(IoffeError) as captured,
        ):
            client.get_response(URL)

        self.assertNotIsInstance(captured.exception, IoffeTransientNetworkError)
        self.opener.open.assert_not_called()

    def test_failed_rate_write_overrides_transient_network_classification(self) -> None:
        """Неудачная запись паузы после сбоя требует проверки диска, а не нового запроса."""

        client = IoffeClient(self.state_dir)
        self.opener.open.side_effect = TimeoutError("network")

        with (
            patch.object(client, "_write_state", side_effect=[None, TimeoutError("disk")]),
            self.assertRaises(IoffeError) as captured,
        ):
            client.get_response(URL)

        self.assertNotIsInstance(captured.exception, IoffeTransientNetworkError)
        self.assertIn("disk", str(captured.exception))

    def test_incomplete_read_during_success_is_transient_but_http_refusal_is_not(self) -> None:
        """Обрыв успешного ответа отличается от обрыва тела уже известного отказа HTTP."""

        for status in (200, 301, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.response.getcode.return_value = status
                self.response.read.side_effect = http.client.IncompleteRead(b"part", 10)

                with self.assertRaises(IoffeError) as captured:
                    IoffeClient(self.state_dir).get_response(URL)

                self.assertEqual(isinstance(captured.exception, IoffeTransientNetworkError), status == 200)

    def test_mismatched_bytes_are_not_retryable(self) -> None:
        """Неправильная длина уже полученных байтов остаётся ошибкой целостности."""

        self.response.headers["Content-Length"] = "10"

        with self.assertRaises(IoffeError) as captured:
            IoffeClient(self.state_dir).get_response(URL)

        self.assertNotIsInstance(captured.exception, IoffeTransientNetworkError)

    def test_http_errors_return_complete_evidence_and_retry_after(self) -> None:
        """HTTP 429 и 503 возвращаются снимком и продлевают общую паузу."""

        for status, retry_after in ((429, "180"), (503, formatdate(1180, usegmt=True))):
            with self.subTest(status=status):
                self.timestamp = 1000.0
                (self.state_dir / "ioffe_rate_limit.json").unlink(missing_ok=True)
                headers = Message()
                headers["Retry-After"] = retry_after
                headers["Content-Length"] = "4"
                self.opener.open.side_effect = urllib.error.HTTPError(URL, status, "test", headers, io.BytesIO(b"busy"))
                snapshot = IoffeClient(self.state_dir).get_response(URL)

                self.assertEqual(snapshot.status_code, status)
                self.assertEqual(snapshot.body, b"busy")
                self.assertEqual(self._state()["next_request_at"], 1180.0)

    def test_redirect_is_not_followed(self) -> None:
        """Обработчик перенаправления выбрасывает HTTPError до дополнительного запроса."""

        headers = Message()
        headers["Location"] = "https://example.com/"

        with self.assertRaises(urllib.error.HTTPError) as captured:
            _RejectRedirect().redirect_request(urllib.request.Request(URL), io.BytesIO(), 302, "test", headers, "https://example.com/")

        captured.exception.close()
        self.opener.open.side_effect = urllib.error.HTTPError(URL, 302, "test", headers, io.BytesIO(b"redirect"))
        snapshot = IoffeClient(self.state_dir).get_response(URL)
        self.assertEqual(snapshot.status_code, 302)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_partial_or_oversized_body_is_rejected(self) -> None:
        """Неполное тело и превышение лимита не становятся снимком ответа."""

        self.response.headers["Content-Length"] = "10"

        with self.assertRaisesRegex(IoffeError, "неполные"):
            IoffeClient(self.state_dir).get_response(URL)

        del self.response.headers["Content-Length"]

        with patch("src.collect.ioffe_http.MAX_RESPONSE_BYTES", 3), self.assertRaisesRegex(IoffeError, "лимит"):
            IoffeClient(self.state_dir).get_response(URL)

    def test_duplicate_lengths_and_invalid_parameters(self) -> None:
        """Неоднозначный размер ответа и некорректные параметры не допускаются."""

        self.response.headers["Content-Length"] = "7"
        self.response.headers["Content-Length"] = "7"

        with self.assertRaisesRegex(IoffeError, "Content-Length"):
            IoffeClient(self.state_dir).get_response(URL)

        for delay in (0.0, 59.0, float("nan"), float("inf")):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                IoffeClient(self.state_dir, delay_seconds=delay)

        for timeout in (0.0, -1.0, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                IoffeClient(self.state_dir, timeout=timeout)

    def test_crash_state_and_corrupt_state(self) -> None:
        """Незавершённый запрос требует защитной паузы, повреждённое состояние останавливает сеть."""

        state_path = self.state_dir / "ioffe_rate_limit.json"
        state_path.write_text('{"next_request_at": 900, "in_flight": true}')
        IoffeClient(self.state_dir).get_response(URL)
        self.assertEqual(self.sleeps, [60.0])
        state_path.write_text('{"next_request_at": true, "in_flight": false}')
        self.opener.open.reset_mock()

        with self.assertRaisesRegex(IoffeError, "Повреждено"):
            IoffeClient(self.state_dir).get_response(URL)

        self.opener.open.assert_not_called()

    def test_in_flight_is_written_before_open_and_pause_after_completion(self) -> None:
        """До передачи записан in_flight; минута отсчитывается после получения тела."""

        def open_response(*arguments: object, **keywords: object) -> MagicMock:
            """Проверить аварийное состояние и имитировать пятисекундный ответ."""

            self.assertTrue(self._state()["in_flight"])
            self.timestamp += 5

            return self.response

        self.opener.open.side_effect = open_response
        IoffeClient(self.state_dir).get_response(URL)
        self.assertEqual(self._state()["next_request_at"], 1065.0)
        IoffeClient(self.state_dir).get_response(URL)
        self.assertEqual(self.sleeps, [60.0])

    def test_retry_after_survives_incomplete_error_body(self) -> None:
        """Даже при обрыве тела HTTP 503 необходимо сохранить серверную паузу."""

        self.response.getcode.return_value = 503
        self.response.headers["Retry-After"] = "180"
        self.response.headers["Content-Length"] = "100"

        with self.assertRaises(IoffeError):
            IoffeClient(self.state_dir).get_response(URL)

        self.assertEqual(self._state()["next_request_at"], 1180.0)

    def test_raised_delay_is_persistent_for_other_clients(self) -> None:
        """Crawl-delay, увеличенный после robots.txt, действует уже на следующий запрос."""

        client = IoffeClient(self.state_dir)
        client.get_response(URL)
        client.set_minimum_delay(100.0)
        self.assertEqual(self._state()["next_request_at"], 1100.0)
        second = IoffeClient(self.state_dir)
        second.get_response(URL)
        self.assertEqual(self.sleeps, [60.0, 40.0])
        self.assertEqual(second.delay_seconds, 100.0)
        self.assertEqual(self._state()["next_request_at"], 1200.0)
        self.assertEqual(self._state()["minimum_delay_seconds"], 100.0)

    def test_raised_delay_keeps_retry_after_and_rejects_corrupt_state(self) -> None:
        """Повышение паузы не сокращает Retry-After и не перезаписывает повреждённое состояние."""

        state_path = self.state_dir / "ioffe_rate_limit.json"
        state_path.write_text('{"next_request_at": 2000, "in_flight": false}')
        client = IoffeClient(self.state_dir)
        client.set_minimum_delay(100.0)
        self.assertEqual(self._state()["next_request_at"], 2000)
        client.set_minimum_delay(60.0)
        self.assertEqual(client.delay_seconds, 100.0)
        state_path.write_text('{"next_request_at": 2000, "in_flight": false, "minimum_delay_seconds": true}')

        with self.assertRaisesRegex(IoffeError, "Повреждено"):
            client.set_minimum_delay(120.0)

        self.assertIs(self._state()["minimum_delay_seconds"], True)
        self.opener.open.assert_not_called()

    def test_raised_delay_validates_arguments_and_locks(self) -> None:
        """Изменение паузы использует ту же блокировку и не принимает обход лимита."""

        client = IoffeClient(self.state_dir)

        with patch("src.collect.ioffe_http.fcntl.flock") as flock:
            client.set_minimum_delay(120.0)

        self.assertEqual([call.args[1] for call in flock.call_args_list], [fcntl.LOCK_EX, fcntl.LOCK_UN])

        for delay in (0.0, 59.0, float("nan"), float("inf")):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                client.set_minimum_delay(delay)
