"""Проверки адаптера «Квантовой электроники» без сетевых запросов."""

from __future__ import annotations

import unittest
from typing import cast

from src.collect.base import HttpResponseSnapshot
from src.collect.journal_sources import get_source
from src.collect.quantum_electronics import (
    ALLOWED_ORIGINS,
    DEFAULT_ENCODING,
    SOURCE_ID,
    START_URL,
    _labeled_text,
    _parse_html,
    classify_url,
    extract_links,
    parse_article,
    parse_issue_articles,
    parse_unlinked_issue,
    resolve_article_redirect,
)

ORIGIN = "https://quantum-electronics.ru"
ARTICLE_URL = f"{ORIGIN}/issledovanie-lazernyh-impulsov/"
RELATIVE_ISSUE_URL = f"{ORIGIN}/2022_52_1/"
NESTED_ARTICLE_URL = f"{RELATIVE_ISSUE_URL}issledovanie-lazernyh-impulsov/"
PDF_URL = f"{ORIGIN}/wp-content/uploads/2024/01/test_article.pdf"
ARTICLE_HTML = """
<html><body>
<article><header>
<h1 class="entry-title">Исследование <i>лазерных</i> импульсов</h1>
<div class="below-title-meta">Автор: <a rel="author">editor</a>
<span class="date updated">15.04.2026</span></div></header>
<div class="entry-content">
<p>А. А. Иванов, Б. Б. Петров</p>
<ul><li>Научный институт, Москва</li></ul>
<div class="around-button"><b>Аннотация:</b> Изучены&nbsp;лазеры.<br>
Сравнены <em>два</em> режима.<script>скрытое</script></div>
<div class="around-button"><b>Ключевые&nbsp;слова:</b> лазер, импульс.</div>
<div><b>Образец цитирования:</b> А. А. Иванов, Б. Б. Петров,
«Исследование лазерных импульсов», <i>Квантовая электроника</i>,
<b>54</b>:1 (2024), 1–3</div>
<div><b>DOI:</b> <a href="https://doi.org/10.1234/example">10.1234/example</a></div>
<p><a href="/wp-content/uploads/2024/01/test_article.pdf">Скачать (.pdf)</a></p>
</div><footer><a rel="category tag" href="/category/lasers/">Лазеры</a></footer>
</article>
<aside><a href="/unrelated-article/">Другая статья</a>
<a href="/wp-content/uploads/2024/other.pdf">Скачать (.pdf)</a></aside>
</body></html>
"""
PLAIN_FIELDS_HTML = """
<article><h1 class="entry-title">История физических исследований</h1>
<div class="entry-content"><p>А. А. Иванов</p>
<ul><li>Исследовательский институт, Москва</li></ul>
<p>Аннотация: Описана история <em>лазерных</em> исследований.<br />
Поступила в редакцию: 26.05.2022</p>
<p>Образец цитирования: А. А. Иванов, «История физических исследований»,
<i>Квантовая электроника</i>, <b>52</b>:12 (2022), 1064–1069</p>
<p><a href="https://quantum-electron.lebedev.ru/wp-content/uploads/2023/02/1064.pdf">Скачать</a> (.pdf)</p>
</div><footer><a rel="category tag">Специальный исторический выпуск</a></footer></article>
"""
UNLINKED_ISSUE_URL = f"{ORIGIN}/2026_56_5/"
MOVED_ISSUE_URL = f"{ORIGIN}/2025_55_2-2/"
UNLINKED_ISSUE_HTML = """
<article><h1 class="entry-title">2026, том 56, номер 5</h1>
<div class="entry-content">
<p><strong>Лазерная плазма</strong></p>
<p>Исследование изомерных состояний <sup>83</sup>Kr в плазме<br />
А. А. Иванов, Б. Б. Петров <strong>317</strong></p>
<p><strong>Лазерная спектроскопия</strong></p>
<p>Флуоресценция кристалла Ti<sup>3+</sup>:Al<sub>2</sub>O<sub>3</sub><br />
Д. В. Яковин, A. Ф. Лелекова <strong>333</strong></p>
</div></article>
"""
INLINE_SECTION_ISSUE_URL = f"{ORIGIN}/2025_55_5/"
INLINE_SECTION_ISSUE_HTML = """
<h1 class="entry-title">2025, том 55, номер 5</h1>
<div class="entry-content"><p><strong>Некролог</strong><br />
<a href="https://quantum-electronics.ru/wp-content/uploads/2025/05/336.pdf">
Памяти Юрия Михайловича Попова</a>\t\t<strong>336</strong></p></div>
"""
OBITUARY_URL = f"{ORIGIN}/pamyati-georgiya-mitrofanovicha-zvereva/"
OBITUARY_NOTICE = "Редколлегия сообщает о кончине учёного 5 октября 2025 года."
OBITUARY_FOOTER = """
<footer class="entry-meta"><span>Раздел:
<a href="https://quantum-electronics.ru/category/nekrolog/" rel="category tag">Некролог</a>
</span></footer>
"""
OBITUARY_HTML = f"""
<article class="post category-nekrolog">
<header><h1 class="entry-title">Памяти Георгия Митрофановича Зверева</h1>
<div class="below-title-meta">Автор: <a rel="author">vasileva</a>
<span class="date updated">21.04.2026</span></div></header>
<div class="entry-content"><p>{OBITUARY_NOTICE}</p>
<p>Скачать (<a href="https://quantum-electronics.ru/wp-content/uploads/2025/10/685.pdf">.pdf</a>)</p>
</div>{OBITUARY_FOOTER}</article>
"""


