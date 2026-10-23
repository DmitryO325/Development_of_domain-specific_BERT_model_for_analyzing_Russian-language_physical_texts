"""Проверки команды сбора журналов без реальных сетевых запросов."""

from __future__ import annotations

import io
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.download_journals import JournalClient, RequestBudgetReached, build_parser, main
from src.collect.continuous_collection import _attempt_result, _command
from src.collect.ioffe_http import IoffeError, IoffeTransientNetworkError
from src.collect.jetp import START_URL as JETP_START_URL
from src.collect.journal_sources import get_source
from tests.test_journal_collection import ARTICLE, _source


class DownloadJournalsTests(unittest.TestCase):
    """Проверять ограничения порций и честный итог незавершённого сбора."""

    def setUp(self) -> None:
        """Подменить транспорт, архив, источник и вывод команды."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name)
        self.client = MagicMock()
        self.client.request_count = 5
        self.collector = MagicMock()
        self.collector.completed = 1
        self.collector.new_records = 1
        self.collector.pdf_count = 1
        self.collector.deferred_issues_count = 0
        self.collector.deferred_entries_count = 0
        self.collector.deferred_issues = {}
        self.collector.article_limit_reached = False
        self.collector.archive.state_dir = self.state_dir

        for patcher in (
            patch("scripts.download_journals.STATE_ROOT", self.state_dir / "sources"),
            patch("scripts.download_journals.get_source", return_value=_source()),
            patch("scripts.download_journals.JournalClient", return_value=self.client),
            patch("scripts.download_journals.IoffeArchive"),
            patch("scripts.download_journals.JournalCollector", return_value=self.collector),
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_network_is_disabled_by_default(self) -> None:
        """Обычный вызов не даёт разрешения на обращения к сайту."""

        arguments = build_parser().parse_args(["crawl", "jetp"])
        self.assertFalse(arguments.allow_network)
        self.assertEqual(arguments.max_requests, 20)
        self.assertEqual(arguments.max_articles, 0)

    def test_probe_only_prepares_robots(self) -> None:
        """Проверка доступности не скачивает страницы статей."""

        self.assertEqual(main(["probe", "jetp"]), 0)
        self.collector.prepare.assert_called_once_with()
        self.collector.crawl.assert_not_called()
        self.collector.collect_article.assert_not_called()

    def test_crawl_passes_limits_and_start(self) -> None:
        """Явная точка начала и лимит статей доходят до сборщика."""

        self.assertEqual(main(["crawl", "jetp", "--max-articles", "2", "--start-url", ARTICLE]), 0)
        self.collector.crawl.assert_called_once_with(start_url=ARTICLE, max_articles=2)

    def test_selected_volume_is_passed_and_reported(self) -> None:
        """Режим одного тома отражён и в вызове сборщика, и в итоговой сводке."""

        self.assertEqual(main(["crawl", "jetp", "--volume", "169", "--max-articles", "2"]), 0)
        self.collector.crawl.assert_called_once_with(volume=169, max_articles=2)
        summary = json.loads((self.state_dir / "last_run.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["selected_volume"], 169)
        self.assertIn("Только том 169", summary["scope"])

    def test_volume_arguments_are_validated_before_collection(self) -> None:
        """Том несовместим с чужим источником, произвольным URL и нулевым номером."""

        for source in ("quantum", "jetp_letters", "ufn"):
            with self.subTest(source=source):
                self.assertEqual(main(["crawl", source, "--volume", "169"]), 1)

        self.collector.crawl.assert_not_called()
        self.client.get_response.assert_not_called()

        for options in (
            ["--volume", "0"], ["--volume", "-1"],
            ["--volume", "169", "--start-url", ARTICLE],
        ):
            with self.subTest(options=options), self.assertRaises(SystemExit):
                build_parser().parse_args(["crawl", "jetp", *options])

    def test_request_limit_is_not_claimed_as_full_archive(self) -> None:
        """Штатная остановка имеет отдельный статус вместо обещания полного архива."""

        self.collector.crawl.side_effect = RequestBudgetReached
        self.assertEqual(main(["crawl", "jetp"]), 0)
        self.assertIn('"status": "request_limit"', (self.state_dir / "last_run.json").read_text())

    def test_http_error_is_reported_without_retry(self) -> None:
        """Отказ доступа возвращает ненулевой код и не повторяет обход."""

        self.collector.crawl.side_effect = IoffeError("HTTP 403")
        self.assertEqual(main(["crawl", "jetp"]), 1)
        self.assertEqual(self.collector.crawl.call_count, 1)
        self.assertIn('"status": "error"', (self.state_dir / "last_run.json").read_text())

    def test_interrupt_retains_summary(self) -> None:
        """Ctrl+C оставляет отдельный статус и счётчики завершённых карточек."""

        self.collector.crawl.side_effect = KeyboardInterrupt
        self.assertEqual(main(["crawl", "jetp"]), 130)
        self.assertIn('"status": "interrupted"', (self.state_dir / "last_run.json").read_text())

    def test_unknown_source_and_negative_limit(self) -> None:
        """Неподдержанный источник и отрицательный лимит отклоняет argparse."""

        for arguments in (["crawl", "unknown"], ["crawl", "jetp", "--max-requests", "-1"]):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                build_parser().parse_args(arguments)

    def test_deferred_issues_are_not_claimed_as_full_collection(self) -> None:
        """Окончание обхода с пропусками явно отражается в статусе и счётчиках."""

        self.collector.deferred_issues_count = 1
        self.collector.deferred_entries_count = 9
        self.collector.deferred_issues = {"https://journal.example/issue/1": {
            "issue_url": "https://journal.example/issue/1",
            "record_path": "deferred_issues/example.json",
            "entries_count": 9,
        }}
        self.assertEqual(main(["crawl", "quantum", "--metadata-only"]), 0)
        summary = json.loads((self.state_dir / "last_run.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "complete_with_deferred")
        self.assertEqual(summary["deferred_issues_count"], 1)
        self.assertEqual(summary["deferred_entries_count"], 9)
        self.assertEqual(summary["deferred_issues"], list(self.collector.deferred_issues.values()))
        self.assertEqual(summary["cards_processed"], 1)

    def test_stopping_reason_survives_deferred_issues(self) -> None:
        """Наличие пропусков не маскирует сбой, лимит запросов или прерывание."""

        self.collector.deferred_issues_count = 1
        self.collector.deferred_entries_count = 9

        for exception, status, exit_code in (
            (RequestBudgetReached(), "request_limit", 0),
            (IoffeError("HTTP 403"), "error", 1),
            (KeyboardInterrupt(), "interrupted", 130),
        ):
            with self.subTest(status=status):
                self.collector.crawl.side_effect = exception
                self.assertEqual(main(["crawl", "quantum"]), exit_code)
                summary = json.loads((self.state_dir / "last_run.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["status"], status)
                self.assertEqual(summary["deferred_entries_count"], 9)

    def test_article_limit_is_not_reported_as_completed_crawl(self) -> None:
        """Лимит готовых карточек не означает обход всего доступного архива."""

        self.collector.article_limit_reached = True
        self.collector.deferred_issues_count = 1
        self.assertEqual(main(["crawl", "quantum", "--max-articles", "1"]), 0)
        summary = json.loads((self.state_dir / "last_run.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "article_limit")

    def test_named_run_summary_preserves_parameters_and_counters(self) -> None:
        """Отдельный итог позволяет отличить новый запуск от старого last_run."""

        summary_path = self.state_dir / "runs" / "run-15.json"
        self.assertEqual(main([
            "crawl", "jetp", "--volume", "168", "--max-requests", "0", "--max-articles", "0",
            "--metadata-only", "--run-id", "run-15", "--summary-path", str(summary_path),
        ]), 0)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary, json.loads((self.state_dir / "last_run.json").read_text(encoding="utf-8")))
        self.assertEqual(summary["run_id"], "run-15")
        self.assertEqual(summary["source_id"], "jetp")
        self.assertEqual(summary["command"], "crawl")
        self.assertEqual(summary["selected_volume"], 168)
        self.assertIsNone(summary["selected_journal"])
        self.assertTrue(summary["metadata_only"])
        self.assertEqual(summary["max_requests"], 0)
        self.assertEqual(summary["max_articles"], 0)
        self.assertIsNone(summary["max_issues"])
        self.assertIsNone(summary["start_url"])
        self.assertEqual(summary["status"], "complete")
        self.assertFalse(summary["retryable_error"])

    def test_validation_error_before_collector_still_writes_both_summaries(self) -> None:
        """Проверка параметров после argparse не должна оставлять устаревший итог запуска."""

        summary_path = self.state_dir / "runs" / "early.json"
        state_dir = self.state_dir / "early-state"
        self.assertEqual(main([
            "crawl", "quantum", "--volume", "168", "--state-dir", str(state_dir),
            "--run-id", "early", "--summary-path", str(summary_path),
        ]), 1)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary, json.loads((state_dir / "last_run.json").read_text(encoding="utf-8")))
        self.assertEqual(summary["run_id"], "early")
        self.assertEqual(summary["status"], "error")
        self.assertFalse(summary["retryable_error"])
        self.assertEqual(summary["network_requests"], 0)
        self.assertEqual(summary["cards_processed"], 0)

    def test_retry_flag_only_accepts_transport_exception_type(self) -> None:
        """Обычная ошибка диска или строка с DNS не разрешают повторить запрос."""

        for failure, retryable in (
            (IoffeTransientNetworkError("DNS"), True),
            (IoffeError("HTTP 403"), False),
            (IoffeError("HTTP 429"), False),
            (IoffeError("HTTP 503"), False),
            (IoffeError("timeout DNS"), False),
            (TimeoutError("disk"), False),
        ):
            with self.subTest(failure=failure):
                self.collector.crawl.side_effect = failure
                self.assertEqual(main(["crawl", "jetp", "--run-id", "failure"]), 1)
                summary = json.loads((self.state_dir / "last_run.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["status"], "error")
                self.assertEqual(summary["retryable_error"], retryable)
                self.assertEqual(summary["error"], str(failure))

    def test_last_run_write_failure_is_reported_in_named_summary(self) -> None:
        """Недоступный last_run не вызывает повтор скачивания и не скрывает отдельный итог."""

        summary_path = self.state_dir / "runs" / "disk-error.json"
        self.collector.crawl.side_effect = IoffeTransientNetworkError("network")

        with patch("scripts.download_journals.write_json", side_effect=[OSError("disk"), None]) as writer:
            self.assertEqual(main(["crawl", "jetp", "--summary-path", str(summary_path)]), 1)

        self.assertEqual(writer.call_count, 2)
        summary = writer.call_args.args[1]
        self.assertEqual(summary["status"], "error")
        self.assertFalse(summary["retryable_error"])
        self.assertIn("disk", summary["error"])

    def test_runner_command_and_real_cli_summary_have_matching_protocol(self) -> None:
        """Команда запускателя и реально записанный CLI итог должны согласовываться для всех журналов."""

        queue = {"ioffe_origin": "http://journals.ioffe.ru"}
        outcomes = (
            (None, "complete", 0),
            (IoffeTransientNetworkError("DNS"), "retry_wait", 1),
            (IoffeError("HTTP 403"), "blocked", 1),
            (KeyboardInterrupt(), "interrupted", 130),
        )

        for source in ("ufn", "jetp", "jetp_letters", "quantum"):
            for failure, expected_status, expected_code in outcomes:
                with self.subTest(source=source, expected_status=expected_status):
                    task = {
                        "source_id": source, "last_run_id": f"{source}-{expected_status}",
                        "volume": 168 if source == "jetp" else None,
                        "journal_id": None, "metadata_only": source == "quantum",
                    }
                    summary_path = self.state_dir.resolve() / f"{task['last_run_id']}.json"
                    command = _command(task, queue, summary_path, 60.0)
                    self.collector.crawl.side_effect = failure
                    exit_code = main(command[4:])
                    status, error = _attempt_result(task, queue, summary_path, exit_code)

                    self.assertEqual(exit_code, expected_code)
                    self.assertEqual(status, expected_status, error)
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    self.assertIsNone(summary["start_url"])
                    self.assertIs(type(summary["max_articles"]), int)
                    self.assertEqual(summary["max_articles"], 0)

    def test_runner_accepts_deferred_result_and_rejects_stale_run(self) -> None:
        """Отложенные карточки не становятся ошибкой протокола, но чужой run_id не принимается."""

        task = {
            "source_id": "quantum", "last_run_id": "deferred-run", "volume": None,
            "journal_id": None, "metadata_only": True,
        }
        queue = {"ioffe_origin": "http://journals.ioffe.ru"}
        summary_path = self.state_dir.resolve() / "deferred-run.json"
        self.collector.deferred_issues_count = 1
        self.collector.deferred_entries_count = 2
        command = _command(task, queue, summary_path, 60.0)
        exit_code = main(command[4:])

        self.assertEqual(_attempt_result(task, queue, summary_path, exit_code)[0], "complete_with_deferred")
        task["last_run_id"] = "another-run"
        status, error = _attempt_result(task, queue, summary_path, exit_code)
        self.assertEqual(status, "blocked")
        self.assertIn("run_id", str(error))


class JournalClientTests(unittest.TestCase):
    """Проверять бюджет запросов отдельно от подменённого запуска команды."""

    def test_budget_stops_before_extra_request(self) -> None:
        """Лимит не включает чтение кеша и проверяется до вызова транспорта."""

        with tempfile.TemporaryDirectory() as directory, \
                patch("scripts.download_journals.STATE_ROOT", Path(directory)), \
                patch("src.collect.ioffe_http.IoffeClient.get_response") as request, \
                patch("sys.stdout", new_callable=io.StringIO):
            client = JournalClient(_source(), max_requests=1, timeout=30)
            client.get_response(ARTICLE)

            with self.assertRaises(RequestBudgetReached):
                client.get_response(ARTICLE)

            self.assertEqual(request.call_count, 1)
            self.assertEqual(client.request_count, 1)

    def test_invalid_url_does_not_spend_budget(self) -> None:
        """Чужой сервер не становится запросом даже при наличии свободного бюджета."""

        client = JournalClient(_source(), max_requests=1, timeout=30)

        with self.assertRaises(ValueError):
            client.get_response("https://example.org/article/1")

        self.assertEqual(client.request_count, 0)

    def test_get_and_post_share_request_budget(self) -> None:
        """Отправка формы расходует тот же лимит, что и загрузка обычной страницы."""

        with tempfile.TemporaryDirectory() as directory, \
                patch("scripts.download_journals.STATE_ROOT", Path(directory)), \
                patch("src.collect.ioffe_http.IoffeClient.get_response") as get_request, \
                patch("src.collect.ioffe_http.IoffeClient.post_form_response") as post_request, \
                patch("sys.stdout", new_callable=io.StringIO):
            client = JournalClient(get_source("jetp"), max_requests=2, timeout=30)
            body = b"a=select&issues_lang=r&volume=169"
            client.get_response(JETP_START_URL)
            client.post_form_response(JETP_START_URL, body)

            with self.assertRaises(RequestBudgetReached):
                client.post_form_response(JETP_START_URL, body)

            get_request.assert_called_once_with(JETP_START_URL)
            post_request.assert_called_once_with(JETP_START_URL, body)
            self.assertEqual(client.request_count, 2)

    def test_post_is_limited_to_volume_selection(self) -> None:
        """Чужой endpoint, метод формы и лишние поля не расходуют сетевой бюджет."""

        client = JournalClient(get_source("jetp"), max_requests=2, timeout=30)
        cases = (
            (JETP_START_URL + "/r/169?a=list", b"a=select&issues_lang=r&volume=169"),
            (JETP_START_URL, b"a=select&issues_lang=e&volume=169"),
            (JETP_START_URL, b"a=delete&volume=169"),
            (JETP_START_URL, b"a=select&issues_lang=r&volume=169&extra=1"),
            (JETP_START_URL, b"a=select&issues_lang=r&volume=0"),
            (JETP_START_URL, b"a=select&issues_lang=r&volume=" + b"1" * 4096),
        )

        for url, body in cases:
            with self.subTest(url=url, body=body[:60]), self.assertRaises(ValueError):
                client.post_form_response(url, body)

        self.assertEqual(client.request_count, 0)
        other_client = JournalClient(_source(), max_requests=2, timeout=30)

        with self.assertRaises(ValueError):
            other_client.post_form_response(ARTICLE, b"a=select&issues_lang=r&volume=169")

        self.assertEqual(other_client.request_count, 0)
