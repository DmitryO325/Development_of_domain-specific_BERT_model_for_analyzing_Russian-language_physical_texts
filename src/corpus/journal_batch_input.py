"""Проверка неизменяемого состава партии журналов без обхода каталогов и сети."""

from __future__ import annotations

import hashlib
import json
import re

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .journal_import import (
    COLLECTED_SOURCES,
    MAX_RECORD_BYTES,
    CollectedArticle,
    read_collected_article,
)

MAX_BATCH_BYTES = 8 * 1024 * 1024
BATCH_SCHEMA = "prepared-journal-registration-batch-v1"


@dataclass(frozen=True)
class FrozenJournalBatch:
    """Проверенная партия и точные исходные сведения о её утверждённом составе."""

    batch_id: str
    path: Path
    sha256: str
    prepared_at: str
    records: tuple[CollectedArticle, ...]
    record_sources: tuple[str, ...]
    raw_packet: dict[str, Any]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Не принимать неоднозначный JSON с повторяющимися именами полей."""

    result: dict[str, Any] = {}

    for key, value in pairs:
        if key in result:
            raise ValueError(f"Поле пакета повторяется: {key}")

        result[key] = value

    return result


def _digest(value: Any) -> str:
    """Проверить формат контрольной суммы SHA-256."""

    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Ожидается контрольная сумма SHA-256 из 64 шестнадцатеричных символов")

    return value


def _timestamp(value: Any) -> datetime:
    """Прочитать временную метку с явно указанным часовым поясом."""

    if not isinstance(value, str):
        raise ValueError("Временная метка партии должна быть строкой")

    try:
        timestamp = datetime.fromisoformat(value)

    except ValueError as exception:
        raise ValueError("Некорректная временная метка партии") from exception

    if timestamp.utcoffset() is None:
        raise ValueError("Временная метка партии должна содержать часовой пояс")

    return timestamp


def _relative_path(value: Any) -> PurePosixPath:
    """Разрешить только однозначный относительный путь без выхода из проекта."""

    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("В пакете требуется непустой относительный путь")

    path = PurePosixPath(value)

    if path.is_absolute() or ".." in path.parts or str(path) != value or not path.parts:
        raise ValueError(f"Недопустимый относительный путь пакета: {value}")

    return path


def _inside_project(root: Path, value: Any, *, directory: bool = False) -> Path:
    """Проверить каждый компонент пути внутри явно выбранного корня проекта."""

    relative = _relative_path(value)
    path = root

    for part in relative.parts:
        path = path / part

        if path.is_symlink():
            raise ValueError(f"Путь партии содержит символическую ссылку: {path}")

    if not path.resolve().is_relative_to(root):
        raise ValueError("Путь партии выходит за корень проекта")

    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f"Файл или каталог партии отсутствует: {path}")

    return path


def _read_limited(path: Path, limit: int) -> bytes:
    """Прочитать ограниченное число байтов из проверенного обычного файла."""

    with path.open("rb") as input_file:
        body = input_file.read(limit + 1)

    if len(body) > limit:
        raise ValueError(f"Слишком большой файл партии: {path}")

    return body


def _verify_policy(packet: dict[str, Any]) -> None:
    """Проверить сохранённую принятую DEC-012, не подменяя её текущим документом."""

    policy = packet.get("policy")

    if not isinstance(policy, dict):
        raise ValueError("В пакете отсутствует сохранённая политика DEC-012")

    _relative_path(policy.get("path"))
    _digest(policy.get("file_sha256"))
    snapshot = policy.get("dec012_snapshot")

    if (
        not isinstance(snapshot, str) or
        re.search(r"^## DEC-012\.", snapshot, re.MULTILINE) is None or
        re.search(r"\*\*Состояние:\*\* принято \d{2}\.\d{2}\.\d{4}\.", snapshot) is None or
        "lawful_access_research_assumption" not in snapshot
    ):
        raise ValueError("В сохранённой DEC-012 нет отметки о принятии исследовательской политики")


def _verify_descriptor(
    descriptor: Any,
    *,
    root: Path,
    raw_roots: dict[str, Path],
    seen_paths: set[Path],
    skipped: bool,
) -> tuple[str, Path]:
    """Проверить закреплённый путь, источник и точную контрольную сумму карточки."""

    if not isinstance(descriptor, dict):
        raise ValueError("Описание карточки в партии должно быть объектом")

    expected = {"source", "record_path", "record_sha256"}

    if not skipped:
        expected.add("has_pdf")

    if set(descriptor) != expected:
        raise ValueError("Неизвестные или недостающие поля описания карточки")

    source = descriptor["source"]

    if not isinstance(source, str) or source not in raw_roots:
        raise ValueError("Источник карточки не совпадает с каталогами партии")

    if not skipped and type(descriptor["has_pdf"]) is not bool:
        raise ValueError("Поле has_pdf должно иметь логический тип")

    path = _inside_project(root, descriptor["record_path"])

    if path in seen_paths:
        raise ValueError(f"Карточка повторяется в составе партии: {path}")

    seen_paths.add(path)
    expected_digest = _digest(descriptor["record_sha256"])

    if hashlib.sha256(_read_limited(path, MAX_RECORD_BYTES)).hexdigest() != expected_digest:
        raise ValueError(f"Контрольная сумма закреплённой карточки изменилась: {path}")

    return source, path


def _verify_counts(packet: dict[str, Any], counts: dict[str, dict[str, int]]) -> None:
    """Проверить заявленные итоги, различая целые числа и логические значения."""

    declared = packet.get("counts")

    if not isinstance(declared, dict) or set(declared) != set(counts):
        raise ValueError("Итоги партии не соответствуют списку источников")

    for source, expected in counts.items():
        actual = declared[source]

        if (
            not isinstance(actual, dict) or set(actual) != set(expected) or
            any(type(value) is not int for value in actual.values()) or actual != expected
        ):
            raise ValueError(f"Итоги партии не соответствуют карточкам источника {source}")


def read_frozen_journal_batch(
    path: Path,
    *,
    project_root: Path,
    expected_sha256: str | None = None,
) -> FrozenJournalBatch:
    """Загрузить только закреплённые карточки, проверив их состав, байты и исходники."""

    declared_root = Path(project_root).absolute()

    if declared_root.is_symlink() or not declared_root.is_dir():
        raise ValueError("Корень проекта отсутствует или является символической ссылкой")

    root = declared_root.resolve()
    packet_path = Path(path)

    if packet_path.is_absolute():
        if packet_path.is_relative_to(declared_root):
            packet_path = packet_path.relative_to(declared_root)

        elif packet_path.is_relative_to(root):
            packet_path = packet_path.relative_to(root)

        else:
            raise ValueError("Пакет партии должен находиться внутри проекта")

    packet_path = _inside_project(root, packet_path.as_posix())
    packet_bytes = _read_limited(packet_path, MAX_BATCH_BYTES)
    digest = hashlib.sha256(packet_bytes).hexdigest()

    if expected_sha256 is not None and digest != _digest(expected_sha256):
        raise ValueError("Контрольная сумма пакета партии изменилась")

    packet = json.loads(packet_bytes, object_pairs_hook=_unique_object)

    if not isinstance(packet, dict) or packet.get("schema_version") != BATCH_SCHEMA:
        raise ValueError("Неизвестный формат пакета партии журналов")

    if packet.get("status") != "prepared_not_registered":
        raise ValueError("Ожидается неизменяемый пакет со статусом prepared_not_registered")

    prepared_at = _timestamp(packet.get("prepared_at"))
    _verify_policy(packet)
    raw_values = packet.get("raw_roots")

    if not isinstance(raw_values, dict) or not raw_values or not set(raw_values) <= COLLECTED_SOURCES:
        raise ValueError("В пакете указаны неизвестные или пустые каталоги источников")

    raw_roots = {source: _inside_project(root, value, directory=True) for source, value in raw_values.items()}
    descriptors = packet.get("records")
    skipped = packet.get("skipped")

    if not isinstance(descriptors, list) or not descriptors or not isinstance(skipped, list):
        raise ValueError("Партия должна содержать непустой список records и список skipped")

    articles: list[CollectedArticle] = []
    sources: list[str] = []
    seen_paths: set[Path] = set()
    seen_urls: set[tuple[str, str]] = set()
    counts = {source: {"cards": 0, "pdfs": 0, "skipped_non_russian": 0} for source in raw_roots}

    for descriptor in descriptors:
        source, record_path = _verify_descriptor(
            descriptor, root=root, raw_roots=raw_roots, seen_paths=seen_paths, skipped=False,
        )
        article = read_collected_article(record_path, source=source, raw_root=raw_roots[source])
        identity = source, article.document.url

        if identity in seen_urls:
            raise ValueError(f"Статья повторяется в составе партии: {article.document.url}")

        seen_urls.add(identity)

        if descriptor["has_pdf"] != (article.pdf_response is not None):
            raise ValueError("Наличие PDF не совпадает с закреплённым составом партии")

        if source == "quantum" and article.pdf_response is not None:
            raise ValueError("В этой партии Quantum разрешены только карточки без PDF")

        for response in (article.card_response, article.pdf_response):
            if response is not None and _timestamp(response.retrieved_at) > prepared_at:
                raise ValueError("HTTP-снимок получен позднее фиксации партии")

        # Повторная проверка замечает изменение живой карточки во время разбора.
        _inside_project(root, descriptor["record_path"])

        if hashlib.sha256(_read_limited(record_path, MAX_RECORD_BYTES)).hexdigest() != descriptor["record_sha256"]:
            raise ValueError("Карточка изменилась во время проверки партии")

        articles.append(article)
        sources.append(source)
        counts[source]["cards"] += 1
        counts[source]["pdfs"] += int(article.pdf_response is not None)

    for descriptor in skipped:
        source, _ = _verify_descriptor(
            descriptor, root=root, raw_roots=raw_roots, seen_paths=seen_paths, skipped=True,
        )
        counts[source]["skipped_non_russian"] += 1

    _verify_counts(packet, counts)

    return FrozenJournalBatch(
        batch_id=packet_path.parent.name,
        path=packet_path,
        sha256=digest,
        prepared_at=packet["prepared_at"],
        records=tuple(articles),
        record_sources=tuple(sources),
        raw_packet=packet,
    )
