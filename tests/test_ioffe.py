"""Проверки чистого HTML-парсера журналов Иоффе на вымышленных карточках."""

from __future__ import annotations

import unittest

from src.collect.ioffe import (
    IOFFE_JOURNALS,
    extract_article_urls,
    extract_issue_urls,
    parse_article,
)

ORIGIN = "https://journals.ioffe.ru"
ARTICLE_URL = f"{ORIGIN}/articles/123"

ARTICLE_HTML = """
<html><head><title>Название журнала, не статьи</title></head><body>
<nav><a href="/journals/5">Другой журнал в меню</a>
<a href="/issues/200">Год 2026, выпуск 5</a></nav>
<span class="breadcrumbs">
<a href="/journals/1">Физика твердого тела</a>
<a href="/issues/100">Год 2021, выпуск 12</a>
<a href="/articles/123">Статья</a>
</span>
<div class="art_title">Исследование &amp; сравнение <i>кристаллов</i></div>
<div class="art_biblio_doi">DOI:
<a href="https://doi.org/10.1234/Test.123">10.1234/Test.123</a></div>
<div class="art_authors"><div class="authors">
Иванов И.И.<sup>1</sup>, Петров П.П.<span><script>секрет()</script></span>
<a href="https://example.test"><img alt="счётчик"></a><sup>2</sup>
</div><div class="organizations">Университет</div>
<div class="emails">Email: author@example.test</div></div>
<div class="art_date_received">Поступила в редакцию: 2020</div>
<div class="art_date_online">Выставление онлайн: 2022</div>
<div class="art_pdf"><a href="/articles/viewPDF/123">PDF</a></div>
<a href="/articles/viewPDF/124">PDF соседней статьи</a>
<div class="art_tabs" id="yw8"><ul>
<li><a href="#yw8_tab_0" title="Абстракт">Абстракт</a></li>
<li><a href="#yw8_tab_1">Литература</a></li></ul>
<div id="yw8_tab_0">Изучены свойства&nbsp;кристаллов.<br>
Проверены &lt;новые&gt; образцы.
Ключевые слова: кристаллы, оптика, релаксация.</div>
<div id="yw8_tab_1">Литература: DOI 10.9999/NotCurrentArticle</div></div>
</body></html>
"""


