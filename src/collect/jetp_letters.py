"""Разбор публичного русского архива журнала «Письма в ЖЭТФ» без сети."""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from .base import Document

SOURCE_ID = "jetp_letters"
ALLOWED_ORIGINS = ("http://jetpletters.ru",)
START_URL = "http://jetpletters.ru/ps/index-v-tru_0.shtml"
DEFAULT_ENCODING = "koi8-r"

ARCHIVE_PATH = re.compile(r"/ps/(?:archive\.shtml|index-v-(?:tru_[01]|[1-9][0-9]*_ru)\.shtml)?")
ISSUE_PATH = re.compile(r"/ps/[1-9][0-9]*/index\.shtml")
ARTICLE_PATH = re.compile(r"/ps/([1-9][0-9]*)/article_([1-9][0-9]*)\.shtml")
PDF_PATH = re.compile(r"/ps/[1-9][0-9]*/article_[1-9][0-9]*\.pdf")

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
BLOCK_TAGS = {"br", "div", "p", "td", "th", "tr"}
HIDDEN_TAGS = {"script", "style", "noscript"}


@dataclass
class _Element:
    """Узел HTML с атрибутами и видимым текстом вложенных элементов."""

    tag: str
    attributes: dict[str, str]
    children: list[_Element | str] = field(default_factory=list)

    def text(self, *, exclude_superscripts: bool = False) -> str:
        """Собрать текст, сохраняя формулы из alt и исключая служебные скрипты."""

        if self.tag in HIDDEN_TAGS or (exclude_superscripts and self.tag == "sup"):
            return ""

        if self.tag == "img":
            return self.attributes.get("alt", "")

        parts = [
            child.text(exclude_superscripts=exclude_superscripts)
            if isinstance(child, _Element) else child
            for child in self.children
        ]
        text = "".join(parts)

        return f" {text} " if self.tag in BLOCK_TAGS else text


