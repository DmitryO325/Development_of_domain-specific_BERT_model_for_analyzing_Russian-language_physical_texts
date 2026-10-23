"""Проверки адаптера «Писем в ЖЭТФ» на примерах подтверждённой разметки."""

from __future__ import annotations

import unittest

from src.collect.jetp_letters import (
    ALLOWED_ORIGINS,
    DEFAULT_ENCODING,
    START_URL,
    classify_url,
    extract_links,
    parse_article,
)

ORIGIN = "http://jetpletters.ru"
ARTICLE_URL = f"{ORIGIN}/ps/2554/article_37329.shtml"
PDF_URL = f"{ORIGIN}/ps/2554/article_37329.pdf"
ARTICLE_HTML = r"""
<html><head><title>JETP Letters: issues online</title>
<meta name="keywords" content="physics, journal"></head><body>
<a href="/ps/index-v-124_ru.shtml">Том 124</a>
<th class="pshead1">VOLUME 124 (2026) | ISSUE 7 | PAGE 514</th>
<div itemscope itemtype="http://schema.org/Article">
<div class="arttitle" itemprop="headline">Исследование свойств CH<sub>3</sub>OH &amp; воды</div>
<div itemprop="author" itemscope itemtype="http://schema.org/Person">
<span itemprop="name">А. И. Иванов<sup>+</sup>, Ю. С. Петрова<sup>+*</sup></span>
<span itemprop="affiliation"><sup>+</sup>Институт, Россия</span></div>
<div class="article-info"><b>DOI</b>:
<a href="https://dx.doi.org/10%2E1234%2Fexample">10.1234/example</a></div>
<div class="abstract"><b>Abstract</b><br><span itemprop="articleBody">
Изучены свойства&nbsp;воды. <img alt="\mu = 1" src="/img/latex/1.png">
<script>лишний текст</script><br>Получены результаты.</span></div>
<span itemprop="encoding"><a href="/ps/2554/article_37329.ps">PS</a></span>
<span itemprop="encoding"><a href="/ps/2554/article_37329.pdf">Download PDF file</a></span>
<div>Литература: DOI: 10.9999/not-current</div>
</div></body></html>
"""


class JetpLettersLinksTests(unittest.TestCase):
    """Проверки известных путей и исключения чужих и английских разделов."""

    def test_verified_constants_and_paths(self) -> None:
        """Константы соответствуют проверенному открытому русскому архиву."""

        self.assertEqual(ALLOWED_ORIGINS, (ORIGIN,))
        self.assertEqual(DEFAULT_ENCODING, "koi8-r")
        self.assertEqual(classify_url(START_URL), "archive")

        paths = {
            "/ps/": "archive",
            "/ps/archive.shtml": "archive",
            "/ps/index-v-tru_1.shtml": "archive",
            "/ps/index-v-124_ru.shtml": "archive",
            "/ps/2554/index.shtml": "issue",
            "/ps/2554/article_37329.shtml": "article",
            "/ps/2554/article_37329.pdf": "pdf",
        }

        for path, expected in paths.items():
            with self.subTest(path=path):
                self.assertEqual(classify_url(ORIGIN + path), expected)

    def test_english_and_service_paths_are_rejected(self) -> None:
        """Английский архив, поиск, PostScript и внешние узлы не обходятся."""

        urls = [
            f"{ORIGIN}/ps/index-v-ten_1.shtml",
            f"{ORIGIN}/ps/index-v-62_en.shtml",
            f"{ORIGIN}/ps/authors-v-124_ru.shtml",
            f"{ORIGIN}/cgi-bin/articles/search.cgi",
            f"{ORIGIN}/ps/2554/article_37329.ps",
            "https://jetpletters.ru/ps/archive.shtml",
            "http://www.jetpletters.ru/ps/archive.shtml",
            "http://jetpletters.ru.example.test/ps/archive.shtml",
            "http://user@jetpletters.ru/ps/archive.shtml",
            "http://jetpletters.ru:8080/ps/archive.shtml",
        ]

        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(classify_url(url))

    def test_ambiguous_addresses_are_rejected(self) -> None:
        """Неоднозначные адреса не должны нормализоваться в разрешённые пути."""

        suffixes = ["?", "?download=1", "#", "#x", " ", "\n", "\x00", "\\foo"]

        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                self.assertIsNone(classify_url(ARTICLE_URL + suffix))

        for path in ("/ps/../ps/archive.shtml", "/ps/%61rchive.shtml", "/ps/0/index.shtml"):
            with self.subTest(path=path):
                self.assertIsNone(classify_url(ORIGIN + path))

    def test_links_are_explicit_and_ordered(self) -> None:
        """Извлекаются уникальные ссылки в исходном порядке, без угадывания PDF."""

        html = """
        <a href="/ps/index-v-ten_1.shtml">Английский архив</a>
        <a href="/ps/2554/index.shtml">Выпуск</a>
        <a href="//jetpletters.ru/ps/2554/index.shtml">Повтор</a>
        <a href="article_37329.shtml">Карточка</a>
        <a href="article_37329.shtml&#63;download=1">Неоднозначный адрес</a>
        <a href="../../ps/2554/article_37330.shtml">Обход пути</a>
        <a href="https://example.test/ps/2554/article_37331.shtml">Чужой узел</a>
        """

        self.assertEqual(
            extract_links(html, f"{ORIGIN}/ps/2554/index.shtml"),
            [f"{ORIGIN}/ps/2554/index.shtml", ARTICLE_URL],
        )

    def test_invalid_page_url_and_empty_html_fail(self) -> None:
        """Ошибочные входные данные дают понятный ValueError."""

        with self.assertRaises(ValueError):
            extract_links("<html></html>", "https://example.test/")

        with self.assertRaises(ValueError):
            extract_links("", START_URL)


