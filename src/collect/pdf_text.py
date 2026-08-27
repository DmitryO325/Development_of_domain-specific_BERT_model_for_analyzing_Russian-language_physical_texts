"""Скачивание PDF и извлечение текста (УФН и др.)."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import tempfile
import time
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
        return None

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
    """Y (pt) начала двухколоночной части страницы.

    None — вся страница в 2 колонки с верха (типично со 2-й страницы).
    page.rect.height — вся страница одна колонка (нет gutter после шапки).
    иначе — сверху одна колонка до Y, ниже 2 колонки (на стр. 1 после PACS/DOI).
    """
    if page_index > 0:
        return None

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


def extract_text_from_pdf(path: Path) -> str:
    """Извлечь встроенный текстовый слой из всех страниц PDF."""
    parts: list[str] = []
    document = _fitz_open(path)

    try:
        for page in document:
            parts.append(_clean_pdf_lines(page.get_text("text")))
    finally:
        document.close()

    return _clean_pdf_lines(normalize_whitespace("\n".join(parts)))


def extract_text_from_pdf_ocr(
    path: Path, *, lang: str = "rus", dpi: int = OCR_DPI
) -> str:
    """Распознать PDF УФН с учётом смешанной вёрстки.

    - верх страницы (название, PACS, DOI) — одна колонка на всю ширину;
    - ниже — левая колонка целиком, затем правая.
    """
    _require_tesseract()

    parts: list[str] = []
    document = _fitz_open(path)

    try:
        for i, page in enumerate(document):
            page_text = _ocr_page_layout(page, page_index=i, dpi=dpi, lang=lang)
            parts.append(_clean_pdf_lines(page_text))

    finally:
        document.close()

    return _clean_pdf_lines(normalize_whitespace("\n\n".join(parts)))


def _clean_pdf_lines(text: str) -> str:
    """Удалить номер страницы только с её верхней или нижней границы.

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


def extract_best_text(
    pdf_path: Path,
    *,
    text_dir: Path | None = None,
    try_ocr: bool = True,
) -> tuple[str, str, bool]:
    """Выбрать лучший текст: встроенный PDF-слой, затем OCR.

    Возвращает кортеж ``(текст, метод, читаемость)``. Если ``text_dir``
    задан, имя сопутствующего файла всегда совпадает с возвращённым методом.
    """
    raw = extract_text_from_pdf(pdf_path)

    if is_readable_russian(raw):
        if text_dir:
            save_text_sidecar(pdf_path, raw, "pdf", text_dir=text_dir)

        return raw, "pdf", True

    if try_ocr:
        try:
            ocr = extract_text_from_pdf_ocr(pdf_path)
            method = "pdf_ocr_layout"

            if text_dir:
                save_text_sidecar(pdf_path, ocr, method, text_dir=text_dir)

            if is_readable_russian(ocr):
                return ocr, method, True

            if len(ocr) >= MIN_PDF_TEXT_CHARS:
                return ocr, method, False

        except (ImportError, OSError, RuntimeError, ValueError) as exception:
            LOGGER.warning("Не удалось распознать %s: %s", pdf_path, exception)

    if text_dir:
        save_text_sidecar(pdf_path, raw, "pdf_unreadable", text_dir=text_dir)

    return raw, "pdf_unreadable", False


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
    """Скачать или взять из кэша PDF и извлечь лучший текст.

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
