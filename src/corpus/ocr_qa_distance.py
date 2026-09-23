"""Нормализация прозы и точный расчёт CER и WER для OCR QA."""

from __future__ import annotations

import re
import unicodedata

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

OCR_PROSE_NORMALIZATION_VERSION = "ocr-prose-norm-v1"
OCR_METRIC_VERSION = "ocr-metrics-v1"
_SOFT_HYPHEN = "\u00ad"
_WHITESPACE_PATTERN = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class LevenshteinResult:
    """Хранить счётчики выравнивания Левенштейна."""

    reference_length: int
    candidate_length: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def distance(self) -> int:
        """Вернуть полное расстояние Левенштейна."""

        return self.substitutions + self.deletions + self.insertions

    @property
    def error_rate(self) -> float | None:
        """Вернуть долю ошибок или ``None`` без эталона."""

        if self.reference_length == 0:
            return

        return self.distance / self.reference_length


@dataclass(frozen=True, slots=True)
class OcrMetricsResult:
    """Хранить тексты и метрики на двух уровнях."""

    normalized_reference: str
    normalized_candidate: str
    characters: LevenshteinResult
    words: LevenshteinResult
    normalization_version: str = OCR_PROSE_NORMALIZATION_VERSION
    metric_version: str = OCR_METRIC_VERSION

    @property
    def cer(self) -> float | None:
        """Вернуть долю ошибок по кодовым точкам Unicode."""

        return self.characters.error_rate

    @property
    def wer(self) -> float | None:
        """Вернуть долю ошибок по словам."""

        return self.words.error_rate


def normalize_ocr_prose(text: str) -> str:
    """
    Применить ``ocr-prose-norm-v1`` без угадывания переносов.

    Склейка ложного дефиса и переноса строки зависит от
    изображения. До вызова функции из обоих текстов нужно
    вручную выделить одни и те же видимые области прозы и
    разрешить ложные переносы по изображению. Остальные символы
    кандидата исправлять нельзя. Функция сохраняет настоящий дефис,
    а ``"-\n"`` не склеивает.
    """

    if not isinstance(text, str):
        raise TypeError("Текст для нормализации должен быть строкой")

    normalized = unicodedata.normalize("NFC", text)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _WHITESPACE_PATTERN.sub(" ", normalized.strip())

    return normalized.replace(_SOFT_HYPHEN, "")


def tokenize_wer_words(text: str) -> tuple[str, ...]:
    """Нормализовать текст и разделить его на слова."""

    normalized = normalize_ocr_prose(text)

    if not normalized:
        return ()

    return tuple(normalized.split(" "))


def levenshtein_counts(
    reference: Sequence[str],
    candidate: Sequence[str],
) -> LevenshteinResult:
    """
    Точно посчитать замены, удаления и вставки.

    При нескольких оптимальных путях выбирается путь с
    меньшим числом вставок и удалений, а затем с меньшим
    числом удалений. Такое правило делает счётчики
    детерминированными.
    """

    reference_units = _validated_units(reference, field_name="reference")
    candidate_units = _validated_units(candidate, field_name="candidate")
    distance = _levenshtein_distance(reference_units, candidate_units)
    encoded_counts = _encoded_counts_in_band(
        reference_units,
        candidate_units,
        band_width=distance,
    )
    substitutions, deletions, insertions = _decode_counts(
        encoded_counts,
        reference_length=len(reference_units),
        candidate_length=len(candidate_units),
    )

    return LevenshteinResult(
        reference_length=len(reference_units),
        candidate_length=len(candidate_units),
        substitutions=substitutions,
        deletions=deletions,
        insertions=insertions,
    )


def calculate_ocr_metrics(
    reference_text: str,
    candidate_text: str,
) -> OcrMetricsResult:
    """Нормализовать два текста и рассчитать их CER и WER."""

    normalized_reference = normalize_ocr_prose(reference_text)
    normalized_candidate = normalize_ocr_prose(candidate_text)
    reference_words = _split_normalized_words(normalized_reference)
    candidate_words = _split_normalized_words(normalized_candidate)

    return OcrMetricsResult(
        normalized_reference=normalized_reference,
        normalized_candidate=normalized_candidate,
        characters=levenshtein_counts(
            normalized_reference,
            normalized_candidate,
        ),
        words=levenshtein_counts(reference_words, candidate_words),
    )


def calculate_ocr_metrics_from_files(
    reference_path: str | Path,
    candidate_path: str | Path,
) -> OcrMetricsResult:
    """Прочитать два UTF-8-файла и рассчитать CER и WER."""

    resolved_reference = _validated_file(reference_path, label="эталона")
    resolved_candidate = _validated_file(candidate_path, label="кандидата")
    reference_text = resolved_reference.read_text(encoding="utf-8")
    candidate_text = resolved_candidate.read_text(encoding="utf-8")

    return calculate_ocr_metrics(reference_text, candidate_text)


