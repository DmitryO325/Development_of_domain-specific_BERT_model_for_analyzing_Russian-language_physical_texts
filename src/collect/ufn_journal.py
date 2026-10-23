"""Разбор публичного русского архива УФН без сети и угадывания адресов PDF."""

from __future__ import annotations

import codecs
import logging
import re

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .base import Document

SOURCE_ID = "ufn"
START_URL = "https://ufn.ru/ru/articles/"
ALLOWED_ORIGINS = ("https://ufn.ru",)
DEFAULT_ENCODING = "utf-8"
JOURNAL_NAME = "Успехи физических наук"
LOGGER = logging.getLogger(__name__)

YEAR = r"[12][0-9]{3}"
ISSUE = r"(?:[1-9]|1[0-2])"
ARCHIVE_PATH = re.compile(rf"/ru/articles/(?:{YEAR}(?:-{YEAR})?/)?")
ISSUE_PATH = re.compile(rf"/ru/articles/({YEAR})/({ISSUE})/")
ARTICLE_PATH = re.compile(rf"/ru/articles/({YEAR})/({ISSUE})/([a-z])/")
LEGACY_PDF_PATH = re.compile(
    rf"/ufn(?P<year>{YEAR})/ufn(?P=year)_(?P<issue>{ISSUE})/Russian/"
    r"(?P<filename>r[0-9]{3,4}[a-z])\.pdf"
)
PDF_PATH = re.compile(
    rf"/ufn(?P<year>{YEAR}|[0-9]{{2}})/ufn(?P=year)_(?P<issue>{ISSUE})/Russian/"
    r"(?P<filename>r[0-9]{3,6}[a-z])\.pdf"
)

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
BLOCK_TAGS = {"br", "div", "h1", "h2", "h3", "li", "p", "section", "td", "tr"}
HIDDEN_TAGS = {"script", "style", "noscript", "template"}
META_ATTRIBUTES = re.compile(rb"\s+([A-Za-z][A-Za-z0-9_-]*)\s*=\s*([\"'])(.*?)\2", re.DOTALL)
SUBSCRIPTION_NOTICE = "Полный текст предоставляется по подписке"


def _is_hidden(tag: str, attributes: dict[str, str]) -> bool:
    """Исключить служебные узлы и явно скрытые элементы вместе с их содержимым."""

    style = re.sub(r"\s+", "", attributes.get("style", "").lower())

    return (
        tag in HIDDEN_TAGS or "hidden" in attributes or
        attributes.get("aria-hidden", "").lower() == "true" or
        re.search(
            r"(?:^|;)(?:display:none|visibility:(?:hidden|collapse))(?:!important)?(?:;|$)",
            style,
        ) is not None
    )


def _broken_keywords_span(body: bytes, error_position: int) -> tuple[int, int] | None:
    """Найти повреждение только внутри значения content служебных keywords."""

    head = re.search(rb"<head(?:\s[^<>]*)?>(.*?)</head\s*>", body, re.IGNORECASE | re.DOTALL)

    if head is None or not head.start(1) <= error_position < head.end(1):
        return

    for tag in re.finditer(rb"<meta\b[^<>]*>", head.group(1), re.IGNORECASE):
        start = head.start(1) + tag.start()
        end = head.start(1) + tag.end()

        if not start <= error_position < end:
            continue

        prefix = _PageParser()
        prefix.feed(body[:start].decode("utf-8", errors="strict"))

        if (
            prefix.rawdata or not any(element.tag == "head" for element in prefix.stack) or
            any(element.tag in HIDDEN_TAGS for element in prefix.stack)
        ):
            return

        attributes: dict[bytes, bytes] = {}
        content_span = None
        position = len(b"<meta")

        for attribute in META_ATTRIBUTES.finditer(tag.group()):
            name = attribute.group(1).lower()

            if attribute.start() != position or name in attributes:
                return

            attributes[name] = attribute.group(3)
            position = attribute.end()

            if name == b"content":
                content_span = (start + attribute.start(3), start + attribute.end(3))

        if (
            tag.group()[position:].strip() not in {b">", b"/>"} or
            set(attributes) != {b"name", b"content"} or
            attributes[b"name"].lower() != b"keywords" or
            content_span is None or
            not content_span[0] <= error_position < content_span[1]
        ):
            return

        return start, end


