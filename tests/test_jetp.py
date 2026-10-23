"""Проверки ЖЭТФ на сокращённой разметке опубликованных русских страниц."""

from __future__ import annotations

import unittest

from dataclasses import replace
from typing import Any

from src.collect.base import HttpResponseSnapshot
from src.collect.jetp import (
    ALLOWED_ORIGINS,
    DEFAULT_ENCODING,
    JOURNAL_NAME,
    SOURCE_ID,
    START_URL,
    build_volume_form,
    classify_url,
    extract_links,
    extract_volumes,
    parse_article,
    validate_volume_page,
    volume_redirect,
)

ORIGIN = "https://www.jetp.ras.ru"
ARTICLE_URL = f"{ORIGIN}/cgi-bin/r/index/r/170/4/p309?a=list"
PDF_URL = f"{ORIGIN}/cgi-bin/dn/r_170_0309.pdf"

ARTICLE_HTML = """
<html><head><title>Журнал, не название статьи</title></head><body>
<a class="authorlink" href="/outside">Чужой автор</a>
<Font Class="issue">ЖЭТФ,
<a href="/cgi-bin/r/index/r/170?a=list">Том 170</a>,
<a href="/cgi-bin/r/index/r/170/4?a=list">Вып. 4</a>,
стр. 309 (Октябрь 2026)<br>
<font class="issuelinked">Английский перевод: October 2027</font></Font>
<P Class="article0">
<Font Class="atitle">Исследование &amp; сравнение атомов <sup>87</sup>Rb</Font><br>
<Font Class="author"><a class="authorlink" href="?a=s&amp;auid=1">Иванов И.И.</a>,
<a class="authorlink" href="?a=s&amp;auid=2">Петров П.П.</a></Font><br>
<Font Class="reddate">Поступила в редакцию: 30 Апреля 2025</Font><br>
<Font Class="pacs">DOI: <a href="http://dx.doi.org/10.1234/Test123">10.1234/Test123</a></Font>
<table><tr><td><a href="/cgi-bin/dn/r_170_0309.pdf">PDF</a></td></tr></table>
<Font Class="abstract">Изучены свойства &lt;новых&gt; атомов.<br>Выделены уровни.</Font>
</P>
<a href="/cgi-bin/dn/r_170_0400.pdf">Соседняя статья</a>
<div>Литература: DOI 10.9999/Unrelated</div>
</body></html>
"""

VOLUME_FORM = """
<Form Action="/cgi-bin/r/index" Method="POST">
<Input type="hidden" Name="a" Value="select">
<Input type="hidden" Name="issues_lang" Value="r">
<Select Name="volume">
<Option Value="170">Vol. 170, 2026</Option>
<Option Value="169">Vol. 169, 2026</Option>
</Select>
<Input Align="absmiddle" Type="image" Src="/img/r/go_vol.png">
</Form>
"""

VOLUME_HTML = """
<Font Class="issue">ЖЭТФ, Том 169, 2026<br>
<font class="issuelinked"><i>(Английский перевод - JETP, Vol. 142, 2026)</i></font>
</Font>
<A Href="/cgi-bin/r/index/r/169/6?a=list">Вып. 6, Июнь 2026</A>
"""


