"""Проверки команды очистки метаданных H2 и сообщений о безопасном результате."""

from __future__ import annotations

import io
import tempfile
import unittest

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import clean_h2_candidates
from src.preprocess.h2_cleaning_io import PreparedH2Cleaning


class CleanH2CandidatesCommandTests(unittest.TestCase):
    """Проверки аргументов, отсутствия скрытой записи и обработки ошибок."""

    def setUp(self) -> None:
        """Создать временный корень и подставной результат независимого обработчика."""

        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.prepared = Mock(spec=PreparedH2Cleaning)
        self.prepared.output_dir = self.root / "manifests/results/clean_v1"
        self.prepared.report = {
            "counts": {
                "candidates": 3,
                "changed_candidates": 1,
                "changes": 2,
                "issues": 4,
                "candidates_with_issues": 2,
                "missing_title": 0,
                "missing_abstract": 1,
            },
        }

    def _run(self, arguments: list[str], *, created: bool = True) -> tuple[int, str]:
        """Выполнить команду с контролируемыми подготовкой и публикацией."""

        output = io.StringIO()

        with (
            patch.object(clean_h2_candidates, "prepare_h2_cleaning", return_value=self.prepared),
            patch.object(clean_h2_candidates, "publish_h2_cleaning", return_value=created),
            redirect_stdout(output),
        ):
            result = clean_h2_candidates.main(arguments, project_root=self.root)

        return result, output.getvalue()

    def test_default_queue_is_relative_to_selected_project(self) -> None:
        """Очередь по умолчанию находится в выбранном проекте, а не рабочем каталоге."""

        output = io.StringIO()

        with (
            patch.object(
                clean_h2_candidates, "prepare_h2_cleaning", return_value=self.prepared,
            ) as prepare,
            patch.object(clean_h2_candidates, "publish_h2_cleaning", return_value=True) as publish,
            redirect_stdout(output),
        ):
            result = clean_h2_candidates.main(
                ["--output-dir", "manifests/results/clean_v1"], project_root=self.root,
            )

        self.assertEqual(result, 0)
        prepare.assert_called_once_with(
            project_root=self.root,
            queue_dir=self.root / clean_h2_candidates.DEFAULT_QUEUE,
            output_dir=Path("manifests/results/clean_v1"),
        )
        publish.assert_called_once_with(self.prepared, dry_run=False)

    def test_explicit_paths_and_dry_run_are_forwarded(self) -> None:
        """Проверка без записи явно передаётся обработчику вместе с путями."""

        output = io.StringIO()

        with (
            patch.object(
                clean_h2_candidates, "prepare_h2_cleaning", return_value=self.prepared,
            ) as prepare,
            patch.object(clean_h2_candidates, "publish_h2_cleaning", return_value=False) as publish,
            redirect_stdout(output),
        ):
            result = clean_h2_candidates.main(
                ["--queue-dir", "queue", "--output-dir", "manifests/results/clean_v1", "--dry-run"],
                project_root=self.root,
            )

        self.assertEqual(result, 0)
        prepare.assert_called_once_with(
            project_root=self.root, queue_dir=Path("queue"),
            output_dir=Path("manifests/results/clean_v1"),
        )
        publish.assert_called_once_with(self.prepared, dry_run=True)
        self.assertIn("без записи", output.getvalue())
        self.assertIn("Будущий каталог", output.getvalue())

    def test_created_result_reports_counts_and_limitations(self) -> None:
        """Сообщения различают очистку, замечания и допуск к обучению."""

        result, output = self._run(["--output-dir", "manifests/results/clean_v1"])

        self.assertEqual(result, 0)
        self.assertIn("Создан новый пакет. Карточек: 3", output)
        self.assertIn("Изменено карточек: 1; замен: 2", output)
        self.assertIn("Карточек с замечаниями: 2; замечаний: 4", output)
        self.assertIn("без аннотации: 1", output)
        self.assertIn("не готовая обучающая выборка", output)
        self.assertIn("реестры не изменены", output)

    def test_identical_repeat_is_not_reported_as_creation(self) -> None:
        """Точный повтор явно сообщает отсутствие новой записи."""

        result, output = self._run(
            ["--output-dir", "manifests/results/clean_v1"], created=False,
        )

        self.assertEqual(result, 0)
        self.assertIn("Точный повтор, запись не нужна", output)
        self.assertNotIn("Создан новый пакет", output)

    def test_preparation_error_prevents_publication(self) -> None:
        """Ошибка происхождения данных останавливает команду до публикации."""

        error_output = io.StringIO()

        with (
            patch.object(
                clean_h2_candidates, "prepare_h2_cleaning", side_effect=ValueError("SHA-256"),
            ),
            patch.object(clean_h2_candidates, "publish_h2_cleaning") as publish,
            redirect_stderr(error_output),
        ):
            result = clean_h2_candidates.main(
                ["--output-dir", "manifests/results/clean_v1"], project_root=self.root,
            )

        self.assertEqual(result, 1)
        self.assertIn("ОШИБКА", error_output.getvalue())
        self.assertIn("SHA-256", error_output.getvalue())
        publish.assert_not_called()

    def test_publication_error_is_reported_in_stderr(self) -> None:
        """Отказ записи возвращает ненулевой код и понятную причину."""

        error_output = io.StringIO()

        with (
            patch.object(clean_h2_candidates, "prepare_h2_cleaning", return_value=self.prepared),
            patch.object(
                clean_h2_candidates, "publish_h2_cleaning", side_effect=OSError("Нет доступа"),
            ),
            redirect_stderr(error_output),
        ):
            result = clean_h2_candidates.main(
                ["--output-dir", "manifests/results/clean_v1"], project_root=self.root,
            )

        self.assertEqual(result, 1)
        self.assertIn("Нет доступа", error_output.getvalue())

    def test_output_directory_is_required(self) -> None:
        """Команда не выбирает молча каталог, способный затронуть прежнюю версию."""

        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                clean_h2_candidates.main([], project_root=self.root)

        self.assertEqual(context.exception.code, 2)

    def test_help_exits_before_processing(self) -> None:
        """Справка не читает очередь и не создаёт результат."""

        with (
            patch.object(clean_h2_candidates, "prepare_h2_cleaning") as prepare,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as context:
                clean_h2_candidates.main(["--help"], project_root=self.root)

        self.assertEqual(context.exception.code, 0)
        prepare.assert_not_called()
