# Шаблон калибровки и аудита разметки H2

Этот документ задаёт рабочий порядок калибровки на 30 статьях и аудита на
200 статьях. Критерии допуска и определения классов находятся в
[`H2_LABELING.md`](../H2_LABELING.md), а канонические сущности корпуса — в
[`CORPUS_V1.md`](../cards/CORPUS_V1.md).

Шаблон не означает, что аудит уже выполнен. Все файлы в
[`manifests/templates/`](../../manifests/templates/) содержат только
вымышленные данные и показывают формат.

## 1. Комплект файлов

Реальные локальные реестры создаются в корне `manifests/`:

```text
manifests/works.jsonl
manifests/artifacts.jsonl
manifests/rights.jsonl
manifests/h2_audit_run.json
manifests/h2_queries.jsonl
manifests/h2_audit_frame.jsonl
manifests/h2_labels.jsonl
manifests/h2_annotation_form.annotator_01.jsonl
manifests/h2_annotation_form.annotator_02.jsonl
manifests/h2_adjudication_form.adjudicator_01.jsonl
manifests/h2_audit_report.yaml
manifests/frozen/                 # руководства, выписка, дерево и правила
manifests/results/                # матрицы и анализ чувствительности
manifests/checksums.sha256
```

Они исключены из Git. В репозиторий входят только схемы и синтетические
примеры:

```text
manifests/schemas/
manifests/templates/
```

Соответствие настоящих людей псевдонимам `annotator_01`, `annotator_02` и
`adjudicator_01` хранится вне репозитория.

## 2. Что зафиксировать до отбора

До калибровки скопируйте
[`h2_audit_run.example.json`](../../manifests/templates/h2_audit_run.example.json)
в `manifests/h2_audit_run.json` и заполните его по
[`h2_audit_run.schema.json`](../../manifests/schemas/h2_audit_run.schema.json).
До калибровки в паспорте заполняются версия и SHA-256 калибровочного
руководства. После обсуждения 30 работ отдельно фиксируются версия и SHA-256
окончательного руководства аудита; только затем паспорт и руководство
замораживаются перед отбором 200 работ. Паспорт также связывает запуск с
версией кода и SHA-256 официальной таблицы ГРНТИ, выписки подрубрик, дерева
решений, запросов, правил очистки и исходного кадра кандидатов. Одинакового
имени версии без хеша недостаточно.

Исходные байты официальной таблицы сохраните локально как
`data/raw/references/grnti_2025_source`; запросы поиска — как
`manifests/h2_queries.jsonl`. Оба файла входят в итоговые контрольные суммы.

## 3. Порядок заполнения

1. Скопируйте синтетические примеры в корень `manifests/` и замените все
   вымышленные значения реальными. Не редактируйте файлы в
   `manifests/templates/`.
2. В `works.jsonl` создайте одну каноническую строку на `work_id`. PACS и УДК
   сохраняйте только как сигналы поиска и обязательно вместе с URL, датой и
   хешем доказательства.
3. В `rights.jsonl` зафиксируйте отдельную применимую запись для каждой
   операции. В `artifacts.jsonl` сохраните точный очищенный вход
   `title+abstract`, его `h2_input_sha256`, результат проверки утечки метки и
   ссылки на записи прав.
   Для `input_serialization=utf8-nfc-title-lf2-abstract-lf-v1` хешируются
   UTF-8-байты без BOM строки `NFC(title) + "\n\n" + NFC(abstract) + "\n"`;
   `null`-аннотация заменяется пустой строкой. Иных неявных преобразований при
   сериализации нет.
4. Постройте полный кадр кандидатов, затем заморозьте его до случайного отбора.
   `source_manifest_sha256` — хеш входного реестра, из которого построена
   строка кадра, а не хеш файла, содержащего это поле.
5. Добавьте в `h2_audit_frame.jsonl` все найденные работы, включая
   `not_selected`. Для выбранных работ создайте случайный
   `annotation_item_id`, не вычисляемый из DOI, URL, журнала или `work_id`.
   Все идентификаторы, видимые разметчику, должны быть нейтральными и не
   кодировать предполагаемый класс, источник или журнал.
6. Сначала сформируйте калибровочную партию из 30 работ: 15 чистых примеров
   C1–C5 и 15 пограничных либо относящихся к `OTHER`, `AMBIGUOUS` и
   `INSUFFICIENT`. Эти работы не входят в аудит 200 статей.
