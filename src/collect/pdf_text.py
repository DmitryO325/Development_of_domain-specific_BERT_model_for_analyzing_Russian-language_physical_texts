"""Скачивание PDF и извлечение текста (УФН и др.)."""

from __future__ import annotations

import io
import re
import shutil
from pathlib import Path

from .base import fetch_bytes, normalize_whitespace

MIN_PDF_TEXT_CHARS = 500
MIN_CYRILLIC_RATIO = 0.12
COLUMN_GUTTER_PT = 14
COLUMN_SPLIT_RATIO = 0.5
OCR_DPI = 200
LAYOUT_SCAN_DPI = 150
# Отступ ниже линии PACS/DOI / начала колонок (pt)
SPLIT_MARGIN_PT = 10
# Минимальная высота «шапки» в одну колонку перед 2 колонками
MIN_HEADER_PT = 50


def cyrillic_letter_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    cyr = sum(1 for c in letters if "\u0400" <= c <= "\u04FF")
    return cyr / len(letters)


def is_readable_russian(text: str, *, min_chars: int = 200, min_ratio: float = MIN_CYRILLIC_RATIO) -> bool:
    if len(text.strip()) < min_chars:
        return False
    return cyrillic_letter_ratio(text) >= min_ratio


def pdf_url_from_article_path(article_path: str) -> str | None:
    m = re.match(r"/ru/articles/(\d{4})/(\d+)/([a-z])/?", article_path.strip("/") + "/")
    if not m:
        return None
    year, vol, letter = int(m.group(1)), int(m.group(2)), m.group(3)
    yy = str(year)[2:]
    return f"https://ufn.ru/ufn{yy}/ufn{yy}_{vol}/Russian/r{yy}{vol}{letter}.pdf"


def pdf_filename(pdf_url: str) -> str:
    name = pdf_url.rstrip("/").split("/")[-1]
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


def download_pdf(pdf_url: str, dest: Path, *, delay_sec: float = 0.0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = fetch_bytes(pdf_url, delay_sec=delay_sec)
    if not data.startswith(b"%PDF"):
        raise ValueError(f"Не PDF: {pdf_url} ({data[:32]!r})")
    dest.write_bytes(data)
    return dest


def _fitz_open(path: Path):
    try:
        import fitz  # pymupdf
    except ImportError as exc:
        raise ImportError("Установите: pip install pymupdf") from exc
    return fitz.open(path)


def _require_tesseract() -> None:
    if not shutil.which("tesseract"):
        raise RuntimeError(
            "Нужен Tesseract OCR: brew install tesseract tesseract-lang  (macOS)"
        )


def _column_clips(page, *, gutter: float = COLUMN_GUTTER_PT, split: float = COLUMN_SPLIT_RATIO):
    import fitz

    r = page.rect
    mid = r.x0 + r.width * split
    half_g = gutter / 2
    left = fitz.Rect(r.x0, r.y0, mid - half_g, r.y1)
    right = fitz.Rect(mid + half_g, r.y0, r.x1, r.y1)
    return left, right


def _ocr_pixmap(pix, *, lang: str = "rus") -> str:
    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(pix.tobytes("png")))
    return pytesseract.image_to_string(img, lang=lang, config="--psm 6")


def _ocr_page_region(page, clip, *, dpi: int = OCR_DPI, lang: str = "rus") -> str:
    import fitz

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, clip=clip)
    return _ocr_pixmap(pix, lang=lang)


def _ocr_columns_in_rect(page, rect, *, dpi: int = OCR_DPI, lang: str = "rus") -> str:
    """Две колонки внутри rect: сначала левая, потом правая."""
    import fitz

    left, right = _column_clips(page)
    left = left & rect
    right = right & rect
    parts = []
    for clip in (left, right):
        if clip.is_empty or clip.height < 20:
            continue
        parts.append(_ocr_page_region(page, clip, dpi=dpi, lang=lang).rstrip())
    return "\n\n".join(parts)


def _ocr_page_full(page, clip=None, *, dpi: int = OCR_DPI, lang: str = "rus") -> str:
    import fitz

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    if clip is None:
        pix = page.get_pixmap(matrix=mat)
    else:
        pix = page.get_pixmap(matrix=mat, clip=clip)
    return _ocr_pixmap(pix, lang=lang)


