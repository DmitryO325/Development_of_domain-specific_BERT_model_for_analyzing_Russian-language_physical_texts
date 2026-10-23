"""Проверки чистого адаптера УФН без сетевых запросов."""

from __future__ import annotations

import unittest

from src.collect.ufn_journal import (
    ALLOWED_ORIGINS,
    DEFAULT_ENCODING,
    START_URL,
    classify_url,
    decode_body,
    extract_links,
    parse_article,
)

ORIGIN = "https://ufn.ru"
ARTICLE_URL = f"{ORIGIN}/ru/articles/2026/8/a/"
PDF_URL = f"{ORIGIN}/ufn2026/ufn2026_8/Russian/r268a.pdf"
ARTICLE_HTML = """
<html><head>
<title>Первичные чёрные дыры</title>
<meta name="keywords" content="Служебные слова сайта" />
<meta name="citation_title" content="Первичные чёрные дыры" />
<meta name="citation_author" content="Вячеслав Иванович Докучаев" />
<meta name="citation_author" content="Юрий Николаевич Ерошенко" />
<meta name="citation_date" content="2026/08/01" />
<meta name="citation_volume" content="196" />
<meta name="citation_issue" content="8" />
<meta name="citation_pdf_url" content="https://ufn.ru/ufn2026/ufn2026_8/Russian/r268a.pdf" />
</head><body>
<a href="/ru/rubrics/reviews-of-topical-problems/">Обзоры актуальных проблем</a>
<span itemscope itemtype="http://schema.org/ScholarlyArticle">
<h1 itemprop="headline">Первичные чёрные дыры</h1>
<div class="p"><span itemprop="author"><a rel="author">В.И.&nbsp;Докучаев</a></span><sup>а</sup>,
<span itemprop="author"><a rel="author">Ю.Н.&nbsp;Ерошенко</a></span><sup>а</sup>
<em itemprop="sourceOrganization">Институт ядерных исследований</em></div>
<p itemprop="articleBody" class="mathjax">Рассмотрены свойства&nbsp;чёрных дыр.
<script>Не аннотация</script>Показаны результаты.</p>
<div class="fulltext"><span class="fulltext-desc">Текст</span>
<a class="file-link track-fulltext-download pdf"
href="/ufn2026/ufn2026_8/Russian/r268a.pdf">pdf</a></div>
<div class="p">
<span class="gray">Ключевые&nbsp;слова:</span> чёрные дыры, космология<br />
<span class="gray">PACS:</span>
<a href="/ru/pacs/04.20.-q/">04.20.&minus;q</a>,
<a href="/ru/pacs/04.70.Bw/">04.70.Bw</a>
(<noindex><a href="/ru/pacs/04.20.-q,04.70.Bw/">все</a></noindex>)<br />
<span class="gray">DOI:</span>
<noindex><a href="https://doi.org/10.3367/UFNr.2026.07.040164">
10.3367/UFNr.2026.07.040164</a></noindex><br />
<span class="gray">URL:</span> https://ufn.ru/ru/articles/2026/8/a/<br />
</div>
<p class="c"><span class="gray">English citation:</span>
<a href="https://doi.org/10.3367/UFNe.2026.07.040164">10.3367/UFNe.2026.07.040164</a></p>
<p><a href="/ru/authors/659/">Я.Б.&nbsp;Зельдович</a> — автор УФН</p>
<div class="references-menu">DOI: 10.9999/reference</div>
</span></body></html>
"""
SUBSCRIPTION_FULLTEXT = """
<form name="fulltext-form">
<table><tr><td><div class="fulltext">
<span class="gray">Текст:&nbsp;</span><a class="file-link pdf disabled-link"
style="opacity: 0.5;">pdf</a>
<noindex><span>(Полный текст предоставляется по подписке)</span></noindex>
</div></td><td><div class="fulltext"><noindex>
English fulltext is available at DOI: <a href="https://doi.org/10.9999/english">DOI</a>
</noindex></div></td></tr></table>
</form>
"""
SUBSCRIPTION_HTML = ARTICLE_HTML.replace(
    '<div class="fulltext"><span class="fulltext-desc">Текст</span>\n'
    '<a class="file-link track-fulltext-download pdf"\n'
    'href="/ufn2026/ufn2026_8/Russian/r268a.pdf">pdf</a></div>',
    SUBSCRIPTION_FULLTEXT,
)