7. После калибровки заморозьте руководство. Затем по зафиксированным стратам и
   `selection_seed` выберите 200 других работ для основного аудита.
8. Для каждого разметчика создайте отдельную копию обезличенного бланка. В ней
   допустимы нейтральные идентификаторы запуска и задания,
   `annotation_item_id`, версия схемы, очищенные заголовок и аннотация, версия
   руководства, SHA-256 входа и поля ответа. Не передавайте `work_id`, DOI,
   EDN, журнал, источник, рубрику, ключевые слова, PACS, УДК или ГРНТИ.
   До сдачи партии разметчикам запрещены поиск статьи в интернете и обсуждение
   ответов друг с другом.
9. Перенесите каждый независимый ответ в `h2_labels.jsonl` отдельной строкой с
   `record_type=annotation`. Старые строки не меняйте.
10. При расхождении двух ответов сформируйте отдельный арбитражный бланк: он
    содержит обезличенный текст и два исходных решения, но не коды ГРНТИ.
    Результат добавляется строкой `record_type=annotation_adjudication`; арбитр
    не исправляет строки разметчиков.
11. Назначения ГРНТИ храните отдельными строками
    `record_type=grnti_assignment`, по одной строке на код и источник
    доказательства. Согласованная экспертная метка вычисляется однозначно:
    совпавшие ответы дают общее решение, а несовпавшие — решение единственной
    строки `annotation_adjudication`, ссылающейся на обе исходные строки.
12. Конфликт согласованной метки с ГРНТИ рассматривается только после проверки
    второго независимого источника. Для него арбитру передаётся отдельный пакет
    с одной детерминированной согласованной экспертной меткой и минимум двумя
    доказательствами кодов с разными `assignment_source_id`; результат сохраняется как
    `grnti_conflict_resolution`. Он не меняет согласованную экспертную метку, а
    подтверждает каталог либо исключает работу из подтверждающего H2.

## 4. Ограничения для просмотренных работ

Все 30 калибровочных и 200 аудиторских работ получают:

```json
{"seen_by_annotators":true,"allowed_classification_splits":["train"]}
```

Они могут использоваться только в обучающей части классификации и, при
соблюдении остальных правил, в обучающей части MLM. Их запрещено включать в
валидационные, тестовые и формульные наборы проекта.

## 5. Проверочный список перед разметкой

- в кадре нет повторяющихся `frame_record_id`;
- ключ `{audit_id, candidate_frame_id, audit_frame_version, work_id}` уникален;
- все пути обнаружения работы собраны в `query_refs`, а страта ровно одна;
- один `work_id` не представляет две версии одной статьи;
- у каждой выбранной строки уникален `annotation_item_id`;
- `annotation_item_id` не раскрывает DOI, URL, источник или журнал;
- для входа есть применимые записи прав на получение, хранение и оценку;
- каждый поисковый запрос ссылается на запись прав совместимой области,
  способа и масштаба получения;
- у сохранённого объекта `artifact_id == "sha256:" + sha256`;
- `artifacts.h2_input_sha256` совпадает с `input_sha256` в обоих бланках;
- калибровочные и аудиторские работы не пересекаются;
- в каждой партии сохранены размер страты и вероятность отбора;
- оба разметчика получили одинаковые очищенные `title+abstract`;
- SHA-256 входа совпадает у двух бланков;
- по каждой выбранной работе есть ровно два независимых ответа;
- арбитраж присутствует только при разногласии;
- экспертный арбитраж и разрешение конфликта ГРНТИ имеют разные типы строк;
- при любом исходе конфликта пакет содержит ровно одну детерминированную
  согласованную экспертную метку: два одинаковых ответа `annotation` либо одну
  строку `annotation_adjudication`;
- при любом исходе конфликта пакет содержит минимум две строки
  `grnti_assignment` с разными `assignment_source_id`, а
  `source_label_record_ids` ссылается на все экспертные и каталожные строки;
- при `catalog_confirmed` итоговый класс совпадает с подтверждёнными строками
  ГРНТИ;
- ни один исходный ответ не был перезаписан;
- просмотренные работы разрешены только для `train`;
- реальные имена разметчиков и закрытое соответствие идентификаторов не
  попали в Git.