class QuantumRouteTests(unittest.TestCase):
    """Проверки известных маршрутов и границ сетевого обхода."""

    def test_adapter_constants(self) -> None:
        """Начальная точка указывает на проверенный русский архив."""

        self.assertEqual(SOURCE_ID, "quantum")
        self.assertEqual(START_URL, f"{ORIGIN}/archive/")
        self.assertEqual(ALLOWED_ORIGINS, (ORIGIN,))
        self.assertEqual(DEFAULT_ENCODING, "utf-8")

    def test_public_paths(self) -> None:
        """Архив, выпуски, карточки и файлы различаются по виду URL."""

        for url, expected in (
            (START_URL, "archive"),
            (f"{ORIGIN}/2026_56_1", "issue"),
            (f"{ORIGIN}/1999_29_12/", "issue"),
            (ARTICLE_URL, "article"),
            (PDF_URL, "pdf"),
        ):
            with self.subTest(url=url):
                self.assertEqual(classify_url(url), expected)

    def test_external_service_and_ambiguous_paths_are_rejected(self) -> None:
        """Внешние узлы, английская версия, WordPress и обход пути исключены."""

        urls = [
            "https://example.test/issledovanie-lazernyh-impulsov/",
            "http://quantum-electronics.ru/2026_56_1/",
            "https://user@quantum-electronics.ru/2026_56_1/",
            "https://quantum-electronics.ru:444/2026_56_1/",
            f"{ORIGIN}/wp-admin/", f"{ORIGIN}/wp-json/",
            f"{ORIGIN}/xmlrpc.php", f"{ORIGIN}/?p=10",
            f"{ORIGIN}/en/issledovanie-lazernyh-impulsov/",
            f"{ORIGIN}/2026_56_1/?lang=en", f"{ARTICLE_URL}#respond",
            f"{ORIGIN}/2026_56_1/../2026_56_2/",
            f"{ORIGIN}/%32%30%32%36_56_1/", f"{ARTICLE_URL}\n",
            f"{ORIGIN}/impact-factor-quantum-electronics-jcr/",
            f"{ORIGIN}/tablica-sootvetstviya-russkih-ssylok/",
            f"{ORIGIN}/wp-content/uploads/2024/author_agreement.docx",
        ]

        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(classify_url(url))

    def test_archive_links_are_scoped_and_canonicalized(self) -> None:
        """Выпуски берутся из содержимого, дубли и меню исключаются."""

        html = """
        <nav><a href="/2025_55_1/">Меню</a></nav>
        <div class="entry-content">
        <a href="/2026_56_1/">1</a><a href="/2026_56_1">Повтор</a>
        <a href="/2000_30_1">Старый выпуск</a>
        <a href="/wp-json/">API</a><a href="/some-article/">Не выпуск</a>
        <a href="/2026_56_2&#63;x=1">Параметр</a>
        <a href="/2026_56_3/../2026_56_2/">Обход пути</a>
        </div>
        """

        self.assertEqual(
            extract_links(html, START_URL),
            [f"{ORIGIN}/2026_56_1/", f"{ORIGIN}/2000_30_1/"],
        )

    def test_issue_extracts_only_explicit_article_links(self) -> None:
        """Карточки берутся из выпуска без переходов в меню и служебные разделы."""

        html = """
        <div class="entry-content"><p>
        <a href="/issledovanie-lazernyh-impulsov/">Название</a>
        <a href="/issledovanie-lazernyh-impulsov/">Повтор</a>
        <a href="/en/other-article/">English</a>
        <a href="/wp-json/">API</a><a href="../unsafe-route/">Обход</a>
        </p></div><aside><a href="/another-article/">Меню</a></aside>
        """

        self.assertEqual(extract_links(html, f"{ORIGIN}/2024_54_1/"), [ARTICLE_URL])

    def test_relative_issue_links_keep_the_published_nested_path(self) -> None:
        """Относительный href разрешается от выпуска без угадывания корневого адреса."""

        html = """
        <div class="entry-content">
        <a href="raboty-po-lazernoj-biofotonike/">Работы по лазерной биофотонике</a>
        <a href="raboty-po-lazernoj-biofotonike/">Повтор</a>
        <a href="/issledovanie-lazernyh-impulsov/">Корневая карточка</a>
        </div>
        <aside><a href="unrelated-article/">Меню</a></aside>
        """
        nested_url = f"{RELATIVE_ISSUE_URL}raboty-po-lazernoj-biofotonike/"

        self.assertEqual(extract_links(html, RELATIVE_ISSUE_URL), [nested_url, ARTICLE_URL])
        self.assertEqual(classify_url(nested_url), "article")
        get_source("quantum").validate_url(nested_url)

    def test_nested_paths_do_not_expand_service_or_unsafe_routes(self) -> None:
        """Вложенная карточка не разрешает служебные пути, чужие узлы и обход каталога."""

        references = [
            "../issledovanie-lazernyh-impulsov/", "./issledovanie-lazernyh-impulsov/",
            "en/issledovanie-lazernyh-impulsov/", "wp-admin/", "wp-json/", "cat-lasers/",
            "impact-factor-quantum-electronics-jcr/", "tablica-sootvetstviya-russkih-ssylok/",
            "issledovanie-lazernyh-impulsov/?page=2", "issledovanie-lazernyh-impulsov/#comments",
            "%69ssledovanie-lazernyh-impulsov/", "issledovanie-lazernyh-impulsov//",
            "https://other.test/2022_52_1/issledovanie-lazernyh-impulsov/",
            "http://quantum-electronics.ru/2022_52_1/issledovanie-lazernyh-impulsov/",
        ]

        for reference in references:
            html = f'<div class="entry-content"><a href="{reference}">Ссылка</a></div>'

            with self.subTest(reference=reference):
                self.assertEqual(extract_links(html, RELATIVE_ISSUE_URL), [])

    def test_nested_article_301_is_the_only_new_accepted_status(self) -> None:
        """HTTP 301 принимается только у вложенной карточки, не у корня или PDF."""

        source = get_source("quantum")

        self.assertEqual(source.accepted_statuses(NESTED_ARTICLE_URL), frozenset({200, 301}))

        for url in (ARTICLE_URL, RELATIVE_ISSUE_URL, START_URL, PDF_URL):
            with self.subTest(url=url):
                self.assertEqual(source.accepted_statuses(url), frozenset({200}))

    def test_confirmed_issue_move_is_normalized_without_duplicates(self) -> None:
        """Опубликованный старый адрес ведёт в точный подтверждённый Location."""

        html = """
        <div class="entry-content">
        <a href="/2025_55_2">2</a><a href="/2025_55_2/">Повтор</a>
        <a href="https://quantum-electronics.ru/2025_55_2-2/">Новый адрес</a>
        <a href="/2025_55_2-2">Повтор нового</a><a href="/2025_55_3/">3</a>
        </div>
        """

        self.assertEqual(
            extract_links(html, START_URL), [MOVED_ISSUE_URL, f"{ORIGIN}/2025_55_3/"],
        )

        for url in (MOVED_ISSUE_URL, MOVED_ISSUE_URL.removesuffix("/")):
            with self.subTest(url=url):
                self.assertEqual(classify_url(url), "issue")
                get_source("quantum").validate_url(url)

    def test_issue_move_does_not_allow_other_suffixes_or_origins(self) -> None:
        """Подтверждённая пара не разрешает произвольные суффиксы и подмену узла."""

        urls = [
            f"{ORIGIN}/2025_55_2-3/", f"{ORIGIN}/2025_55_3-2/",
            f"{ORIGIN}/2024_54_2-2/", f"{ORIGIN}/2025_55_2-2-2/",
            f"{ORIGIN}/2025_55_2-02/", f"{ORIGIN}/2025_55_2-2//",
            f"{MOVED_ISSUE_URL}?page=2", f"{MOVED_ISSUE_URL}#other",
            f"{ORIGIN}/2025_55_2%2D2/", f"{ORIGIN}/../2025_55_2-2/",
            "http://quantum-electronics.ru/2025_55_2-2/",
            "https://quantum-electronics.ru.example.test/2025_55_2-2/",
            "https://quantum-electronics.ru@other.test/2025_55_2-2/",
        ]

        for url in urls:
            html = f'<div class="entry-content"><a href="{url}">2</a></div>'

            with self.subTest(url=url):
                self.assertIsNone(classify_url(url))
                self.assertEqual(extract_links(html, START_URL), [])

                with self.assertRaises(ValueError):
                    get_source("quantum").validate_url(url)

    def test_confirmed_move_does_not_accept_http_redirects(self) -> None:
        """Нормализация ссылки не включает транспортное следование за HTTP 301."""

        source = get_source("quantum")

        for url in (
            f"{ORIGIN}/2025_55_2/", MOVED_ISSUE_URL,
            f"{ORIGIN}/2025_55_3/", ARTICLE_URL, PDF_URL,
        ):
            with self.subTest(url=url):
                self.assertEqual(source.accepted_statuses(url), frozenset({200}))

    def test_missing_content_and_invalid_page_raise(self) -> None:
        """Неизвестная разметка не выглядит как успешный пустой архив."""

        for html, url in (("<p>Ошибка</p>", START_URL), ("", PDF_URL)):
            with self.subTest(url=url), self.assertRaises(ValueError):
                extract_links(html, url)


