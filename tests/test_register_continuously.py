"""Проверки команды автоматического учёта без изменения рабочих реестров."""

from __future__ import annotations

import io
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.register_continuously import build_parser, main


class RegisterContinuouslyTests(unittest.TestCase):
    """Проверять параметры, отдельный просмотр состояния и сообщения об ошибках."""

    def setUp(self) -> None:
        """Изолировать команды от рабочих файлов и фонового процесса."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.status = {
            "state_path": str(self.root / "manifests/imports/continuous_inventory/state.json"),
            "state_exists": True, "completed_batches": 2, "seen_cards": 10,
            "pending": False, "last_receipt": "manifests/imports/example/receipt.json",
        }

        for patcher in (
            patch("scripts.register_continuously.PROJECT_ROOT", self.root),
            patch("sys.stdout", self.stdout), patch("sys.stderr", self.stderr),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        runner = patch("scripts.register_continuously.run_inventory", return_value=self.status)
        loader = patch("scripts.register_continuously.load_inventory_status", return_value=self.status)
        self.runner = runner.start()
        self.loader = loader.start()
        self.addCleanup(runner.stop)
        self.addCleanup(loader.stop)

    def test_run_defaults_use_fifteen_minute_pause(self) -> None:
        """Обычный запуск передаёт безопасные значения без сетевых параметров."""

        self.assertEqual(main(["run"]), 0)
        self.runner.assert_called_once_with(
            self.root, state_dir=None, interval_seconds=900.0, once=False,
            seed_batch=None, seed_receipt=None,
        )
        self.loader.assert_not_called()

    def test_run_passes_seed_and_once(self) -> None:
        """Начальная партия и квитанция доходят до проверяющего их движка вместе."""

        self.assertEqual(main([
            "run", "--once", "--interval", "1200", "--state-dir", "manifests/imports/local",
            "--seed-batch", "batch.json", "--seed-receipt", "receipt.json",
        ]), 0)
        self.runner.assert_called_once_with(
            self.root, state_dir=Path("manifests/imports/local"), interval_seconds=1200,
            once=True, seed_batch=Path("batch.json"), seed_receipt=Path("receipt.json"),
        )

    def test_incomplete_seed_is_rejected_before_runner(self) -> None:
        """Без второго файла не начинается ни регистрация, ни создание состояния."""

        for option in ("--seed-batch", "--seed-receipt"):
            with self.subTest(option=option):
                self.assertEqual(main(["run", option, "file.json"]), 1)

        self.runner.assert_not_called()

    def test_invalid_interval_is_rejected(self) -> None:
        """Нулевой, отрицательный, нечисловой и бесконечный интервалы запрещены."""

        for value in ("0", "-1", "nan", "inf", "-inf", "text"):
            with self.subTest(value=value), self.assertRaises(SystemExit):
                build_parser().parse_args(["run", f"--interval={value}"])

        self.runner.assert_not_called()

    def test_status_never_starts_registration(self) -> None:
        """Просмотр сообщает только сохранённый результат и не пишет реестры."""

        self.assertEqual(main(["status"]), 0)
        self.loader.assert_called_once_with(self.root, state_dir=None)
        self.runner.assert_not_called()
        self.assertIn("Учтено партий: 2", self.stdout.getvalue())
        self.assertIn("не подтверждение работающего процесса", self.stdout.getvalue())

    def test_missing_state_is_not_reported_as_finished(self) -> None:
        """Отсутствующий файл состояния не превращается в заявление об успехе."""

        self.status["state_exists"] = False
        self.assertEqual(main(["status"]), 0)
        self.assertIn("ещё не создавала состояние", self.stdout.getvalue())
        self.assertNotIn("Учтено партий", self.stdout.getvalue())

    def test_pending_batch_is_visible(self) -> None:
        """Незавершённая партия явно обозначена в кратком выводе."""

        self.status["pending"] = True
        self.assertEqual(main(["status"]), 0)
        self.assertIn("Есть незавершённая партия", self.stdout.getvalue())

    def test_expected_failure_is_not_success(self) -> None:
        """Ошибка движка не скрывается успешным завершением команды."""

        self.runner.side_effect = ValueError("Повреждена квитанция")
        self.assertEqual(main(["run", "--once"]), 1)
        self.assertIn("Повреждена квитанция", self.stderr.getvalue())

    def test_interruption_preserves_non_success_exit(self) -> None:
        """Прерывание не выдаётся за полностью обработанную партию."""

        self.runner.side_effect = KeyboardInterrupt
        self.assertEqual(main(["run"]), 130)
        self.assertIn("Команда прервана", self.stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
