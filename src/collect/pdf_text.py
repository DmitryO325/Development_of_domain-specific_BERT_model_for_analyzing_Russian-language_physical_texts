"""Скачивание PDF и извлечение текста (УФН и др.)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from urllib.parse import unquote, urlsplit

from .base import fetch_bytes, normalize_whitespace

if TYPE_CHECKING:
    import fitz

LOGGER = logging.getLogger(__name__)

# Минимальный ориентир по длине текста.
MIN_PDF_TEXT_CHARS = 500
# Не менее 12 % букв должны быть кириллическими.
MIN_CYRILLIC_RATIO = 0.12
# Ширина промежутка между колонками.
COLUMN_GUTTER_PT = 14
# Доля ширины страницы, на которой проходит граница колонок.
COLUMN_SPLIT_RATIO = 0.5
# Разрешение изображения для OCR.
OCR_DPI = 200
# Разрешение для анализа вёрстки.
LAYOUT_SCAN_DPI = 150
# Отступ ниже найденного начала колонок.
SPLIT_MARGIN_PT = 10
# Минимальная высота одноколоночной шапки.
MIN_HEADER_PT = 50

PAGE_EXPORT_SCHEMA_VERSION = "pdf-page-export-v1"
SUPPORTED_EXTRACTION_METHODS = frozenset(
    {"pdf", "pdf_ocr_layout", "pdf_unreadable"}
)


@dataclass(frozen=True, slots=True)
class PdfPageText:
    """Текст одной физической страницы PDF с устойчивой нумерацией."""

    page_index: int
    page_number: int
    text: str

    def __post_init__(self) -> None:
        """Проверить связь машинного индекса и человеческого номера."""

        if self.page_index < 0:
            raise ValueError("page_index не может быть отрицательным")

        if self.page_number != self.page_index + 1:
            raise ValueError("page_number должен быть равен page_index + 1")


@dataclass(frozen=True, slots=True)
class PdfTextExtraction:
    """Выбранный результат извлечения всего PDF и составляющие его страницы."""

    text: str
    method: str
    readable: bool
    pages: tuple[PdfPageText, ...]


@dataclass(frozen=True, slots=True)
class ExportedPdfPage:
    """Метаданные сохранённого постраничного TXT-файла."""

    page_index: int
    page_number: int
    path: Path
    sha256: str
    characters: int


@dataclass(frozen=True, slots=True)
class PdfPageExportResult:
    """Результат записи постраничных TXT-файлов и их индекса."""

    manifest_path: Path
    pages: tuple[ExportedPdfPage, ...]


def cyrillic_letter_ratio(text: str) -> float:
    """Вычислить долю кириллицы среди всех букв текста."""

    letters = [character for character in text if character.isalpha()]

    if not letters:
        return 0.0

    cyrillic = sum(1 for char in letters if "\u0400" <= char <= "\u04ff")
    return cyrillic / len(letters)


def is_readable_russian(
    text: str,
    *,
    min_chars: int = 200,
    min_ratio: float = MIN_CYRILLIC_RATIO,
) -> bool:
    """Проверить, похож ли извлечённый текст на читаемый русский."""

    if min_chars < 0:
        raise ValueError("min_chars не может быть отрицательным")

    if not 0 <= min_ratio <= 1:
        raise ValueError("min_ratio должен быть в диапазоне от 0 до 1")

    if len(text.strip()) < min_chars:
        return False

    return cyrillic_letter_ratio(text) >= min_ratio


def pdf_url_from_article_path(article_path: str) -> str | None:
    """Построить резервный PDF-адрес по пути статьи УФН."""

    normalized_path = f"/{article_path.strip('/')}"

    matched = re.fullmatch(
        r"/ru/articles/(\d{4})/(\d+)/([a-z])/?",
        normalized_path,
        flags=re.IGNORECASE,
    )

    if not matched:
        return

    year = int(matched.group(1))
    volume = int(matched.group(2))
    letter = matched.group(3).lower()
    yy = str(year)[2:]

    return (
        f"https://ufn.ru/ufn{year}/ufn{year}_{volume}/Russian/r{yy}{volume}{letter}.pdf"
    )


def pdf_filename(pdf_url: str) -> str:
    """Получить безопасное локальное имя PDF из URL."""

    name = Path(unquote(urlsplit(pdf_url).path)).name

    if not name:
        raise ValueError(f"В URL нет имени файла: {pdf_url}")

    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


def download_pdf(
    pdf_url: str,
    destination: Path,
    *,
    delay_seconds: float = 0.0,
) -> Path:
    """Скачать PDF, проверить сигнатуру и сохранить файл."""

    destination.parent.mkdir(parents=True, exist_ok=True)

    if delay_seconds:
        time.sleep(delay_seconds)

    data = fetch_bytes(pdf_url, delay_seconds=delay_seconds)

    if not data.startswith(b"%PDF") or b"%%EOF" not in data[-4096:]:
        raise ValueError(f"Не PDF: {pdf_url} ({data[:32]!r})")

    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as temporary_file:
            temporary_file.write(data)
            temporary_path = Path(temporary_file.name)

        temporary_path.replace(destination)

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return destination


def _fitz_open(path: Path) -> fitz.Document:
    """Открыть PDF через PyMuPDF с понятной ошибкой о зависимости."""

    try:
        import fitz  # pymupdf

    except ImportError as exception:
        raise ImportError("Установите: pip install pymupdf") from exception

    return fitz.open(path)


def _require_tesseract() -> None:
    """Проверить, что исполняемый файл Tesseract доступен в PATH."""

    if not shutil.which("tesseract"):
        raise RuntimeError(
            "Нужен Tesseract OCR: brew install tesseract tesseract-lang (macOS)"
        )


def _column_clips(
    page: fitz.Page,
    *,
    gutter: float = COLUMN_GUTTER_PT,
    split: float = COLUMN_SPLIT_RATIO,
) -> tuple[fitz.Rect, fitz.Rect]:
    """Разделить страницу на левую и правую колонки."""

    import fitz

    rectangle = page.rect
    middle = rectangle.x0 + rectangle.width * split
    half_gutter = gutter / 2
    left = fitz.Rect(rectangle.x0, rectangle.y0, middle - half_gutter, rectangle.y1)
    right = fitz.Rect(middle + half_gutter, rectangle.y0, rectangle.x1, rectangle.y1)

    return left, right


def _ocr_pixmap(pixmap: fitz.Pixmap, *, lang: str = "rus") -> str:
    """Распознать текст на растровом изображении PyMuPDF."""

    import pytesseract
    from PIL import Image

    with Image.open(io.BytesIO(pixmap.tobytes("png"))) as image:
        return pytesseract.image_to_string(image, lang=lang, config="--psm 6")


def _ocr_page_region(
    page: fitz.Page,
    clip: fitz.Rect,
    *,
    dpi: int = OCR_DPI,
    lang: str = "rus",
) -> str:
    """Распознать заданную прямоугольную область PDF-страницы."""

    import fitz

    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, clip=clip)
    return _ocr_pixmap(pixmap, lang=lang)


def _ocr_columns_in_rect(
    page: fitz.Page,
    rectangle: fitz.Rect,
    *,
    dpi: int = OCR_DPI,
    lang: str = "rus",
) -> str:
    """Распознать две колонки: сначала левую, затем правую."""

    left, right = _column_clips(page)
    left = left & rectangle
    right = right & rectangle
    parts: list[str] = []

    for clip in (left, right):
        if clip.is_empty or clip.height < 20:
            continue

        parts.append(_ocr_page_region(page, clip, dpi=dpi, lang=lang).rstrip())

    return "\n\n".join(parts)


def _ocr_page_full(
    page: fitz.Page,
    clip: fitz.Rect | None = None,
    *,
    dpi: int = OCR_DPI,
    lang: str = "rus",
) -> str:
    """Распознать всю PDF-страницу или одну её область."""

    import fitz

    matrix = fitz.Matrix(dpi / 72, dpi / 72)

    if clip is None:
        pixmap = page.get_pixmap(matrix=matrix)

    else:
        pixmap = page.get_pixmap(matrix=matrix, clip=clip)

    return _ocr_pixmap(pixmap, lang=lang)


def _detect_two_column_split_y(
    page: fitz.Page,
    *,
    page_index: int = 0,
) -> float | None:
    """
    Y (pt) начала двухколоночной части страницы.

    None — вся страница в 2 колонки с верха (типично со 2-й страницы).
    page.rect.height — вся страница одна колонка (нет gutter после шапки).
    иначе — сверху одна колонка до Y, ниже 2 колонки (на стр. 1 после PACS/DOI).
    """

    if page_index > 0:
        return

    import fitz

    dpi = LAYOUT_SCAN_DPI
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csGRAY)

    width, height = pixmap.width, pixmap.height
    samples = pixmap.samples

    strip_px = 12

    left_end, right_start = int(width * 0.38), int(width * 0.62)
    center_lo, center_hi = int(width * 0.42), int(width * 0.58)

    min_y_px = int(height * 0.20)

    score_thresh = 8.0
    persist_need = 2

    def strip_gutter_score(y0: int, y1: int) -> float:
        """Оценить, насколько горизонтальная полоса похожа на две колонки."""

        left = right = center = 0

        for y_px in range(y0, y1, 2):
            for x in range(0, width, 2):
                if samples[y_px * width + x] >= 210:
                    continue

                if x < left_end:
                    left += 1

                elif x > right_start:
                    right += 1

                elif center_lo <= x <= center_hi:
                    center += 1

        if left < 20 or right < 20:
            return 0.0

        return (left + right) / (center + 1)

    first_px: int | None = None
    persist = 0

    for y0 in range(min_y_px, height - strip_px, strip_px):
        if strip_gutter_score(y0, y0 + strip_px) >= score_thresh:
            persist += 1

            if persist >= persist_need and first_px is None:
                first_px = y0 - (persist_need - 1) * strip_px

        else:
            persist = 0

    rectangle = page.rect

    if first_px is None:
        return rectangle.height

    split_y = first_px * (rectangle.height / height) + SPLIT_MARGIN_PT

    if split_y >= rectangle.height * 0.88:
        return rectangle.height

    if split_y < MIN_HEADER_PT:
        return rectangle.height

    return split_y


def _ocr_page_layout(
    page: fitz.Page,
    *,
    page_index: int = 0,
    dpi: int = OCR_DPI,
    lang: str = "rus",
) -> str:
    """Распознать шапку в одну колонку, а основной текст — в две."""

    import fitz

    rectangle = page.rect
    split_y = _detect_two_column_split_y(page, page_index=page_index)

    if split_y is None:
        return _ocr_columns_in_rect(page, rectangle, dpi=dpi, lang=lang)

    if split_y >= rectangle.height - 20:
        return _ocr_page_full(page, clip=rectangle, dpi=dpi, lang=lang)

    top = fitz.Rect(rectangle.x0, rectangle.y0, rectangle.x1, split_y)
    bottom = fitz.Rect(rectangle.x0, split_y, rectangle.x1, rectangle.y1)

    t_top = _ocr_page_full(page, clip=top, dpi=dpi, lang=lang).rstrip()
    t_bottom = _ocr_columns_in_rect(page, bottom, dpi=dpi, lang=lang).rstrip()

    if t_top and t_bottom:
        return f"{t_top}\n\n{t_bottom}"

    return t_top or t_bottom


def _combine_page_texts(
    pages: tuple[PdfPageText, ...],
    *,
    method: str,
) -> str:
    """Собрать общий текст, не теряя выбранный порядок страниц."""

    if method not in SUPPORTED_EXTRACTION_METHODS:
        raise ValueError(f"Неизвестный метод извлечения: {method}")

    separator = "\n\n" if method == "pdf_ocr_layout" else "\n"
    combined = separator.join(page.text for page in pages)

    return _clean_pdf_lines(normalize_whitespace(combined))


def extract_pages_from_pdf(path: Path) -> tuple[PdfPageText, ...]:
    """Извлечь встроенный текст отдельно из каждой физической страницы PDF."""

    pages: list[PdfPageText] = []
    document = _fitz_open(path)

    try:
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            page_text = page.get_text("text")

            if not isinstance(page_text, str):
                raise TypeError("PyMuPDF вернул не строку для режима text")

            pages.append(
                PdfPageText(
                    page_index=page_index,
                    page_number=page_index + 1,
                    text=_clean_pdf_lines(page_text),
                )
            )

    finally:
        document.close()

    return tuple(pages)


def extract_pages_from_pdf_ocr(
    path: Path,
    *,
    lang: str = "rus",
    dpi: int = OCR_DPI,
) -> tuple[PdfPageText, ...]:
    """
    Распознать каждую страницу PDF УФН с учётом смешанной вёрстки.

    Верх первой страницы распознаётся как одна колонка, а основной текст —
    как левая и правая колонки. Граница каждой физической страницы сохраняется.
    """

    _require_tesseract()

    pages: list[PdfPageText] = []
    document = _fitz_open(path)

    try:
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            page_text = _ocr_page_layout(
                page,
                page_index=page_index,
                dpi=dpi,
                lang=lang,
            )
            pages.append(
                PdfPageText(
                    page_index=page_index,
                    page_number=page_index + 1,
                    text=_clean_pdf_lines(page_text),
                )
            )

    finally:
        document.close()

    return tuple(pages)


def extract_text_from_pdf(path: Path) -> str:
    """Извлечь встроенный текстовый слой из всех страниц PDF."""

    pages = extract_pages_from_pdf(path)
    return _combine_page_texts(pages, method="pdf")


def extract_text_from_pdf_ocr(
    path: Path,
    *,
    lang: str = "rus",
    dpi: int = OCR_DPI,
) -> str:
    """Распознать все страницы PDF УФН с учётом смешанной вёрстки."""

    pages = extract_pages_from_pdf_ocr(path, lang=lang, dpi=dpi)
    return _combine_page_texts(pages, method="pdf_ocr_layout")


def _clean_pdf_lines(text: str) -> str:
    """
    Удалить номер страницы только с её верхней или нижней границы.

    Отдельные числа внутри текста сохраняются: это могут быть годы
    или физические величины.
    """

    lines = text.splitlines()
    nonempty_indices = [index for index, line in enumerate(lines) if line.strip()]

    if not nonempty_indices:
        return ""

    for index in {nonempty_indices[0], nonempty_indices[-1]}:
        if re.fullmatch(r"\d{1,3}", lines[index].strip()):
            lines[index] = ""

    return normalize_whitespace("\n".join(lines))


def save_text_sidecar(
    pdf_path: Path, text: str, method: str, *, text_dir: Path
) -> Path:
    """Сохранить извлечённый текст и технический заголовок в UTF-8."""

    text_dir.mkdir(parents=True, exist_ok=True)
    output_path = text_dir / f"{pdf_path.stem}_{method}.txt"

    layout = (
        "top=1 col (title/PACS/DOI), bottom=left col then right col"
        if method == "pdf_ocr_layout"
        else "embedded text layer"
    )

    header = (
        f"# extracted via {method}\n"
        f"# source pdf: {pdf_path.name}\n"
        f"# chars: {len(text)}\n"
        f"# layout: {layout}\n\n"
    )

    output_path.write_text(header + text, encoding="utf-8")
    return output_path


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Атомарно заменить файл заданными байтами."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".part",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        temporary_path.replace(path)

    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    """Вычислить SHA-256 файла без загрузки целого файла в память."""

    digest = hashlib.sha256()

    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)

    return digest.hexdigest()


def _page_export_record(
    page: ExportedPdfPage,
    *,
    output_dir: Path,
    source_pdf_path: str,
    source_pdf_sha256: str,
    extraction_method: str,
    extraction_version: str,
) -> dict[str, str | int]:
    """Собрать одну строку индекса постраничного экспорта."""

    return {
        "schema_version": PAGE_EXPORT_SCHEMA_VERSION,
        "source_pdf_path": source_pdf_path,
        "source_pdf_sha256": source_pdf_sha256,
        "extraction_method": extraction_method,
        "extraction_version": extraction_version,
        "page_index": page.page_index,
        "page_number": page.page_number,
        "path": page.path.relative_to(output_dir).as_posix(),
        "sha256": page.sha256,
        "characters": page.characters,
    }


def _raise_page_export_conflict(output_dir: Path, detail: str) -> None:
    """Отклонить изменение уже опубликованного набора страниц."""

    raise ValueError(
        f"Конфликт постраничного экспорта {output_dir}: {detail}; "
        "смените extraction_version"
    )


def _verify_published_page_export(
    output_dir: Path,
    *,
    expected_files: dict[str, bytes],
) -> None:
    """Проверить неизменность уже опубликованного набора без перезаписи."""

    if output_dir.is_symlink() or not output_dir.is_dir():
        _raise_page_export_conflict(output_dir, "путь не является каталогом")

    actual_names = {path.name for path in output_dir.iterdir()}
    expected_names = set(expected_files)

    if actual_names != expected_names:
        _raise_page_export_conflict(output_dir, "состав файлов отличается")

    for file_name, expected_data in expected_files.items():
        file_path = output_dir / file_name

        if file_path.is_symlink() or not file_path.is_file():
            _raise_page_export_conflict(
                output_dir,
                f"{file_name} не является обычным файлом",
            )

        if file_path.read_bytes() != expected_data:
            _raise_page_export_conflict(
                output_dir,
                f"содержимое {file_name} отличается",
            )


def export_pdf_pages(
    pdf_path: Path,
    extraction: PdfTextExtraction,
    output_dir: Path,
    *,
    extraction_version: str,
    source_pdf_path: str | None = None,
    source_pdf_sha256: str | None = None,
) -> PdfPageExportResult:
    """Сохранить чистый TXT каждой страницы и воспроизводимый JSONL-индекс."""

    if extraction.method not in SUPPORTED_EXTRACTION_METHODS:
        raise ValueError(f"Неизвестный метод извлечения: {extraction.method}")

    if not extraction_version.strip():
        raise ValueError("extraction_version не может быть пустой")

    if not extraction.pages:
        raise ValueError(f"PDF не содержит страниц: {pdf_path}")

    for expected_index, page in enumerate(extraction.pages):
        if page.page_index != expected_index:
            raise ValueError("Страницы должны идти подряд начиная с page_index=0")

    combined_text = _combine_page_texts(
        extraction.pages,
        method=extraction.method,
    )

    if combined_text != extraction.text:
        raise ValueError(
            "Общий текст не совпадает с текстом, собранным из страниц"
        )

    actual_source_sha256 = _file_sha256(pdf_path)

    if (
        source_pdf_sha256 is not None
        and source_pdf_sha256 != actual_source_sha256
    ):
        raise ValueError(f"SHA-256 исходного PDF не совпадает: {pdf_path}")

    source_label = source_pdf_path or pdf_path.as_posix()
    exported_pages: list[ExportedPdfPage] = []
    page_files: dict[str, bytes] = {}

    for page in extraction.pages:
        file_name = f"page_{page.page_number:04d}.txt"
        output_path = output_dir / file_name
        encoded_text = page.text.encode("utf-8")
        sha256 = hashlib.sha256(encoded_text).hexdigest()
        page_files[file_name] = encoded_text
        exported_pages.append(
            ExportedPdfPage(
                page_index=page.page_index,
                page_number=page.page_number,
                path=output_path,
                sha256=sha256,
                characters=len(page.text),
            )
        )

    records = [
        _page_export_record(
            page,
            output_dir=output_dir,
            source_pdf_path=source_label,
            source_pdf_sha256=actual_source_sha256,
            extraction_method=extraction.method,
            extraction_version=extraction_version,
        )
        for page in exported_pages
    ]
    serialized_records = [
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        for record in records
    ]
    manifest_data = "".join(
        f"{serialized_record}\n"
        for serialized_record in serialized_records
    ).encode("utf-8")
    expected_files = {**page_files, "pages.jsonl": manifest_data}
    manifest_path = output_dir / "pages.jsonl"

    if output_dir.exists() or output_dir.is_symlink():
        _verify_published_page_export(
            output_dir,
            expected_files=expected_files,
        )

        return PdfPageExportResult(
            manifest_path=manifest_path,
            pages=tuple(exported_pages),
        )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_path = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.",
            suffix=".staging",
            dir=output_dir.parent,
        )
    )

    try:
        for file_name, data in expected_files.items():
            _write_bytes_atomic(staging_path / file_name, data)

        if output_dir.exists() or output_dir.is_symlink():
            _verify_published_page_export(
                output_dir,
                expected_files=expected_files,
            )

        else:
            try:
                staging_path.rename(output_dir)

            except OSError:
                if not output_dir.exists() and not output_dir.is_symlink():
                    raise

                _verify_published_page_export(
                    output_dir,
                    expected_files=expected_files,
                )

    finally:
        if staging_path.exists():
            shutil.rmtree(staging_path)

    return PdfPageExportResult(
        manifest_path=manifest_path,
        pages=tuple(exported_pages),
    )


def extract_best_text_result(
    pdf_path: Path,
    *,
    text_dir: Path | None = None,
    try_ocr: bool = True,
) -> PdfTextExtraction:
    """
    Выбрать лучший общий и постраничный текст: PDF-слой, затем OCR.

    Один проход каждого способа сразу даёт общий текст и устойчивые границы
    физических страниц. OCR для одного варианта повторно не запускается.
    """

    raw_pages = extract_pages_from_pdf(pdf_path)
    raw = _combine_page_texts(raw_pages, method="pdf")

    if is_readable_russian(raw):
        if text_dir:
            save_text_sidecar(pdf_path, raw, "pdf", text_dir=text_dir)

        return PdfTextExtraction(
            text=raw,
            method="pdf",
            readable=True,
            pages=raw_pages,
        )

    if try_ocr:
        try:
            ocr_pages = extract_pages_from_pdf_ocr(pdf_path)
            method = "pdf_ocr_layout"
            ocr = _combine_page_texts(ocr_pages, method=method)

            if text_dir:
                save_text_sidecar(pdf_path, ocr, method, text_dir=text_dir)

            if is_readable_russian(ocr):
                return PdfTextExtraction(
                    text=ocr,
                    method=method,
                    readable=True,
                    pages=ocr_pages,
                )

            if len(ocr) >= MIN_PDF_TEXT_CHARS:
                return PdfTextExtraction(
                    text=ocr,
                    method=method,
                    readable=False,
                    pages=ocr_pages,
                )

        except (ImportError, OSError, RuntimeError, ValueError) as exception:
            LOGGER.warning("Не удалось распознать %s: %s", pdf_path, exception)

    if text_dir:
        save_text_sidecar(pdf_path, raw, "pdf_unreadable", text_dir=text_dir)

    return PdfTextExtraction(
        text=raw,
        method="pdf_unreadable",
        readable=False,
        pages=raw_pages,
    )


def extract_best_text(
    pdf_path: Path,
    *,
    text_dir: Path | None = None,
    try_ocr: bool = True,
) -> tuple[str, str, bool]:
    """
    Выбрать лучший текст и вернуть совместимый кортеж результата.

    Если ``text_dir`` задан, имя сопутствующего файла совпадает с методом.
    """

    result = extract_best_text_result(
        pdf_path,
        text_dir=text_dir,
        try_ocr=try_ocr,
    )

    return result.text, result.method, result.readable


def extract_text_from_pdf_checked(
    path: Path,
    *,
    text_dir: Path | None = None,
    try_ocr: bool = True,
) -> tuple[str, bool]:
    """Извлечь текст и вернуть только текст и признак читаемости."""

    text, _method, readable = extract_best_text(
        path,
        text_dir=text_dir,
        try_ocr=try_ocr,
    )

    return text, readable


def _is_valid_cached_pdf(path: Path) -> bool:
    """Проверить сигнатуру и маркер конца кэшированного PDF."""

    if not path.is_file() or path.stat().st_size < 8:
        return False

    with path.open("rb") as file:
        if file.read(4) != b"%PDF":
            return False

        file.seek(max(0, path.stat().st_size - 4096))
        return b"%%EOF" in file.read()


def _source_url_path(pdf_path: Path) -> Path:
    """Построить путь файла с URL-источником кэша."""

    return pdf_path.with_suffix(f"{pdf_path.suffix}.source-url")


def _cache_path(pdf_url: str, cache_dir: Path) -> Path:
    """Выбрать путь кэша без коллизий одинаковых имён PDF."""

    original = cache_dir / pdf_filename(pdf_url)
    source_path = _source_url_path(original)

    if not original.exists() or not source_path.exists():
        return original

    try:
        cached_url = source_path.read_text(encoding="utf-8").strip()

    except OSError:
        cached_url = ""

    if cached_url == pdf_url:
        return original

    digest = hashlib.sha256(pdf_url.encode("utf-8")).hexdigest()[:12]
    return original.with_name(f"{original.stem}_{digest}{original.suffix}")


def _record_source_url(pdf_path: Path, pdf_url: str) -> None:
    """Сохранить URL, которому соответствует локальный PDF."""

    source_path = _source_url_path(pdf_path)
    source_path.write_text(f"{pdf_url}\n", encoding="utf-8")


def pdf_to_text(
    pdf_url: str,
    cache_dir: Path,
    *,
    delay_seconds: float = 0.0,
    reuse_cached: bool = True,
    text_dir: Path | None = None,
    try_ocr: bool = True,
) -> tuple[str, Path, bool, str]:
    """
    Скачать или взять из кэша PDF и извлечь лучший текст.
    Возвращает ``(текст, путь PDF, читаемость, метод)``.
    """

    local = _cache_path(pdf_url, cache_dir)
    used_cache = reuse_cached and _is_valid_cached_pdf(local)

    if not used_cache:
        download_pdf(pdf_url, local, delay_seconds=delay_seconds)
        _record_source_url(local, pdf_url)

    if text_dir is None:
        text_dir = cache_dir.parent / "pdf_text"

    try:
        text, method, readable = extract_best_text(
            local,
            text_dir=text_dir,
            try_ocr=try_ocr,
        )

    except (OSError, RuntimeError, ValueError):
        if not used_cache:
            raise

        LOGGER.warning("Повреждённый PDF в кэше %s; скачиваем заново", local)
        download_pdf(pdf_url, local, delay_seconds=delay_seconds)
        _record_source_url(local, pdf_url)

        text, method, readable = extract_best_text(
            local,
            text_dir=text_dir,
            try_ocr=try_ocr,
        )

    if not _source_url_path(local).exists():
        _record_source_url(local, pdf_url)

    return text, local, readable, method