class QuantumArticleRedirectTests(unittest.TestCase):
    """Проверки единственного перехода от опубликованного вложенного адреса."""

    def _snapshot(
        self,
        *,
        url: str = NESTED_ARTICLE_URL,
        status: int = 301,
        headers: tuple[tuple[str, str], ...] = (("Location", ARTICLE_URL),),
    ) -> HttpResponseSnapshot:
        """Создать ответ без автоматического следования за перенаправлением."""

        return HttpResponseSnapshot(
            url, url, status, headers, "2026-10-16T12:00:00+00:00", b"redirect",
        )

    def test_exact_root_location_is_resolved(self) -> None:
        """Единственный Location сохраняет исходный slug и проверенный узел."""

        snapshot = self._snapshot()

        self.assertEqual(resolve_article_redirect(snapshot), ARTICLE_URL)
        self.assertEqual(get_source("quantum").resolve_article_redirect(snapshot), ARTICLE_URL)

    def test_successful_response_does_not_redirect(self) -> None:
        """Ответ 200 не превращается в переход из-за лишнего заголовка Location."""

        for url in (NESTED_ARTICLE_URL, ARTICLE_URL):
            with self.subTest(url=url):
                self.assertIsNone(resolve_article_redirect(self._snapshot(url=url, status=200)))

    def test_other_targets_are_rejected(self) -> None:
        """Перенос не разрешает другой slug, узел, параметры или второй вложенный путь."""

        locations = [
            f"{ORIGIN}/another-article/", f"{ORIGIN}/wp-admin/", NESTED_ARTICLE_URL,
            f"{ORIGIN}/2022_52_2/issledovanie-lazernyh-impulsov/",
            "https://other.test/issledovanie-lazernyh-impulsov/",
            "https://quantum-electronics.ru.other.test/issledovanie-lazernyh-impulsov/",
            "https://quantum-electronics.ru@other.test/issledovanie-lazernyh-impulsov/",
            "http://quantum-electronics.ru/issledovanie-lazernyh-impulsov/",
            ARTICLE_URL + "?page=2", ARTICLE_URL + "#comments", ARTICLE_URL + "/",
            ARTICLE_URL.replace("/issledovanie", "/%69ssledovanie"),
            ARTICLE_URL.replace("/issledovanie", "/../issledovanie"),
            ARTICLE_URL.removesuffix("/"), "/issledovanie-lazernyh-impulsov/", "",
        ]

        for location in locations:
            with self.subTest(location=location), self.assertRaises(ValueError):
                resolve_article_redirect(self._snapshot(headers=(("Location", location),)))

    def test_missing_or_duplicate_location_is_rejected(self) -> None:
        """Отсутствующий или неоднозначный Location не выбирается произвольно."""

        header_sets = [
            (),
            (("Location", ARTICLE_URL), ("location", ARTICLE_URL)),
            (("Location", ARTICLE_URL), ("Location", f"{ORIGIN}/another-article/")),
        ]

        for headers in header_sets:
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                resolve_article_redirect(self._snapshot(headers=headers))

    def test_root_redirect_and_other_statuses_are_rejected(self) -> None:
        """Исключение не разрешает цепочки, временные перенаправления и ошибки доступа."""

        for url, status in (
            (ARTICLE_URL, 301), (RELATIVE_ISSUE_URL, 301), (PDF_URL, 301),
            (NESTED_ARTICLE_URL, 302), (NESTED_ARTICLE_URL, 307),
            (NESTED_ARTICLE_URL, 403), (NESTED_ARTICLE_URL, 404),
        ):
            with self.subTest(url=url, status=status), self.assertRaises(ValueError):
                resolve_article_redirect(self._snapshot(url=url, status=status))


class QuantumPlainFieldTests(unittest.TestCase):
    """Проверки подтверждённых обычных подписей без расширения границ полей."""

    def test_confirmed_plain_structure_keeps_bibliography_and_separates_received_date(self) -> None:
        """Обычные подписи дают аннотацию и цитирование, но не дату поступления в тексте."""

        document = parse_article(PLAIN_FIELDS_HTML, ARTICLE_URL)

        self.assertEqual(document.text, "Описана история лазерных исследований.")
        self.assertEqual(document.authors, ["А. А. Иванов"])
        self.assertEqual(document.extra["year"], 2022)
        self.assertEqual(document.extra["volume"], "52")
        self.assertEqual(document.extra["issue"], "12")
        self.assertIn("1064–1069", document.extra["citation"])
        self.assertEqual(document.section, "Специальный исторический выпуск")
        self.assertEqual(document.language, "ru")
        self.assertIsNone(document.published)
        self.assertIsNone(document.pdf_url)

    def test_plain_heading_accepts_inline_text_and_whitespace(self) -> None:
        """Пробелы, переносы и встроенное выделение значения не уничтожают текст поля."""

        for tag in ("p", "div"):
            html = f"<{tag}>\n аннотация&nbsp;: Изучены <em>два</em> режима.<br>Сравнены результаты.</{tag}>"

            with self.subTest(tag=tag):
                self.assertEqual(
                    _labeled_text(_parse_html(html), "Аннотация", allow_plain=True),
                    "Изучены два режима. Сравнены результаты.",
                )

    def test_plain_field_stops_before_next_field_on_new_line(self) -> None:
        """Соседняя явная подпись после br не попадает в значение аннотации."""

        for boundary in (
            "Поступила в редакцию: 26.05.2022", "Ключевые слова: лазер",
            "Образец цитирования: другая запись", "DOI: 10.1234/example",
        ):
            html = f"<p>Аннотация: Изучены лазеры.<br />{boundary}</p>"

            with self.subTest(boundary=boundary):
                self.assertEqual(_labeled_text(_parse_html(html), "Аннотация", allow_plain=True), "Изучены лазеры.")

    def test_plain_field_stops_before_adjacent_bold_field(self) -> None:
        """Выделенная подпись соседнего поля задаёт границу даже без br."""

        html = "<p>Аннотация: Изучены лазеры. <strong>DOI:</strong> 10.1234/example</p>"

        self.assertEqual(_labeled_text(_parse_html(html), "Аннотация", allow_plain=True), "Изучены лазеры.")

    def test_field_mention_in_prose_is_not_a_heading(self) -> None:
        """Упоминание подписи в середине абзаца либо без двоеточия не создаёт поле."""

        for text in (
            "В тексте есть Аннотация: это не поле", "Аннотация исследования приведена ниже",
            "Аннотация результатов: это не подтверждённая подпись", "Неаннотация: текст",
            "Текст <b>Аннотация:</b> после вступления", "Образец цитирования отсутствует",
        ):
            with self.subTest(text=text):
                content = _parse_html(f"<p>{text}</p>")
                self.assertEqual(_labeled_text(content, "Аннотация", allow_plain=True), "")
                self.assertEqual(_labeled_text(content, "Образец цитирования", allow_plain=True), "")

    def test_container_does_not_merge_labeled_paragraphs(self) -> None:
        """Общий div не склеивает аннотацию с отдельным абзацем цитирования."""

        html = "<div><p>Аннотация: Изучены лазеры.</p><p>Образец цитирования: Отдельная запись.</p></div>"
        content = _parse_html(html)

        self.assertEqual(_labeled_text(content, "Аннотация", allow_plain=True), "Изучены лазеры.")
        self.assertEqual(_labeled_text(content, "Образец цитирования", allow_plain=True), "Отдельная запись.")

    def test_unscoped_container_label_does_not_swallow_nested_body(self) -> None:
        """Подпись вне отдельного блока не позволяет присвоить аннотации все дочерние абзацы."""

        html = "<div>Аннотация:<p>Первый блок.</p><p>Следующий блок.</p></div>"

        self.assertEqual(_labeled_text(_parse_html(html), "Аннотация", allow_plain=True), "")

    def test_bold_heading_with_inline_content_keeps_previous_behavior(self) -> None:
        """Прежние b и strong работают с двоеточием и без него, сохраняя встроенное оформление."""

        for heading in ("<b>Аннотация:</b>", "<strong>Аннотация</strong>"):
            html = f"<div>{heading} Изучены <i>лазеры</i> и <b>два</b> режима.</div>"

            with self.subTest(heading=heading):
                self.assertEqual(_labeled_text(_parse_html(html), "Аннотация"), "Изучены лазеры и два режима.")

    def test_existing_bold_field_is_not_rewritten_by_plain_field_boundaries(self) -> None:
        """Старая поддерживаемая разметка не получает молчаливого изменения ранее сохранённого значения."""

        html = "<p><b>Аннотация:</b> Изучены лазеры.<br>Поступила в редакцию: 26.05.2022</p>"

        self.assertEqual(
            _labeled_text(_parse_html(html), "Аннотация"),
            "Изучены лазеры. Поступила в редакцию: 26.05.2022",
        )

    def test_plain_doi_is_not_newly_accepted(self) -> None:
        """Новая поддержка не превращает невыделенные DOI и ключевые слова в подтверждённые поля."""

        content = _parse_html("<p>DOI: 10.1234/example</p><p>Ключевые слова: лазер</p>")

        self.assertEqual(_labeled_text(content, "DOI", allow_plain=True), "")
        self.assertEqual(_labeled_text(content, "Ключевые слова", allow_plain=True), "")

    def test_mixed_saved_format_is_not_silently_enriched(self) -> None:
        """Частично поддерживаемая карточка не меняет прежние поля без отдельной версии дополнения."""

        bold_citation = PLAIN_FIELDS_HTML.replace("Образец цитирования:", "<b>Образец цитирования:</b>")
        document = parse_article(bold_citation, ARTICLE_URL)

        self.assertEqual(document.text, "")
        self.assertEqual(document.extra["year"], 2022)
        self.assertIn("1064–1069", document.extra["citation"])

        bold_abstract = PLAIN_FIELDS_HTML.replace("Аннотация:", "<b>Аннотация:</b>")
        document = parse_article(bold_abstract, ARTICLE_URL)

        self.assertEqual(document.text, "Описана история лазерных исследований. Поступила в редакцию: 26.05.2022")
        self.assertNotIn("citation", document.extra)
        self.assertNotIn("year", document.extra)