class UfnLinksTests(unittest.TestCase):
    """Проверки явных ссылок и границ русского архива УФН."""

    def test_verified_constants_and_paths(self) -> None:
        """Распознаются выпуски, годы, диапазоны архива и русские PDF."""

        self.assertEqual(ALLOWED_ORIGINS, (ORIGIN,))
        self.assertEqual(DEFAULT_ENCODING, "utf-8")
        self.assertEqual(classify_url(START_URL), "archive")

        paths = {
            "/ru/articles/2026/": "archive",
            "/ru/articles/1918-1924/": "archive",
            "/ru/articles/2007-2016/": "archive",
            "/ru/articles/2026/10/": "issue",
            "/ru/articles/2026/8/a/": "article",
            "/ufn2026/ufn2026_8/Russian/r268a.pdf": "pdf",
            "/ufn17/ufn17_12/Russian/r1712a.pdf": "pdf",
            "/ufn2018/ufn2018_3/Russian/r20183a.pdf": "pdf",
        }

        for path, expected in paths.items():
            with self.subTest(path=path):
                self.assertEqual(classify_url(ORIGIN + path), expected)

    def test_english_service_and_external_paths_are_rejected(self) -> None:
        """Адаптер не обходит английские, закрытые и посторонние адреса."""

        urls = [
            f"{ORIGIN}/en/articles/2026/8/a/",
            f"{ORIGIN}/ufn2026/ufn2026_8/English/e268a.pdf",
            f"{ORIGIN}/ufn2026/ufn2026_10/Russian/obl2610.pdf",
            f"{ORIGIN}/ru/articles/accepted/",
            f"{ORIGIN}/ru/articles/rss.xml",
            f"{ORIGIN}/ru/subscription.html",
            f"{ORIGIN}/ru/signin/",
            f"{ORIGIN}/ru/search/",
            f"{ORIGIN}/ru/authors/12345/",
            f"{ORIGIN}/ru/articles/2026/0/a/",
            f"{ORIGIN}/ru/articles/2026/13/a/",
            f"{ORIGIN}/ufn2026/ufn2025_8/Russian/r268a.pdf",
            f"{ORIGIN}/ufn17/ufn16_12/Russian/r1712a.pdf",
            f"{ORIGIN}/ufn17/ufn2017_12/Russian/r1712a.pdf",
            f"{ORIGIN}/ufn16/ufn16_12/Russian/r1612h_p.pdf",
            f"{ORIGIN}/ufn17/ufn17_12/English/e1712a.pdf",
            "http://ufn.ru/ru/articles/",
            "https://www.ufn.ru/ru/articles/",
            "https://ufn.ru.example.test/ru/articles/",
            "https://user@ufn.ru/ru/articles/",
            "https://ufn.ru:444/ru/articles/",
            "https://[broken/ru/articles/",
        ]

        for url in urls:
            with self.subTest(url=url):
                self.assertIsNone(classify_url(url))

    def test_ambiguous_addresses_are_rejected(self) -> None:
        """Обход пути, кодированные сегменты и служебные параметры запрещены."""

        for suffix in ("?", "?download=1", "#", "#ref", " ", "\n", "\x00", "\x7f", "\\foo"):
            with self.subTest(suffix=suffix):
                self.assertIsNone(classify_url(ARTICLE_URL + suffix))

        for path in ("/ru/../ru/articles/", "/ru/%61rticles/"):
            with self.subTest(path=path):
                self.assertIsNone(classify_url(ORIGIN + path))

    def test_links_are_explicit_unique_and_ordered(self) -> None:
        """Извлекаются только опубликованные ссылки, без угадывания PDF."""

        html = """
        <a href="/en/articles/2026/8/a/">English</a>
        <a href="/ru/articles/2026/8/">Выпуск</a>
        <a href="//ufn.ru/ru/articles/2026/8/">Повтор</a>
        <a href="a/">Статья</a>
        <a href="/ru/articles/2026/8/a/&#63;download=1">Параметр</a>
        <a href="../8/b/">Неоднозначный путь</a>
        <a href="https://example.test/ru/articles/2026/8/c/">Чужой узел</a>
        <template><a href="/ru/articles/2026/8/d/">Шаблон</a></template>
        <!-- <a href="/ru/articles/2026/8/e/">Комментарий</a> -->
        """

        self.assertEqual(
            extract_links(html, f"{ORIGIN}/ru/articles/2026/8/"),
            [f"{ORIGIN}/ru/articles/2026/8/", ARTICLE_URL],
        )

    def test_invalid_input_fails_explicitly(self) -> None:
        """Ошибочный адрес и пустая HTML-страница дают понятный ValueError."""

        with self.assertRaises(ValueError):
            extract_links("<html></html>", "https://example.test/")

        with self.assertRaises(ValueError):
            extract_links("", START_URL)


