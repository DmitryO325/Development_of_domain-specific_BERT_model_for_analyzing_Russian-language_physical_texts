# НИР: доменно-специализированная BERT для русскоязычных физических текстов

**Студент:** Оберемок Дмитрий  
**Научный руководитель:** Куратов Андрей Сергеевич  
**GitHub:** https://github.com/DmitryO325/Development_of_domain-specific_BERT_model_for_analyzing_Russian-language_physical_texts

> Полная выгрузка из Google Doc: [`docs/plan_from_google.txt`](plan_from_google.txt)

---

## 1. Цель

Разработать и оценить **доменно-специализированную** языковую модель (BERT-семейство) для анализа **русскоязычных физических** текстов: статьи, учебники, аннотации, рефераты, задачи и т.п.

**Целевая модель (рабочее имя):** ruPhysBERT — continued pretraining / fine-tune поверх сильного baseline.

---

## 2. Модели для сравнения (из плана)

### Базовые энкодеры (общий русский)

| Модель | Ссылка | Назначение |
|--------|--------|------------|
| ruBERT-base | [DeepPavlov](https://huggingface.co/DeepPavlov/rubert-base-cased), [ai-forever](https://huggingface.co/ai-forever/ruBert-base) | Baseline: MLM/эмбеддинги, не заточен под классификацию без fine-tune |
| ruRoBERTa-large | [ai-forever/ruRoberta-large](https://huggingface.co/ai-forever/ruRoberta-large) | Усиленный общий baseline |
| RuModernBERT-base | [deepvk/RuModernBERT-base](https://huggingface.co/deepvk/RuModernBERT-base) | RU+EN+код; тяжелее, опционально |

### Научные модели

| Модель | Ссылка | Комментарий |
|--------|--------|-------------|
| **ruSciBERT** | [ai-forever/ruSciBERT](https://huggingface.co/ai-forever/ruSciBERT) | eLibrary, arXiv.ru, диссертации, журналы; **основной кандидат для дообучения** |
| SciRus tiny/small | [sci-rus-tiny](https://huggingface.co/mlsa-iai-msu-lab/sci-rus-tiny), [sci-rus-small](https://huggingface.co/mlsa-iai-msu-lab/sci-rus-small) | MLSA Lab, мультиязычная наука |
| GigaEmbeddings | [ai-sage/Giga-Embeddings-instruct](https://huggingface.co/ai-sage/Giga-Embeddings-instruct) | Эмбеддинги GigaChat; для retrieval |

> В документе ruSciBERT указан как BERT; фактически на HF это **RoBERTa** — в отчёте уточнить архитектуру. Лицензия: некоммерческое использование — для НИР/диплома подходит.

**Уже в репозитории:** `draft/rubert.ipynb`, `draft/ruscibert.ipynb`.

---

## 3. Источники данных (из плана + письма)

### Журналы и порталы (приоритет от руководителя)

1. [Успехи физических наук (ufn.ru)](https://ufn.ru/) — есть [Colab-парсер](https://colab.research.google.com/drive/1fjGg2EzReglbHhY_fk01td4e7uLQUNKm?usp=sharing)
2. [JETP Letters](http://jetpletters.ru/)
3. [sciencejournals.ru](https://sciencejournals.ru/)
4. [ЖЭТФ (jetp.ras.ru)](https://www.jetp.ras.ru/cgi-bin/r/index)
5. [Журналы Иоффе](https://journals.ioffe.ru/)
6. [Квантовая электроника](https://quantum-electronics.ru/)

### Дополнительно

7. [НКРЯ](https://ruscorpora.ru/) — мало физики, точечный поиск  
8. [russian_physics (HF)](https://huggingface.co/datasets/Vikhrmodels/russian_physics) — олимпиады  
9. [Kaggle: russian-scientific-articles](https://www.kaggle.com/datasets/ergkerg/russian-scientific-articles)  
10. [eLibrary](https://elibrary.ru/defaultx.asp?)  
11. [libnauka.ru](https://www.libnauka.ru/)  
12. arXiv — в основном EN; нужен парсер для редких RU-статей  

### Метаданные корпуса (продумать при сборе)

Для каждого документа: заголовок, аннотация, домен/секция, ключевые слова, источник, лицензия, URL, дата.  
Общий журнал корпуса: источники, объём, поддомены физики (ГРНТИ 29.xx), правовые ограничения.

---

## 4. Важные технические моменты (из плана)

1. **PDF:** двухколоночная вёрстка + формулы → простые парсеры не всегда работают; для сложных PDF — OCR (уже тестировали на рус. физ. текстах; возможно ВНИИА).
2. **Формулы:** вероятно нужен **свой токенизатор** или спец. токены (`[FORMULA]`); базовые BPE плохо режут LaTeX. См. [arXiv 2405.07886](https://arxiv.org/html/2405.07886v1).
3. **Контекст:** нужна модель с хорошим улавливанием длинного контекста (512 токенов — ограничение классического BERT; учитывать при выборе).
4. **Идея на перспективу:** лекции (МИФИ, МГУ, МФТИ) → ASR → текст (отдельный источник данных).
5. **Вычисления:** Colab / Kaggle → при нехватке ВНИИА им. Духова / Яндекс.

---

## 5. План работы (как в Google Doc)

| Этап | Содержание | Артефакт |
|------|------------|----------|
| **1** | Обзор: модели и подходы для научных/физ. текстов (RU/EN) | Глава обзора, `docs/RESOURCES.md` |
| **2** | Сбор корпуса RU физ. документов | `data/raw/`, реестр метаданных |
| **3** | Извлечение текста (PDF/OCR) | `data/extracted/` |
| **4** | Формулы + EDA (длины, термины, доля формул) | `notebooks/eda.ipynb`, отчёт |
| **5** | ML: выбор модели, baseline, токенизатор, MLM vs fine-tune | `src/train_*.py` |
| **6** | Гипотезы, метрики, протокол абляции | раздел «Методология» |
| **7** | Обучение, абляция, бенчмарки (RuSciBench subset, fill-mask) | `models/`, таблицы |
| **8** | Статья (если результаты позволяют) | черновик публикации |

---

## 6. Гипотезы и метрики (черновик для этапа 6)

**Гипотезы:**
- H1: MLM-дообучение ruSciBERT на физ. корпусе снижает perplexity vs ruSciBERT и ruBERT.
- H2: На классификации подразделов физики (ГРНТИ 29 / рубрики журналов) ruPhysBERT ≥ ruSciBERT > ruBERT (macro-F1).
- H3: Кастомные токены для формул улучшают fill-mask на физ. терминах (абляция: с/без).

**Метрики:** MLM perplexity / acc@1; macro-F1, accuracy; опционально RuSciBench-подзадачи на физ. подвыборке.

**Абляции:** база (ruBERT / ruSciBERT); объём корпуса; токенизатор; обработка формул.

---

## 7. Структура репозитория (целевая)

```
data/raw/           # сырые PDF/HTML
data/processed/     # чистый текст + метаданные
src/collect/        # парсеры (ufn, elibrary, …)
src/preprocess.py
src/train_mlm.py
src/train_cls.py
src/evaluate.py
models/             # чекпоинты
draft/              # текущие ноутбуки
notebooks/          # EDA, эксперименты
docs/               # план, ресурсы, plan_from_google.txt
reports/            # графики для НИР
```

---

## 8. Статус (май 2026)

Подробно: [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) (схема, JSONL), [`docs/PARSERS.md`](PARSERS.md) (парсеры ufn/RSS/PDF-OCR).

| Задача | Статус |
|--------|--------|
| План из Google Doc | [x] `docs/plan_from_google.txt`, `docs/PLAN.md` |
| Ноутбуки ruBERT / ruSciBERT | [x] `draft/` |
| Парсер ufn.ru в репозитории | [x] `src/collect/ufn.py`, `scripts/scrape.py` |
| Скачивание PDF + OCR + 2 колонки | [x] `src/collect/pdf_text.py`, `rebuild_from_pdf.py` |
| ~100 статей УФН (PDF локально) | [x] `data/raw/pdf/`, `ufn_100.jsonl` |
| Полный OCR-корпус на 100 статей | [ ] пересборка `ufn_100_pdf.jsonl` |
| Парсеры других журналов | [ ] |
| Реестр корпуса + 1k документов | [ ] |
| MLM pilot | [ ] |

---

## 9. Ближайший шаг

1. Пересобрать **текст из 100 PDF** (`rebuild_from_pdf.py` → `ufn_100_pdf.jsonl`).  
2. **Реестр** метаданных и проверка качества OCR.  
3. **Baseline** (ruBERT vs ruSciBERT) на fill-mask / pilot MLM.


Задача сходства текстов