class QuantumArticleTests(unittest.TestCase):
    """Проверки метаданных и выбора русской версии текста."""

    def test_fields_without_editor_or_post_date(self) -> None:
        """Авторы и год берутся из библиографии, не из служебных данных WordPress."""

        document = parse_article(ARTICLE_HTML, ARTICLE_URL)

        self.assertEqual(document.title, "Исследование лазерных импульсов")
        self.assertEqual(document.authors, ["А. А. Иванов", "Б. Б. Петров"])
        self.assertEqual(document.text, "Изучены лазеры. Сравнены два режима.")
        self.assertEqual(document.extra["year"], 2024)
        self.assertEqual(document.extra["volume"], "54")
        self.assertEqual(document.extra["issue"], "1")
        self.assertEqual(document.extra["keywords"], ["лазер", "импульс"])
        self.assertEqual(document.extra["doi"], "10.1234/example")
        self.assertEqual(document.extra["content_role"], "metadata_only")
        self.assertEqual(document.section, "Лазеры")
        self.assertEqual(document.pdf_url, PDF_URL)
        self.assertIsNone(document.published)

    def test_article_link_extraction_returns_its_pdf_only(self) -> None:
        """Полный текст не подменяется соседней публикацией из меню."""

        self.assertEqual(extract_links(ARTICLE_HTML, ARTICLE_URL), [PDF_URL])

    def test_download_caption_without_link_does_not_invent_pdf(self) -> None:
        """Надпись «Скачать» без href оставляет карточку без PDF."""

        html = ARTICLE_HTML.replace(
            '<a href="/wp-content/uploads/2024/01/test_article.pdf">Скачать (.pdf)</a>',
            "Скачать (.pdf)",
        )

        document = parse_article(html, ARTICLE_URL)

        self.assertIsNone(document.pdf_url)
        self.assertEqual(extract_links(html, ARTICLE_URL), [])
        self.assertTrue(document.text)

    def test_english_pdf_is_not_selected(self) -> None:
        """Явная английская версия не выдаётся за русский полный текст."""

        for html in (
            ARTICLE_HTML.replace("Скачать (.pdf)</a>", "English PDF</a>"),
            ARTICLE_HTML.replace("test_article.pdf", "test_article_eng.pdf"),
        ):
            with self.subTest(html=html):
                self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_non_russian_article_metadata_cannot_authorize_pdf(self) -> None:
        """Русский URL и кнопка скачивания не определяют язык заголовка и аннотации."""

        for title, abstract, expected_language in (
            ("Study of laser pulses", "Two laser modes were studied.", "en"),
            ("Study of laser pulses а", "Two laser modes were studied.", "und"),
            ("Исследование лазеров", "Two laser modes were studied.", "und"),
        ):
            html = ARTICLE_HTML.replace(
                "Исследование <i>лазерных</i> импульсов", title,
            ).replace(
                "Изучены&nbsp;лазеры.<br>\nСравнены <em>два</em> режима.", abstract,
            )

            with self.subTest(title=title):
                document = parse_article(html, ARTICLE_URL)
                self.assertEqual(document.language, expected_language)
                self.assertIsNone(document.pdf_url)
                self.assertTrue(document.extra["skipped_non_russian"])
                self.assertEqual(
                    document.extra["language_basis"], "title_and_abstract_heuristic",
                )
                self.assertEqual(extract_links(html, ARTICLE_URL), [])

    def test_ambiguous_pdf_is_left_empty(self) -> None:
        """Два разных русских PDF требуют отдельной проверки выбора."""

        html = ARTICLE_HTML.replace(
            "</div><footer>",
            '<p><a href="/wp-content/uploads/2024/second.pdf">Скачать PDF</a></p>'
            "</div><footer>",
        )

        self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_reference_doi_is_not_article_doi(self) -> None:
        """Немаркированный DOI из библиографии не присваивается текущей статье."""

        html = ARTICLE_HTML.replace("<b>DOI:</b>", "<b>Литература:</b>")

        self.assertNotIn("doi", parse_article(html, ARTICLE_URL).extra)

    def test_non_article_and_missing_title_raise(self) -> None:
        """Служебные страницы и неполные карточки не регистрируются как статьи."""

        for html in (
            (
                "<article><h1 class='entry-title'>Новости</h1>"
                "<div class='entry-content'><p>Новость</p></div></article>"
            ),
            ARTICLE_HTML.replace("entry-title", "other-title"),
            "<html>Страница отсутствует</html>",
        ):
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_article(html, ARTICLE_URL)

        with self.assertRaises(ValueError):
            parse_article(ARTICLE_HTML, START_URL)

    def test_invalid_html_type(self) -> None:
        """Неверный тип HTML вызывает понятную ошибку аргумента."""

        with self.assertRaisesRegex(TypeError, "HTML должен быть строкой"):
            parse_article(cast(str, None), ARTICLE_URL)


