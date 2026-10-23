"""Проверки команд сбора Иоффе без реальной сети и рабочих реестров."""

from __future__ import annotations

import io
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.download_ioffe import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_STATE_DIR,
    BudgetedIoffeClient,
    RequestBudgetReached,
    main,
)
from src.collect.ioffe_archive import write_json
from src.collect.ioffe_http import IoffeError, IoffeTransientNetworkError
from src.collect.continuous_collection import _attempt_result, _command


class IoffeDownloadCommandTests(unittest.TestCase):
    """Проверять параметры, явное разрешение сети и предсказуемые коды выхода."""

    def setUp(self) -> None:
        """Подменить все операции получения данных и захватить вывод команды."""

        client_patch = patch("scripts.download_ioffe.BudgetedIoffeClient")
        archive_patch = patch("scripts.download_ioffe.IoffeArchive")
        collector_patch = patch("scripts.download_ioffe.IoffeCollector")
        writer_patch = patch("scripts.download_ioffe.write_json")
        stdout_patch = patch("sys.stdout", new_callable=io.StringIO)
        stderr_patch = patch("sys.stderr", new_callable=io.StringIO)
        self.client_class = client_patch.start()
        self.archive_class = archive_patch.start()
        self.collector_class = collector_patch.start()
        self.writer = writer_patch.start()
        self.stdout = stdout_patch.start()
        self.stderr = stderr_patch.start()

        for active_patch in (client_patch, archive_patch, collector_patch, writer_patch, stdout_patch, stderr_patch):
            self.addCleanup(active_patch.stop)

        self.collector = self.collector_class.return_value
        self.client_class.return_value.request_count = 4
        self.collector.completed = 1
        self.collector.pdf_count = 1
        self.collector.metadata_only_count = 0
        self.collector.article_urls.return_value = ["https://journals.ioffe.ru/articles/10"]

    def test_probe_defaults_to_offline_https_and_twenty_requests(self) -> None:
        """Без отдельного флага архив не должен разрешать сеть, а схема остаётся HTTPS."""

        self.assertEqual(main(["probe"]), 0)
        self.client_class.assert_called_once_with(max_requests=20, timeout=30.0)
        self.archive_class.assert_called_once_with(
            DEFAULT_OUTPUT_DIR, DEFAULT_STATE_DIR, self.client_class.return_value,
            allow_network=False,
        )
        self.collector_class.assert_called_once_with(
            self.archive_class.return_value, DEFAULT_STATE_DIR, "https://journals.ioffe.ru",
        )
        self.collector.prepare.assert_called_once_with()
        self.collector.article_urls.assert_not_called()

    def test_all_collection_commands_are_offline_by_default(self) -> None:
        """Статья, выпуск и журнал не должны неявно включать сетевой режим."""

        for command in (["article", "10"], ["issue", "100"], ["journal", "1"]):
            with self.subTest(command=command):
                self.assertEqual(main(command), 0)
                self.assertFalse(self.archive_class.call_args.kwargs["allow_network"])

    def test_explicit_network_http_directories_and_zero_budget_are_forwarded(self) -> None:
        """Явно выбранные параметры должны передаваться без изменения лимита или схемы."""

        result = main([
            "probe", "--allow-network", "--origin", "http://journals.ioffe.ru",
            "--output-dir", "raw", "--state-dir", "state", "--max-requests", "0",
            "--timeout", "45",
        ])

        self.assertEqual(result, 0)
        self.client_class.assert_called_once_with(max_requests=0, timeout=45.0)
        self.archive_class.assert_called_once_with(
            Path("raw"), Path("state"), self.client_class.return_value, allow_network=True,
        )
        self.collector_class.assert_called_once_with(
            self.archive_class.return_value, Path("state"), "http://journals.ioffe.ru",
        )

    def test_journal_zero_limits_are_forwarded(self) -> None:
        """Ноль должен оставаться запросом всех выпусков и статей, а не заменяться значением по умолчанию."""

        self.assertEqual(main(["journal", "2", "--max-issues", "0", "--max-articles", "0"]), 0)
        self.collector.article_urls.assert_called_once_with(
            journal_id=2, issue_id=None, article_id=None, max_issues=0, max_articles=0,
        )
        self.collector.collect_article.assert_called_once_with("https://journals.ioffe.ru/articles/10")

    def test_invalid_arguments_stop_before_client_creation(self) -> None:
        """Неверные ID, лимиты и посторонние адреса должны отклоняться до любых обращений."""

        commands = (
            ["article", "0"], ["article", "-1"], ["article", "../10"],
            ["issue", "0"], ["journal", "6"], ["issue", "1", "--max-articles", "-1"],
            ["journal", "1", "--max-issues", "-2"], ["probe", "--max-requests", "-1"],
            ["probe", "--origin", "https://example.org"],
        )

        for arguments in commands:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit) as exception:
                    main(arguments)

                self.assertEqual(exception.exception.code, 2)

        self.client_class.assert_not_called()
        self.archive_class.assert_not_called()

    def test_budget_exhaustion_is_normal_stop(self) -> None:
        """Остановка по бюджету запросов должна сохранять штатный нулевой код."""

        self.collector.collect_article.side_effect = RequestBudgetReached

        self.assertEqual(main(["article", "10", "--allow-network"]), 0)
        self.assertIn("Достигнут лимит", self.stdout.getvalue())
        self.collector.collect_article.assert_called_once()

    def test_expected_errors_return_one_without_retry(self) -> None:
        """Ошибки сети, файлов и аргументов не должны превращаться в успешный сбор."""

        for failure in (IoffeError("HTTP 403"), OSError("Нет места"), ValueError("Неверный ответ")):
            with self.subTest(failure=failure):
                self.collector.prepare.reset_mock()
                self.collector.prepare.side_effect = failure

                self.assertEqual(main(["probe"]), 1)
                self.collector.prepare.assert_called_once()
                self.assertIn(str(failure), self.stderr.getvalue())

        self.collector.article_urls.assert_not_called()

    def test_keyboard_interrupt_returns_130(self) -> None:
        """Остановка пользователем должна оставаться отличимой от ошибки скачивания."""

        self.collector.collect_article.side_effect = KeyboardInterrupt

        self.assertEqual(main(["article", "10"]), 130)
        self.assertIn("сохранённые ответы", self.stdout.getvalue())

    def test_help_does_not_create_clients_or_archives(self) -> None:
        """Справка должна работать независимо от сети и существования кеша."""

        for arguments in (["--help"], ["journal", "--help"]):
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit) as exception:
                    main(arguments)

                self.assertEqual(exception.exception.code, 0)

        self.client_class.assert_not_called()
        self.archive_class.assert_not_called()

    def test_complete_run_writes_identical_last_and_named_summaries(self) -> None:
        """Успешный журнал оставляет два итога с идентификатором запуска и точной областью."""

        self.writer.side_effect = write_json

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            summary_path = state_dir / "runs" / "run-17.json"
            exit_code = main([
                "journal", "2", "--state-dir", str(state_dir), "--run-id", "run-17",
                "--summary-path", str(summary_path), "--max-requests", "0",
                "--max-articles", "0", "--max-issues", "0", "--origin", "http://journals.ioffe.ru",
            ])
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            last_summary = json.loads((state_dir / "last_run.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary, last_summary)
        self.assertEqual(summary["run_id"], "run-17")
        self.assertEqual(summary["source_id"], "ioffe")
        self.assertEqual(summary["command"], "journal")
        self.assertEqual(summary["selected_journal"], 2)
        self.assertEqual(summary["origin"], "http://journals.ioffe.ru")
        self.assertIsNone(summary["selected_volume"])
        self.assertFalse(summary["metadata_only"])
        self.assertEqual(summary["max_requests"], 0)
        self.assertEqual(summary["max_articles"], 0)
        self.assertEqual(summary["max_issues"], 0)
        self.assertEqual(summary["status"], "complete")
        self.assertFalse(summary["retryable_error"])
        self.assertEqual(summary["cards_processed"], 1)
        self.assertEqual(summary["cards_with_pdf"], 1)

    def test_early_client_error_writes_zero_counters_and_named_summary(self) -> None:
        """Ошибка настройки до создания сборщика всё равно оставляет машинный итог."""

        self.client_class.side_effect = ValueError("timeout должен быть положительным")

        self.assertEqual(main([
            "probe", "--state-dir", "state", "--run-id", "early", "--summary-path", "runs/early.json",
        ]), 1)
        self.assertEqual([call.args[0] for call in self.writer.call_args_list], [
            Path("state/last_run.json"), Path("runs/early.json"),
        ])
        summary = self.writer.call_args.args[1]
        self.assertEqual(summary["run_id"], "early")
        self.assertEqual(summary["status"], "error")
        self.assertFalse(summary["retryable_error"])
        self.assertEqual(summary["network_requests"], 0)
        self.assertEqual(summary["cards_processed"], 0)

    def test_only_typed_network_error_is_marked_for_retry(self) -> None:
        """Текст ошибки и даже обычный TimeoutError не подменяют транспортный тип."""

        for failure, retryable in (
            (IoffeTransientNetworkError("DNS"), True),
            (IoffeError("HTTP 403"), False),
            (IoffeError("HTTP 429"), False),
            (IoffeError("HTTP 503"), False),
            (IoffeError("timeout DNS"), False),
            (TimeoutError("disk"), False),
        ):
            with self.subTest(failure=failure):
                self.collector.prepare.side_effect = failure
                self.assertEqual(main(["probe", "--run-id", "failed"]), 1)
                summary = self.writer.call_args.args[1]
                self.assertEqual(summary["status"], "error")
                self.assertEqual(summary["retryable_error"], retryable)
                self.assertEqual(summary["error"], str(failure))

    def test_limited_runs_are_not_complete(self) -> None:
        """Все ограничения объёма отражаются отдельными причинами остановки."""

        for command, status in (
            (["journal", "1", "--max-articles", "1"], "article_limit"),
            (["issue", "10", "--max-articles", "1"], "article_limit"),
            (["journal", "1", "--max-articles", "0", "--max-issues", "1"], "issue_limit"),
        ):
            with self.subTest(command=command):
                self.assertEqual(main(command), 0)
                self.assertEqual(self.writer.call_args.args[1]["status"], status)

        self.collector.collect_article.side_effect = RequestBudgetReached
        self.assertEqual(main(["article", "10"]), 0)
        self.assertEqual(self.writer.call_args.args[1]["status"], "request_limit")

    def test_probe_and_interrupt_always_write_summary(self) -> None:
        """Проверка robots и ручная остановка также видны непрерывному запускателю."""

        self.assertEqual(main(["probe", "--run-id", "probe"]), 0)
        self.assertEqual(self.writer.call_args.args[1]["status"], "complete")
        self.collector.prepare.side_effect = KeyboardInterrupt
        self.assertEqual(main(["probe", "--run-id", "interrupt"]), 130)
        summary = self.writer.call_args.args[1]
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(summary["run_id"], "interrupt")
        self.assertFalse(summary["retryable_error"])

    def test_summary_write_failure_disables_retry_in_remaining_copy(self) -> None:
        """Ошибка записи last_run не мешает попробовать отдельный итог, но запрещает сетевой повтор."""

        self.collector.prepare.side_effect = IoffeTransientNetworkError("network")
        self.writer.side_effect = [OSError("disk"), None]

        self.assertEqual(main(["probe", "--summary-path", "runs/failed.json"]), 1)
        self.assertEqual(self.writer.call_count, 2)
        summary = self.writer.call_args.args[1]
        self.assertEqual(summary["status"], "error")
        self.assertFalse(summary["retryable_error"])
        self.assertIn("disk", summary["error"])

    def test_runner_command_and_real_summary_have_matching_protocol(self) -> None:
        """Все пять журналов передают запускателю итог без ложного blocked из-за типов или лимитов."""

        self.writer.side_effect = write_json
        queue = {"ioffe_origin": "http://journals.ioffe.ru"}
        outcomes = (
            (None, "complete", 0),
            (IoffeTransientNetworkError("timeout"), "retry_wait", 1),
            (IoffeError("HTTP 403"), "blocked", 1),
            (KeyboardInterrupt(), "interrupted", 130),
        )

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory).resolve()

            with patch("scripts.download_ioffe.DEFAULT_STATE_DIR", state_dir):
                for journal_id in range(1, 6):
                    for failure, expected_status, expected_code in outcomes:
                        with self.subTest(journal_id=journal_id, expected_status=expected_status):
                            task = {
                                "source_id": "ioffe", "last_run_id": f"ioffe-{journal_id}-{expected_status}",
                                "volume": None, "journal_id": journal_id, "metadata_only": False,
                            }
                            summary_path = state_dir / "runs" / f"{task['last_run_id']}.json"
                            command = _command(task, queue, summary_path, 60.0)
                            self.collector.prepare.side_effect = failure
                            exit_code = main(command[4:])
                            status, error = _attempt_result(task, queue, summary_path, exit_code)

                            self.assertEqual(exit_code, expected_code)
                            self.assertEqual(status, expected_status, error)
                            summary = json.loads(summary_path.read_text(encoding="utf-8"))
                            self.assertIs(type(summary["selected_journal"]), int)
                            self.assertIs(type(summary["max_articles"]), int)
                            self.assertEqual(summary["max_articles"], 0)
                            self.assertEqual(summary["max_issues"], 0)


class IoffeRequestBudgetTests(unittest.TestCase):
    """Проверять счётчик запросов независимо от подмены командного интерфейса."""

    def test_positive_budget_blocks_next_call_before_network(self) -> None:
        """После исчерпания лимита базовый HTTP-клиент больше не вызывается."""

        with (
            patch("src.collect.ioffe_http.IoffeClient.get_response") as request,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            client = BudgetedIoffeClient(max_requests=2, timeout=30)
            client.get_response("https://journals.ioffe.ru/robots.txt")
            client.get_response("https://journals.ioffe.ru/articles/10")

            with self.assertRaises(RequestBudgetReached):
                client.get_response("https://journals.ioffe.ru/articles/20")

        self.assertEqual(request.call_count, 2)
        self.assertEqual(client.request_count, 2)

    def test_zero_budget_is_unlimited(self) -> None:
        """Нулевой бюджет не должен запрещать первый запрос или устанавливать скрытый лимит."""

        with (
            patch("src.collect.ioffe_http.IoffeClient.get_response") as request,
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            client = BudgetedIoffeClient(max_requests=0, timeout=30)

            for article_id in range(1, 25):
                client.get_response(f"https://journals.ioffe.ru/articles/{article_id}")

        self.assertEqual(request.call_count, 24)

    def test_invalid_timeout_stops_before_archive_creation(self) -> None:
        """Неконечный или неположительный тайм-аут не должен разрешать запуск архива."""

        with (
            patch("scripts.download_ioffe.IoffeArchive") as archive,
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            for timeout in ("0", "-1", "nan", "inf"):
                with self.subTest(timeout=timeout):
                    self.assertEqual(main(["probe", "--timeout", timeout]), 1)

        archive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