JSON Schema проверяет форму отдельной строки, но не проверяет межфайловые
связи, квоты 30/200, наличие ровно двух ответов и запрет утечки. До появления
автоматической проверки этот список выполняется вручную и подписывается в
отчёте.

## 6. Контрольные суммы

После заморозки реестров создайте отдельный файл контрольных сумм:

```bash
shasum -a 256 manifests/works.jsonl \
  manifests/artifacts.jsonl \
  manifests/rights.jsonl \
  manifests/h2_audit_run.json \
  manifests/h2_queries.jsonl \
  manifests/h2_audit_frame.jsonl \
  manifests/h2_labels.jsonl \
  manifests/h2_annotation_form.annotator_01.jsonl \
  manifests/h2_annotation_form.annotator_02.jsonl \
  manifests/h2_adjudication_form.adjudicator_01.jsonl \
  manifests/h2_audit_report.yaml \
  manifests/frozen/calibration_guide.md \
  manifests/frozen/audit_guide.md \
  manifests/frozen/grnti_excerpt.json \
  manifests/frozen/decision_tree.yaml \
  manifests/frozen/preprocessing_rules.json \
  manifests/results/h2_annotator_confusion_matrix.csv \
  manifests/results/h2_expert_grnti_confusion_matrix.csv \
  manifests/results/h2_journal_class_matrix.csv \
  manifests/results/h2_leave_one_journal_out.jsonl \
  data/raw/references/grnti_2025_source \
  > manifests/checksums.sha256
```

Хеш самого реестра не хранится внутри этого же реестра: это создало бы
круговую зависимость.

## 7. Итоговый отчёт

После аудита заполните:

```yaml
audit_id: ""
audit_frame_version: ""
candidate_frame_id: ""
h2_scheme_version: ""
calibration_labeling_guide_version: ""
audit_labeling_guide_version: ""
created_at: ""
audit_run_file: "manifests/h2_audit_run.json"
calibration_n: 0
audit_n: 0
double_annotated_n: 0
adjudicated_n: 0
russian_title_abstract_rate: null
any_grnti_rate: null
explicit_primary_grnti_rate: null
single_label_rate: null
without_verified_primary_grnti_n: 0
observed_agreement: null
cohen_kappa: null
cohen_kappa_ci95: [null, null]
design_weighted_kappa: null
design_weighted_kappa_ci95: [null, null]
annotator_grnti_kappa: {annotator_01: null, annotator_02: null}
annotator_grnti_macro_f1: {annotator_01: null, annotator_02: null}
annotator_grnti_per_class_f1: {annotator_01: {}, annotator_02: {}}
expert_grnti_macro_f1: null
expert_grnti_macro_f1_ci95: [null, null]
expert_grnti_per_class_f1: {}
grnti_conflict_rate: null
grnti_conflict_rate_ci95: [null, null]
unresolved_grnti_conflicts_n: 0
source_only_macro_f1: null
source_only_macro_f1_ci95: [null, null]
journal_class_cramers_v: null
journal_class_mutual_information: null
journal_class_graph_connected: null
journal_class_min_class_degree: null
eligible_by_class: {C1: 0, C2: 0, C3: 0, C4: 0, C5: 0}
distinct_sources_by_class: {C1: 0, C2: 0, C3: 0, C4: 0, C5: 0}
source_ids_by_class: {C1: [], C2: [], C3: [], C4: [], C5: []}
exclusions_by_reason: {}
annotator_confusion_matrix_file: "manifests/results/h2_annotator_confusion_matrix.csv"
expert_grnti_confusion_matrix_file: "manifests/results/h2_expert_grnti_confusion_matrix.csv"
journal_class_matrix_file: "manifests/results/h2_journal_class_matrix.csv"
leave_one_journal_out_results_file: "manifests/results/h2_leave_one_journal_out.jsonl"
format_and_relation_checks: "pass|fail"
decision: "pass|revise|reject"
decision_rationale: ""
checksums_file: "manifests/checksums.sha256"
```

Пороговые значения не дублируются здесь: применяются критерии текущей версии
[`H2_LABELING.md`](../H2_LABELING.md). Любое изменение схемы классов,
руководства или кадра после просмотра данных создаёт новую версию и новый
аудит, а не переписывает старый результат.