class QuantumObituaryTests(unittest.TestCase):
    """Проверки строго распознаваемых некрологов без аннотации и библиографии."""

    def test_confirmed_obituary_is_metadata_without_authors_date_or_pdf(self) -> None:
        """Редактор, даты и извещение не превращаются в поля научной статьи."""

        document = parse_article(OBITUARY_HTML, OBITUARY_URL)

        self.assertEqual(document.title, "Памяти Георгия Митрофановича Зверева")
        self.assertEqual(document.url, OBITUARY_URL)
        self.assertEqual(document.section, "Некролог")
        self.assertEqual(document.authors, [])
        self.assertEqual(document.text, "")
        self.assertEqual(document.language, "ru")
        self.assertIsNone(document.published)
        self.assertIsNone(document.pdf_url)
        self.assertNotIn("year", document.extra)
        self.assertNotIn("abstract", document.extra)
        self.assertNotIn("citation", document.extra)
        self.assertEqual(document.extra["publication_type"], "obituary")
        self.assertEqual(document.extra["metadata_source"], "obituary_card")
        self.assertEqual(document.extra["content_role"], "metadata_only")
        self.assertEqual(extract_links(OBITUARY_HTML, OBITUARY_URL), [])

    def test_obituary_requires_matching_category_and_title(self) -> None:
        """Название или отдельная рубрика сами по себе не отменяют прежний отказ."""

        replacements = [
            ('class="post category-nekrolog"', 'class="post"'),
            ('class="entry-meta"', 'class="other-meta"'),
            ('rel="category tag"', 'rel="tag"'),
            ('>Некролог</a>', '>Новости</a>'),
            ('/category/nekrolog/', '/category/news/'),
            ('https://quantum-electronics.ru/category/', 'https://other.test/category/'),
            ('/category/nekrolog/', '/category/nekrolog/?lang=en'),
            ('Памяти Георгия Митрофановича Зверева', 'Служебное сообщение'),
        ]

        for before, after in replacements:
            html = OBITUARY_HTML.replace(before, after)

            with self.subTest(before=before, after=after), self.assertRaises(ValueError):
                parse_article(html, OBITUARY_URL)

    def test_obituary_category_outside_own_footer_is_not_confirmation(self) -> None:
        """Боковое меню и рубрика в основном тексте не подтверждают тип публикации."""

        variants = [
            OBITUARY_HTML.replace(OBITUARY_FOOTER, "") + OBITUARY_FOOTER,
            OBITUARY_HTML.replace(OBITUARY_FOOTER, OBITUARY_FOOTER.replace("footer", "aside")),
            OBITUARY_HTML.replace(OBITUARY_FOOTER, '<footer class="entry-meta"></footer>') +
            '<aside><a rel="category tag" href="/category/nekrolog/">Некролог</a></aside>',
        ]

        for html in variants:
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_article(html, OBITUARY_URL)

    def test_obituary_requires_nonempty_notice_not_error_or_download(self) -> None:
        """Пустой блок, заглушка и ссылка скачивания не являются извещением."""

        notices = [
            "", "Страница временно недоступна.", "Скачать (.pdf)",
            "The editorial board announces that the scientist passed away.",
            '<a href="/another-page/">Редколлегия сообщает о кончине учёного.</a>',
            f"<script>{OBITUARY_NOTICE}</script>",
        ]

        for notice in notices:
            with self.subTest(notice=notice), self.assertRaises(ValueError):
                parse_article(OBITUARY_HTML.replace(OBITUARY_NOTICE, notice), OBITUARY_URL)

    def test_obituary_rejects_ambiguous_article_heading_content_or_category(self) -> None:
        """Несколько возможных источников полей требуют проверки вместо выбора первого."""

        variants = [
            OBITUARY_HTML + '<article class="category-nekrolog"></article>',
            OBITUARY_HTML.replace('</header>', '<h1 class="entry-title">Памяти другого учёного</h1></header>'),
            OBITUARY_HTML.replace('</article>', '<div class="entry-content">Другой текст</div></article>'),
            OBITUARY_HTML.replace('</article>', OBITUARY_FOOTER + '</article>'),
            OBITUARY_HTML.replace('</span></footer>',
                                  '<a rel="category tag" href="/category/news/">Новости</a></span></footer>'),
        ]

        for html in variants:
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_article(html, OBITUARY_URL)

    def test_scientific_card_keeps_existing_fields_when_title_mentions_memory(self) -> None:
        """Карточка с научными полями продолжает разбираться прежним способом."""

        html = ARTICLE_HTML.replace('Исследование <i>лазерных</i> импульсов', 'Памяти исследователя лазеров')
        document = parse_article(html, ARTICLE_URL)

        self.assertEqual(document.authors, ["А. А. Иванов", "Б. Б. Петров"])
        self.assertEqual(document.extra["year"], 2024)
        self.assertEqual(document.pdf_url, PDF_URL)
        self.assertEqual(document.text, "Изучены лазеры. Сравнены два режима.")
        self.assertNotIn("publication_type", document.extra)


