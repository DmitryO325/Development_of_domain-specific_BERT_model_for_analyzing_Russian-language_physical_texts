"""Разбор открытых карточек русскоязычных журналов ФТИ им. А. Ф. Иоффе."""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .base import Document

IOFFE_ORIGIN = "https://journals.ioffe.ru"
IOFFE_JOURNALS: dict[int, str] = {
    1: "Физика твердого тела",
    2: "Физика и техника полупроводников",
    3: "Журнал технической физики",
    4: "Письма в Журнал технической физики",
    5: "Оптика и спектроскопия",
}

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
BLOCK_TAGS = {"br", "div", "h1", "h2", "h3", "li", "p", "section"}
HIDDEN_TAGS = {"script", "style", "noscript"}


@dataclass
class _Element:
    """Минимальное дерево HTML для извлечения конкретных полей карточки."""

    tag: str
    attributes: dict[str, str]
    children: list[_Element | str] = field(default_factory=list)

    def text(self, *, exclude_superscripts: bool = False) -> str:
        """Собрать видимый текст элемента с разделением блочных узлов."""

        if self.tag in HIDDEN_TAGS or (exclude_superscripts and self.tag == "sup"):
            return ""

        parts = [
            child.text(exclude_superscripts=exclude_superscripts)
            if isinstance(child, _Element) else child
            for child in self.children
        ]
        text = "".join(parts)

        return f" {text} " if self.tag in BLOCK_TAGS else text


