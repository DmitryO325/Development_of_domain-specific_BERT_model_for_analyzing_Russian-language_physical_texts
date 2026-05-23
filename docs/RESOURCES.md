# Ресурсы и выгрузка с сайтов

Сводка по материалам, которые удалось получить автоматически (май 2026).

## Google Doc (план НИР)

**Ссылка:** https://docs.google.com/document/d/1vGZf0AHvZ7xSU9tAO8pq1zRjB3PX-vWJBOpoi_SA5v4/edit

**Статус:** доступ открыт. Локальная копия: [`docs/plan_from_google.txt`](plan_from_google.txt)  
Структурированный план: [`docs/PLAN.md`](PLAN.md)

**Участники:** Оберемок Дмитрий (студент), Куратов Андрей Сергеевич (научный руководитель).

---

## Модели из Google Doc (дополнение к HF)

| Модель | URL |
|--------|-----|
| ruBERT-base (DeepPavlov) | https://huggingface.co/DeepPavlov/rubert-base-cased |
| ruBERT-base (ai-forever) | https://huggingface.co/ai-forever/ruBert-base |
| ruRoBERTa-large | https://huggingface.co/ai-forever/ruRoberta-large |
| RuModernBERT-base | https://huggingface.co/deepvk/RuModernBERT-base |
| ruSciBERT | https://huggingface.co/ai-forever/ruSciBERT |
| SciRus-tiny | https://huggingface.co/mlsa-iai-msu-lab/sci-rus-tiny |
| SciRus-small | https://huggingface.co/mlsa-iai-msu-lab/sci-rus-small |
| Giga-Embeddings-instruct | https://huggingface.co/ai-sage/Giga-Embeddings-instruct |

---

## Источники данных (из плана / письма)

| № | Источник | URL | Примечание |
|---|----------|-----|------------|
| 1 | УФН | https://ufn.ru/ | [Colab-парсер](https://colab.research.google.com/drive/1fjGg2EzReglbHhY_fk01td4e7uLQUNKm?usp=sharing) |
| 2 | JETP Letters | http://jetpletters.ru/ | |
| 3 | sciencejournals.ru | https://sciencejournals.ru/ | |
| 4 | ЖЭТФ | https://www.jetp.ras.ru/cgi-bin/r/index | |
| 5 | journals.ioffe.ru | https://journals.ioffe.ru/ | |
| 6 | Квантовая электроника | https://quantum-electronics.ru/ | |
| 7 | НКРЯ | https://ruscorpora.ru/ | мало физики |
| 8 | russian_physics | https://huggingface.co/datasets/Vikhrmodels/russian_physics | олимпиады |
| 9 | Kaggle scientific articles | https://www.kaggle.com/datasets/ergkerg/russian-scientific-articles | |
| 10 | eLibrary | https://elibrary.ru/ | RuSciBench, ruSciBERT |
| 11 | libnauka.ru | https://www.libnauka.ru/ | |

**Формулы:** https://arxiv.org/html/2405.07886v1

---

## Модели (Hugging Face)