class UfnArticleTests(unittest.TestCase):
    """Проверки точного отделения метаданных статьи от служебного текста."""

    def test_russian_metadata_and_explicit_pdf(self) -> None:
        """Поля берутся из карточки, а не SEO, английской цитаты или аффилиаций."""

        document = parse_article(ARTICLE_HTML, ARTICLE_URL)

        self.assertEqual(document.source, "ufn")
        self.assertEqual(document.title, "Первичные чёрные дыры")
        self.assertEqual(document.text, "Рассмотрены свойства чёрных дыр. Показаны результаты.")
        self.assertEqual(document.authors, [
            "Вячеслав Иванович Докучаев", "Юрий Николаевич Ерошенко",
        ])
        self.assertEqual(document.section, "Обзоры актуальных проблем")
        self.assertEqual(document.pdf_url, PDF_URL)
        self.assertEqual(document.language, "ru")
        self.assertEqual(document.extra["year"], 2026)
        self.assertEqual(document.extra["volume"], "196")
        self.assertEqual(document.extra["issue"], "8")
        self.assertEqual(document.extra["doi"], "10.3367/UFNr.2026.07.040164")
        self.assertEqual(document.extra["keywords"], ["чёрные дыры", "космология"])
        self.assertEqual(document.extra["pacs_codes_raw"], ["04.20.−q", "04.70.Bw"])
        self.assertEqual(document.extra["content_role"], "metadata_only")
        self.assertEqual(document.extra["text_source"], "abstract")

    def test_article_can_have_no_abstract_or_authors(self) -> None:
        """Редакционная заметка без авторов и аннотации не получает выдуманных полей."""

        html = """
        <span itemscope itemtype="http://schema.org/ScholarlyArticle">
        <h1 itemprop="headline">Памяти выдающегося физика</h1>
        </span>
        """
        document = parse_article(html, ARTICLE_URL)

        self.assertEqual(document.authors, [])
        self.assertEqual(document.text, "")
        self.assertIsNone(document.section)
        self.assertIsNone(document.pdf_url)
        self.assertNotIn("doi", document.extra)
        self.assertNotIn("pacs_codes_raw", document.extra)
        self.assertNotIn("keywords", document.extra)
        self.assertNotIn("volume", document.extra)

    def test_authors_fall_back_to_itemprop_without_affiliations(self) -> None:
        """Микроразметка сохраняет авторов без надстрочных номеров и организаций."""

        html = ARTICLE_HTML.replace('name="citation_author"', 'name="other"')

        self.assertEqual(parse_article(html, ARTICLE_URL).authors, ["В.И. Докучаев", "Ю.Н. Ерошенко"])

    def test_doi_in_english_citation_is_not_current_doi(self) -> None:
        """При отсутствии русского подписанного DOI английский не подставляется."""

        html = ARTICLE_HTML.replace('<span class="gray">DOI:</span>', '<span>DOI:</span>')

        self.assertNotIn("doi", parse_article(html, ARTICLE_URL).extra)

    def test_absent_pdf_is_not_invented(self) -> None:
        """Знание схемы имени файла не заменяет опубликованную PDF-ссылку."""

        html = ARTICLE_HTML.replace('name="citation_pdf_url"', 'name="other"').replace(
            'href="/ufn2026/ufn2026_8/Russian/r268a.pdf"', 'data-href="not-a-link"'
        )

        self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_pdf_must_match_current_year_issue_and_russian_path(self) -> None:
        """Чужой год, выпуск, язык и неподдержанное имя PDF не принимаются."""

        replacements = [
            ("r268a.pdf", "r258a.pdf"),
            ("ufn2026_8/Russian/r268a.pdf", "ufn2026_9/Russian/r269a.pdf"),
            ("ufn2026/ufn2026_8/Russian/r268a.pdf", "ufn2025/ufn2025_8/Russian/r258a.pdf"),
            ("Russian/r268a.pdf", "English/e268a.pdf"),
            ("Russian/r268a.pdf", "Russian/article.pdf"),
        ]

        for old, new in replacements:
            with self.subTest(replacement=new), self.assertLogs("src.collect.ufn_journal", "WARNING"):
                self.assertIsNone(parse_article(ARTICLE_HTML.replace(old, new), ARTICLE_URL).pdf_url)

    def test_june_cards_preserve_their_explicit_shifted_pdf_letters(self) -> None:
        """Все десять подтверждённых ссылок июня сохраняются без подстановки буквы карточки."""

        for article_letter, pdf_letter in zip("abcdefghij", "zabcdefghi", strict=True):
            with self.subTest(article=article_letter, pdf=pdf_letter):
                article_url = f"{ORIGIN}/ru/articles/2026/6/{article_letter}/"
                pdf_url = f"{ORIGIN}/ufn2026/ufn2026_6/Russian/r266{pdf_letter}.pdf"
                html = ARTICLE_HTML.replace("ufn2026_8", "ufn2026_6").replace(
                    "r268a.pdf", f"r266{pdf_letter}.pdf"
                ).replace(ARTICLE_URL, article_url).replace('content="8"', 'content="6"')

                self.assertEqual(parse_article(html, article_url).pdf_url, pdf_url)

    def test_archival_pdf_year_formats_match_the_current_article(self) -> None:
        """Опубликованные короткие каталоги и полные годы в именах сохраняются точно."""

        examples = [
            ("2014", "12", "14", "r1412b"),
            ("2015", "5", "15", "r155i"),
            ("2017", "12", "17", "r1712a"),
            ("2018", "3", "2018", "r20183a"),
            ("2018", "6", "2018", "r20186a"),
            ("2018", "7", "2018", "r20187a"),
        ]

        for year, issue, directory_year, filename in examples:
            article_url = f"{ORIGIN}/ru/articles/{year}/{issue}/a/"
            pdf_path = f"/ufn{directory_year}/ufn{directory_year}_{issue}/Russian/{filename}.pdf"
            html = ARTICLE_HTML.replace("/ufn2026/ufn2026_8/Russian/r268a.pdf", pdf_path)

            with self.subTest(pdf=pdf_path):
                self.assertEqual(parse_article(html, article_url).pdf_url, ORIGIN + pdf_path)

                self.assertIsNone(parse_article(html, article_url, legacy_pdf_links=True).pdf_url)

    def test_archival_pdf_requires_matching_year_issue_and_filename(self) -> None:
        """Расширение форматов не разрешает чужой год, выпуск или неподтверждённый файл."""

        article_url = f"{ORIGIN}/ru/articles/2017/12/a/"
        paths = [
            "/ufn16/ufn16_12/Russian/r1612a.pdf",
            "/ufn17/ufn17_11/Russian/r1711a.pdf",
            "/ufn17/ufn17_12/Russian/r1612a.pdf",
            "/ufn17/ufn17_12/Russian/r201612a.pdf",
            "/ufn17/ufn17_12/Russian/r1711a.pdf",
            "/ufn17/ufn17_12/Russian/r201711a.pdf",
            "/ufn1917/ufn1917_12/Russian/r1712a.pdf",
            "/ufn17/ufn17_12/Russian/r1712a_p.pdf",
            "/ufn17/ufn17_12/Russian/obl1712.pdf",
            "/ufn17/ufn17_12/Russian/r1712a.pdf?download=1",
            "/ufn17/ufn17_12/Russian/r1712a.pdf#fulltext",
        ]

        for pdf_path in paths:
            html = ARTICLE_HTML.replace("/ufn2026/ufn2026_8/Russian/r268a.pdf", pdf_path)

            with self.subTest(pdf=pdf_path), self.assertLogs("src.collect.ufn_journal", "WARNING"):
                self.assertIsNone(parse_article(html, article_url).pdf_url)

    def test_archival_pdf_conflicts_are_not_resolved_by_layout_preference(self) -> None:
        """Короткий и полный каталог не считаются одним файлом без подтверждения сервера."""

        article_url = f"{ORIGIN}/ru/articles/2017/12/a/"
        html = ARTICLE_HTML.replace(
            PDF_URL, f"{ORIGIN}/ufn17/ufn17_12/Russian/r1712a.pdf",
        ).replace(
            'href="/ufn2026/ufn2026_8/Russian/r268a.pdf"',
            'href="/ufn2017/ufn2017_12/Russian/r1712a.pdf"',
        )

        with self.assertRaisesRegex(ValueError, "разные русские PDF"):
            parse_article(html, article_url)

    def test_archival_presentation_is_not_selected_as_article_pdf(self) -> None:
        """Отдельная презентация не заменяет полный текст и не создаёт ложный конфликт."""

        article_url = f"{ORIGIN}/ru/articles/2016/12/h/"
        pdf_path = "/ufn16/ufn16_12/Russian/r1612h.pdf"
        html = ARTICLE_HTML.replace("/ufn2026/ufn2026_8/Russian/r268a.pdf", pdf_path).replace(
            '</span></body>', '<div class="fulltext"><span>Презентация</span>'
            '<a class="pdf" href="/ufn16/ufn16_12/Russian/r1612h_p.pdf">pdf</a></div></span></body>',
        )

        self.assertEqual(parse_article(html, article_url).pdf_url, ORIGIN + pdf_path)

    def test_legacy_pdf_policy_preserves_supported_modern_links(self) -> None:
        """Совместимый режим не скрывает ссылку, которую прежний парсер уже поддерживал."""

        self.assertEqual(parse_article(ARTICLE_HTML, ARTICLE_URL, legacy_pdf_links=True).pdf_url, PDF_URL)

    def test_archival_subscription_is_not_bypassed_by_new_pdf_layout(self) -> None:
        """Короткий каталог не меняет запрет скачивания из явно подписной карточки."""

        article_url = f"{ORIGIN}/ru/articles/2017/12/a/"
        html = SUBSCRIPTION_HTML.replace(
            "/ufn2026/ufn2026_8/Russian/r268a.pdf", "/ufn17/ufn17_12/Russian/r1712a.pdf",
        )

        with self.assertLogs("src.collect.ufn_journal", "WARNING"):
            document = parse_article(html, article_url)

        self.assertIsNone(document.pdf_url)
        self.assertEqual(document.extra["pdf_access_status"], "subscription_required")

    def test_explicit_metadata_pdf_does_not_require_matching_card_letter(self) -> None:
        """Единственная citation_pdf_url достаточна, даже если буква файла отличается."""

        html = ARTICLE_HTML.replace("r268a.pdf", "r268z.pdf").replace('class="fulltext"', 'class="other"')

        self.assertEqual(parse_article(html, ARTICLE_URL).pdf_url, PDF_URL.replace("r268a.pdf", "r268z.pdf"))

    def test_scoped_button_pdf_does_not_require_matching_card_letter(self) -> None:
        """Явная кнопка полного текста достаточна без дублирующего citation_pdf_url."""

        html = ARTICLE_HTML.replace("r268a.pdf", "r268z.pdf").replace('name="citation_pdf_url"', 'name="other"')

        self.assertEqual(parse_article(html, ARTICLE_URL).pdf_url, PDF_URL.replace("r268a.pdf", "r268z.pdf"))

    def test_conflicting_metadata_and_button_pdfs_are_rejected(self) -> None:
        """Разные допустимые PDF одного выпуска не выбираются по совпадению буквы."""

        html = ARTICLE_HTML.replace(
            'href="/ufn2026/ufn2026_8/Russian/r268a.pdf"',
            'href="/ufn2026/ufn2026_8/Russian/r268z.pdf"',
        )

        with self.assertRaisesRegex(ValueError, "разные русские PDF"):
            parse_article(html, ARTICLE_URL)

    def test_conflicting_metadata_pdf_values_are_rejected(self) -> None:
        """Две разные citation_pdf_url не сводятся молча к первому значению."""

        second_pdf = PDF_URL.replace("r268a.pdf", "r268z.pdf")
        html = ARTICLE_HTML.replace("</head>", f'<meta name="citation_pdf_url" content="{second_pdf}"></head>')

        with self.assertRaisesRegex(ValueError, "разные русские PDF"):
            parse_article(html, ARTICLE_URL)

    def test_outside_article_download_button_is_not_a_candidate(self) -> None:
        """Кнопка соседней статьи вне области текущей не создаёт ложный конфликт PDF."""

        html = ARTICLE_HTML.replace(
            "</body>", '<div class="fulltext"><a class="pdf" '
            'href="/ufn2026/ufn2026_8/Russian/r268z.pdf">pdf</a></div></body>',
        )

        self.assertEqual(parse_article(html, ARTICLE_URL).pdf_url, PDF_URL)

    def test_only_scoped_download_link_is_accepted(self) -> None:
        """PDF в литературе не считается полным текстом текущей статьи."""

        html = ARTICLE_HTML.replace('name="citation_pdf_url"', 'name="other"').replace(
            'class="fulltext"', 'class="references"'
        )

        self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_external_pdf_is_not_accepted(self) -> None:
        """Даже явно опубликованный внешний адрес не расширяет область сборщика."""

        html = ARTICLE_HTML.replace(PDF_URL, "https://example.test/paper.pdf").replace(
            'href="/ufn2026/ufn2026_8/Russian/r268a.pdf"',
            'href="https://example.test/paper.pdf"',
        )

        with self.assertLogs("src.collect.ufn_journal", "WARNING"):
            self.assertIsNone(parse_article(html, ARTICLE_URL).pdf_url)

    def test_article_metadata_pdf_is_an_explicit_link(self) -> None:
        """citation_pdf_url допускается и при отсутствии дублирующей кнопки скачивания."""

        html = ARTICLE_HTML.replace('class="fulltext"', 'class="other"')

        self.assertEqual(parse_article(html, ARTICLE_URL).pdf_url, PDF_URL)

    def test_subscription_notice_and_disabled_button_override_metadata_pdf(self) -> None:
        """Явная подписка сохраняет карточку и причину, но не запрашивает PDF из метаполя."""

        with self.assertLogs("src.collect.ufn_journal", "WARNING") as captured:
            document = parse_article(SUBSCRIPTION_HTML, ARTICLE_URL)

        self.assertIsNone(document.pdf_url)
        self.assertEqual(document.title, "Первичные чёрные дыры")
        self.assertEqual(document.extra["content_role"], "metadata_only")
        self.assertEqual(document.extra["pdf_access_status"], "subscription_required")
        self.assertEqual(document.extra["pdf_access_reason"], "Полный текст предоставляется по подписке")
        self.assertTrue(any("citation_pdf_url не запрашивается" in message for message in captured.output))

    def test_subscription_requires_both_notice_and_disabled_button_in_current_form(self) -> None:
        """Одна фраза, одна кнопка или неподтверждённый контейнер не отменяют PDF из meta."""

        variants = [
            SUBSCRIPTION_HTML.replace("Полный текст предоставляется по подписке", "Текст статьи"),
            SUBSCRIPTION_HTML.replace(" disabled-link", ""),
            SUBSCRIPTION_HTML.replace('name="fulltext-form"', 'name="other"'),
            SUBSCRIPTION_HTML.replace('style="opacity: 0.5;"', 'href="" style="opacity: 0.5;"'),
            SUBSCRIPTION_HTML.replace(
                '(Полный текст предоставляется по подписке)',
                '(Полный текст)\n</span></noindex></div><div class="fulltext">'
                '<noindex><span>(Полный текст предоставляется по подписке)',
            ),
        ]

        for html in variants:
            with self.subTest(html=html):
                document = parse_article(html, ARTICLE_URL)
                self.assertEqual(document.pdf_url, PDF_URL)
                self.assertNotIn("pdf_access_status", document.extra)

    def test_hidden_subscription_forms_do_not_override_metadata_pdf(self) -> None:
        """Скрытая форма и служебные шаблоны не свидетельствуют об ограничении текущего PDF."""

        wrappers = [
            ('<div hidden>', '</div>'),
            ('<div aria-hidden="true">', '</div>'),
            ('<div style="display: none !important;">', '</div>'),
            ('<div style="color: black; visibility: hidden">', '</div>'),
            ('<script>', '</script>'),
            ('<style>', '</style>'),
            ('<noscript>', '</noscript>'),
            ('<template>', '</template>'),
        ]

        for opening, closing in wrappers:
            with self.subTest(wrapper=opening):
                html = SUBSCRIPTION_HTML.replace(SUBSCRIPTION_FULLTEXT, opening + SUBSCRIPTION_FULLTEXT + closing)
                document = parse_article(html, ARTICLE_URL)
                self.assertEqual(document.pdf_url, PDF_URL)
                self.assertNotIn("pdf_access_status", document.extra)

    def test_hidden_notice_or_button_is_not_a_subscription_pair(self) -> None:
        """Видимость одного признака не компенсирует скрытость другого."""

        variants = [
            SUBSCRIPTION_HTML.replace('<noindex><span>(', '<noindex><span hidden>('),
            SUBSCRIPTION_HTML.replace('<a class="file-link pdf disabled-link"', '<a hidden class="file-link pdf disabled-link"'),
            SUBSCRIPTION_HTML.replace('<noindex><span>(', '<noindex><span style="display:none">('),
        ]

        for html in variants:
            with self.subTest(html=html):
                document = parse_article(html, ARTICLE_URL)
                self.assertEqual(document.pdf_url, PDF_URL)
                self.assertNotIn("pdf_access_status", document.extra)

    def test_unrelated_subscription_notes_do_not_override_current_pdf(self) -> None:
        """Подписка в аннотации, рекомендациях или вне карточки не относится к текущему PDF."""

        variants = [ARTICLE_HTML.replace('</body>', SUBSCRIPTION_FULLTEXT + '</body>')]

        for container in (
            '<div itemprop="articleBody">', '<div class="references">',
            '<div class="related-articles">', '<div class="recommendations">',
            '<div class="old-material">',
        ):
            variants.append(ARTICLE_HTML.replace(
                '</span></body>', container + SUBSCRIPTION_FULLTEXT + '</div></span></body>',
            ))

        for html in variants:
            with self.subTest(html=html):
                document = parse_article(html, ARTICLE_URL)
                self.assertEqual(document.pdf_url, PDF_URL)
                self.assertNotIn("pdf_access_status", document.extra)

    def test_multiple_article_scopes_do_not_assign_subscription_to_current_article(self) -> None:
        """Другая научная карточка делает область неоднозначной, а не помечает текущую подписной."""

        html = ARTICLE_HTML.replace(
            '</body>', '<span itemtype="http://schema.org/ScholarlyArticle">'
            + SUBSCRIPTION_FULLTEXT + '</span></body>',
        )

        with self.assertRaisesRegex(ValueError, "единственная область научной статьи"):
            parse_article(html, ARTICLE_URL)

    def test_active_pdf_link_is_not_overridden_by_stale_subscription_notice(self) -> None:
        """Действующая ссылка в той же форме не отменяется устаревшей подсказкой о подписке."""

        html = SUBSCRIPTION_HTML.replace(
            '</form>', f'<div class="fulltext"><a class="pdf" href="{PDF_URL}">pdf</a></div></form>',
        )
        document = parse_article(html, ARTICLE_URL)

        self.assertEqual(document.pdf_url, PDF_URL)
        self.assertNotIn("pdf_access_status", document.extra)

    def test_english_metadata_is_not_downloaded(self) -> None:
        """Русский маршрут не превращает английскую карточку в русский текст."""

        html = ARTICLE_HTML.replace("Первичные чёрные дыры", "Primordial black holes").replace(
            "Рассмотрены свойства&nbsp;чёрных дыр.", "We study black holes."
        ).replace("Показаны результаты.", "Results are presented.")
        document = parse_article(html, ARTICLE_URL)

        self.assertEqual(document.language, "en")
        self.assertIsNone(document.pdf_url)
        self.assertTrue(document.extra["skipped_non_russian"])

    def test_error_or_issue_page_is_not_an_article(self) -> None:
        """Заголовок сайта или оглавления не подменяет название научной публикации."""

        with self.assertRaisesRegex(ValueError, "область научной статьи"):
            parse_article("<h1>Страница не найдена</h1>", ARTICLE_URL)

        with self.assertRaises(ValueError):
            parse_article(ARTICLE_HTML, START_URL)