class _PageParser(HTMLParser):
    """Разобрать HTML без сети, исполнения скриптов и внешних зависимостей."""

    def __init__(self) -> None:
        """Подготовить корневой узел и список элементов в исходном порядке."""

        super().__init__(convert_charrefs=True)
        self.root = _Element("root", {})
        self.elements: list[_Element] = []
        self.stack = [self.root]

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Добавить открывающий тег, не считая одиночные теги контейнерами."""

        element = _Element(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(element)
        self.elements.append(element)

        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Обработать явно самозакрывающийся тег."""

        self.handle_starttag(tag, attrs)

        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Закрыть ближайший соответствующий открытый элемент."""

        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """Сохранить текст внутри текущего элемента."""

        self.stack[-1].children.append(data)


def _parse_html(html: str) -> _PageParser:
    """Разобрать строку HTML и завершить обработку буфера парсера."""

    if not isinstance(html, str):
        raise TypeError("HTML должен быть строкой")

    parser = _PageParser()
    parser.feed(html)
    parser.close()

    return parser


def _validate_base_url(base_url: str) -> None:
    """Проверить явно выбранный HTTP/HTTPS-адрес публичного узла Иоффе."""

    if not isinstance(base_url, str):
        raise TypeError("Базовый URL должен быть строкой")

    if (
        not base_url or
        any(character.isspace() or ord(character) < 32 for character in base_url) or
        any(character in base_url for character in "?#\\%")
    ):
        raise ValueError("Недопустимый базовый URL журналов Иоффе")

    parsed = urlsplit(base_url)

    if (
        parsed.scheme not in {"http", "https"} or
        parsed.netloc != "journals.ioffe.ru" or
        any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        raise ValueError("Базовый URL должен принадлежать HTTP/HTTPS-узлу journals.ioffe.ru")


def _public_url(reference: str, base_url: str, path_pattern: str) -> str | None:
    """Принять лишь явную числовую ссылку без обхода пути и смены узла."""

    if (
        not reference or
        any(character.isspace() or ord(character) < 32 for character in reference) or
        any(character in reference for character in "?#\\%")
    ):
        return

    try:
        raw_parts = urlsplit(reference)

        if any(part in {".", ".."} for part in raw_parts.path.split("/")):
            return

        resolved = urlsplit(urljoin(base_url, reference))

    except ValueError:
        return

    if (
        resolved.scheme != urlsplit(base_url).scheme or
        resolved.netloc != urlsplit(base_url).netloc or
        re.fullmatch(path_pattern, resolved.path) is None
    ):
        return

    return f"{resolved.scheme}://{resolved.netloc}{resolved.path.rstrip('/')}"


def _extract_urls(html: str, base_url: str, path_pattern: str) -> list[str]:
    """Извлечь разрешённые ссылки без повторов в порядке появления."""

    _validate_base_url(base_url)
    parser = _parse_html(html)
    urls: list[str] = []
    seen: set[str] = set()

    for element in parser.elements:
        if element.tag != "a":
            continue

        url = _public_url(element.attributes.get("href", ""), base_url, path_pattern)

        if url is not None and url not in seen:
            urls.append(url)
            seen.add(url)

    return urls


def extract_issue_urls(html: str, base_url: str) -> list[str]:
    """Извлечь из архива только явно указанные ссылки на выпуски."""

    return _extract_urls(html, base_url, r"/issues/[1-9][0-9]*/?")


def extract_article_urls(html: str, base_url: str) -> list[str]:
    """Извлечь из выпуска только явно указанные ссылки на карточки статей."""

    return _extract_urls(html, base_url, r"/articles/[1-9][0-9]*/?")


def _compact(text: str) -> str:
    """Свести пробельные символы видимого текста к обычным пробелам."""

    return " ".join(text.split())


def _has_class(element: _Element, name: str) -> bool:
    """Проверить отдельное имя CSS-класса, а не подстроку атрибута."""

    return name in element.attributes.get("class", "").split()


def _descendants(element: _Element) -> list[_Element]:
    """Получить дочерние элементы в порядке появления в HTML."""

    result: list[_Element] = []
    pending = list(reversed(element.children))

    while pending:
        child = pending.pop()

        if isinstance(child, _Element):
            result.append(child)
            pending.extend(reversed(child.children))

    return result


def _metadata(parser: _PageParser, name: str) -> list[str]:
    """Прочитать непустые значения заданного метаполя без двойного HTML-декодирования."""

    return [
        _compact(element.attributes["content"])
        for element in parser.elements
        if element.tag == "meta" and
        element.attributes.get("name", "").casefold() == name and
        element.attributes.get("content", "").strip()
    ]


def _class_text(parser: _PageParser, name: str) -> str:
    """Получить первый непустой текст блока с заданным классом."""

    for element in parser.elements:
        if _has_class(element, name):
            text = _compact(element.text())

            if text:
                return text

    return ""


def _authors(parser: _PageParser) -> list[str]:
    """Извлечь авторов без надстрочных индексов, аффилиаций и адресов почты."""

    values = _metadata(parser, "citation_author")

    if values:
        return list(dict.fromkeys(values))

    for container in parser.elements:
        if not _has_class(container, "art_authors"):
            continue

        for element in _descendants(container):
            if not _has_class(element, "authors"):
                continue

            text = _compact(element.text(exclude_superscripts=True))
            return list(dict.fromkeys(
                author.strip() for author in re.split(r"[,;]", text) if author.strip()
            ))

    return []


def _abstract(parser: _PageParser) -> str:
    """Найти вкладку аннотации, не захватывая литературу и статистику."""

    values = _metadata(parser, "citation_abstract")

    if values:
        return values[0]

    for container in parser.elements:
        if not _has_class(container, "art_tabs"):
            continue

        elements = _descendants(container)

        for element in elements:
            if element.tag != "a":
                continue

            labels = {
                _compact(element.text()).casefold(),
                element.attributes.get("title", "").strip().casefold(),
            }
            target = element.attributes.get("href", "")

            if (
                not labels.intersection({"абстракт", "аннотация", "abstract"}) or
                re.fullmatch(r"#[A-Za-z][A-Za-z0-9_-]*", target) is None
            ):
                continue

            matches = [
                candidate for candidate in elements
                if candidate.attributes.get("id") == target[1:]
            ]

            if len(matches) == 1:
                return _compact(matches[0].text())

    return ""


def _abstract_and_keywords(parser: _PageParser) -> tuple[str, list[str]]:
    """Отделить ключевые слова в конце аннотации и сохранить их списком."""

    text = _abstract(parser)
    parts = re.split(
        r"\bКлючевые\s+слова\s*:\s*", text, maxsplit=1, flags=re.IGNORECASE
    )
    keyword_values = _metadata(parser, "citation_keywords")

    if len(parts) == 2:
        text = parts[0].strip()
        keyword_values = [parts[1]]

    keywords = list(dict.fromkeys(
        keyword.strip().rstrip(".")
        for value in keyword_values
        for keyword in re.split(r"[,;]", value)
        if keyword.strip().rstrip(".")
    ))

    return text, keywords


def _bibliographic_context(
    parser: _PageParser, article_url: str
) -> tuple[int | None, str | None, int | None]:
    """Извлечь журнал и год из хлебных крошек текущей статьи, а не бокового меню."""

    journal_ids: set[int] = set()
    years: set[int] = set()

    for container in parser.elements:
        if not _has_class(container, "breadcrumbs"):
            continue

        for element in _descendants(container):
            if element.tag != "a":
                continue

            reference = element.attributes.get("href", "")
            journal_url = _public_url(reference, article_url, r"/journals/[1-9][0-9]*/?")

            if journal_url is not None:
                journal_ids.add(int(journal_url.rsplit("/", maxsplit=1)[-1]))

            issue_url = _public_url(reference, article_url, r"/issues/[1-9][0-9]*/?")
            year_match = re.search(r"\bГод\s+(\d{4})\b", element.text(), re.IGNORECASE)

            if issue_url is not None and year_match:
                years.add(int(year_match.group(1)))

    journal_id = next(iter(journal_ids)) if len(journal_ids) == 1 else None
    journal = IOFFE_JOURNALS.get(journal_id) if journal_id is not None else None
    year = next(iter(years)) if len(years) == 1 else None
    journal_values = _metadata(parser, "citation_journal_title")

    if journal is None and journal_values:
        journal = journal_values[0]

    if year is None:
        dates = _metadata(parser, "citation_publication_date")

        if dates:
            year_match = re.match(r"(\d{4})(?:\b|[-/])", dates[0])

            if year_match:
                year = int(year_match.group(1))

    return journal_id, journal, year


def _doi(parser: _PageParser) -> str | None:
    """Прочитать DOI именно из библиографического поля статьи."""

    values = _metadata(parser, "citation_doi")
    candidate = values[0] if values else _class_text(parser, "art_biblio_doi")
    matches = list(dict.fromkeys(re.findall(r"10\.\d{4,9}/[^\s<>\"']+", candidate)))

    if len(matches) == 1:
        return matches[0]

    return


def _pdf_url(parser: _PageParser, article_url: str, article_id: str) -> str | None:
    """Принять явную PDF-ссылку только с идентификатором текущей статьи."""

    references = _metadata(parser, "citation_pdf_url")
    references.extend(
        element.attributes.get("href", "")
        for element in parser.elements
        if element.tag == "a"
    )
    urls: set[str] = set()

    for reference in references:
        url = _public_url(reference, article_url, rf"/articles/viewPDF/{article_id}/?")

        if url is not None:
            urls.add(url)

    if len(urls) == 1:
        return next(iter(urls))

    return


def parse_article(html: str, url: str) -> Document:
    """Разобрать карточку статьи; ``text`` содержит только аннотацию, не полный текст."""

    _validate_base_url(url)
    article_url = _public_url(url, url, r"/articles/[1-9][0-9]*/?")

    if article_url is None:
        raise ValueError("Ожидается URL карточки статьи Иоффе вида /articles/123")

    parser = _parse_html(html)
    titles = _metadata(parser, "citation_title")
    title = _class_text(parser, "art_title") or (titles[0] if titles else "")

    if not title:
        raise ValueError("В карточке Иоффе не найдено название статьи")

    article_id = article_url.rsplit("/", maxsplit=1)[-1]
    abstract, keywords = _abstract_and_keywords(parser)
    journal_id, journal, year = _bibliographic_context(parser, article_url)

    return Document(
        source="journals.ioffe.ru",
        url=article_url,
        title=title,
        text=abstract,
        authors=_authors(parser),
        published=str(year) if year is not None else None,
        pdf_url=_pdf_url(parser, article_url, article_id),
        extra={
            "article_id": article_id,
            "doi": _doi(parser),
            "journal_id": journal_id,
            "journal": journal,
            "year": year,
            "keywords": keywords,
            "content_role": "metadata_only",
        },
    )
