"""Проверки отдельного кеша POST ЖЭТФ без сети и подмены исходных GET-ответов."""

from __future__ import annotations

import fcntl
import hashlib
import json
import tempfile
import unittest

from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from src.collect import jetp
from src.collect.base import HttpResponseSnapshot
from src.collect.ioffe_archive import IoffeArchive, _json_bytes
from src.collect.ioffe_http import IoffeError
from src.collect.jetp_form_archive import select_volume
from src.collect.journal_sources import JournalSource

FORM_HTML = b"""
<html><body><form action="/cgi-bin/r/index" method="POST">
<input type="hidden" name="a" value="select">
<input type="hidden" name="issues_lang" value="r">
<select name="volume"><option value="169">169</option><option value="168">168</option></select>
<input type="image" src="/img/r/go_vol.png"></form></body></html>
"""


class JetpFormArchiveTests(unittest.TestCase):
    """Проверять происхождение формы, целостность кеша и идемпотентное продолжение."""

    def setUp(self) -> None:
        """Создать изолированный архив и транспорт, который не использует сеть."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.client = MagicMock()
        self.source = JournalSource(jetp)
        self.archive = IoffeArchive(
            self.root / "raw", self.root / "state", self.client,
            allow_network=True, validate_url=self.source.validate_url,
        )
        self.form = HttpResponseSnapshot(
            requested_url=jetp.START_URL,
            final_url=jetp.START_URL,
            status_code=200,
            headers=(("content-type", "text/html; charset=koi8-r"),),
            retrieved_at="2026-10-15T12:00:00+00:00",
            body=FORM_HTML,
        )
        self.response = replace(
            self.form,
            status_code=302,
            headers=(("location", "/cgi-bin/r/index/r/169?a=list"),),
            body=b"",
        )
        self.archive.import_response(self.form)
        self.client.post_form_response.return_value = self.response

    def _pointer(self) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        """Прочитать единственный указатель и соответствующий неизменяемый конверт."""

        path, = (self.archive.state_dir / "form_cache").glob("*.json")
        pointer = json.loads(path.read_bytes())
        envelope = json.loads((self.archive.output_dir / pointer["envelope_path"]).read_bytes())

        return path, pointer, envelope

    def _offline_archive(self) -> IoffeArchive:
        """Создать новый экземпляр для проверки продолжения без разрешения на сеть."""

        return IoffeArchive(
            self.archive.output_dir, self.archive.state_dir, self.client,
            validate_url=self.source.validate_url,
        )

    def test_post_response_does_not_replace_get_cache(self) -> None:
        """GET начальной страницы остаётся исходным HTML после успешной формы POST."""

        get_path, = (self.archive.state_dir / "cache").glob("*.json")
        original = get_path.read_bytes()
        url = select_volume(self.archive, self.form, 169)

        self.assertEqual(url, f"{jetp.START_URL}/r/169?a=list")
        self.assertEqual(get_path.read_bytes(), original)
        self.assertEqual(self.archive.get_response(jetp.START_URL), self.form)
        self.client.get_response.assert_not_called()
        self.client.post_form_response.assert_called_once_with(jetp.START_URL, b"a=select&issues_lang=r&volume=169")

        _, _, envelope = self._pointer()
        self.assertEqual(envelope["form"], self.archive.evidence_for(self.form))
        self.assertEqual(envelope["request"]["method"], "POST")
        body_path = self.archive.output_dir / envelope["request"]["body_path"]
        self.assertEqual(body_path.read_bytes(), b"a=select&issues_lang=r&volume=169")
        metadata_path = self.archive.output_dir / envelope["response"]["response_metadata_path"]
        self.assertEqual(metadata_path.read_bytes(), self.response.canonical_metadata())

    def test_repeat_uses_validated_cache_offline(self) -> None:
        """Повторный запуск не отправляет форму и не меняет исходные файлы."""

        expected = select_volume(self.archive, self.form, 169)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(select_volume(self._offline_archive(), self.form, 169), expected)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(self.client.post_form_response.call_count, 1)

    def test_other_volume_has_separate_request_cache(self) -> None:
        """Совпадающий адрес POST не смешивает ответы разных выбранных томов."""

        select_volume(self.archive, self.form, 169)
        self.client.post_form_response.return_value = replace(
            self.response, headers=(("location", "/cgi-bin/r/index/r/168?a=list"),),
        )
        self.assertEqual(select_volume(self.archive, self.form, 168), f"{jetp.START_URL}/r/168?a=list")
        self.assertEqual(len(list((self.archive.state_dir / "form_cache").glob("*.json"))), 2)
        self.assertEqual(select_volume(self._offline_archive(), self.form, 169), f"{jetp.START_URL}/r/169?a=list")
        self.assertEqual(self.client.post_form_response.call_count, 2)

    def test_missing_cache_and_refresh_need_network_permission(self) -> None:
        """Ни отсутствие кеша, ни обновление не включают сеть без разрешения."""

        with self.assertRaisesRegex(IoffeError, "allow_network"):
            select_volume(self._offline_archive(), self.form, 169)

        self.client.post_form_response.assert_not_called()
        select_volume(self.archive, self.form, 169)

        with self.assertRaisesRegex(IoffeError, "allow_network"):
            select_volume(self._offline_archive(), self.form, 169, refresh=True)

        self.assertEqual(self.client.post_form_response.call_count, 1)

    def test_unsaved_form_cannot_trigger_post(self) -> None:
        """Параметры формы принимаются только из уже сохранённого HTTP-свидетельства."""

        with self.assertRaises(IoffeError):
            select_volume(self.archive, replace(self.form, body=FORM_HTML + b"new"), 169)

        self.client.post_form_response.assert_not_called()

    def test_refresh_preserves_previous_response_and_request(self) -> None:
        """Явное обновление добавляет новое свидетельство и не удаляет предыдущие байты."""

        select_volume(self.archive, self.form, 169)
        _, previous_pointer, previous_envelope = self._pointer()
        previous_path = self.archive.output_dir / previous_pointer["envelope_path"]
        previous_bytes = previous_path.read_bytes()
        self.client.post_form_response.return_value = replace(
            self.response, retrieved_at="2026-10-15T12:02:00+00:00", body=b"redirected",
        )
        select_volume(self.archive, self.form, 169, refresh=True)
        _, updated_pointer, updated_envelope = self._pointer()

        self.assertNotEqual(updated_pointer, previous_pointer)
        self.assertEqual(previous_path.read_bytes(), previous_bytes)
        self.assertEqual(updated_envelope["request"], previous_envelope["request"])
        self.assertEqual(self.client.post_form_response.call_count, 2)

    def test_transport_failure_cannot_publish_cache_or_partial_attempt(self) -> None:
        """Обрыв соединения без полного ответа не превращается в HTTP-свидетельство."""

        self.client.post_form_response.side_effect = IoffeError("Неполный ответ")

        with self.assertRaisesRegex(IoffeError, "Неполный ответ"):
            select_volume(self.archive, self.form, 169)

        self.assertFalse((self.archive.state_dir / "form_cache").exists())
        self.assertFalse((self.archive.state_dir / "form_attempts").exists())
        self.assertFalse((self.archive.output_dir / "form_submissions").exists())

    def test_invalid_content_length_cannot_publish_response(self) -> None:
        """Неполные HTTP-байты отклоняются до записи конверта и указателя кеша."""

        self.client.post_form_response.return_value = replace(
            self.response, headers=(*self.response.headers, ("content-length", "20")),
        )

        with self.assertRaisesRegex(IoffeError, "Content-Length"):
            select_volume(self.archive, self.form, 169)

        self.assertFalse((self.archive.state_dir / "form_cache").exists())
        self.assertFalse((self.archive.output_dir / "form_submissions").exists())

    def test_noncanonical_pointer_is_rejected(self) -> None:
        """Равнозначный JSON с другими точными байтами не принимается за канонический кеш."""

        select_volume(self.archive, self.form, 169)
        pointer_path, pointer, _ = self._pointer()
        pointer_path.write_text(json.dumps(pointer))

        with self.assertRaisesRegex(IoffeError, "каноническая"):
            select_volume(self.archive, self.form, 169)

        self.assertEqual(self.client.post_form_response.call_count, 1)

    def test_changed_form_keeps_old_provenance_but_requires_available_volume(self) -> None:
        """Новая версия страницы не подменяет источник прежней отправки формы."""

        select_volume(self.archive, self.form, 169)
        _, _, envelope = self._pointer()
        changed_form = replace(self.form, body=FORM_HTML + b"<!-- changed -->")
        self.archive.import_response(changed_form)
        self.assertEqual(select_volume(self._offline_archive(), changed_form, 169), f"{jetp.START_URL}/r/169?a=list")
        self.assertEqual(envelope["form"], self.archive.evidence_for(self.form))

        missing_volume = replace(changed_form, body=changed_form.body.replace(b'<option value="169">169</option>', b""))
        self.archive.import_response(missing_volume)

        with self.assertRaisesRegex(IoffeError, "отсутствует"):
            select_volume(self.archive, missing_volume, 169)

        self.assertEqual(self.client.post_form_response.call_count, 1)

    def test_old_form_evidence_is_still_checked_after_page_changes(self) -> None:
        """Повреждение исходной формы обнаруживается даже при новом корректном GET-снимке."""

        select_volume(self.archive, self.form, 169)
        _, _, envelope = self._pointer()
        changed_form = replace(self.form, body=FORM_HTML + b"<!-- new -->")
        self.archive.import_response(changed_form)
        (self.archive.output_dir / envelope["form"]["relative_path"]).write_bytes(b"corrupted")

        with self.assertRaises(IoffeError):
            select_volume(self.archive, changed_form, 169)

        self.assertEqual(self.client.post_form_response.call_count, 1)

    def test_failed_or_unexpected_http_response_is_archived_not_cached(self) -> None:
        """Отказ или чужой переход сохраняется, но не становится успешным адресом тома."""

        responses = (
            replace(self.response, status_code=403, body=b"Forbidden"),
            replace(self.response, status_code=200, body=b"unexpected HTML"),
            replace(self.response, headers=(("location", "https://example.test/"),)),
            replace(self.response, headers=(("location", "/cgi-bin/r/index/r/168?a=list"),)),
        )

        for response in responses:
            with self.subTest(status=response.status_code, headers=response.headers):
                self.client.post_form_response.return_value = response

                with self.assertRaises(IoffeError):
                    select_volume(self.archive, self.form, 169)

                attempt_path, = (self.archive.state_dir / "form_attempts").glob("*.json")
                pointer = json.loads(attempt_path.read_bytes())
                envelope = json.loads((self.archive.output_dir / pointer["envelope_path"]).read_bytes())
                metadata_path = self.archive.output_dir / envelope["response"]["response_metadata_path"]
                self.assertEqual(metadata_path.read_bytes(), response.canonical_metadata())
                self.assertFalse((self.archive.state_dir / "form_cache").exists())

        self.assertEqual(len(list((self.archive.output_dir / "form_submissions").rglob("*.json"))), 4)
        self.client.get_response.assert_not_called()

    def test_corrupted_files_never_fall_back_to_network(self) -> None:
        """Порча любого звена конверта останавливает чтение даже при разрешённой сети."""

        select_volume(self.archive, self.form, 169)
        pointer_path, pointer, envelope = self._pointer()
        paths = (
            pointer_path,
            self.archive.output_dir / pointer["envelope_path"],
            self.archive.output_dir / envelope["request"]["body_path"],
            self.archive.output_dir / envelope["response"]["relative_path"],
            self.archive.output_dir / envelope["response"]["response_metadata_path"],
        )

        for path in paths:
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(b"corrupted")

                try:
                    with self.assertRaises(IoffeError):
                        select_volume(self.archive, self.form, 169, refresh=True)

                finally:
                    path.write_bytes(original)

        self.assertEqual(self.client.post_form_response.call_count, 1)

    def test_rehashed_envelope_cannot_change_method_or_volume(self) -> None:
        """Пересчитанная сумма не делает подменённый запрос допустимым для выбранного тома."""

        select_volume(self.archive, self.form, 169)
        pointer_path, pointer, original_envelope = self._pointer()

        for change in ("method", "volume", "request_url", "form_path"):
            with self.subTest(change=change):
                envelope = json.loads(json.dumps(original_envelope))

                if change == "method":
                    envelope["request"]["method"] = "GET"

                elif change == "volume":
                    envelope["volume"] = 168

                elif change == "request_url":
                    envelope["request"]["url"] += "?other"

                else:
                    envelope["form"]["relative_path"] = "../outside.html"

                body = _json_bytes(envelope)
                digest = hashlib.sha256(body).hexdigest()
                relative_path = f"form_submissions/{pointer['request_sha256']}/{digest}.json"
                (self.archive.output_dir / relative_path).write_bytes(body)
                changed_pointer = {**pointer, "envelope_path": relative_path, "envelope_sha256": digest}
                pointer_path.write_bytes(_json_bytes(changed_pointer))

                with self.assertRaises(IoffeError):
                    select_volume(self.archive, self.form, 169)

        self.assertEqual(self.client.post_form_response.call_count, 1)

    def test_symlink_even_inside_archive_is_rejected(self) -> None:
        """Символическая ссылка не считается неизменяемым файлом, даже оставаясь внутри архива."""

        select_volume(self.archive, self.form, 169)
        pointer_path, pointer, envelope = self._pointer()

        for path in (
            pointer_path,
            self.archive.output_dir / pointer["envelope_path"],
            self.archive.output_dir / envelope["request"]["body_path"],
            self.archive.output_dir / envelope["response"]["response_metadata_path"],
        ):
            with self.subTest(path=path):
                original = path.read_bytes()
                other_path = path.with_suffix(".copy")
                other_path.write_bytes(original)
                path.unlink()
                path.symlink_to(other_path)

                try:
                    with self.assertRaisesRegex(IoffeError, "символические"):
                        select_volume(self.archive, self.form, 169)

                finally:
                    path.unlink()
                    path.write_bytes(original)

        self.assertEqual(self.client.post_form_response.call_count, 1)

    def test_archive_lock_covers_post_and_publication(self) -> None:
        """POST использует тот же замок архива, что и обычное получение страниц."""

        def post_form_response(url: str, body: bytes) -> HttpResponseSnapshot:
            """Убедиться, что другой файловый дескриптор не может захватить замок."""

            self.assertEqual(url, jetp.START_URL)
            self.assertTrue(body.endswith(b"volume=169"))

            with (
                (self.archive.state_dir / "ioffe_archive.lock").open("a+b") as other_lock,
                self.assertRaises(BlockingIOError),
            ):
                fcntl.flock(other_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            return self.response

        self.client.post_form_response.side_effect = post_form_response
        select_volume(self.archive, self.form, 169)
        select_volume(self._offline_archive(), self.form, 169)
        self.assertEqual(self.client.post_form_response.call_count, 1)