class _PageParser(HTMLParser):
    """Разобрать старую табличную разметку без исполнения кода страницы."""

    def __init__(self) -> None:
        """Подготовить корень дерева и список узлов в порядке появления."""

        super().__init__(convert_charrefs=True)
        self.root = _Element("root", {})
        self.elements: list[_Element] = []
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Добавить узел и открыть контейнер, если тег не одиночный."""

        element = _Element(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(element)
        self.elements.append(element)

        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Обработать самозакрывающийся тег, не открывая лишний контейнер."""

        self.handle_starttag(tag, attrs)

        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Закрыть ближайший одноимённый контейнер незавершённого дерева."""

        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """Сохранить текст текущего контейнера без повторного HTML-декодирования."""

        self.stack[-1].children.append(data)


def _parse_html(html: str) -> _PageParser:
    """Разобрать непустую строку HTML с явной проверкой входных данных."""

    if not isinstance(html, str) or not html.strip():
        raise ValueError("HTML страницы «Писем в ЖЭТФ» должен быть непустой строкой")

    parser = _PageParser()
    parser.feed(html)
    parser.close()

    return parser


def _safe_reference(reference: str) -> bool:
    """Исключить неоднозначные адреса до нормализации пути через urljoin."""

    if (
        not isinstance(reference, str) or not reference or
        any(character.isspace() or ord(character) < 32 for character in reference) or
        any(character in reference for character in "?#\\%")
    ):
        return False

    try:
        parts = urlsplit(reference)

    except ValueError:
        return False

    return not any(part in {".", ".."} for part in parts.path.split("/"))


def classify_url(url: str) -> str | None:
    """Определить известный путь архива, исключив английский архив и чужие узлы."""

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
    """Разрешить только явную безопасную ссылку на известный публичный путь."""

    if not _safe_reference(reference):
        return

    resolved = urljoin(page_url, reference)

    return resolved if classify_url(resolved) is not None else None


def extract_links(html: str, page_url: str) -> list[str]:
    """Собрать явные ссылки архива без повторов и без генерации идентификаторов."""

    if classify_url(page_url) not in {"archive", "issue", "article"}:
        raise ValueError("Неизвестный адрес HTML-страницы «Писем в ЖЭТФ»")

    parser = _parse_html(html)
    urls: list[str] = []
    seen: set[str] = set()

    for element in parser.elements:
        if element.tag != "a":
            continue

        url = _resolve_link(element.attributes.get("href", ""), page_url)

        if url is not None and url not in seen:
            seen.add(url)
            urls.append(url)

    return urls


def _descendants(element: _Element) -> list[_Element]:
    """Получить вложенные узлы в порядке их появления в исходном HTML."""

    result: list[_Element] = []
    pending = list(reversed(element.children))

    while pending:
        child = pending.pop()

        if isinstance(child, _Element):
            result.append(child)
            pending.extend(reversed(child.children))

    return result


def _has_class(element: _Element, class_name: str) -> bool:
    """Проверить целое имя CSS-класса, а не совпадение подстроки."""

    return class_name in element.attributes.get("class", "").split()


def _compact(text: str) -> str:
    """Убрать отступы HTML, не изменяя символы и содержание текста."""

    return " ".join(text.split())


def _first_class_text(parser: _PageParser, class_name: str) -> str:
    """Получить видимый текст первого непустого элемента указанного класса."""

    for element in parser.elements:
        if _has_class(element, class_name):
            text = _compact(element.text())

            if text:
                return text

    return ""


def _article_authors(parser: _PageParser) -> list[str]:
    """Извлечь имена авторов, исключив аффилиации и надстрочные отметки."""

    authors: list[str] = []

    for element in parser.elements:
        if element.attributes.get("itemprop") != "author":
            continue

        for child in _descendants(element):
            if child.attributes.get("itemprop") != "name":
                continue

            text = _compact(child.text(exclude_superscripts=True))

            for author in re.split(r"[,;]", text):
                author = author.strip()

                if author and author not in authors:
                    authors.append(author)

    return authors


def _article_abstract(parser: _PageParser) -> str:
    """Прочитать аннотацию внутри abstract, не называя её полным текстом."""

    for element in parser.elements:
        if not _has_class(element, "abstract"):
            continue

        for child in _descendants(element):
            if child.attributes.get("itemprop") == "articleBody":
                return _compact(child.text())

        text = _compact(element.text())

        return re.sub(r"^(?:Abstract|Аннотация)\s*:?\s*", "", text)

    return ""


def _article_doi(parser: _PageParser) -> str | None:
    """Прочитать DOI только из подписанного блока текущей статьи."""

    for element in parser.elements:
        if not _has_class(element, "article-info"):
            continue

        matched = re.fullmatch(r"DOI\s*:\s*(10\.[0-9]{4,9}/\S+)", _compact(element.text()))

        if matched:
            return matched.group(1)


def _metadata_language(title: str, abstract: str) -> str:
    """Консервативно определить язык карточки, не приписывая язык файлу PDF."""

    title_cyrillic = len(re.findall(r"[А-Яа-яЁё]", title))
    title_latin = len(re.findall(r"[A-Za-z]", title))
    abstract_cyrillic = len(re.findall(r"[А-Яа-яЁё]", abstract))
    abstract_latin = len(re.findall(r"[A-Za-z]", abstract))

    if (
        title_cyrillic >= 3 and title_cyrillic > title_latin and
        (not abstract or abstract_cyrillic > abstract_latin)
    ):
        return "ru"

    if title_latin >= 3 and not title_cyrillic and not abstract_cyrillic:
        return "en"

    return "und"


def parse_article(html: str, page_url: str) -> Document:
    """Извлечь метаданные и явную PDF-ссылку только русскоязычной карточки."""

    if classify_url(page_url) != "article":
        raise ValueError("Ожидался адрес карточки статьи «Писем в ЖЭТФ»")

    parser = _parse_html(html)
    title = _first_class_text(parser, "arttitle")

    if not title:
        raise ValueError("На странице «Писем в ЖЭТФ» не найдено название статьи")

    abstract = _article_abstract(parser)
    language = _metadata_language(title, abstract)
    extra: dict[str, Any] = {
        "journal": "Письма в ЖЭТФ",
        "content_role": "metadata_only",
        "text_source": "abstract",
        "language_basis": "title_and_abstract_heuristic",
    }
    header = _first_class_text(parser, "pshead1")
    matched_header = re.search(
        r"VOLUME\s+([0-9]+)\s*\(([0-9]{4})\)\s*\|\s*ISSUE\s+([0-9]+)",
        header,
    )

    if matched_header:
        extra.update({
            "volume": matched_header.group(1),
            "year": int(matched_header.group(2)),
            "issue": matched_header.group(3),
        })

    doi = _article_doi(parser)

    if doi is not None:
        extra["doi"] = doi

    # PDF должен быть явно связан с той же карточкой; соседний файл не подходит.
    expected_pdf = page_url.removesuffix(".shtml") + ".pdf"
    pdf_url = None

    if language == "ru":
        urls = extract_links(html, page_url)

        if expected_pdf in urls:
            pdf_url = expected_pdf

    else:
        extra["skipped_non_russian"] = True

    return Document(
        source=SOURCE_ID,
        url=page_url,
        title=title,
        text=abstract,
        authors=_article_authors(parser),
        pdf_url=pdf_url,
        language=language,
        extra=extra,
    )