class IoffeLinkTests(unittest.TestCase):
    """Проверки границ URL и порядка обхода архива."""

    def test_journal_identifiers_are_explicit(self) -> None:
        """Список источников должен содержать пять согласованных русских журналов."""

        self.assertEqual(set(IOFFE_JOURNALS), {1, 2, 3, 4, 5})
        self.assertEqual(IOFFE_JOURNALS[1], "Физика твердого тела")

    def test_issue_order_and_duplicates(self) -> None:
        """Повторные ссылки удаляются без сортировки исходного порядка."""

        html = """
        <a href="/issues/20">Новый</a><a href="/issues/10/">Старый</a>
        <a href="https://journals.ioffe.ru/issues/20">Повтор</a>
        <a href="//journals.ioffe.ru/issues/10">Повтор</a>
        <a href="/articles/40">Не выпуск</a>
        """

        self.assertEqual(
            extract_issue_urls(html, f"{ORIGIN}/journals/1"),
            [f"{ORIGIN}/issues/20", f"{ORIGIN}/issues/10"],
        )

    def test_article_links_do_not_include_pdf_or_fragment(self) -> None:
        """Карточки, PDF и якоря не должны смешиваться."""

        html = """
        <a href="/articles/8">Статья</a><a href="/articles/8/">Повтор</a>
        <a href="/articles/viewPDF/8">PDF</a><a href="#/articles/9">Якорь</a>
        <a href="/articles/4">Статья</a>
        """

        self.assertEqual(
            extract_article_urls(html, f"{ORIGIN}/issues/1"),
            [f"{ORIGIN}/articles/8", f"{ORIGIN}/articles/4"],
        )

    def test_unsafe_issue_links_are_rejected(self) -> None:
        """Внешние узлы, параметры, обход пути и нечёткие идентификаторы отклоняются."""

        references = [
            "https://example.test/issues/1",
            "https://journals.ioffe.ru.example.test/issues/1",
            "https://user@journals.ioffe.ru/issues/1",
            "https://journals.ioffe.ru:444/issues/1",
            "http://journals.ioffe.ru/issues/1",
            "/issues/1?download=1", "/issues/1?", "/issues/1#", "/issues/1#x",
            "/issues/../issues/1", "../issues/1", "/issues/./1",
            "/issues/%31", "/issues/1%3Fdownload", "/issues/1\\foo",
            "/issues/1 suffix", "/issues/1\n", "/issues/1\x00",
            "/issues/0", "/issues/-1", "/issues/01", "/issues/1/more",
            "javascript:/issues/1", "//example.test/issues/1",
        ]

        for reference in references:
            with self.subTest(reference=reference):
                self.assertEqual(
                    extract_issue_urls(
                        f'<a href="{reference}">Ссылка</a>', f"{ORIGIN}/journals/1"
                    ),
                    [],
                )

    def test_entities_cannot_hide_query_or_foreign_host(self) -> None:
        """HTML-сущности декодируются до проверки URL, а не после неё."""

        html = """
        <a href="/articles/1&#63;download=1">Параметр</a>
        <a href="https://journals.ioffe.ru&#64;example.test/articles/1">Подмена</a>
        <a href="/articles/&#49;">Обычная числовая ссылка</a>
        """

        self.assertEqual(
            extract_article_urls(html, f"{ORIGIN}/issues/1"),
            [f"{ORIGIN}/articles/1"],
        )

    def test_relative_numeric_link_resolves_against_current_directory(self) -> None:
        """Разрешается явная относительная числовая ссылка в текущем разделе."""

        self.assertEqual(
            extract_issue_urls('<a href="2">Выпуск</a>', f"{ORIGIN}/issues/1"),
            [f"{ORIGIN}/issues/2"],
        )

    def test_base_tag_cannot_change_origin(self) -> None:
        """Тег base не должен менять согласованный узел обхода."""

        html = '<base href="https://example.test/"><a href="/issues/1">Выпуск</a>'

        self.assertEqual(
            extract_issue_urls(html, f"{ORIGIN}/journals/1"),
            [f"{ORIGIN}/issues/1"],
        )

    def test_empty_html_returns_no_links(self) -> None:
        """Отсутствующие ссылки не достраиваются по идентификатору страницы."""

        self.assertEqual(extract_issue_urls("", f"{ORIGIN}/journals/1"), [])
        self.assertEqual(extract_article_urls("", f"{ORIGIN}/issues/1"), [])

    def test_invalid_base_urls_raise(self) -> None:
        """Ошибочный базовый URL не должен незаметно превращаться в чужой источник."""

        for url in (
            "", "ftp://journals.ioffe.ru/", "https://example.test/",
            f"{ORIGIN}/journals/1?x", f"{ORIGIN}/../issues/1",
            "https://user@journals.ioffe.ru/", f"{ORIGIN}:443/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                extract_issue_urls("", url)

    def test_explicit_http_origin_is_preserved(self) -> None:
        """Явно выбранный HTTP не должен превращаться в HTTPS при разборе ссылок."""

        html = """
        <a href="/issues/20">Относительная ссылка</a>
        <a href="http://journals.ioffe.ru/issues/10">HTTP</a>
        <a href="//journals.ioffe.ru/issues/5">Без схемы</a>
        <a href="https://journals.ioffe.ru/issues/1">Другой origin</a>
        """

        self.assertEqual(
            extract_issue_urls(html, "http://journals.ioffe.ru/journals/1"),
            [
                "http://journals.ioffe.ru/issues/20",
                "http://journals.ioffe.ru/issues/10",
                "http://journals.ioffe.ru/issues/5",
            ],
        )

    def test_article_links_preserve_selected_scheme_without_switching(self) -> None:
        """Обе схемы допустимы отдельно, но ссылки другой схемы не включаются."""

        html = """
        <a href="/articles/8">Относительная ссылка</a>
        <a href="http://journals.ioffe.ru/articles/4">HTTP</a>
        <a href="https://journals.ioffe.ru/articles/2">HTTPS</a>
        """

        for scheme, last_article in (("http", "4"), ("https", "2")):
            with self.subTest(scheme=scheme):
                origin = f"{scheme}://journals.ioffe.ru"
                self.assertEqual(
                    extract_article_urls(html, f"{origin}/issues/1"),
                    [f"{origin}/articles/8", f"{origin}/articles/{last_article}"],
                )


class IoffeArticleTests(unittest.TestCase):
    """Проверки полей карточки без подмены аннотации полным HTML-текстом."""

    def test_realistic_card_metadata(self) -> None:
        """Вымышленная карточка с реальной структурой сайта разбирается по полям."""

        document = parse_article(ARTICLE_HTML, ARTICLE_URL)

        self.assertEqual(document.title, "Исследование & сравнение кристаллов")
        self.assertEqual(document.authors, ["Иванов И.И.", "Петров П.П."])
        self.assertEqual(document.published, "2021")
        self.assertEqual(document.pdf_url, f"{ORIGIN}/articles/viewPDF/123")
        self.assertEqual(document.source, "journals.ioffe.ru")
        self.assertEqual(document.extra["article_id"], "123")
        self.assertEqual(document.extra["journal_id"], 1)
        self.assertEqual(document.extra["journal"], IOFFE_JOURNALS[1])
        self.assertEqual(document.extra["year"], 2021)
        self.assertEqual(document.extra["doi"], "10.1234/Test.123")
        self.assertEqual(document.extra["content_role"], "metadata_only")

    def test_abstract_excludes_keywords_references_and_navigation(self) -> None:
        """Поле text содержит только аннотацию, а ключевые слова хранятся отдельно."""

        document = parse_article(ARTICLE_HTML, ARTICLE_URL)

        self.assertEqual(
            document.text, "Изучены свойства кристаллов. Проверены <новые> образцы."
        )
        self.assertEqual(document.extra["keywords"], ["кристаллы", "оптика", "релаксация"])

    def test_missing_optional_fields_are_empty(self) -> None:
        """Название достаточно для карточки: остальные поля могут отсутствовать."""

        document = parse_article('<div class="art_title">Название</div>', ARTICLE_URL)

        self.assertEqual(document.text, "")
        self.assertEqual(document.authors, [])
        self.assertIsNone(document.published)
        self.assertIsNone(document.pdf_url)
        self.assertIsNone(document.extra["doi"])
        self.assertIsNone(document.extra["journal"])
        self.assertIsNone(document.extra["journal_id"])
        self.assertEqual(document.extra["keywords"], [])

    def test_page_title_or_description_is_not_article_title(self) -> None:
        """Общее название страницы и SEO-описание не заменяют поле статьи."""

        for html in (
            "", "<title>Журнал</title>", "<h1>Журнал</h1>",
            '<meta name="description" content="Описание сайта">',
        ):
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_article(html, ARTICLE_URL)

    def test_metadata_fallback(self) -> None:
        """Библиографические метаполя допускаются при отсутствии обычных блоков."""

        html = """
        <meta name="citation_title" content="Новая &amp; старая фазы">
        <meta name="citation_author" content="Иванов И.И.">
        <meta name="citation_author" content="Петров П.П.">
        <meta name="citation_author" content="Иванов И.И.">
        <meta name="citation_publication_date" content="1998/12/01">
        <meta name="citation_journal_title" content="Журнал">
        <meta name="citation_abstract" content="Краткая аннотация.">
        <meta name="citation_doi" content="https://doi.org/10.1234/example">
        <meta name="citation_keywords" content="фаза; оптика; фаза">
        <meta name="citation_pdf_url" content="/articles/viewPDF/123">
        """
        document = parse_article(html, ARTICLE_URL)

        self.assertEqual(document.title, "Новая & старая фазы")
        self.assertEqual(document.authors, ["Иванов И.И.", "Петров П.П."])
        self.assertEqual(document.published, "1998")
        self.assertEqual(document.text, "Краткая аннотация.")
        self.assertEqual(document.extra["journal"], "Журнал")
        self.assertEqual(document.extra["doi"], "10.1234/example")
        self.assertEqual(document.extra["keywords"], ["фаза", "оптика"])
        self.assertEqual(document.pdf_url, f"{ORIGIN}/articles/viewPDF/123")

    def test_pdf_is_not_invented_or_borrowed_from_neighbour(self) -> None:
        """Идентификатор карточки сам по себе не доказывает существование PDF."""

        html = """
        <div class="art_title">Название</div>
        <a href="/articles/viewPDF/124">Соседняя статья</a>
        <a href="https://example.test/articles/viewPDF/123">Чужой узел</a>
        <a href="/articles/viewPDF/123?download=true">Параметры</a>
        """

        self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_duplicate_pdf_link_is_one_unambiguous_resource(self) -> None:
        """Повтор одного PDF не считается неоднозначностью."""

        html = """
        <div class="art_title">Название</div>
        <a href="/articles/viewPDF/123">PDF</a>
        <a href="https://journals.ioffe.ru/articles/viewPDF/123/">PDF</a>
        """

        self.assertEqual(
            parse_article(html, ARTICLE_URL).pdf_url,
            f"{ORIGIN}/articles/viewPDF/123",
        )

    def test_current_year_not_taken_from_menu_or_received_date(self) -> None:
        """Год навигации и даты рецензирования не заменяют год выпуска."""

        html = """
        <div class="art_title">Название</div>
        <a href="/journals/2">Журнал</a><a href="/issues/1">Год 2026</a>
        <div class="art_date_received">Поступила в 2025</div>
        """
        document = parse_article(html, ARTICLE_URL)

        self.assertIsNone(document.published)
        self.assertIsNone(document.extra["journal_id"])

    def test_ambiguous_breadcrumb_fields_stay_empty(self) -> None:
        """Противоречащие значения журнала и года не разрешаются произвольно."""

        html = """
        <div class="art_title">Название</div><span class="breadcrumbs">
        <a href="/journals/1">Первый</a><a href="/journals/2">Второй</a>
        <a href="/issues/1">Год 2020</a><a href="/issues/2">Год 2021</a></span>
        """
        document = parse_article(html, ARTICLE_URL)

        self.assertIsNone(document.published)
        self.assertIsNone(document.extra["journal_id"])

    def test_duplicate_abstract_id_does_not_select_arbitrarily(self) -> None:
        """Неоднозначный идентификатор вкладки не должен выбирать случайный текст."""

        html = """
        <div class="art_title">Название</div><div class="art_tabs">
        <a href="#body" title="Абстракт">Текст</a>
        <div id="body">Первый</div><div id="body">Второй</div></div>
        """

        self.assertEqual(parse_article(html, ARTICLE_URL).text, "")

    def test_html_entities_are_decoded_once(self) -> None:
        """Двойная HTML-сущность не декодируется повторно после парсера."""

        html = '<div class="art_title">A &amp;lt; B</div>'

        self.assertEqual(parse_article(html, ARTICLE_URL).title, "A &lt; B")

    def test_title_class_is_not_substring(self) -> None:
        """Похожие имена CSS-классов не должны подменять название статьи."""

        html = '<div class="not_art_title">Подмена</div>'

        with self.assertRaises(ValueError):
            parse_article(html, ARTICLE_URL)

    def test_trailing_slash_in_card_url_is_canonicalized(self) -> None:
        """Завершающий слеш не создаёт отдельную карточку или PDF."""

        self.assertEqual(parse_article(ARTICLE_HTML, ARTICLE_URL + "/").url, ARTICLE_URL)

    def test_non_article_url_is_rejected(self) -> None:
        """Парсер карточки не принимает URL выпуска или файла."""

        for url in (f"{ORIGIN}/issues/1", f"{ORIGIN}/articles/viewPDF/123"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_article(ARTICLE_HTML, url)

    def test_http_article_preserves_its_url_and_pdf_scheme(self) -> None:
        """Явная HTTP-карточка и относительный PDF остаются на том же origin."""

        document = parse_article(ARTICLE_HTML, "http://journals.ioffe.ru/articles/123")

        self.assertEqual(document.url, "http://journals.ioffe.ru/articles/123")
        self.assertEqual(document.pdf_url, "http://journals.ioffe.ru/articles/viewPDF/123")
        self.assertEqual(document.extra["journal_id"], 1)
        self.assertEqual(document.published, "2021")

    def test_pdf_other_scheme_is_rejected_without_downgrade_or_upgrade(self) -> None:
        """Явные PDF другой схемы не переписываются автоматически."""

        for scheme, other_scheme in (("http", "https"), ("https", "http")):
            with self.subTest(scheme=scheme):
                html = (
                    '<div class="art_title">Название</div>'
                    f'<a href="{other_scheme}://journals.ioffe.ru/articles/viewPDF/123">PDF</a>'
                )
                document = parse_article(html, f"{scheme}://journals.ioffe.ru/articles/123")
                self.assertIsNone(document.pdf_url)


if __name__ == "__main__":
    unittest.main()