def decode_body(body: bytes, encoding: str) -> str:
    """Строго декодировать HTML, исключая только повреждённые SEO-keywords."""

    if not isinstance(body, bytes) or not isinstance(encoding, str) or not encoding:
        raise ValueError("Ожидаются байты HTML УФН и непустое имя кодировки")

    removed = 0

    while True:
        try:
            html = body.decode(encoding, errors="strict")

        except UnicodeDecodeError as exception:
            if codecs.lookup(encoding).name != "utf-8":
                raise

            span = _broken_keywords_span(body, exception.start)

            if span is None:
                raise

            # Подтверждённый дефект сайта затрагивает только SEO, не метаданные статьи.
            body = body[:span[0]] + body[span[1]:]
            removed += 1

        else:
            if removed:
                LOGGER.warning(
                    "УФН: исключено повреждённых SEO-meta keywords при разборе: %s; "
                    "исходные байты сохранены без изменений", removed,
                )

            return html


@dataclass
class _Element:
    """HTML-элемент с атрибутами и текстом дочерних узлов."""

    tag: str
    attributes: dict[str, str]
    children: list[_Element | str] = field(default_factory=list)

    def text(self, *, exclude_superscripts: bool = False) -> str:
        """Прочитать видимый текст, исключая сценарии и надстрочные аффилиации."""

        if _is_hidden(self.tag, self.attributes) or (exclude_superscripts and self.tag == "sup"):
            return ""

        if self.tag == "img":
            return self.attributes.get("alt", "")

        text = "".join(
            child.text(exclude_superscripts=exclude_superscripts)
            if isinstance(child, _Element) else child
            for child in self.children
        )

        return f" {text} " if self.tag in BLOCK_TAGS else text


