"""Проверки команды обработки Excel-бланков пробной разметки H2."""

from __future__ import annotations

import io
import tempfile
import unittest

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import check_h2_practice


class CheckH2PracticeCommandTests(unittest.TestCase):
    """Проверки параметров, режимов записи и разграничения ошибок и разногласий."""

    def setUp(self) -> None:
        """Подготовить временный корень и ответ независимого обработчика бланков."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.prepared = Mock()
        self.prepared.output_dir = self.root / "manifests/results/practice_checked_v1"
        self.prepared.report = {
            "status": "practice_answers_checked",
            "formal_h2_ready": False,
            "counts": {
                "items": 10,
                "errors": 0,
                "complete_pairs": 10,
                "disagreements": 2,
            },
            "issues": [],
        }
        self.arguments = [
            "--author-file", "author.xlsx",
            "--supervisor-file", "supervisor.xlsx",
            "--output-dir", "manifests/results/practice_checked_v1",
        ]

    def _run(self, arguments: list[str]) -> tuple[int, str]:
        """Выполнить команду с подставными подготовкой и публикацией результата."""

        output = io.StringIO()

        with (
            patch.object(check_h2_practice, "prepare_h2_practice", return_value=self.prepared),
            patch.object(check_h2_practice, "publish_h2_practice", return_value=True),
            redirect_stdout(output),
        ):
            result = check_h2_practice.main(arguments, project_root=self.root)

        return result, output.getvalue()

    def test_default_paths_are_relative_to_selected_project(self) -> None:
        """Исходные пакеты по умолчанию берутся из явно выбранного проекта."""

        with (
            patch.object(
                check_h2_practice, "prepare_h2_practice", return_value=self.prepared,
            ) as prepare,
            patch.object(check_h2_practice, "publish_h2_practice", return_value=True) as publish,
            redirect_stdout(io.StringIO()),
        ):
            result = check_h2_practice.main(self.arguments, project_root=self.root)

        self.assertEqual(result, 0)
        prepare.assert_called_once_with(
            project_root=self.root,
            packet_dir=self.root / check_h2_practice.DEFAULT_PACKET,
            template_dir=self.root / check_h2_practice.DEFAULT_TEMPLATES,
            author_file=Path("author.xlsx"),
            supervisor_file=Path("supervisor.xlsx"),
            output_dir=Path("manifests/results/practice_checked_v1"),
        )
        publish.assert_called_once_with(self.prepared, dry_run=False)

    def test_explicit_paths_and_dry_run_are_forwarded(self) -> None:
        """Предварительная проверка передаёт все пути и запрет записи обработчику."""

        output = io.StringIO()

        with (
            patch.object(
                check_h2_practice, "prepare_h2_practice", return_value=self.prepared,
            ) as prepare,
            patch.object(check_h2_practice, "publish_h2_practice", return_value=False) as publish,
            redirect_stdout(output),
        ):
            result = check_h2_practice.main(
                [
                    *self.arguments, "--packet-dir", "packet",
                    "--template-dir", "templates", "--dry-run",
                ],
                project_root=self.root,
            )

        self.assertEqual(result, 0)
        prepare.assert_called_once_with(
            project_root=self.root,
            packet_dir=Path("packet"),
            template_dir=Path("templates"),
            author_file=Path("author.xlsx"),
            supervisor_file=Path("supervisor.xlsx"),
            output_dir=Path("manifests/results/practice_checked_v1"),
        )
        publish.assert_called_once_with(self.prepared, dry_run=True)
        self.assertIn("без записи", output.getvalue())
        self.assertIn("Будущий каталог", output.getvalue())
        self.assertNotIn("Создан пакет проверки", output.getvalue())

    def test_disagreements_are_not_validation_errors(self) -> None:
        """Корректные разные решения сохраняют нулевой код завершения."""

        result, output = self._run(self.arguments)

        self.assertEqual(result, 0)
        self.assertIn("Создан пакет проверки. Карточек: 10", output)
        self.assertIn("Ошибок: 0; полных пар ответов: 10; разногласий: 2", output)
        self.assertIn("Разногласия не являются ошибками", output)
        self.assertIn("итоговые метки H2 не назначены", output)
        self.assertIn("Независимость разметки и утверждение памятки", output)
        self.assertIn("реестры не изменены", output)

    def test_invalid_answers_return_two_after_diagnostic_publication(self) -> None:
        """Ошибки заполнения сохраняют диагностику, но не становятся успехом."""

        self.prepared.report["status"] = "invalid"
        self.prepared.report["counts"]["errors"] = 1
        self.prepared.report["counts"]["complete_pairs"] = 9
        self.prepared.report["issues"] = [{
            "role": "author", "sheet": "01", "cell": "B14",
            "code": "missing_response", "message": "Решение не заполнено",
        }]
        output = io.StringIO()

        with (
            patch.object(check_h2_practice, "prepare_h2_practice", return_value=self.prepared),
            patch.object(check_h2_practice, "publish_h2_practice", return_value=True) as publish,
            redirect_stdout(output),
        ):
            result = check_h2_practice.main(self.arguments, project_root=self.root)

        self.assertEqual(result, 2)
        publish.assert_called_once_with(self.prepared, dry_run=False)
        self.assertIn("[author] 01!B14: Решение не заполнено (missing_response)", output.getvalue())
        self.assertIn("Ответы нельзя экспортировать", output.getvalue())

    def test_invalid_dry_run_also_returns_two(self) -> None:
        """Отсутствие записи не скрывает найденные ошибки заполнения."""

        self.prepared.report["counts"]["errors"] = 1

        result, output = self._run([*self.arguments, "--dry-run"])

        self.assertEqual(result, 2)
        self.assertIn("Предварительная проверка без записи", output)

    def test_printed_issues_are_limited_to_ten(self) -> None:
        """Большой список ошибок не превращает вывод команды в полный журнал."""

        self.prepared.report["counts"]["errors"] = 12
        self.prepared.report["issues"] = [
            {
                "role": "supervisor", "sheet": "02", "cell": "B15",
                "code": f"issue_{issue_index}", "message": "Неверное значение",
            }
            for issue_index in range(12)
        ]

        result, output = self._run(self.arguments)

        self.assertEqual(result, 2)
        self.assertEqual(output.count("[supervisor]"), 10)
        self.assertIn("(issue_9)", output)
        self.assertNotIn("(issue_10)", output)
        self.assertIn("Остальных замечаний: 2", output)

    def test_preparation_error_prevents_publication(self) -> None:
        """Повреждённый исходный пакет не допускает записи диагностического пакета."""

        error_output = io.StringIO()

        with (
            patch.object(
                check_h2_practice, "prepare_h2_practice", side_effect=ValueError("SHA-256"),
            ),
            patch.object(check_h2_practice, "publish_h2_practice") as publish,
            redirect_stderr(error_output),
        ):
            result = check_h2_practice.main(self.arguments, project_root=self.root)

        self.assertEqual(result, 1)
        self.assertIn("ОШИБКА", error_output.getvalue())
        self.assertIn("SHA-256", error_output.getvalue())
        publish.assert_not_called()

    def test_publication_error_is_reported_in_stderr(self) -> None:
        """Сбой записи возвращает код ошибки, а не сообщение о готовом результате."""

        error_output = io.StringIO()

        with (
            patch.object(check_h2_practice, "prepare_h2_practice", return_value=self.prepared),
            patch.object(
                check_h2_practice, "publish_h2_practice", side_effect=OSError("Нет доступа"),
            ),
            redirect_stderr(error_output),
        ):
            result = check_h2_practice.main(self.arguments, project_root=self.root)

        self.assertEqual(result, 1)
        self.assertIn("Нет доступа", error_output.getvalue())

    def test_answer_paths_and_output_are_required(self) -> None:
        """Команда не выбирает молча ответы и не перезаписывает прежнюю проверку."""

        for missing_option in ("--author-file", "--supervisor-file", "--output-dir"):
            arguments = list(self.arguments)
            option_index = arguments.index(missing_option)
            del arguments[option_index:option_index + 2]

            with (
                self.subTest(option=missing_option), redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as context,
            ):
                check_h2_practice.main(arguments, project_root=self.root)

            self.assertEqual(context.exception.code, 2)

    def test_help_exits_before_processing(self) -> None:
        """Справка не читает пользовательские ответы и ничего не публикует."""

        with (
            patch.object(check_h2_practice, "prepare_h2_practice") as prepare,
            redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as context,
        ):
            check_h2_practice.main(["--help"], project_root=self.root)

        self.assertEqual(context.exception.code, 0)
        prepare.assert_not_called()