### ruSciBERT
- **URL:** https://huggingface.co/ai-forever/ruSciBERT
- **Авторы:** Sber AI + MLSA Lab (МГУ)
- **Архитектура:** RoBERTa, Masked LM
- **Параметры:** ~123M
- **Словарь:** 50 265 токенов
- **Объём обучения:** ~6.5 GB научных текстов (рус.)
- **Лицензия:** Apache 2.0
- **Статья:** [Doklady Mathematics, 2022](https://www.mathnet.ru/danma345) — «ruSciBERT: языковая модель … для семантических векторных представлений научных текстов»

### ruBERT-base
- **URL:** https://huggingface.co/ai-forever/ruBERT-base
- **Роль:** общеязыковый baseline (уже тестируется в `draft/rubert.ipynb`)

---

## Бенчмарки и смежные работы

### RuSciBench (2024/2025)
- **Статья:** https://link.springer.com/article/10.1134/S1064562424602191 (open access)
- **Суть:** первый бенчмарк для научных текстов на русском; данные из **eLibrary** (~182k статей после фильтра).
- **Задачи:** 9 для RU + 9 для EN — классификация (OECD, ГРНТИ), регрессия (год, цитирования), retrieval.
- **Код:** https://github.com/mlsa-iai-msu-lab/ru_sci_bench_mteb
- **Данные:** https://huggingface.co/collections/mlsa-iai-msu-lab/ruscibench-66c4895619fb0935efdd95cb
- **Для НИР:** можно взять подмножество статей с рубрикой **ГРНТИ 29 (физика)** как размеченный eval.

### RuSERRC (корпус IT-статей, методология)
- **Статья:** https://cyberleninka.ru/article/n/semanticheskiy-analiz-nauchnyh-tekstov-opyt-sozdaniya-korpusa-i-postroeniya-yazykovyh-modeley
- **Объём:** 1600 неразмеченных + 80 размеченных (NER + 6 типов отношений)
- **Вывод авторов:** для русского языка мало открытых доменных корпусов; нужно дообучение BERT-подобных моделей.

### Entity recognition in RU scientific texts
- **arXiv:** https://arxiv.org/abs/2011.09817

---

## Датасеты

| Имя | URL | Описание | Ограничения |
|-----|-----|----------|-------------|
| russian_physics | https://huggingface.co/datasets/Vikhrmodels/russian_physics | Задачи с олимпиад по физике | Gated, ~75 KB, другой жанр |
| RuSciBench collection | см. выше | Научные статьи RU/EN | Нужна фильтрация по физике |
| arXiv RU summarization | https://arxiv.org/abs/2405.07886 | 420 статей, мультимодальность | Не только физика |

---

## Рубрикатор физики (ГРНТИ)

- **Раздел 29 — Физика:** https://grnti.ru/?p1=29
- Подразделы для классификации: 29.05 (частицы), 29.19 (твёрдое тело), 29.29 (атом), 29.31 (оптика), 29.35 (радиофизика) и др.

Используется в RuSciBench для задачи `RuSciBenchGRNTI*Classification`.

---

## КиберЛенинка (примеры физ. статей)

Открытый текст, удобен для пилотного корпуса (проверить условия использования):

- https://cyberleninka.ru/article/n/korpuskulyarnye-i-volnovye-svoystva-mikrosistem
- https://cyberleninka.ru/article/n/diskretnyy-podhod-k-opisaniyu-dalnih-korrelyatsiy-mnozhestvennosti-i-v-modeli-sliyaniya-strun-1

Парсер: аккуратно, rate limit, сохранять только нужные поля (заголовок, аннотация, ключевые слова).

---

## Наблюдения из ваших ноутбуков (`draft/`)

1. **ruBERT** на zero-shot «физика vs биология» даёт **равные вероятности** — модель не выделяет домен без fine-tune.
2. **fill-mask** на ruBERT подбирает бытовые слова («плохая», «скучная»), не термины.
3. **text-generation** на BERT без decoder — артефакты; для генерации не использовать.
4. **ruSciBERT** — лучший старт для continued pretraining под физику (та же «научная» стилистика).

---

## Сбор корпуса (реализовано в репозитории)

См. **[`docs/PARSERS.md`](PARSERS.md)** (подробно) и [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) (схема).

```bash
python scripts/scrape.py ufn --max-docs 100 --fresh -o data/raw/ufn_100.jsonl
python scripts/rebuild_from_pdf.py -i data/raw/ufn_100.jsonl -o data/raw/ufn_100_pdf.jsonl --fresh
```

---

## Полезные команды

```bash
# Установка (из README)
pip install -r requirements.txt

# Скачать модель локально
python -c "from transformers import AutoModel; AutoModel.from_pretrained('ai-forever/ruSciBERT')"
```

```python
# Быстрый fill-mask
from transformers import pipeline
m = pipeline("fill-mask", model="ai-forever/ruSciBERT")
print(m("Уравнение Шрёдингера описывает эволюцию [MASK]."))
```