class _PageParser(HTMLParser):
    """Прочитать HTML без исполнения сценариев и загрузки внешних ресурсов."""

    def __init__(self) -> None:
        """Создать дерево и список видимых элементов в порядке страницы."""

        super().__init__(convert_charrefs=True)
        self.root = _Element("root", {})
        self.elements: list[_Element] = []
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Добавить элемент, исключив вложенные в служебные блоки ссылки."""

        element = _Element(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(element)

        if not _is_hidden(tag, element.attributes) and not any(
            _is_hidden(parent.tag, parent.attributes) for parent in self.stack
        ):
            self.elements.append(element)

        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Разобрать самозакрывающийся элемент без незавершённого контейнера."""

        self.handle_starttag(tag, attrs)

        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Закрыть ближайший открытый контейнер с указанным именем."""

        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """Сохранить текст внутри текущего элемента."""

        self.stack[-1].children.append(data)


def _parse_html(html: str) -> _PageParser:
    """Проверить непустой строковый вход и разобрать страницу УФН."""

    if not isinstance(html, str) or not html.strip():
        raise ValueError("HTML страницы УФН должен быть непустой строкой")

    parser = _PageParser()
    parser.feed(html)
    parser.close()

    return parser


def _safe_reference(reference: str) -> bool:
    """Отклонить неоднозначное кодирование и обход пути до вызова urljoin."""

    if (
        not isinstance(reference, str) or not reference or
        any(character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in reference) or
        any(character in reference for character in "?#\\%")
    ):
        return False

    try:
        parsed = urlsplit(reference)

    except ValueError:
        return False

    return not any(part in {".", ".."} for part in parsed.path.split("/"))


def classify_url(url: str) -> str | None:
    """Распознать известные пути русского архива, исключив служебные страницы."""

    if not _safe_reference(url):
        return

    parsed = urlsplit(url)

    if f"{parsed.scheme}://{parsed.netloc}" not in ALLOWED_ORIGINS:
        return

    for kind, pattern in (
        ("archive", ARCHIVE_PATH), ("issue", ISSUE_PATH),
        ("article", ARTICLE_PATH), ("pdf", PDF_PATH),
    ):
        if pattern.fullmatch(parsed.path):
            return kind


def _resolve_link(reference: str, page_url: str) -> str | None:
    """Разрешить только опубликованную ссылку в подтверждённой области сайта."""

    if not _safe_reference(reference):
        return

    url = urljoin(page_url, reference)

    return url if classify_url(url) is not None else None


def extract_links(html: str, page_url: str) -> list[str]:
    """Собрать уникальные явные ссылки в исходном порядке без генерации адресов."""

    if classify_url(page_url) not in {"archive", "issue", "article"}:
        raise ValueError("Ожидается адрес русской HTML-страницы УФН")

    parser = _parse_html(html)
    urls: list[str] = []

    for element in parser.elements:
        if element.tag != "a":
            continue

        url = _resolve_link(element.attributes.get("href", ""), page_url)

        if url is not None and url not in urls:
            urls.append(url)

    return urls


def _compact(text: str) -> str:
    """Убрать отступы разметки, сохранив символы исходного текста."""

    return " ".join(text.split())


def _has_class(element: _Element, class_name: str) -> bool:
    """Проверить целое имя CSS-класса элемента."""

    return class_name in element.attributes.get("class", "").split()


def _descendants(element: _Element) -> list[_Element]:
    """Получить дочерние элементы без содержимого скрытых контейнеров."""

    result: list[_Element] = []
    pending = list(reversed(element.children))

    while pending:
        child = pending.pop()

        if isinstance(child, _Element) and not _is_hidden(child.tag, child.attributes):
            result.append(child)
            pending.extend(reversed(child.children))

    return result


def _metadata(parser: _PageParser, name: str) -> list[str]:
    """Прочитать непустые значения указанного метаполя без повторов."""

    values = [
        _compact(element.attributes.get("content", ""))
        for element in parser.elements
        if element.tag == "meta" and element.attributes.get("name", "").lower() == name
    ]

    return list(dict.fromkeys(value for value in values if value))


def _article_scope(parser: _PageParser) -> _Element:
    """Найти единственную область научной публикации, а не меню или литературу."""

    articles = [
        element for element in parser.elements
        if element.attributes.get("itemtype") in {
            "http://schema.org/ScholarlyArticle", "https://schema.org/ScholarlyArticle",
        }
    ]

    if len(articles) != 1:
        raise ValueError("В карточке УФН не найдена единственная область научной статьи")

    return articles[0]


def _itemprop_text(elements: list[_Element], name: str) -> str:
    """Прочитать первое непустое поле микроразметки текущей статьи."""

    for element in elements:
        if name in element.attributes.get("itemprop", "").split():
            text = _compact(element.text())

            if text:
                return text

    return ""


def _labeled_field(elements: list[_Element], label: str) -> list[_Element | str]:
    """Прочитать содержимое подписанного поля до переноса или следующей подписи."""

    for element in elements:
        if not _has_class(element, "p"):
            continue

        after_label = False
        nodes: list[_Element | str] = []

        for child in element.children:
            if after_label:
                if isinstance(child, _Element) and (
                    child.tag == "br" or _has_class(child, "gray")
                ):
                    break

                nodes.append(child)
                continue

            after_label = (
                isinstance(child, _Element) and _has_class(child, "gray") and
                _compact(child.text()) == label
            )

        if after_label:
            return nodes

    return []


def _article_doi(elements: list[_Element]) -> str | None:
    """Извлечь DOI из подписанного русского блока, исключив English citation."""

    text = _compact("".join(
        child.text() if isinstance(child, _Element) else child
        for child in _labeled_field(elements, "DOI:")
    ))

    return text if re.fullmatch(r"10\.[0-9]{4,9}/\S+", text) else None


def _article_pacs(elements: list[_Element]) -> list[str]:
    """Сохранить опубликованные коды PACS из подписанного поля без переименования."""

    codes = [
        _compact(child.text()) for child in _labeled_field(elements, "PACS:")
        if isinstance(child, _Element) and child.tag == "a" and re.fullmatch(
            r"/ru/pacs/[0-9]{2}\.[0-9]{2}\.[A-Za-z+\-]{1,2}/",
            child.attributes.get("href", ""),
        )
    ]

    return list(dict.fromkeys(code for code in codes if code))


def _article_authors(parser: _PageParser, elements: list[_Element]) -> list[str]:
    """Прочитать авторов из citation_author или микроразметки, исключив аффилиации."""

    authors = _metadata(parser, "citation_author")

    if not authors:
        authors = [
            _compact(element.text(exclude_superscripts=True)) for element in elements
            if element.attributes.get("itemprop") == "author"
        ]

    return list(dict.fromkeys(author for author in authors if author))


def _article_section(parser: _PageParser) -> str | None:
    """Прочитать явную рубрику над карточкой, не выводя её из названия статьи."""

    sections = [
        _compact(element.text()) for element in parser.elements
        if element.tag == "a" and re.fullmatch(
            r"/ru/rubrics/[a-z-]+/", element.attributes.get("href", "")
        )
    ]
    sections = list(dict.fromkeys(section for section in sections if section))

    return sections[0] if len(sections) == 1 else None


def _article_pdf(
    parser: _PageParser,
    elements: list[_Element],
    page_url: str,
    *,
    legacy_pdf_links: bool = False,
) -> str | None:
    """Выбрать явный PDF текущей статьи, не подставляя адрес отсутствующего файла."""

    article = ARTICLE_PATH.fullmatch(urlsplit(page_url).path)

    if article is None:
        raise ValueError("Ожидается адрес карточки статьи УФН")

    references = _metadata(parser, "citation_pdf_url")

    for element in elements:
        if _has_class(element, "fulltext"):
            references.extend(
                child.attributes.get("href", "") for child in _descendants(element)
                if child.tag == "a" and _has_class(child, "pdf")
            )

    urls: set[str] = set()
    year, issue = article.group(1), article.group(2)
    path_pattern = LEGACY_PDF_PATH if legacy_pdf_links else PDF_PATH
    directory_years = {year} if legacy_pdf_links else {year, year[2:]}
    filename_year = year[2:] if legacy_pdf_links else rf"(?:{year}|{year[2:]})"

    for reference in references:
        url = _resolve_link(reference, page_url)

        if url is None:
            continue

        matched = path_pattern.fullmatch(urlsplit(url).path)

        if (
            matched is not None and matched.group("year") in directory_years and
            matched.group("issue") == issue and
            re.fullmatch(
                rf"r{filename_year}{issue}[a-z]", matched.group("filename"),
            ) is not None
        ):
            # Буква карточки и файла может различаться: в июне 2026 года
            # /a/ явно ссылается на r266z.pdf, /b/ — на r266a.pdf и далее.
            # Принадлежность устанавливает citation_pdf_url или блок fulltext.
            # Проверки года, выпуска, русского пути и отсутствия конфликта сохраняются.
            urls.add(url)

    if len(urls) > 1:
        raise ValueError("В карточке УФН указаны разные русские PDF текущей статьи")

    if any(references) and not urls and not legacy_pdf_links:
        LOGGER.warning(
            "УФН: опубликованные ссылки на PDF не соответствуют проверенному формату "
            "текущей русской статьи; файл не скачивается: %s", page_url,
        )

    return next(iter(urls)) if urls else None


def _article_requires_subscription(article_scope: _Element) -> bool:
    """Распознать отключённый PDF и пояснение только в текущей форме полного текста."""

    # Подтверждённая форма является непосредственным дочерним узлом статьи.
    # Вложенные цитаты, рекомендации и сведения о других материалах не учитываем.
    forms = [
        element for element in article_scope.children
        if isinstance(element, _Element) and element.tag == "form" and
        element.attributes.get("name") == "fulltext-form" and
        not _is_hidden(element.tag, element.attributes)
    ]

    if len(forms) != 1:
        return False

    form_elements = _descendants(forms[0])

    # Одна надпись или устаревшая отключённая кнопка не отменяют действующую
    # ссылку. Распознаём только подтверждённое сочетание в форме текущей статьи.
    if any(
        element.tag == "a" and _has_class(element, "pdf") and element.attributes.get("href", "").strip()
        for element in form_elements
    ):
        return False

    for element in form_elements:
        if not _has_class(element, "fulltext") or SUBSCRIPTION_NOTICE not in _compact(element.text()):
            continue

        if any(
            child.tag == "a" and _has_class(child, "file-link") and
            _has_class(child, "pdf") and _has_class(child, "disabled-link") and
            "href" not in child.attributes
            for child in _descendants(element)
        ):
            return True

    return False


def _metadata_language(title: str, abstract: str) -> str:
    """Не считать русский путь достаточным доказательством языка текста карточки."""

    title_cyrillic = len(re.findall(r"[А-Яа-яЁё]", title))
    title_latin = len(re.findall(r"[A-Za-z]", title))
    abstract_cyrillic = len(re.findall(r"[А-Яа-яЁё]", abstract))
    abstract_latin = len(re.findall(r"[A-Za-z]", abstract))

    if title_cyrillic >= 3 and title_cyrillic > title_latin and (
        not abstract or abstract_cyrillic > abstract_latin
    ):
        return "ru"

    if title_latin >= 3 and not title_cyrillic and not abstract_cyrillic:
        return "en"

    return "und"


def parse_article(html: str, page_url: str, *, legacy_pdf_links: bool = False) -> Document:
    """
    Извлечь метаданные статьи и опубликованный русский PDF без скачивания.

    legacy_pdf_links воспроизводит прежний отбор ссылок только для проверки
    сохранённых карточек: каталог с полным годом и имя файла с коротким годом.
    Новый сбор использует также короткие каталоги и полные годы в именах,
    подтверждённые исходными страницами архива.
    """

    if classify_url(page_url) != "article":
        raise ValueError("Ожидается адрес русской карточки статьи УФН")

    parser = _parse_html(html)
    article_scope = _article_scope(parser)
    elements = _descendants(article_scope)
    titles = _metadata(parser, "citation_title")
    title = _itemprop_text(elements, "headline") or (titles[0] if len(titles) == 1 else "")

    if not title:
        raise ValueError("В карточке УФН не найдено название статьи")

    # На сайте articleBody обозначает аннотацию; полный текст доступен отдельно в PDF.
    abstract = _itemprop_text(elements, "articleBody")
    language = _metadata_language(title, abstract)
    year, issue, _ = urlsplit(page_url).path.strip("/").split("/")[2:]
    document = Document(
        source=SOURCE_ID,
        url=page_url,
        title=title,
        text=abstract,
        authors=_article_authors(parser, elements),
        section=_article_section(parser),
        language=language,
        extra={
            "journal": JOURNAL_NAME,
            "year": int(year),
            "issue": issue,
            "content_role": "metadata_only",
            "text_source": "abstract",
            "language_basis": "title_and_abstract_heuristic",
        },
    )

    volumes = _metadata(parser, "citation_volume")

    if len(volumes) == 1 and volumes[0].isdigit():
        document.extra["volume"] = volumes[0]

    doi = _article_doi(elements)

    if doi is not None:
        document.extra["doi"] = doi

    pacs = _article_pacs(elements)

    if pacs:
        document.extra["pacs_codes_raw"] = pacs

    keywords = _compact("".join(
        child.text() if isinstance(child, _Element) else child
        for child in _labeled_field(elements, "Ключевые слова:")
    ))

    if keywords:
        document.extra["keywords"] = [
            keyword.strip() for keyword in keywords.split(",") if keyword.strip()
        ]

    if language == "ru":
        if _article_requires_subscription(article_scope):
            document.extra["pdf_access_status"] = "subscription_required"
            document.extra["pdf_access_reason"] = SUBSCRIPTION_NOTICE
            LOGGER.warning(
                "УФН: %s; кнопка PDF отключена, ссылка citation_pdf_url не запрашивается: %s",
                SUBSCRIPTION_NOTICE, page_url,
            )

        else:
            document.pdf_url = _article_pdf(parser, elements, page_url, legacy_pdf_links=legacy_pdf_links)

    else:
        document.extra["skipped_non_russian"] = True

    return document
