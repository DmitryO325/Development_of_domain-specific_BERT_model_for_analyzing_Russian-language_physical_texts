Ссылка на документ:
https://docs.google.com/document/d/1vGZf0AHvZ7xSU9tAO8pq1zRjB3PX-vWJBOpoi_SA5v4/edit?tab=t.0

Локальная копия плана: `docs/plan_from_google.txt` · план: `docs/PLAN.md` · архитектура: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · **парсеры (подробно):** [`docs/PARSERS.md`](docs/PARSERS.md) · ресурсы: `docs/RESOURCES.md`

Ссылка на Git:
https://github.com/DmitryO325/Development_of_domain-specific_BERT_model_for_analyzing_Russian-language_physical_texts

### Сбор текстов с сайтов

```bash
# Пилот: 1 выпуск УФН + RSS (≈20 статей)
python scripts/scrape.py pilot

# УФН: 100 статей (PDF скачивается; текст — из PDF или HTML, см. text_source в JSONL)
pip install pymupdf
python scripts/scrape.py ufn --max-docs 100 --fresh -o data/raw/ufn_100.jsonl

# Только HTML (если PDF на ufn.ru даёт кракозябры — так и бывает из-за шрифтов журнала)
python scripts/scrape.py ufn --text-source html --max-docs 100 --fresh -o data/raw/ufn_100.jsonl

# Явно ограничить число выпусков (например только свежие)
python scripts/scrape.py ufn --max-issues 3 --max-docs 20

# Только HTML с сайта (старый режим)
python scripts/scrape.py ufn --text-source html --max-docs 10

# УФН: несколько выпусков без лимита по числу
python scripts/scrape.py ufn --max-issues 15 -o data/raw/ufn.jsonl

# RSS (Квантовая электроника, УФН)
python scripts/scrape.py rss --limit 30
```

Результат: `data/raw/corpus.jsonl` (JSONL: title, text, authors, url, source).

### Куда попадает текст (важно)

| Путь | Что там |
|------|---------|
| `data/raw/pdf/*.pdf` | Скачанные PDF (бинарники) |
| `data/raw/pdf_text/r265b_pdf_ocr_layout.txt` | **Текст из PDF**: шапка 1 кол. (до PACS/DOI), ниже левая → правая колонка |
| `data/raw/ufn_100.jsonl` → поле `"text"` | То, что идёт в корпус для BERT |

На сайте ufn.ru в HTML — только **вступление** (~3k символов). Полная статья (1–2 колонки) — **только в PDF**.  
Нужен **Tesseract**: `brew install tesseract tesseract-lang`

Пересобрать JSONL из уже скачанных 100 PDF:

```bash
python scripts/rebuild_from_pdf.py -i data/raw/ufn_100.jsonl -o data/raw/ufn_100_pdf.jsonl --fresh
```

Установка всех библиотек (в проекте используется Python 3.14.2):\
*pip install -r requirements.txt*

Если на компьютере установлена видеокарта NVIDIA:\
*pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121*