class QuantumIssueTests(unittest.TestCase):
    """Проверки старых оглавлений с прямыми ссылками на PDF."""

    def test_direct_pdf_retains_its_title_authors_and_issue(self) -> None:
        """Название и авторы связываются с единственной ссылкой своей записи."""

        html = """
        <h1 class="entry-title">2024, том 54, номер 1</h1>
        <div class="entry-content"><p><b>Лазеры</b></p>
        <p><a href="/wp-content/uploads/2024/01/test_article.pdf">
        Исследование импульсов</a><br>А.А. Иванов, Б.Б. Петров 23</p></div>
        """
        issue_url = f"{ORIGIN}/2024_54_1/"
        documents = parse_issue_articles(html, issue_url)

        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document.url, PDF_URL)
        self.assertEqual(document.pdf_url, PDF_URL)
        self.assertEqual(document.title, "Исследование импульсов")
        self.assertEqual(document.authors, ["А.А. Иванов", "Б.Б. Петров"])
        self.assertEqual(document.extra["first_page"], 23)
        self.assertEqual(document.extra["year"], 2024)
        self.assertEqual(document.extra["metadata_source"], "issue_toc")
        self.assertEqual(document.extra["issue_url"], issue_url)
        self.assertEqual(document.section, "Лазеры")
        self.assertEqual(document.text, "")
        self.assertEqual(extract_links(html, issue_url), [PDF_URL])

    def test_regular_issue_has_no_synthetic_documents(self) -> None:
        """Обычные карточки обрабатываются отдельно, не дублируясь из оглавления."""

        html = '<div class="entry-content"><a href="/some-article/">Статья</a></div>'

        self.assertEqual(parse_issue_articles(html, f"{ORIGIN}/2026_56_1/"), [])

    def test_inline_section_keeps_obituary_without_inventing_authors(self) -> None:
        """Рубрика внутри абзаца отделяется от названия, а номер остаётся страницей."""

        for tag in ("b", "strong"):
            html = INLINE_SECTION_ISSUE_HTML.replace(
                "<strong>Некролог</strong><br />",
                f"\n <{tag}>Некролог</{tag}> \n <br /> \t ",
            )

            with self.subTest(tag=tag):
                documents = parse_issue_articles(html, INLINE_SECTION_ISSUE_URL)
                self.assertEqual(len(documents), 1)
                document = documents[0]
                self.assertEqual(document.title, "Памяти Юрия Михайловича Попова")
                self.assertEqual(document.section, "Некролог")
                self.assertEqual(document.authors, [])
                self.assertEqual(document.extra["first_page"], 336)
                self.assertEqual(document.extra["issue_url"], INLINE_SECTION_ISSUE_URL)
                self.assertEqual(document.extra["content_role"], "metadata_only")
                self.assertEqual(
                    document.pdf_url, f"{ORIGIN}/wp-content/uploads/2025/05/336.pdf",
                )
                self.assertEqual(extract_links(html, INLINE_SECTION_ISSUE_URL), [document.pdf_url])

    def test_inline_section_preserves_authors_and_subsequent_records(self) -> None:
        """Отделение рубрики не склеивает имена и действует для последующих записей."""

        html = INLINE_SECTION_ISSUE_HTML.replace(
            "</a>\t\t<strong>336</strong>",
            "</a><br />А. А. Иванов, Б. Б. Петров <strong>336</strong>",
        ).replace(
            "</div>",
            '<p><a href="/wp-content/uploads/2025/05/338.pdf">Памяти другого учёного</a>'
            '<br />В. В. Сидоров <strong>338</strong></p></div>',
        )
        documents = parse_issue_articles(html, INLINE_SECTION_ISSUE_URL)

        self.assertEqual(len(documents), 2)
        self.assertEqual(documents[0].authors, ["А. А. Иванов", "Б. Б. Петров"])
        self.assertEqual(documents[0].extra["first_page"], 336)
        self.assertEqual(documents[1].section, "Некролог")
        self.assertEqual(documents[1].authors, ["В. В. Сидоров"])
        self.assertEqual(documents[1].extra["first_page"], 338)

    def test_inline_section_accepts_one_trailing_break_inside_heading(self) -> None:
        """Завершающий перенос внутри рубрики не меняет поля и следующие статьи."""

        section = "Управление параметрами лазерного излучения"

        for tag in ("b", "strong"):
            for break_markup in ("<br>", "<br/>", "<br />"):
                html = INLINE_SECTION_ISSUE_HTML.replace(
                    "<strong>Некролог</strong><br />",
                    f"<{tag}>{section}{break_markup}\n </{tag}>\n<br />",
                ).replace(
                    "</a>\t\t<strong>336</strong>",
                    "</a><br />А. А. Иванов, Б. Б. Петров <strong>336</strong>",
                ).replace(
                    "</div>",
                    '<p><a href="/wp-content/uploads/2025/05/338.pdf">Другая статья</a>'
                    '<br />В. В. Сидоров <strong>338</strong></p></div>',
                )

                with self.subTest(tag=tag, break_markup=break_markup):
                    documents = parse_issue_articles(html, INLINE_SECTION_ISSUE_URL)
                    self.assertEqual(len(documents), 2)
                    document = documents[0]
                    self.assertEqual(document.section, section)
                    self.assertEqual(document.title, "Памяти Юрия Михайловича Попова")
                    self.assertEqual(document.authors, ["А. А. Иванов", "Б. Б. Петров"])
                    self.assertEqual(document.extra["first_page"], 336)
                    self.assertEqual(document.extra["content_role"], "metadata_only")
                    self.assertEqual(document.extra["issue_url"], INLINE_SECTION_ISSUE_URL)
                    self.assertEqual(document.pdf_url, f"{ORIGIN}/wp-content/uploads/2025/05/336.pdf")
                    self.assertEqual(documents[1].section, section)
                    self.assertEqual(documents[1].title, "Другая статья")
                    self.assertEqual(documents[1].authors, ["В. В. Сидоров"])
                    self.assertEqual(documents[1].extra["first_page"], 338)
                    self.assertEqual(
                        extract_links(html, INLINE_SECTION_ISSUE_URL),
                        [document.pdf_url, documents[1].pdf_url],
                    )

    def test_inline_section_rejects_unknown_content_around_trailing_break(self) -> None:
        """Допуск одного завершающего переноса не скрывает неизвестные узлы и текст."""

        prefixes = [
            "<strong>Лазеры<br /></strong>",
            "<strong><br />Лазеры</strong><br />",
            "<strong>Лазеры<br />Другая рубрика</strong><br />",
            "<strong>Лазеры<br /><br /></strong><br />",
            "<strong><br /></strong><br />",
            "<strong><em>Лазеры</em><br /></strong><br />",
            "<strong>Лазеры<br /><script>скрытый текст</script></strong><br />",
            '<strong><a href="https://example.test/">Лазеры</a><br /></strong><br />',
            '<strong>Лазеры<br class="unknown" /></strong><br />',
            "<strong>Лазеры<br /></strong><br />Неизвестный текст",
            "<strong>Лазеры<br /></strong><b>Другая рубрика</b><br />",
            "<strong>Лазеры<br /></strong><br /><br />",
        ]

        for prefix in prefixes:
            html = INLINE_SECTION_ISSUE_HTML.replace("<strong>Некролог</strong><br />", prefix)

            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                parse_issue_articles(html, INLINE_SECTION_ISSUE_URL)

    def test_inline_section_accepts_empty_heading_with_one_break(self) -> None:
        """Пустой выделенный перенос не меняет поля статьи и следующей записи."""

        for tag in ("b", "strong"):
            for break_markup in ("<br>", "<br/>", "<br />"):
                html = INLINE_SECTION_ISSUE_HTML.replace(
                    "<strong>Некролог</strong><br />",
                    f"<strong>Некролог</strong><br />\n<{tag}> \t{break_markup}\n </{tag}>\t",
                ).replace(
                    "</a>\t\t<strong>336</strong>",
                    "</a><br />А. А. Иванов, Б. Б. Петров <strong>336</strong>",
                ).replace(
                    "</div>",
                    '<p><a href="/wp-content/uploads/2025/05/338.pdf">Другая статья</a>'
                    '<br />В. В. Сидоров <strong>338</strong></p></div>',
                )
                expected_html = html.replace(f"<{tag}> \t{break_markup}\n </{tag}>\t", "")

                with self.subTest(tag=tag, break_markup=break_markup):
                    documents = parse_issue_articles(html, INLINE_SECTION_ISSUE_URL)
                    expected = parse_issue_articles(expected_html, INLINE_SECTION_ISSUE_URL)

                    self.assertEqual(len(documents), 2)
                    self.assertEqual(documents, expected)
                    self.assertEqual(documents[0].authors, ["А. А. Иванов", "Б. Б. Петров"])
                    self.assertEqual(documents[0].extra["first_page"], 336)
                    self.assertEqual(documents[1].section, "Некролог")
                    self.assertEqual(documents[1].extra["first_page"], 338)
                    self.assertEqual(
                        extract_links(html, INLINE_SECTION_ISSUE_URL),
                        [document.pdf_url for document in documents],
                    )

    def test_december_2012_portrait_does_not_invent_authors_or_page(self) -> None:
        """Фотография из реального оглавления сохраняется без выдуманных полей."""

        html = """
        <h1 class="entry-title">2012, том 42, номер 12</h1>
        <div class="entry-content">
        <p><strong>Специальный выпуск, посвященный 90-летию Н.Г.Басова</strong><br />
        <strong><br />
        </strong><a href="/wp-content/uploads/2012/12/15079.pdf">Николай Геннадиевич Басов (фотография)</a></p>
        </div>
        """
        issue_url = f"{ORIGIN}/2012_42_12/"
        documents = parse_issue_articles(html, issue_url)

        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document.section, "Специальный выпуск, посвященный 90-летию Н.Г.Басова")
        self.assertEqual(document.title, "Николай Геннадиевич Басов (фотография)")
        self.assertEqual(document.authors, [])
        self.assertNotIn("first_page", document.extra)
        self.assertEqual(document.extra["year"], 2012)
        self.assertEqual(document.extra["volume"], "42")
        self.assertEqual(document.extra["issue"], "12")
        self.assertEqual(document.extra["issue_url"], issue_url)
        self.assertEqual(document.extra["content_role"], "metadata_only")
        self.assertEqual(document.pdf_url, f"{ORIGIN}/wp-content/uploads/2012/12/15079.pdf")
        self.assertEqual(document.text, "")

    def test_inline_section_rejects_unknown_empty_heading_content(self) -> None:
        """Пустым разделителем не признаётся содержательный или неоднозначный узел."""

        separators = [
            "<strong></strong>",
            "<strong> \t </strong>",
            "<strong><br /><br /></strong>",
            "<strong>Другая рубрика<br /></strong>",
            "<strong><br />Другая рубрика</strong>",
            "<strong><script>скрытый текст</script><br /></strong>",
            "<strong><br /><style>скрытый текст</style></strong>",
            "<strong><template></template><br /></strong>",
            '<strong hidden><br /></strong>',
            '<strong class="unknown"><br /></strong>',
            '<strong style="display:none"><br /></strong>',
            '<strong><br class="unknown" /></strong>',
            "<strong><b><br /></b></strong>",
            "<span><br /></span>",
            "<strong><br /></strong><strong><br /></strong>",
            "<strong><br /></strong><br />",
            "<br /><strong><br /></strong>",
            "<strong><br /></strong>Неизвестный текст",
            '<strong><a href="https://example.test/"></a><br /></strong>',
        ]

        for separator in separators:
            html = INLINE_SECTION_ISSUE_HTML.replace(
                "<strong>Некролог</strong><br />",
                f"<strong>Некролог</strong><br />{separator}",
            )

            with self.subTest(separator=separator), self.assertRaises(ValueError):
                parse_issue_articles(html, INLINE_SECTION_ISSUE_URL)

    def test_separate_section_without_authors_keeps_previous_metadata(self) -> None:
        """Старая форма записи не получает новые поля при повторном разборе."""

        html = INLINE_SECTION_ISSUE_HTML.replace(
            "<strong>Некролог</strong><br />", "<strong>Некролог</strong></p><p>",
        )
        documents = parse_issue_articles(html, INLINE_SECTION_ISSUE_URL)

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].section, "Некролог")
        self.assertEqual(documents[0].authors, [])
        self.assertNotIn("first_page", documents[0].extra)

    def test_inline_section_rejects_unknown_or_ambiguous_prefix(self) -> None:
        """Произвольный текст, несколько рубрик и вложенные ссылки не отбрасываются."""

        prefixes = [
            "<strong>Некролог</strong>",
            "Некролог<br />",
            "Неизвестный текст<strong>Некролог</strong><br />",
            "<strong>Некролог</strong><br />Неизвестный текст",
            "<strong>Некролог</strong><br /><b>Другая рубрика</b><br />",
            "<strong>Некролог</strong><strong>Другая рубрика</strong><br />",
            '<strong><a href="https://example.test/">Некролог</a></strong><br />',
            '<strong><a href="/wp-content/uploads/2025/05/330.pdf">Некролог</a></strong><br />',
            "<strong><em>Некролог</em></strong><br />",
            "<strong> </strong><br />",
            "<strong>Некролог</strong><br /><br />",
        ]

        for prefix in prefixes:
            html = INLINE_SECTION_ISSUE_HTML.replace("<strong>Некролог</strong><br />", prefix)

            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                parse_issue_articles(html, INLINE_SECTION_ISSUE_URL)

    def test_inline_section_does_not_allow_two_pdfs_or_mismatched_issue(self) -> None:
        """Новая разметка сохраняет проверки единственной ссылки и номера выпуска."""

        ambiguous = INLINE_SECTION_ISSUE_HTML.replace(
            "</p>", '<a href="/wp-content/uploads/2025/05/338.pdf">Другая статья</a></p>',
        )

        with self.assertRaisesRegex(ValueError, "несколько PDF"):
            parse_issue_articles(ambiguous, INLINE_SECTION_ISSUE_URL)

        with self.assertRaisesRegex(ValueError, "Заголовок выпуска не соответствует"):
            parse_issue_articles(INLINE_SECTION_ISSUE_HTML, f"{ORIGIN}/2025_55_6/")

    def test_moved_issue_retains_original_number_and_actual_source_url(self) -> None:
        """Суффикс адреса не меняет год, том и номер подтверждённого оглавления."""

        html = """
        <h1 class="entry-title">2025, том 55, номер 2</h1>
        <div class="entry-content"><p>
        <a href="/wp-content/uploads/2024/01/test_article.pdf">
        Исследование импульсов</a><br>А.А. Иванов 23</p></div>
        """
        documents = parse_issue_articles(html, MOVED_ISSUE_URL)

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].extra["year"], 2025)
        self.assertEqual(documents[0].extra["volume"], "55")
        self.assertEqual(documents[0].extra["issue"], "2")
        self.assertEqual(documents[0].extra["issue_url"], MOVED_ISSUE_URL)
        self.assertEqual(documents[0].pdf_url, PDF_URL)
        self.assertEqual(extract_links(html, MOVED_ISSUE_URL), [PDF_URL])

        for heading in (
            "2024, том 55, номер 2", "2025, том 54, номер 2",
            "2025, том 55, номер 3", "2025, том 55, номер 2-2",
        ):
            changed = html.replace("2025, том 55, номер 2", heading)

            with self.subTest(heading=heading), self.assertRaises(ValueError):
                parse_issue_articles(changed, MOVED_ISSUE_URL)

    def test_english_pdf_does_not_enter_russian_issue(self) -> None:
        """Английский файл не получает русскую языковую отметку из URL выпуска."""

        html = """
        <h1 class="entry-title">2024, том 54, номер 1</h1>
        <div class="entry-content">
        <p><a href="/wp-content/uploads/2024/article_eng.pdf">Русское название</a></p>
        <p><a href="/wp-content/uploads/2024/article.pdf">English article</a></p>
        <p><a href="/wp-content/uploads/2024/mixed.pdf">English article а</a></p>
        </div>
        """

        self.assertEqual(parse_issue_articles(html, f"{ORIGIN}/2024_54_1/"), [])
        self.assertEqual(extract_links(html, f"{ORIGIN}/2024_54_1/"), [])

    def test_ambiguous_or_mismatched_issue_raises(self) -> None:
        """Неоднозначная привязка или неверный год требуют проверки источника."""

        html = """
        <h1 class="entry-title">2024, том 54, номер 1</h1>
        <div class="entry-content"><p>
        <a href="/wp-content/uploads/2024/one.pdf">Первая статья</a><br>А.А. Иванов 1
        </p></div>
        """

        with self.assertRaisesRegex(ValueError, "Заголовок выпуска не соответствует"):
            parse_issue_articles(html, f"{ORIGIN}/2023_53_1/")

        ambiguous = html.replace(
            "</p>", '<a href="/wp-content/uploads/2024/two.pdf">Вторая статья</a></p>',
        )

        with self.assertRaisesRegex(ValueError, "несколько PDF"):
            parse_issue_articles(ambiguous, f"{ORIGIN}/2024_54_1/")