class JetpLettersArticleTests(unittest.TestCase):
    """Проверки извлечения метаданных отдельно от полного текста статьи."""

    def test_russian_article_metadata(self) -> None:
        """Название, авторы, аннотация и год извлекаются из своих блоков."""

        document = parse_article(ARTICLE_HTML, ARTICLE_URL)

        self.assertEqual(document.title, "Исследование свойств CH3OH & воды")
        self.assertEqual(document.authors, ["А. И. Иванов", "Ю. С. Петрова"])
        self.assertEqual(document.language, "ru")
        self.assertEqual(document.pdf_url, PDF_URL)
        self.assertEqual(document.text, r"Изучены свойства воды. \mu = 1 Получены результаты.")
        self.assertEqual(document.extra["doi"], "10.1234/example")
        self.assertEqual(document.extra["year"], 2026)
        self.assertEqual(document.extra["volume"], "124")
        self.assertEqual(document.extra["issue"], "7")
        self.assertEqual(document.extra["content_role"], "metadata_only")
        self.assertEqual(document.extra["text_source"], "abstract")
        self.assertNotIn("keywords", document.extra)

    def test_pdf_is_not_invented_from_article_url(self) -> None:
        """Отсутствующая ссылка не заменяется предположением о соседнем файле."""

        html = ARTICLE_HTML.replace("/ps/2554/article_37329.pdf", "/ps/2554/article_37330.pdf")

        self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_external_pdf_is_not_accepted(self) -> None:
        """Чужой PDF не должен скачиваться даже из разметки encoding."""

        html = ARTICLE_HTML.replace(
            "/ps/2554/article_37329.pdf", "https://example.test/article_37329.pdf"
        )

        self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_english_article_has_no_downloadable_pdf(self) -> None:
        """Английская статья внутри русского выпуска сохраняется только как пропуск."""

        html = """
        <div class="arttitle">A physical experiment</div>
        <div class="abstract"><span itemprop="articleBody">We study the effect.</span></div>
        <a href="/ps/2554/article_37329.pdf">Download PDF</a>
        """
        document = parse_article(html, ARTICLE_URL)

        self.assertEqual(document.language, "en")
        self.assertIsNone(document.pdf_url)
        self.assertTrue(document.extra["skipped_non_russian"])

    def test_mixed_metadata_language_is_not_assumed_russian(self) -> None:
        """Смешанные языковые признаки требуют отдельной проверки, а не загрузки."""

        html = """
        <div class="arttitle">Исследование свойств вещества</div>
        <div class="abstract"><span itemprop="articleBody">We study the effect.</span></div>
        <a href="/ps/2554/article_37329.pdf">Download PDF</a>
        """
        document = parse_article(html, ARTICLE_URL)

        self.assertEqual(document.language, "und")
        self.assertIsNone(document.pdf_url)

    def test_absent_optional_fields_remain_absent(self) -> None:
        """Неизвестные DOI, авторы и год не заполняются выдуманными значениями."""

        document = parse_article('<div class="arttitle">Название статьи</div>', ARTICLE_URL)

        self.assertEqual(document.authors, [])
        self.assertEqual(document.text, "")
        self.assertIsNone(document.pdf_url)
        self.assertNotIn("doi", document.extra)
        self.assertNotIn("year", document.extra)

    def test_doi_in_references_is_not_article_doi(self) -> None:
        """Без подписанного блока DOI ссылка в литературе игнорируется."""

        html = ARTICLE_HTML.replace('class="article-info"', 'class="references"')

        self.assertNotIn("doi", parse_article(html, ARTICLE_URL).extra)

    def test_abstract_label_and_missing_body_attribute(self) -> None:
        """Название блока аннотации удаляется и без необязательной микроразметки."""

        html = ARTICLE_HTML.replace(' itemprop="articleBody"', "")

        self.assertTrue(parse_article(html, ARTICLE_URL).text.startswith("Изучены свойства"))

    def test_title_is_not_taken_from_site_header(self) -> None:
        """Страница ошибки или оглавления не должна превращаться в статью."""

        with self.assertRaisesRegex(ValueError, "название"):
            parse_article("<title>JETP Letters: issues online</title>", ARTICLE_URL)

        with self.assertRaises(ValueError):
            parse_article(ARTICLE_HTML, START_URL)
