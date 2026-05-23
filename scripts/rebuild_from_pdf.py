#!/usr/bin/env python3
"""Пересобрать текст в JSONL из уже скачанных PDF (data/raw/pdf/).

Полный текст статей УФН — в PDF (2 колонки), не на HTML-странице.
Извлечённый текст сохраняется в data/raw/pdf_text/*.txt и в поле text JSONL.

Требуется Tesseract для кириллицы:
  brew install tesseract tesseract-lang

Пример:
  python scripts/rebuild_from_pdf.py -i data/raw/ufn_100.jsonl -o data/raw/ufn_100_pdf.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.collect.base import append_jsonl  # noqa: E402
from src.collect.pdf_text import extract_best_text, is_readable_russian  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("-i", "--input", default=str(ROOT / "data/raw/ufn_100.jsonl"))
    p.add_argument("-o", "--output", default=str(ROOT / "data/raw/ufn_100_pdf.jsonl"))
    p.add_argument("--pdf-dir", default=str(ROOT / "data/raw/pdf"))
    p.add_argument("--text-dir", default=str(ROOT / "data/raw/pdf_text"))
    p.add_argument("--no-ocr", action="store_true")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("-n", "--limit", type=int, default=None, help="Сколько статей обработать (например 10)")
    args = p.parse_args()

    inp = Path(args.input)
    out = Path(args.output)
    pdf_dir = Path(args.pdf_dir)
    text_dir = Path(args.text_dir)

    if args.fresh and out.exists():
        out.unlink()

    ok = skip = 0
    for line in inp.read_text(encoding="utf-8").strip().split("\n"):
        if not line.strip():
            continue
        doc = json.loads(line)
        pdf_name = doc.get("extra", {}).get("pdf_file") or (
            Path(doc["pdf_url"]).name if doc.get("pdf_url") else None
        )
        if not pdf_name:
            skip += 1
            continue
        pdf_path = pdf_dir / pdf_name
        if not pdf_path.is_file():
            print(f"  нет PDF: {pdf_name}")
            skip += 1
            continue

        text, method, readable = extract_best_text(
            pdf_path, text_dir=text_dir, try_ocr=not args.no_ocr
        )
        if not readable and not is_readable_russian(text):
            print(f"  пропуск (нечитаемо): {pdf_name} [{method}]")
            skip += 1
            continue

        doc["text"] = text
        doc.setdefault("extra", {})["text_source"] = method
        doc["extra"]["pdf_extract_method"] = method
        doc["extra"]["pdf_text_file"] = str(text_dir / f"{pdf_path.stem}_{method}.txt")

        from src.collect.base import Document

        append_jsonl(
            Document(
                source=doc["source"],
                url=doc["url"],
                title=doc["title"],
                text=text,
                authors=doc.get("authors") or [],
                published=doc.get("published"),
                section=doc.get("section"),
                pdf_url=doc.get("pdf_url"),
                language=doc.get("language", "ru"),
                extra=doc["extra"],
            ),
            out,
        )
        ok += 1
        print(f"  [{ok}] {pdf_name} → {method}, {len(text)} симв.")
        if args.limit is not None and ok >= args.limit:
            break

    print(f"\nГотово: {ok} в {out}, пропущено {skip}")
    print(f"Тексты из PDF: {text_dir}/")


if __name__ == "__main__":
    main()