class QuantumUnlinkedIssueTests(unittest.TestCase):
    """Проверки оглавлений без ссылок, которые нельзя считать собранными статьями."""

    def test_unlinked_issue_retains_bibliographic_entries(self) -> None:
        """Подтверждённый формат сохраняет названия, авторов, страницы и рубрики."""

        entries = parse_unlinked_issue(UNLINKED_ISSUE_HTML, UNLINKED_ISSUE_URL)

        self.assertEqual(entries, [
            {
                "title": "Исследование изомерных состояний 83Kr в плазме",
                "authors": ["А. А. Иванов", "Б. Б. Петров"],
                "first_page": 317,
                "section": "Лазерная плазма",
            },
            {
                "title": "Флуоресценция кристалла Ti3+:Al2O3",
                "authors": ["Д. В. Яковин", "A. Ф. Лелекова"],
                "first_page": 333,
                "section": "Лазерная спектроскопия",
            },
        ])
        self.assertEqual(extract_links(UNLINKED_ISSUE_HTML, UNLINKED_ISSUE_URL), [])
        self.assertEqual(parse_issue_articles(UNLINKED_ISSUE_HTML, UNLINKED_ISSUE_URL), [])

    def test_parenthetical_surname_is_retained_in_full(self) -> None:
        """Одна альтернативная фамилия сохраняется в имени без создания ссылки статьи."""

        authors = [
            "Ю. К. Кириченко (Бобрецова)",
            "Ю. К. Кириченко (Бобрецова-Петрова)",
            "J. K. Smith (Jones)",
        ]

        for author in authors:
            html = UNLINKED_ISSUE_HTML.replace("А. А. Иванов", author)

            with self.subTest(author=author):
                entries = parse_unlinked_issue(html, UNLINKED_ISSUE_URL)
                self.assertEqual(len(entries), 2)
                self.assertEqual(entries[0]["authors"], [author, "Б. Б. Петров"])
                self.assertEqual(entries[0]["first_page"], 317)
                self.assertEqual(extract_links(html, UNLINKED_ISSUE_URL), [])
                self.assertEqual(parse_issue_articles(html, UNLINKED_ISSUE_URL), [])

    def test_moved_unlinked_issue_requires_original_heading(self) -> None:
        """Отложить можно только тот выпуск, для которого подтверждён перенос."""

        html = UNLINKED_ISSUE_HTML.replace("2026, том 56, номер 5", "2025, том 55, номер 2")

        self.assertEqual(
            parse_unlinked_issue(html, MOVED_ISSUE_URL),
            parse_unlinked_issue(UNLINKED_ISSUE_HTML, UNLINKED_ISSUE_URL),
        )
        self.assertEqual(extract_links(html, MOVED_ISSUE_URL), [])
        self.assertEqual(parse_issue_articles(html, MOVED_ISSUE_URL), [])

        for heading in (
            "2024, том 55, номер 2", "2025, том 54, номер 2", "2025, том 55, номер 3",
        ):
            changed = html.replace("2025, том 55, номер 2", heading)

            with self.subTest(heading=heading), self.assertRaisesRegex(
                ValueError, "Заголовок выпуска не соответствует",
            ):
                parse_unlinked_issue(changed, MOVED_ISSUE_URL)

        ranged = html.replace("номер 2", "номер 2-2")
        self.assertEqual(parse_unlinked_issue(ranged, MOVED_ISSUE_URL), [])

    def test_invalid_parenthetical_surname_rejects_entire_issue(self) -> None:
        """Пустые, вложенные и незакрытые скобки или посторонний текст не принимаются."""

        authors = [
            "Ю. К. Кириченко ()",
            "Ю. К. Кириченко ( )",
            "Ю. К. Кириченко ((Бобрецова))",
            "Ю. К. Кириченко (Бобрецова (Петрова))",
            "Ю. К. Кириченко (Бобрецова",
            "Ю. К. Кириченко Бобрецова)",
            "Ю. К. Кириченко (Бобрецова) (Петрова)",
            "Ю. К. Кириченко (Бобрецова) Далее",
            "Ю. К. Кириченко (Бобрецова)*",
            "Ю. К. Кириченко (Бобрецова123)",
            "Ю. К. Кириченко (Бобрецова-Петрова-)",
            "Ю. К. Кириченко (Бобрецова--Петрова)",
            "Ю. К. Кириченко (Ю. К. Бобрецова)",
        ]

        for author in authors:
            html = UNLINKED_ISSUE_HTML.replace("А. А. Иванов", author)

            with self.subTest(author=author):
                self.assertEqual(parse_unlinked_issue(html, UNLINKED_ISSUE_URL), [])

    def test_section_is_optional_and_inline_spacing_is_preserved(self) -> None:
        """Без рубрики остаётся None, а пробелы между оформленными словами не теряются."""

        html = UNLINKED_ISSUE_HTML.replace(
            "<p><strong>Лазерная плазма</strong></p>", "",
        ).replace(
            "Исследование изомерных состояний",
            "<i>Исследование</i> <em>изомерных состояний</em>",
        )
        entry = parse_unlinked_issue(html, UNLINKED_ISSUE_URL)[0]

        self.assertIsNone(entry["section"])
        self.assertEqual(entry["title"], "Исследование изомерных состояний 83Kr в плазме")

    def test_any_content_anchor_rejects_unlinked_status(self) -> None:
        """Даже отклонённые адреса и пустые якоря не превращаются в отсутствие ссылок."""

        anchors = [
            '<a href="/some-article/">Статья</a>',
            '<a href="/wp-content/uploads/2026/paper.pdf">PDF</a>',
            '<a href="https://example.test/article">Другой сайт</a>',
            '<a href="/en/other-article/">English</a>',
            '<a href="/wp-json/">API</a>',
            '<a href="#article"></a>',
            '<a name="article"></a>',
        ]

        for anchor in anchors:
            html = UNLINKED_ISSUE_HTML.replace("</div>", anchor + "</div>")

            with self.subTest(anchor=anchor):
                self.assertEqual(parse_unlinked_issue(html, UNLINKED_ISSUE_URL), [])

    def test_sidebar_links_do_not_change_unlinked_content(self) -> None:
        """Навигация вне основного содержимого не подменяет ссылки оглавления."""

        html = UNLINKED_ISSUE_HTML + '<aside><a href="/archive/">Архив</a></aside>'

        self.assertEqual(len(parse_unlinked_issue(html, UNLINKED_ISSUE_URL)), 2)

    def test_mismatched_heading_and_invalid_url_raise(self) -> None:
        """Подтверждение не относится к другому выпуску или служебному адресу."""

        with self.assertRaisesRegex(ValueError, "Заголовок выпуска не соответствует"):
            parse_unlinked_issue(UNLINKED_ISSUE_HTML, f"{ORIGIN}/2026_56_6/")

        with self.assertRaisesRegex(ValueError, "Ожидается URL русского выпуска"):
            parse_unlinked_issue(UNLINKED_ISSUE_HTML, START_URL)

    def test_missing_or_ambiguous_structure_is_not_confirmed(self) -> None:
        """Заглушки и неоднозначные блоки не признаются подходящим оглавлением."""

        variants = [
            "",
            "<html><p>Страница отсутствует</p></html>",
            '<h1 class="entry-title">2026, том 56, номер 5</h1>'
            '<div class="entry-content"></div>',
            '<h1 class="entry-title">2026, том 56, номер 5</h1>'
            '<div class="entry-content"><p>Содержание появится позже</p></div>',
            UNLINKED_ISSUE_HTML.replace("entry-title", "other-title"),
            UNLINKED_ISSUE_HTML.replace("entry-content", "other-content"),
            UNLINKED_ISSUE_HTML.replace("2026, том 56, номер 5", "Ошибка сервера"),
            UNLINKED_ISSUE_HTML + '<div class="entry-content"></div>',
            UNLINKED_ISSUE_HTML + '<h1 class="entry-title">2026, том 56, номер 5</h1>',
        ]

        for html in variants:
            with self.subTest(html=html):
                self.assertEqual(parse_unlinked_issue(html, UNLINKED_ISSUE_URL), [])

    def test_unknown_content_rejects_entire_issue(self) -> None:
        """Нельзя сохранить понятную часть, незаметно проигнорировав остальные узлы."""

        additions = [
            "Неизвестный текст",
            "<p>Неизвестная запись</p>",
            "<div>Дополнительная статья</div>",
            "<table><tr><td>Другая разметка</td></tr></table>",
            '<img src="toc.png" alt="Продолжение оглавления">',
            '<noscript><a href="/some-article/">Статья</a></noscript>',
        ]

        for addition in additions:
            html = UNLINKED_ISSUE_HTML.replace("</div>", addition + "</div>")

            with self.subTest(addition=addition):
                self.assertEqual(parse_unlinked_issue(html, UNLINKED_ISSUE_URL), [])

    def test_incomplete_or_ambiguous_entry_rejects_entire_issue(self) -> None:
        """Отсутствие авторов или страницы и неизвестное оформление требуют проверки."""

        replacements = [
            ("А. А. Иванов, Б. Б. Петров", ""),
            ("А. А. Иванов, Б. Б. Петров", "Автор неизвестен"),
            ("А. А. Иванов, Б. Б. Петров", "А. А. Иванов, "),
            ("<strong>317</strong>", ""),
            ("<strong>317</strong>", "317"),
            ("<strong>317</strong>", "<strong>317–325</strong>"),
            ("<strong>317</strong>", "<strong>0</strong>"),
            ("<strong>317</strong>", "<strong>317</strong> Далее"),
            ("<strong>317</strong>", "<strong><em>317</em></strong>"),
            ("<br />", "<br /><br />"),
            ("А. А. Иванов", "<em>А. А. Иванов</em>"),
            ("<sup>83</sup>", "<span>83</span>"),
        ]

        for before, after in replacements:
            html = UNLINKED_ISSUE_HTML.replace(before, after, 1)

            with self.subTest(before=before, after=after):
                self.assertEqual(parse_unlinked_issue(html, UNLINKED_ISSUE_URL), [])


if __name__ == "__main__":
    unittest.main()