class UfnDecodingTests(unittest.TestCase):
    """Проверки узкого исключения для повреждённого SEO-поля живого сайта."""

    def test_correct_utf8_and_explicit_legacy_encoding_remain_unchanged(self) -> None:
        """Для исправного HTML байты строго декодируются в указанной кодировке."""

        self.assertEqual(decode_body(ARTICLE_HTML.encode(), "utf-8"), ARTICLE_HTML)
        self.assertEqual(decode_body("<p>Русский текст</p>".encode("cp1251"), "windows-1251"),
                         "<p>Русский текст</p>")

    def test_only_broken_keywords_meta_is_removed(self) -> None:
        """Испорченные SEO-keywords исключаются с предупреждением, исходные байты неизменны."""

        body = b'<html><head><meta name="keywords" content="word\xd1, word" /></head>'
        body += "<body>Русский текст</body></html>".encode()
        original = bytes(body)

        with self.assertLogs("src.collect.ufn_journal", "WARNING") as captured:
            html = decode_body(body, "utf-8")

        self.assertEqual(html, "<html><head></head><body>Русский текст</body></html>")
        self.assertEqual(body, original)
        self.assertIn("исходные байты сохранены", captured.output[0])

    def test_multiple_broken_keywords_are_counted(self) -> None:
        """Несколько повреждённых SEO-полей отмечаются одним сообщением с количеством."""

        body = b"<head><META CONTENT='x\xd1' NAME='keywords'/>"
        body += b'<meta name="keywords" content="y\xd1" /></head><body>ok</body>'

        with self.assertLogs("src.collect.ufn_journal", "WARNING") as captured:
            self.assertEqual(decode_body(body, "utf8"), "<head></head><body>ok</body>")

        self.assertIn("разборе: 2", captured.output[0])

    def test_damage_outside_seo_content_is_not_hidden(self) -> None:
        """Повреждения названия, аннотации, ссылки и обычных атрибутов останавливают разбор."""

        bodies = [
            b"<head><title>bad\xd1</title></head>",
            b'<head><meta name="description" content="bad\xd1" /></head>',
            b'<head><meta name="citation_title" content="bad\xd1" /></head>',
            b'<head><meta name="keyw\xd1ords" content="bad" /></head>',
            b'<head><meta name="keywords" content="ok" bad="\xd1" /></head>',
            b'<head><meta name="keywords" name="other" content="bad\xd1" /></head>',
            b'<body><meta name="keywords" content="bad\xd1" /></body>',
            b'<head></head><body><p>bad\xd1</p></body>',
            b'<head></head><body><a href="/ru/articles/2026/8/\xd1">link</a></body>',
            b'<head><!-- <meta name="keywords" content="bad\xd1" /> --></head>',
            b'<head><script><meta name="keywords" content="bad\xd1" /></script></head>',
        ]

        for body in bodies:
            with self.subTest(body=body), self.assertRaises(UnicodeDecodeError):
                decode_body(body, "utf-8")

    def test_damage_after_ignored_keywords_is_still_fatal(self) -> None:
        """Удаление повреждённого SEO-поля не разрешает ошибки в остальном документе."""

        body = b'<head><meta name="keywords" content="bad\xd1" /></head><body>bad\xd1</body>'

        with self.assertRaises(UnicodeDecodeError):
            decode_body(body, "utf-8")