def _detect_two_column_split_y(page, *, page_index: int = 0) -> float | None:
    """
    Y (pt) начала двухколоночной части страницы.

    None — вся страница в 2 колонки с верха (типично со 2-й страницы).
    page.rect.height — вся страница одна колонка (нет gutter после шапки).
    иначе — сверху одна колонка до Y, ниже 2 колонки (на стр. 1 после PACS/DOI).
    """
    if page_index > 0:
        return None

    import fitz

    dpi = LAYOUT_SCAN_DPI
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    w, h = pix.width, pix.height
    samples = pix.samples
    strip_px = 12
    left_end, right_start = int(w * 0.38), int(w * 0.62)
    center_lo, center_hi = int(w * 0.42), int(w * 0.58)
    min_y_px = int(h * 0.20)
    score_thresh = 8.0
    persist_need = 2

    def strip_gutter_score(y0: int, y1: int) -> float:
        left = right = center = 0
        for y_px in range(y0, y1, 2):
            for x in range(0, w, 2):
                if samples[y_px * w + x] >= 210:
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
    for y0 in range(min_y_px, h - strip_px, strip_px):
        if strip_gutter_score(y0, y0 + strip_px) >= score_thresh:
            persist += 1
            if persist >= persist_need and first_px is None:
                first_px = y0 - (persist_need - 1) * strip_px
        else:
            persist = 0

    r = page.rect
    if first_px is None:
        return r.height

    split_y = first_px * (r.height / h) + SPLIT_MARGIN_PT
    if split_y >= r.height * 0.88:
        return r.height
    if split_y < MIN_HEADER_PT:
        return r.height
    return split_y


def _ocr_page_layout(page, *, page_index: int = 0, dpi: int = OCR_DPI, lang: str = "rus") -> str:
    """Страница: шапка (1 кол.) + при необходимости 2 колонки снизу."""
    import fitz

    r = page.rect
    split_y = _detect_two_column_split_y(page, page_index=page_index)

    if split_y is None:
        return _ocr_columns_in_rect(page, r, dpi=dpi, lang=lang)

    if split_y >= r.height - 20:
        return _ocr_page_full(page, clip=r, dpi=dpi, lang=lang)

    top = fitz.Rect(r.x0, r.y0, r.x1, split_y)
    bottom = fitz.Rect(r.x0, split_y, r.x1, r.y1)
    t_top = _ocr_page_full(page, clip=top, dpi=dpi, lang=lang).rstrip()
    t_bottom = _ocr_columns_in_rect(page, bottom, dpi=dpi, lang=lang).rstrip()
    if t_top and t_bottom:
        return f"{t_top}\n\n{t_bottom}"
    return t_top or t_bottom


def extract_text_from_pdf(path: Path) -> str:
    parts: list[str] = []
    doc = _fitz_open(path)
    try:
        for page in doc:
            parts.append(page.get_text("text"))
    finally:
        doc.close()
    return _clean_pdf_lines(normalize_whitespace("\n".join(parts)))


def extract_text_from_pdf_ocr(path: Path, *, lang: str = "rus", dpi: int = OCR_DPI) -> str:
    """
    OCR УФН с учётом смешанной вёрстки:
    - верх страницы (название, PACS, DOI) — одна колонка на всю ширину;
    - ниже — левая колонка целиком, затем правая.
    """
    _require_tesseract()

    parts: list[str] = []
    doc = _fitz_open(path)
    try:
        for i, page in enumerate(doc):
            parts.append(_ocr_page_layout(page, page_index=i, dpi=dpi, lang=lang))
    finally:
        doc.close()
    return _clean_pdf_lines(normalize_whitespace("\n\n".join(parts)))


def _clean_pdf_lines(text: str) -> str:
    lines = []
    for ln in text.split("\n"):
        if re.fullmatch(r"\d{1,4}", ln.strip()):
            continue
        lines.append(ln)
    return normalize_whitespace("\n".join(lines))


def save_text_sidecar(pdf_path: Path, text: str, method: str, *, text_dir: Path) -> Path:
    text_dir.mkdir(parents=True, exist_ok=True)
    out = text_dir / f"{pdf_path.stem}_{method}.txt"
    header = (
        f"# extracted via {method}\n"
        f"# source pdf: {pdf_path.name}\n"
        f"# chars: {len(text)}\n"
        f"# layout: top=1 col (title/PACS/DOI), bottom=left col then right col\n\n"
    )
    out.write_text(header + text, encoding="utf-8")
    return out


def extract_best_text(
    pdf_path: Path,
    *,
    text_dir: Path | None = None,
    try_ocr: bool = True,
) -> tuple[str, str, bool]:
    raw = extract_text_from_pdf(pdf_path)
    if is_readable_russian(raw):
        if text_dir:
            save_text_sidecar(pdf_path, raw, "raw", text_dir=text_dir)
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
        except Exception:
            pass

    if text_dir:
        save_text_sidecar(pdf_path, raw, "raw_unreadable", text_dir=text_dir)
    return raw, "pdf_unreadable", False


def extract_text_from_pdf_checked(path: Path, **kwargs) -> tuple[str, bool]:
    text, _method, readable = extract_best_text(path, **kwargs)
    return text, readable


def pdf_to_text(
    pdf_url: str,
    cache_dir: Path,
    *,
    delay_sec: float = 0.0,
    reuse_cached: bool = True,
    text_dir: Path | None = None,
    try_ocr: bool = True,
) -> tuple[str, Path, bool, str]:
    local = cache_dir / pdf_filename(pdf_url)
    if not (reuse_cached and local.is_file() and local.stat().st_size > 1000):
        download_pdf(pdf_url, local, delay_sec=delay_sec)

    if text_dir is None:
        text_dir = cache_dir.parent / "pdf_text"

    text, method, readable = extract_best_text(local, text_dir=text_dir, try_ocr=try_ocr)
    return text, local, readable, method
