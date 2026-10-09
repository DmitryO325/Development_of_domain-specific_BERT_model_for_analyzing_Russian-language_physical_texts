"""Проверки одиночных команд Math-Net без сетевых запросов."""

from __future__ import annotations

import io
import unittest

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.download_mathnet import DEFAULT_OUTPUT_DIR, DEFAULT_STATE_DIR, main
from src.collect.mathnet_api import DEFAULT_API_URL, MathnetApiError


class MathnetDownloadCommandTests(unittest.TestCase):
    """Проверять маршруты, явное разрешение сети и обработку ошибок команды."""

    def setUp(self) -> None:
        """Подменить архив и клиент, не создавая реальные файлы или соединения."""

        client_patch = patch("scripts.download_mathnet.MathnetApiClient")
        archive_patch = patch("scripts.download_mathnet.MathnetArchive")
        stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        stderr_patch = patch("sys.stderr", new_callable=io.StringIO)

        self.client_class = client_patch.start()
        self.addCleanup(client_patch.stop)
        self.archive_class = archive_patch.start()
        self.addCleanup(archive_patch.stop)
        self.stdout = stdout_patch.start()
        self.addCleanup(stdout_patch.stop)
        self.stderr = stderr_patch.start()
        self.addCleanup(stderr_patch.stop)

        self.archive = self.archive_class.return_value
        self.archive.fetch.return_value = SimpleNamespace(
            body_path=Path("archive/response.bin"),
            metadata_path=Path("archive/response.http.json"),
            from_cache=True,
            data={"title": "Полный ответ не должен печататься"},
        )

    def test_default_probe_is_cache_only(self) -> None:
        """Первичная проверка без флага должна читать только точный URL из письма."""

        result = main(["probe"])

        self.assertEqual(result, 0)
        self.client_class.assert_not_called()
        self.archive_class.assert_called_once_with(DEFAULT_OUTPUT_DIR, DEFAULT_STATE_DIR, None)
        self.archive.fetch.assert_called_once_with(
            f"{DEFAULT_API_URL}/journals/list",
            kind="json",
            allow_network=False,
            refresh=False,
            encoding=None,
        )
        self.assertIn("сетевого запроса не было", self.stdout.getvalue())
        self.assertIn("archive/response.bin", self.stdout.getvalue())
        self.assertIn("archive/response.http.json", self.stdout.getvalue())
        self.assertNotIn("Полный ответ", self.stdout.getvalue())

    def test_all_commands_are_offline_by_default(self) -> None:
        """Ни одна подкоманда не должна разрешать сеть без отдельного флага."""

        commands = (
            ["journals"],
            ["articles", "tmf"],
            ["article", "tmf1234"],
            ["text", "tmf1234"],
        )

        for arguments in commands:
            with self.subTest(arguments=arguments):
                self.archive.fetch.reset_mock()

                self.assertEqual(main(arguments), 0)
                self.assertFalse(self.archive.fetch.call_args.kwargs["allow_network"])
                self.assertFalse(self.archive.fetch.call_args.kwargs["refresh"])

        self.client_class.assert_not_called()

    def test_example_routes_keep_exact_identifiers(self) -> None:
        """Команда должна использовать примеры письма без добавления префикса к ID."""

        routes = (
            (["journals"], "/journals/list", "json"),
            (["articles", "tmf"], "/journals/tmf", "json"),
            (["article", "tmf1234"], "/journals/tmf1234", "json"),
            (["article", "1234"], "/journals/1234", "json"),
            (["text", "tmf1234"], "/texts/tmf1234", "text"),
        )

        for arguments, path, kind in routes:
            with self.subTest(arguments=arguments):
                self.archive.fetch.reset_mock()

                self.assertEqual(main(arguments), 0)
                self.archive.fetch.assert_called_once_with(
                    f"{DEFAULT_API_URL}{path}",
                    kind=kind,
                    allow_network=False,
                    refresh=False,
                    encoding=None,
                )

    def test_nested_routes_are_selected_explicitly(self) -> None:
        """Вложенные маршруты должны включаться явно и не вызывать перебор адресов."""

        routes = (
            (["journals", "--route-style", "nested"], "/journals"),
            (["articles", "tmf", "--route-style", "nested"], "/journals/tmf/articles"),
            (
                ["article", "1234", "--route-style", "nested", "--journal-id", "tmf"],
                "/journals/tmf/articles/1234",
            ),
        )

        for arguments, path in routes:
            with self.subTest(arguments=arguments):
                self.archive.fetch.reset_mock()

                self.assertEqual(main(arguments), 0)
                self.archive.fetch.assert_called_once_with(
                    f"{DEFAULT_API_URL}{path}",
                    kind="json",
                    allow_network=False,
                    refresh=False,
                    encoding=None,
                )

    def test_nested_article_requires_journal_identifier(self) -> None:
        """Без ID журнала вложенный маршрут нельзя угадывать по ID статьи."""

        self.assertEqual(main(["article", "tmf1234", "--route-style", "nested"]), 1)
        self.assertIn("нужен --journal-id", self.stderr.getvalue())
        self.archive_class.assert_not_called()
        self.client_class.assert_not_called()

    def test_journal_identifier_is_not_silently_ignored(self) -> None:
        """ID журнала не должен молча отбрасываться в маршруте из примеров."""

        self.assertEqual(main(["article", "1234", "--journal-id", "tmf"]), 1)
        self.assertIn("только с --route-style nested", self.stderr.getvalue())
        self.archive_class.assert_not_called()

    def test_invalid_identifiers_are_rejected_before_access(self) -> None:
        """Пути, кодирование разделителей и неподтверждённые символы запрещены."""

        invalid_identifiers = (
            "",
            "../texts/tmf1234",
            "tmf/1234",
            "tmf%2F1234",
            "tmf\\1234",
            "tmf?paper=1234",
            "tmf#1234",
            "tmf 1234",
            "tmf\n1234",
            "тмф1234",
            "tmf_1234",
            "tmf-1234",
            "a" * 81,
        )

        for identifier in invalid_identifiers:
            for command in ("articles", "article", "text"):
                with self.subTest(identifier=identifier, command=command):
                    with self.assertRaises(SystemExit) as exception:
                        main([command, identifier])

                    self.assertEqual(exception.exception.code, 2)

        with self.assertRaises(SystemExit):
            main(["article", "1234", "--route-style", "nested", "--journal-id", "../tmf"])

        self.archive_class.assert_not_called()
        self.client_class.assert_not_called()

    def test_explicit_network_permission_creates_client(self) -> None:
        """Разрешённый запуск должен передавать тайм-аут и паузу сетевому клиенту."""

        self.archive.fetch.return_value.from_cache = False

        result = main([
            "probe", "--allow-network", "--timeout", "45", "--delay", "90",
            "--output-dir", "raw", "--state-dir", "state",
        ])

        self.assertEqual(result, 0)
        self.client_class.assert_called_once_with(
            Path("state"), timeout=45.0, delay_seconds=90.0,
        )
        self.archive_class.assert_called_once_with(
            Path("raw"), Path("state"), self.client_class.return_value,
        )
        self.assertTrue(self.archive.fetch.call_args.kwargs["allow_network"])
        self.assertIn("Один ответ получен из сети", self.stdout.getvalue())
        self.assertNotIn("сетевого запроса не было", self.stdout.getvalue())

    def test_network_permission_does_not_claim_request_on_cache_hit(self) -> None:
        """При разрешённой сети попадание в кеш всё равно не является загрузкой."""

        self.assertEqual(main(["probe", "--allow-network"]), 0)
        self.assertIn("сетевого запроса не было", self.stdout.getvalue())
        self.assertNotIn("получен из сети", self.stdout.getvalue())

    def test_common_options_before_command_are_preserved(self) -> None:
        """Подкоманда не должна перезаписывать ранее указанные общие параметры."""

        result = main([
            "--allow-network", "--refresh", "--timeout", "45", "--delay", "90",
            "--output-dir", "raw", "--state-dir", "state", "probe",
        ])

        self.assertEqual(result, 0)
        self.client_class.assert_called_once_with(
            Path("state"), timeout=45.0, delay_seconds=90.0,
        )
        self.archive_class.assert_called_once_with(
            Path("raw"), Path("state"), self.client_class.return_value,
        )
        self.assertTrue(self.archive.fetch.call_args.kwargs["allow_network"])
        self.assertTrue(self.archive.fetch.call_args.kwargs["refresh"])

    def test_refresh_requires_explicit_network_permission(self) -> None:
        """Обновление без разрешения сети должно завершаться до открытия архива."""

        self.assertEqual(main(["probe", "--refresh"]), 1)
        self.assertIn("--refresh требует", self.stderr.getvalue())
        self.archive_class.assert_not_called()
        self.client_class.assert_not_called()

    def test_refresh_is_forwarded_with_network_permission(self) -> None:
        """Флаг обновления должен передаваться архиву без автоматических повторов."""

        self.assertEqual(main(["probe", "--refresh", "--allow-network"]), 0)
        self.archive.fetch.assert_called_once_with(
            f"{DEFAULT_API_URL}/journals/list",
            kind="json",
            allow_network=True,
            refresh=True,
            encoding=None,
        )

    def test_explicit_text_encoding_is_forwarded(self) -> None:
        """Подтверждённая пользователем кодировка применяется только к тексту."""

        self.assertEqual(main(["text", "tmf1234", "--encoding", "windows-1251"]), 0)
        self.archive.fetch.assert_called_once_with(
            f"{DEFAULT_API_URL}/texts/tmf1234",
            kind="text",
            allow_network=False,
            refresh=False,
            encoding="windows-1251",
        )

    def test_unknown_options_do_not_access_archive(self) -> None:
        """Непредусмотренные обход, URL, кодировка JSON и смена probe запрещены."""

        commands = (
            ["probe", "--route-style", "nested"],
            ["journals", "--route-style", "auto"],
            ["article", "tmf1234", "--encoding", "windows-1251"],
            ["articles", "tmf", "--all"],
            ["authors", "18133"],
            ["probe", "--url", "https://example.org"],
        )

        for arguments in commands:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit) as exception:
                    main(arguments)

                self.assertEqual(exception.exception.code, 2)

        self.archive_class.assert_not_called()
        self.client_class.assert_not_called()

    def test_expected_errors_return_failure_without_retry(self) -> None:
        """Ошибки доступа, кеша и файлов не должны запускать повторный запрос."""

        for failure in (MathnetApiError("HTTP 403"), OSError("Нет файла"), ValueError("Нет кеша")):
            with self.subTest(failure=failure):
                self.archive.fetch.reset_mock()
                self.archive.fetch.side_effect = failure

                self.assertEqual(main(["probe"]), 1)
                self.archive.fetch.assert_called_once()
                self.assertIn(str(failure), self.stderr.getvalue())

        self.assertEqual(self.stdout.getvalue(), "")
        self.client_class.assert_not_called()

    def test_client_configuration_error_returns_failure(self) -> None:
        """Неверная настройка клиента не должна доходить до получения ответа."""

        self.client_class.side_effect = ValueError("Пауза должна быть не менее 60 секунд")

        self.assertEqual(main(["probe", "--allow-network", "--delay", "1"]), 1)
        self.assertIn("не менее 60 секунд", self.stderr.getvalue())
        self.archive_class.assert_not_called()

    def test_keyboard_interrupt_returns_standard_code(self) -> None:
        """Остановка пользователем должна сохранять обычный код завершения 130."""

        self.archive.fetch.side_effect = KeyboardInterrupt

        self.assertEqual(main(["probe"]), 130)
        self.archive.fetch.assert_called_once()
        self.assertIn("сохранённые ответы остаются", self.stdout.getvalue())

    def test_help_does_not_access_archive_or_network(self) -> None:
        """Справка должна быть доступна без кеша и инициализации клиента."""

        for arguments in (["--help"], ["probe", "--help"]):
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit) as exception:
                    main(arguments)

                self.assertEqual(exception.exception.code, 0)

        self.archive_class.assert_not_called()
        self.client_class.assert_not_called()


if __name__ == "__main__":
    unittest.main()