class JetpVolumeTests(unittest.TestCase):
    """Проверки опубликованной POST-формы и строго ограниченного перехода к тому."""

    def setUp(self) -> None:
        """Подготовить наблюдавшийся исходный ответ формы без сетевых запросов."""

        self.redirect = HttpResponseSnapshot(
            requested_url=START_URL,
            final_url=START_URL,
            status_code=302,
            headers=(("location", "/cgi-bin/r/index/r/169?a=list"),),
            retrieved_at="2026-10-15T12:00:00+03:00",
            body=b"",
        )

    def test_form_bytes_follow_published_fields(self) -> None:
        """Выбранный номер отправляется только после проверки списка вариантов."""

        self.assertEqual(build_volume_form(VOLUME_FORM, START_URL, 169),
                         b"a=select&issues_lang=r&volume=169")
        self.assertEqual(build_volume_form(VOLUME_FORM, START_URL, 170),
                         b"a=select&issues_lang=r&volume=170")

    def test_extract_volumes_preserves_gaps_and_ignores_english_form(self) -> None:
        """Очередь использует только опубликованные русские номера, не заполняя пробелы."""

        russian_form = VOLUME_FORM.replace('Value="169"', 'Value="28"')
        english_form = VOLUME_FORM.replace('Value="r"', 'Value="e"')

        self.assertEqual(extract_volumes(english_form + russian_form, START_URL), [170, 28])

    def test_extract_volumes_ignores_disabled_option(self) -> None:
        """Отключённый вариант не становится заданием автоматической очереди."""

        html = VOLUME_FORM.replace('Value="169"', 'Value="169" disabled')

        self.assertEqual(extract_volumes(html, START_URL), [170])

    def test_extract_volumes_rejects_malformed_forms(self) -> None:
        """Неоднозначные, отключённые и изменённые формы не создают частичный план."""

        cases = [
            "", VOLUME_FORM + VOLUME_FORM,
            VOLUME_FORM.replace('Value="r"', 'Value="e"'),
            VOLUME_FORM.replace('Value="170"', 'Value="169"'),
            VOLUME_FORM.replace('Value="170"', 'Value="0170"'),
            VOLUME_FORM.replace('Value="170"', 'Value="bad"'),
            VOLUME_FORM.replace('Value="169"', 'Value="169" disabled').replace(
                'Value="170"', 'Value="170" disabled',
            ),
            VOLUME_FORM.replace('<Select Name="volume">', '<Select Name="volume" disabled>'),
            VOLUME_FORM.replace('Method="POST"', 'Method="GET"'),
            '<base href="https://example.test/">' + VOLUME_FORM,
        ]

        for html in cases:
            with self.subTest(html=html), self.assertRaises(ValueError):
                extract_volumes(html, START_URL)

        with self.assertRaises(ValueError):
            extract_volumes(VOLUME_FORM, ARTICLE_URL)

    def test_current_issue_and_english_forms_are_not_candidates(self) -> None:
        """Соседние формы выбора выпуска и английского тома не создают неоднозначность."""

        issue_form = """
        <Form Action="/cgi-bin/r/index" Method="POST">
        <Input type="hidden" Name="volume" Value="170">
        <Input type="hidden" Name="issues_lang" Value="r">
        <Input type="hidden" Name="a" Value="select">
        <Select Name="issue"><Option Value="4">No 4</Option></Select>
        <Input Align="absmiddle" Type="image" Src="/img/r/go_issue.png">
        </Form>
        """
        english_form = VOLUME_FORM.replace('Value="r"', 'Value="e"')

        self.assertEqual(build_volume_form(issue_form + VOLUME_FORM + english_form, START_URL, 169),
                         b"a=select&issues_lang=r&volume=169")

    def test_explicit_form_encoding_and_absolute_action_are_allowed(self) -> None:
        """Стандартная кодировка и тот же абсолютный адрес сохраняют смысл формы."""

        html = VOLUME_FORM.replace('Action="/cgi-bin/r/index"', f'Action="{START_URL}"')
        html = html.replace('Method="POST"', 'Method="post" enctype="application/x-www-form-urlencoded"')

        self.assertEqual(build_volume_form(html, START_URL, 169), b"a=select&issues_lang=r&volume=169")

    def test_invalid_arguments_fail_before_form_selection(self) -> None:
        """Неверный тип тома, исходный URL и нестроковый HTML отклоняются."""

        invalid_volumes: list[Any] = [True, False, 0, -1, 169.0, "169", None]

        for volume in invalid_volumes:
            with self.subTest(volume=volume), self.assertRaises(ValueError):
                build_volume_form(VOLUME_FORM, START_URL, volume)

        for page_url in (ARTICLE_URL, START_URL + "?a=list", START_URL.replace("https", "http")):
            with self.subTest(page_url=page_url), self.assertRaises(ValueError):
                build_volume_form(VOLUME_FORM, page_url, 169)

        invalid_html: Any = None

        with self.assertRaises(ValueError):
            build_volume_form(invalid_html, START_URL, 169)

    def test_ambiguous_missing_and_unavailable_volume_fail(self) -> None:
        """Нельзя выбирать отсутствующий, отключённый или неоднозначно опубликованный том."""

        cases = [
            "", VOLUME_FORM + VOLUME_FORM,
            VOLUME_FORM.replace('Value="r"', 'Value="e"'),
            VOLUME_FORM.replace('Value="169"', 'Value="168"'),
            VOLUME_FORM.replace('Value="169"', 'Value="169" disabled'),
            VOLUME_FORM.replace('Value="170"', 'Value="169"'),
            VOLUME_FORM.replace('Value="170"', 'Value="0170"'),
            VOLUME_FORM.replace('<Select Name="volume">', '<Select Name="volume" disabled>'),
            VOLUME_FORM.replace('<Select Name="volume">', '<Select Name="volume" multiple>'),
        ]

        for html in cases:
            with self.subTest(html=html), self.assertRaises(ValueError):
                build_volume_form(html, START_URL, 169)

    def test_changed_submission_and_foreign_controls_fail(self) -> None:
        """Иной адрес, метод, формат, скрытые поля и переопределения кнопки не принимаются."""

        cases = [
            VOLUME_FORM.replace('Action="/cgi-bin/r/index"', 'Action="https://example.test/"'),
            VOLUME_FORM.replace('Action="/cgi-bin/r/index"', 'Action="/cgi-bin/r/index?a=select"'),
            VOLUME_FORM.replace('Method="POST"', 'Method="GET"'),
            VOLUME_FORM.replace('Method="POST"', 'enctype="multipart/form-data" Method="POST"'),
            VOLUME_FORM.replace('Value="select"', 'Value="delete"'),
            VOLUME_FORM.replace('type="hidden" Name="a"', 'type="text" Name="a"'),
            VOLUME_FORM.replace('Name="a"', 'Name="a" disabled'),
            VOLUME_FORM.replace('Name="a"', 'Name="a" Name="a"'),
            VOLUME_FORM.replace('Method="POST"', 'Method="GET" Method="POST"'),
            VOLUME_FORM + VOLUME_FORM.replace('Value="r"', 'Value="r" Value="e"'),
            VOLUME_FORM + VOLUME_FORM.replace('Name="volume"', 'Name="volume" Name="issue"'),
            VOLUME_FORM.replace('</Form>', '<input type="hidden" name="extra" value="1"></Form>'),
            VOLUME_FORM.replace('</Form>', '<input type="hidden" name="a" value="select"></Form>'),
            VOLUME_FORM.replace('</Form>', '<textarea name="extra">x</textarea></Form>'),
            VOLUME_FORM.replace('Type="image"', 'Type="image" Name="submit"'),
            VOLUME_FORM.replace('Type="image"', 'Type="image" formaction="/other"'),
            VOLUME_FORM.replace('<Select Name="volume">', '<Select Name="volume" form="other">'),
            '<fieldset disabled>' + VOLUME_FORM + '</fieldset>',
            '<base href="https://example.test/">' + VOLUME_FORM,
            '<form action="/other">' + VOLUME_FORM + '</form>',
        ]

        for html in cases:
            with self.subTest(html=html), self.assertRaises(ValueError):
                build_volume_form(html, START_URL, 169)

    def test_redirect_allows_only_exact_observed_destination(self) -> None:
        """Абсолютный и относительный Location обязаны вести ровно к выбранному тому."""

        target = f"{START_URL}/r/169?a=list"

        for location in (target, "/cgi-bin/r/index/r/169?a=list", "index/r/169?a=list"):
            with self.subTest(location=location):
                snapshot = replace(self.redirect, headers=(("Location", location),))
                self.assertEqual(volume_redirect(snapshot, 169), target)

    def test_other_statuses_and_request_origins_fail(self) -> None:
        """HTTP 200 и прочие перенаправления не заменяют наблюдавшийся исходный 302."""

        for status in (200, 301, 303, 307, 308, 403, True):
            with self.subTest(status=status), self.assertRaises(ValueError):
                volume_redirect(replace(self.redirect, status_code=status), 169)

        for field in ("requested_url", "final_url"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                volume_redirect(replace(self.redirect, **{field: ARTICLE_URL}), 169)

    def test_ambiguous_and_unsafe_redirects_fail(self) -> None:
        """Повторы Location, чужие тома, параметры и неоднозначное кодирование запрещены."""

        paths = [
            "", "https://example.test/cgi-bin/r/index/r/169?a=list",
            "http://www.jetp.ras.ru/cgi-bin/r/index/r/169?a=list",
            "/cgi-bin/r/index/r/170?a=list", "/cgi-bin/r/index/e/169?a=list",
            "/cgi-bin/r/index/r/169", "/cgi-bin/r/index/r/169?a=list&extra=1",
            "/cgi-bin/r/index/r/169?a=list#", "/cgi-bin/r/index/r/0169?a=list",
            "/cgi-bin/r/index/r/%31%36%39?a=list", "/cgi-bin/r/index/r/./169?a=list",
            "/cgi-bin/r/index/r/169?a=list\n", "\x7f/cgi-bin/r/index/r/169?a=list",
            " /cgi-bin/r/index/r/169?a=list", "/cgi-bin/r/index/r/169?a=list\\",
        ]

        for location in paths:
            with self.subTest(location=location), self.assertRaises(ValueError):
                volume_redirect(replace(self.redirect, headers=(("location", location),)), 169)

        for headers in ((), self.redirect.headers * 2, self.redirect.headers + (("Location", "x"),)):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                volume_redirect(replace(self.redirect, headers=headers), 169)

    def test_volume_heading_ignores_english_translation(self) -> None:
        """Русский номер проверяется в заголовке, номер английского перевода не подставляется."""

        validate_volume_page(VOLUME_HTML, f"{START_URL}/r/169?a=list", 169)

        cases = [
            VOLUME_HTML.replace("Том 169", "Том 170"),
            VOLUME_HTML.replace("Том 169", "Том 142"),
            VOLUME_HTML.replace('Class="issue"', 'Class="issuelinked"'),
            VOLUME_HTML + VOLUME_HTML, "<h1>ЖЭТФ, Том 169, 2026</h1>",
            VOLUME_HTML.replace("Том 169, 2026", "Том 169, Вып. 6, стр. 1 (2026)"),
        ]

        for html in cases:
            with self.subTest(html=html), self.assertRaises(ValueError):
                validate_volume_page(html, f"{START_URL}/r/169?a=list", 169)

        with self.assertRaises(ValueError):
            validate_volume_page(VOLUME_HTML, f"{START_URL}/r/170?a=list", 169)


class JetpLinkTests(unittest.TestCase):
    """Проверки узла, языка, маршрутов и опубликованных ссылок."""

    def test_source_constants(self) -> None:
        """Используется проверенный HTTPS-узел и заявленная сайтом кодировка."""

        self.assertEqual(SOURCE_ID, "jetp")
        self.assertEqual(ALLOWED_ORIGINS, (ORIGIN,))
        self.assertEqual(DEFAULT_ENCODING, "koi8-r")
        self.assertEqual(START_URL, f"{ORIGIN}/cgi-bin/r/index")

    def test_public_routes(self) -> None:
        """Распознаются архив, том, оба опубликованных пути выпуска, статья и PDF."""

        cases = {
            "/cgi-bin/r/index": "archive",
            "/cgi-bin/r/index/r/170?a=list": "archive",
            "/cgi-bin/r/index/170/4?a=list": "issue",
            "/cgi-bin/r/index/r/170/4?a=list": "issue",
            "/cgi-bin/r/index/r/170/4/p309?a=list": "article",
            "/cgi-bin/dn/r_170_0309.pdf": "pdf",
        }

        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(classify_url(f"{ORIGIN}{path}"), expected)

    def test_foreign_origins_are_rejected(self) -> None:
        """HTTP, посторонние узлы, учётные данные и нестандартные порты отклоняются."""

        urls = [
            ARTICLE_URL.replace("https:", "http:"),
            ARTICLE_URL.replace("www.jetp.ras.ru", "jetp.ras.ru"),
            ARTICLE_URL.replace("www.jetp.ras.ru", "www.jetp.ras.ru.example.test"),
            ARTICLE_URL.replace("www.jetp.ras.ru", "user@www.jetp.ras.ru"),
            ARTICLE_URL.replace("www.jetp.ras.ru", "www.jetp.ras.ru:443"),
            "//www.jetp.ras.ru/cgi-bin/r/index",
        ]

        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(classify_url(url))

    def test_english_service_and_ambiguous_paths_are_rejected(self) -> None:
        """Не разрешаются английский архив, служебные формы и параметры поиска."""

        paths = [
            "/cgi-bin/e/index", "/cgi-bin/r/index/e/83?a=list",
            "/cgi-bin/dn/e_143_0309.pdf", "/cgi-bin/r/upload",
            "/admin", "/content", "/cgi-bin/r/stats",
            "/cgi-bin/r/index?a=s", "/cgi-bin/r/index?a=au",
            "/cgi-bin/r/index/r/170?a=list&action=delete",
            "/cgi-bin/r/index/r/170?a=list&a=list",
            "/cgi-bin/r/index/forthcoming?a=list", "/cgi-bin/r/index?",
            "/cgi-bin/r/index#", "/cgi-bin/r/index%3fa=list",
            "/cgi-bin/r/../r/index", "/cgi-bin/r/./index",
            "/cgi-bin/r/index\\bad", "/cgi-bin/r/index\n",
            "/cgi-bin/dn/r_170_0309.pdf?download=1",
            "/cgi-bin/r/index/r/0170?a=list",
        ]

        for path in paths:
            with self.subTest(path=path):
                self.assertIsNone(classify_url(f"{ORIGIN}{path}"))

    def test_extracts_only_explicit_links_in_order(self) -> None:
        """Повторы удаляются, HTML-сущности раскрываются, ссылки сохраняют порядок."""

        html = """
        <a href="/cgi-bin/r/index/170/4?a=list">Выпуск</a>
        <a href="/cgi-bin/r/index/170/4?a=list">Повтор</a>
        <a href="r/170/4/p309?a=list">Относительный путь не от корня</a>
        <a href="/cgi-bin/r/index/r/170/4/p309?a=list">Статья</a>
        <a href="/cgi-bin/r/index?a=s&amp;auid=1">Поиск</a>
        <a href="/cgi-bin/dn/r_170_0309.pdf">PDF</a>
        """

        self.assertEqual(
            extract_links(html, START_URL),
            [f"{ORIGIN}/cgi-bin/r/index/170/4?a=list", ARTICLE_URL, PDF_URL],
        )

    def test_forms_do_not_generate_guessed_urls(self) -> None:
        """Номера томов в POST-форме не превращаются в несуществующие href."""

        html = """
        <form action="/cgi-bin/r/index" method="POST">
        <input name="a" value="select"><input name="issues_lang" value="r">
        <select name="volume"><option value="169">Том 169</option></select>
        </form>
        """

        self.assertEqual(extract_links(html, START_URL), [])

    def test_hidden_links_and_base_tag_are_ignored(self) -> None:
        """Скрытые элементы и тег base не задают маршрут обхода."""

        html = """
        <base href="https://example.test/">
        <noscript><a href="/cgi-bin/dn/r_170_0309.pdf">Скрыто</a></noscript>
        <a href="/cgi-bin/r/index/r/170?a=list">Том</a>
        """

        self.assertEqual(
            extract_links(html, START_URL),
            [f"{ORIGIN}/cgi-bin/r/index/r/170?a=list"],
        )

    def test_invalid_page_url_fails(self) -> None:
        """Источник HTML обязан быть известной страницей русского архива."""

        with self.assertRaises(ValueError):
            extract_links("", "https://example.test/")


class JetpArticleTests(unittest.TestCase):
    """Проверки контекстного разбора метаданных и русской PDF-ссылки."""

    def test_parses_current_article_only(self) -> None:
        """Поля берутся из карточки, а не навигации, перевода или литературы."""

        document = parse_article(ARTICLE_HTML, ARTICLE_URL)

        self.assertEqual(document.title, "Исследование & сравнение атомов 87Rb")
        self.assertEqual(document.authors, ["Иванов И.И.", "Петров П.П."])
        self.assertEqual(document.text, "Изучены свойства <новых> атомов. Выделены уровни.")
        self.assertEqual(document.published, "2026")
        self.assertEqual(document.pdf_url, PDF_URL)
        self.assertEqual(document.language, "ru")
        self.assertEqual(document.extra["doi"], "10.1234/Test123")
        self.assertEqual(document.extra["journal"], JOURNAL_NAME)
        self.assertEqual(document.extra["year"], 2026)
        self.assertEqual(document.extra["content_role"], "metadata_only")
        self.assertEqual(document.extra["volume"], 170)
        self.assertEqual(document.extra["issue"], 4)
        self.assertEqual(document.extra["first_page"], 309)

    def test_missing_pdf_is_not_guessed(self) -> None:
        """Отсутствующий href нельзя восстанавливать из номера страницы."""

        html = ARTICLE_HTML.replace("/cgi-bin/dn/r_170_0309.pdf", "/not-a-pdf")

        self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_english_or_wrong_page_pdf_is_not_selected(self) -> None:
        """PDF английского перевода или соседней статьи не принадлежит карточке."""

        for replacement in ("e_143_0309.pdf", "r_170_0310.pdf", "r_169_0309.pdf"):
            with self.subTest(replacement=replacement):
                html = ARTICLE_HTML.replace("r_170_0309.pdf", replacement)
                self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_duplicate_pdf_link_is_accepted(self) -> None:
        """Повтор одной и той же явной PDF-ссылки не создаёт неоднозначность."""

        html = ARTICLE_HTML.replace(
            "</P>", '<a href="/cgi-bin/dn/r_170_0309.pdf">Ещё PDF</a></P>'
        )

        self.assertEqual(parse_article(html, ARTICLE_URL).pdf_url, PDF_URL)

    def test_doi_in_abstract_is_not_current_article_metadata(self) -> None:
        """DOI внешней работы не присваивается статье без подписанного поля DOI."""

        html = ARTICLE_HTML.replace('Class="pacs"', 'Class="other"')
        html = html.replace("Изучены свойства", "Сравнение с DOI 10.1111/Other. Изучены свойства")

        self.assertIsNone(parse_article(html, ARTICLE_URL).extra["doi"])

    def test_ambiguous_doi_is_not_selected(self) -> None:
        """Два разных DOI в подписанных полях оставляют значение неизвестным."""

        html = ARTICLE_HTML.replace(
            "</P>", '<font class="pacs">DOI: 10.1234/Other</font></P>'
        )

        self.assertIsNone(parse_article(html, ARTICLE_URL).extra["doi"])

    def test_publication_year_is_not_submission_or_translation_year(self) -> None:
        """При отсутствии года в русской библиографии другие даты не используются."""

        html = ARTICLE_HTML.replace("Октябрь 2026", "Октябрь")
        document = parse_article(html, ARTICLE_URL)

        self.assertIsNone(document.published)
        self.assertIsNone(document.extra["year"])

    def test_missing_abstract_remains_empty(self) -> None:
        """Навигация и название не подставляются вместо отсутствующей аннотации."""

        html = ARTICLE_HTML.replace('Class="abstract"', 'Class="other"')

        self.assertEqual(parse_article(html, ARTICLE_URL).text, "")

    def test_multiple_article_cards_are_rejected(self) -> None:
        """Список статей не выдаётся за единственную карточку."""

        with self.assertRaises(ValueError):
            parse_article(ARTICLE_HTML + ARTICLE_HTML, ARTICLE_URL)

    def test_missing_title_is_rejected(self) -> None:
        """Заголовок сайта не заменяет отсутствующее название статьи."""

        with self.assertRaises(ValueError):
            parse_article(ARTICLE_HTML.replace('Class="atitle"', 'Class="other"'), ARTICLE_URL)

    def test_english_body_does_not_trigger_pdf_download(self) -> None:
        """Русская оболочка страницы не доказывает язык самой публикации."""

        html = """
        <font class="issue">ЖЭТФ, Том 170, Вып. 4, стр. 309 (Октябрь 2026)</font>
        <p class="article0"><font class="atitle">Quantum atoms</font>
        <font class="abstract">Optical properties of atoms.</font>
        <a href="/cgi-bin/dn/r_170_0309.pdf">PDF</a></p>
        """
        document = parse_article(html, ARTICLE_URL)

        self.assertEqual(document.language, "en")
        self.assertIsNone(document.pdf_url)

    def test_wrong_page_kind_and_non_string_html_are_rejected(self) -> None:
        """Неподходящий вход завершается понятной ошибкой до разбора."""

        with self.assertRaises(ValueError):
            parse_article(ARTICLE_HTML, START_URL)

        invalid_html: Any = None

        with self.assertRaises(ValueError):
            parse_article(invalid_html, ARTICLE_URL)


if __name__ == "__main__":
    unittest.main()