def _validated_units(
    units: Sequence[str],
    *,
    field_name: str,
) -> tuple[str, ...]:
    """Проверить и зафиксировать последовательность."""

    if not isinstance(units, Sequence):
        raise TypeError(
            f"Поле {field_name} должно быть "
            "последовательностью"
        )

    frozen_units = tuple(units)

    if any(not isinstance(unit, str) for unit in frozen_units):
        raise TypeError(
            f"Все единицы поля {field_name} должны быть строками"
        )

    return frozen_units


def _levenshtein_distance(
    reference: tuple[str, ...],
    candidate: tuple[str, ...],
) -> int:
    """Вычислить расстояние алгоритмом Майерса."""

    if not reference:
        return len(candidate)

    equality_masks: dict[str, int] = {}

    for unit_index, unit in enumerate(reference):
        equality_masks[unit] = equality_masks.get(unit, 0) | (1 << unit_index)

    full_mask = (1 << len(reference)) - 1
    highest_bit = 1 << (len(reference) - 1)
    positive_vertical = full_mask
    negative_vertical = 0
    distance = len(reference)

    for unit in candidate:
        equality = equality_masks.get(unit, 0)
        vertical_or_equality = equality | negative_vertical
        horizontal = (
            ((equality & positive_vertical) + positive_vertical)
            ^ positive_vertical
        ) | equality
        positive_horizontal = negative_vertical | ~(
            horizontal | positive_vertical
        )
        negative_horizontal = positive_vertical & horizontal

        if positive_horizontal & highest_bit:
            distance += 1
        elif negative_horizontal & highest_bit:
            distance -= 1

        positive_horizontal = ((positive_horizontal << 1) | 1) & full_mask
        negative_horizontal = (negative_horizontal << 1) & full_mask
        positive_vertical = (
            negative_horizontal
            | ~(vertical_or_equality | positive_horizontal)
        ) & full_mask
        negative_vertical = (
            positive_horizontal & vertical_or_equality
        ) & full_mask

    return distance


def _encoded_counts_in_band(
    reference: tuple[str, ...],
    candidate: tuple[str, ...],
    *,
    band_width: int,
) -> int:
    """Найти счётчики в точной диагональной полосе."""

    reference_length = len(reference)
    candidate_length = len(candidate)
    base = reference_length + candidate_length + 1
    distance_weight = base * base
    insertion_cost = distance_weight + base
    deletion_cost = insertion_cost + 1
    substitution_cost = distance_weight
    previous = {
        candidate_index: candidate_index * insertion_cost
        for candidate_index in range(min(candidate_length, band_width) + 1)
    }

    for reference_index in range(1, reference_length + 1):
        current: dict[int, int] = {}
        first_candidate_index = max(0, reference_index - band_width)
        last_candidate_index = min(
            candidate_length,
            reference_index + band_width,
        )

        for candidate_index in range(
            first_candidate_index,
            last_candidate_index + 1,
        ):
            paths: list[int] = []
            diagonal = previous.get(candidate_index - 1)

            if diagonal is not None:
                if reference[reference_index - 1] == candidate[candidate_index - 1]:
                    paths.append(diagonal)
                else:
                    paths.append(diagonal + substitution_cost)

            deleted = previous.get(candidate_index)

            if deleted is not None:
                paths.append(deleted + deletion_cost)

            inserted = current.get(candidate_index - 1)

            if inserted is not None:
                paths.append(inserted + insertion_cost)

            if paths:
                current[candidate_index] = min(paths)

        previous = current

    encoded_counts = previous.get(candidate_length)

    if encoded_counts is None:
        raise RuntimeError(
            "Не удалось восстановить путь Левенштейна"
        )

    return encoded_counts


def _decode_counts(
    encoded_counts: int,
    *,
    reference_length: int,
    candidate_length: int,
) -> tuple[int, int, int]:
    """Разложить целочисленный ключ на счётчики."""

    base = reference_length + candidate_length + 1
    distance_weight = base * base
    distance, remainder = divmod(encoded_counts, distance_weight)
    insertion_deletion_count, deletions = divmod(remainder, base)
    insertions = insertion_deletion_count - deletions
    substitutions = distance - insertion_deletion_count

    return substitutions, deletions, insertions


def _split_normalized_words(normalized_text: str) -> tuple[str, ...]:
    """Разделить нормализованную строку на слова."""

    if not normalized_text:
        return ()

    return tuple(normalized_text.split(" "))


def _validated_file(value: str | Path, *, label: str) -> Path:
    """Проверить путь к обычному файлу."""

    path = Path(value)

    if not path.is_file():
        raise FileNotFoundError(f"Файл {label} не найден: {path}")

    return path
