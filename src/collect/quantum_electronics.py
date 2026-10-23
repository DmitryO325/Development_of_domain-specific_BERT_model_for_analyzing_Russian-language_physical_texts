"""Разбор публичного русского архива журнала «Квантовая электроника»."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .base import Document, HttpResponseSnapshot

SOURCE_ID = "quantum"
START_URL = "https://quantum-electronics.ru/archive/"
ALLOWED_ORIGINS = ("https://quantum-electronics.ru",)
DEFAULT_ENCODING = "utf-8"

# Единственный перенос подтверждён сохранённым HTTP 301 от 15.10.2026.
# Суффикс -2 относится к адресу WordPress, а не к номеру или диапазону выпусков.
_ISSUE_CANONICAL_PATHS = {"/2025_55_2": "/2025_55_2-2"}

_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_HIDDEN_TAGS = {"script", "style", "noscript", "template"}
_BLOCK_TAGS = {"br", "div", "h1", "h2", "h3", "li", "p", "section"}
_PLAIN_FIELD_LABELS = {"аннотация", "образец цитирования"}
_FIELD_LABELS = {*_PLAIN_FIELD_LABELS, "ключевые слова", "doi", "поступила в редакцию"}
_NEXT_FIELD = re.compile(
    r"\n[^\S\n]*(?:аннотация|образец\s+цитирования|ключевые\s+слова|doi|поступила\s+в\s+редакцию)"
    r"[^\S\n]*(?::|(?=\n|$))", re.IGNORECASE,
)


@dataclass
class _Element:
    """Элемент HTML с дочерними узлами в исходном порядке."""

    tag: str
    attributes: dict[str, str]
    children: list[_Element | str] = field(default_factory=list)

    def text(self) -> str:
        """Извлечь видимый текст, сохранив границы блочных элементов."""

        if self.tag in _HIDDEN_TAGS:
            return ""

        text = "".join(
            child.text() if isinstance(child, _Element) else child
            for child in self.children
        )

        return f"\n{text}\n" if self.tag in _BLOCK_TAGS else text

    def elements(self) -> list[_Element]:
        """Обойти дочерние элементы, не включая скрытое содержимое."""

        result = []

        for child in self.children:
            if not isinstance(child, _Element) or child.tag in _HIDDEN_TAGS:
                continue

            result.append(child)
            result.extend(child.elements())

        return result


class _PageParser(HTMLParser):
    """Построить небольшое дерево HTML без исполнения скриптов и сети."""

    def __init__(self) -> None:
        """Подготовить корень и стек открытых элементов."""

        super().__init__(convert_charrefs=True)
        self.root = _Element("root", {})
        self.stack = [self.root]

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Добавить элемент, оставив одиночные теги вне стека."""

        element = _Element(tag, {name: value or "" for name, value in attrs})
        self.stack[-1].children.append(element)

        if tag not in _VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Обработать явно самозакрывающийся элемент."""

        self.handle_starttag(tag, attrs)

        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Закрыть ближайший соответствующий тег, терпимо к неполному HTML."""

        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """Сохранить текстовые данные текущего элемента."""

        self.stack[-1].children.append(data)


def _parse_html(html: str) -> _Element:
    """Разобрать строку HTML либо отклонить неверный тип аргумента."""

    if not isinstance(html, str):
        raise TypeError("HTML должен быть строкой")

    parser = _PageParser()
    parser.feed(html)
    parser.close()

    return parser.root


def _normalized_text(element: _Element) -> str:
    """Свернуть пробельные символы видимого текста в одиночные пробелы."""

    return " ".join(element.text().split())


def _with_class(elements: list[_Element], class_name: str) -> _Element | None:
    """Найти первый элемент с точным именем CSS-класса."""

    return next(
        (
            element for element in elements
            if class_name in element.attributes.get("class", "").split()
        ),
        None,
    )


def classify_url(url: str) -> str | None:
    """Распознать только русские публичные пути выбранного узла журнала."""

    if (
        not isinstance(url, str) or not url or
        any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in url) or
        any(character in url for character in "?#\\%")
    ):
        return

    try:
        parsed = urlsplit(url)

    except ValueError:
        return

    if (
        parsed.scheme != "https" or parsed.netloc != "quantum-electronics.ru" or
        any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        return

    if parsed.path in {"/archive", "/archive/"}:
        return "archive"

    if re.fullmatch(r"/[12][0-9]{3}_[1-9][0-9]*_[1-9][0-9]*/?", parsed.path):
        return "issue"

    if parsed.path.removesuffix("/") in _ISSUE_CANONICAL_PATHS.values():
        return "issue"

    if re.fullmatch(
        r"/wp-content/uploads/[12][0-9]{3}/(?:[A-Za-z0-9_-]+/)*"
        r"[A-Za-z0-9_-]+\.pdf", parsed.path,
    ):
        return "pdf"

    article_match = re.fullmatch(
        r"/(?:[12][0-9]{3}_[1-9][0-9]*_[1-9][0-9]*/)?"
        r"(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)+)/?", parsed.path,
    )

    if article_match is not None and (
        not article_match["slug"].startswith(("wp-", "cat-")) and
        article_match["slug"] not in {
            "impact-factor-quantum-electronics-jcr",
            "tablica-sootvetstviya-russkih-ssylok",
        }
    ):
        return "article"

    return


def _nested_article_target(url: str) -> str | None:
    """Описать единственный допустимый адрес назначения вложенной карточки."""

    if classify_url(url) != "article":
        return

    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")

    if len(parts) != 2:
        return

    return parsed._replace(path=f"/{parts[1]}/").geturl()


def accepted_statuses(url: str) -> frozenset[int]:
    """Сохранить 301 вложенной карточки для отдельной проверки Location."""

    return frozenset({200, 301}) if _nested_article_target(url) is not None else frozenset({200})


def resolve_article_redirect(snapshot: HttpResponseSnapshot) -> str | None:
    """Проверить переход вложенной карточки к тому же имени в корне сайта."""

    if not isinstance(snapshot, HttpResponseSnapshot):
        raise TypeError("Ожидается сохранённый HTTP-ответ карточки")

    if snapshot.status_code == 200:
        return

    expected = _nested_article_target(snapshot.requested_url)
    locations = [value for name, value in snapshot.headers if name.lower() == "location"]

    # Такой 301 подтверждён 16.10.2026 для относительной ссылки выпуска 2022_52_1.
    # Адрес не заменяется заранее: каждый переход должен подтвердить сам сервер.
    if (
        snapshot.status_code != 301 or expected is None or
        snapshot.final_url != snapshot.requested_url or locations != [expected]
    ):
        raise ValueError(f"Неподтверждённое перенаправление карточки Quantum: {snapshot.requested_url}")

    return expected


def _explicit_url(reference: str, page_url: str) -> str | None:
    """Разрешить опубликованную ссылку без скрытых параметров и обхода пути."""

    if (
        not reference or
        any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in reference) or
        any(character in reference for character in "?#\\%") or
        any(part in {".", ".."} for part in reference.split("/"))
    ):
        return

    try:
        if "//" in urlsplit(reference).path:
            return

        url = urljoin(page_url, reference)

    except ValueError:
        return

    kind = classify_url(url)

    if kind in {"archive", "issue"}:
        parsed = urlsplit(url)
        canonical_path = _ISSUE_CANONICAL_PATHS.get(parsed.path.removesuffix("/"))

        if canonical_path is not None:
            # Используем ровно один опубликованный сервером Location, а не
            # общее следование перенаправлениям или угадывание адресов выпусков.
            return parsed._replace(path=canonical_path + "/").geturl()

        # Старые ссылки архива опубликованы без слеша, но WordPress
        # отвечает постоянным перенаправлением на тот же путь со слешем.
        return url.rstrip("/") + "/"

    return url if kind is not None else None


def extract_links(html: str, page_url: str) -> list[str]:
    """Извлечь ссылки архива или выпуска только из основного содержимого."""

    page_kind = classify_url(page_url)

    if page_kind not in {"archive", "issue", "article"}:
        raise ValueError("Ожидается URL русского архива, выпуска или статьи")

    root = _parse_html(html)
    content = _with_class(root.elements(), "entry-content")

    if content is None:
        raise ValueError("В странице нет основного блока entry-content")

    if page_kind == "article":
        document = parse_article(html, page_url)
        return [document.pdf_url] if document.pdf_url else []

    direct_pdfs = {
        document.pdf_url for document in parse_issue_articles(html, page_url)
    } if page_kind == "issue" else set()
    expected_kinds = {"issue"} if page_kind == "archive" else {"article", "pdf"}
    links: list[str] = []

    for element in content.elements():
        if element.tag != "a":
            continue

        url = _explicit_url(element.attributes.get("href", ""), page_url)

        if url is not None and classify_url(url) == "pdf" and url not in direct_pdfs:
            continue

        if (
            url is not None and classify_url(url) in expected_kinds and
            url not in links
        ):
            links.append(url)

    return links


def _field_text(element: _Element) -> str:
    """Сохранить переносы строк и границы соседних выделенных подписей поля."""

    if element.tag in _HIDDEN_TAGS:
        return ""

    if element.tag == "br":
        return "\n"

    text = "".join(
        _field_text(child) if isinstance(child, _Element) else child
        for child in element.children
    )

    if element.tag in {"b", "strong"} and _normalized_text(element).casefold().rstrip(": ") in _FIELD_LABELS:
        return "\n" + text

    return text


def _labeled_text(content: _Element, label: str, *, allow_plain: bool = False) -> str:
    """Прочитать поле отдельного блока по выделенной либо подтверждённой обычной подписи."""

    label_pattern = r"\s+".join(re.escape(word) for word in label.split())

    for element in content.elements():
        if element.tag not in {"div", "p"}:
            continue

        first_child = next(
            (child for child in element.children if isinstance(child, _Element)),
            None,
        )
        bold_label = (
            first_child is not None and first_child.tag in {"b", "strong"}
            and _normalized_text(first_child).casefold().rstrip(": ") == label.casefold()
        )

        if bold_label and first_child is not None:
            heading = _normalized_text(first_child)
            text = _normalized_text(element)

            # Для уже поддерживаемой разметки оставляем прежние значения:
            # новые границы служебных полей применяются только к обычным подписям.
            if text.startswith(heading):
                return text[len(heading):].strip()

        if not allow_plain or label.casefold() not in _PLAIN_FIELD_LABELS:
            continue

        # Обёртка нескольких абзацев не является одним обычным полем: её
        # дочерние блоки рассматриваются отдельно, без склейки значений.
        if any(child.tag in _BLOCK_TAGS - {"br"} for child in element.elements()):
            continue

        text = _field_text(element).strip()
        prefix = re.match(rf"^{label_pattern}\s*:", text, re.IGNORECASE)

        if prefix is None:
            continue

        value = text[prefix.end():]
        boundary = _NEXT_FIELD.search(value)

        if boundary is not None:
            value = value[:boundary.start()]

        return " ".join(value.split())

    return ""


def _article_authors(content: _Element) -> list[str]:
    """Получить строку авторов статьи, не принимая за автора редактора WordPress."""

    first_paragraph = next(
        (
            child for child in content.children
            if isinstance(child, _Element) and child.tag == "p"
        ),
        None,
    )

    if first_paragraph is None:
        return []

    text = _normalized_text(first_paragraph)

    if not re.match(r"[А-ЯЁA-Z]\.\s*(?:[А-ЯЁA-Z]\.\s*)?[А-ЯЁA-Z]", text):
        return []

    return [author.strip() for author in text.split(",") if author.strip()]


def _article_pdf(content: _Element, page_url: str) -> str | None:
    """Выбрать явную ссылку на PDF из русского блока скачивания статьи."""

    candidates: list[str] = []

    for element in content.elements():
        if element.tag != "a":
            continue

        url = _explicit_url(element.attributes.get("href", ""), page_url)

        if url is None or classify_url(url) != "pdf":
            continue

        label = _normalized_text(element).casefold()

        if "english" in label or "англ" in label:
            continue

        if re.search(r"\b(?:en|eng|english)\b", url.casefold().replace("_", "-")):
            continue

        if "скачать" in label or "рус" in label:
            candidates.append(url)

    unique_candidates = list(dict.fromkeys(candidates))

    return unique_candidates[0] if len(unique_candidates) == 1 else None


def _metadata_language(title: str, abstract: str) -> str:
    """Консервативно определить язык метаданных, не приписывая язык самому PDF."""

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


def _unlinked_title_text(children: list[_Element | str]) -> str | None:
    """Прочитать название только с простым оформлением букв и индексов."""

    parts: list[str] = []

    for child in children:
        if isinstance(child, str):
            parts.append(child)
            continue

        if child.tag not in {"i", "em", "sup", "sub"}:
            return

        text = _unlinked_title_text(child.children)

        if text is None:
            return

        parts.append(text)

    return "".join(parts)


def _unlinked_issue_entry(
    children: list[_Element | str], section: str | None,
) -> dict[str, object] | None:
    """Подтвердить запись вида название, перенос, авторы и выделенная страница."""

    breaks = [
        index for index, child in enumerate(children)
        if isinstance(child, _Element) and child.tag == "br"
    ]

    if len(breaks) != 1 or not children:
        return

    page_element = children[-1]

    if (
        not isinstance(page_element, _Element) or
        page_element.tag not in {"b", "strong"} or
        any(not isinstance(child, str) for child in page_element.children)
    ):
        return

    first_page = _normalized_text(page_element)

    if re.fullmatch(r"[1-9][0-9]*", first_page) is None:
        return

    title_text = _unlinked_title_text(children[:breaks[0]])

    if title_text is None:
        return

    title = " ".join(title_text.split())
    author_children = children[breaks[0] + 1:-1]

    if (
        _metadata_language(title, "") != "ru" or
        any(not isinstance(child, str) for child in author_children)
    ):
        return

    author_text = "".join(child for child in author_children if isinstance(child, str))
    authors = [" ".join(author.split()) for author in author_text.split(",")]

    # Альтернативную фамилию в скобках сохраняем вместе с именем, как у источника.
    if not all(
        re.fullmatch(
            r"(?:[А-ЯЁA-Z]\.\s*){1,3}[А-ЯЁA-Z][А-Яа-яЁёA-Za-z-]+"
            r"(?:\s+[А-ЯЁA-Z][А-Яа-яЁёA-Za-z-]+)*"
            r"(?:\s+\([А-ЯЁA-Z][А-Яа-яЁёA-Za-z]+"
            r"(?:-[А-ЯЁA-Z][А-Яа-яЁёA-Za-z]+)*\))?", author,
        )
        for author in authors
    ):
        return

    return {
        "title": title,
        "authors": authors,
        "first_page": int(first_page),
        "section": section,
    }


def _validate_issue_path(page_url: str, expected_path: str) -> None:
    """Сверить заголовок с обычным путём или единственным подтверждённым переносом."""

    canonical_path = _ISSUE_CANONICAL_PATHS.get(expected_path, expected_path)

    if urlsplit(page_url).path.removesuffix("/") not in {expected_path, canonical_path}:
        raise ValueError("Заголовок выпуска не соответствует его URL")


def parse_unlinked_issue(html: str, page_url: str) -> list[dict[str, object]]:
    """Подтвердить оглавление без ссылок для явного отложения, не создавая статьи."""

    if classify_url(page_url) != "issue":
        raise ValueError("Ожидается URL русского выпуска журнала")

    elements = _parse_html(html).elements()
    contents = [
        element for element in elements
        if "entry-content" in element.attributes.get("class", "").split()
    ]
    headings = [
        element for element in elements
        if "entry-title" in element.attributes.get("class", "").split()
    ]

    if len(contents) != 1 or len(headings) != 1:
        return []

    content = contents[0]

    if any(element.tag == "a" for element in content.elements()):
        return []

    issue_match = re.fullmatch(
        r"([12][0-9]{3}),\s*том\s+([0-9]+),\s*номер\s+([0-9]+)",
        _normalized_text(headings[0]),
    )

    if issue_match is None:
        return []

    expected_path = f"/{issue_match[1]}_{issue_match[2]}_{issue_match[3]}"
    _validate_issue_path(page_url, expected_path)

    entries: list[dict[str, object]] = []
    section: str | None = None

    for paragraph in content.children:
        if isinstance(paragraph, str):
            if paragraph.strip():
                return []

            continue

        if paragraph.tag != "p":
            return []

        children = list(paragraph.children)

        while children and isinstance(children[0], str) and not children[0].strip():
            children.pop(0)

        while children and isinstance(children[-1], str) and not children[-1].strip():
            children.pop()

        if not children:
            continue

        first_child = children[0]

        if (
            len(children) == 1 and isinstance(first_child, _Element) and
            first_child.tag in {"b", "strong"} and
            all(isinstance(child, str) for child in first_child.children)
        ):
            section = _normalized_text(first_child)

            if _metadata_language(section, "") != "ru":
                return []

            continue

        entry = _unlinked_issue_entry(children, section)

        if entry is None:
            # Нельзя продолжать обход, незаметно отбросив часть неизвестного оглавления.
            return []

        entries.append(entry)

    return entries


def _is_empty_heading_break(element: _Element | str) -> bool:
    """Проверить одиночный перенос в пустом выделении без иных узлов и атрибутов."""

    if (
        not isinstance(element, _Element) or element.tag not in {"b", "strong"} or
        element.attributes
    ):
        return False

    children = [
        child for child in element.children
        if isinstance(child, _Element) or child.strip()
    ]

    if len(children) != 1:
        return False

    child = children[0]

    return (
        isinstance(child, _Element) and child.tag == "br" and
        not child.attributes and not child.children
    )


def _split_issue_section(paragraph: _Element, title_element: _Element) -> tuple[str, str]:
    """Отделить только простую начальную рубрику с переносом перед PDF-ссылкой."""

    children = [
        (index, child) for index, child in enumerate(paragraph.children)
        if isinstance(child, _Element) or child.strip()
    ]

    if len(children) < 3:
        raise ValueError("Заголовок статьи не находится в начале записи оглавления")

    section_element = children[0][1]
    separator = children[1][1]
    title_index = 2

    # В выпуске 2012 № 12 перед ссылкой остался отдельный strong с одним br.
    # Не считаем пустыми элементы со скрытым текстом, атрибутами или вложениями.
    if len(children) >= 4 and _is_empty_heading_break(children[2][1]):
        title_index = 3

    if (
        not isinstance(section_element, _Element) or
        section_element.tag not in {"b", "strong"} or
        not isinstance(separator, _Element) or separator.tag != "br" or
        children[title_index][1] is not title_element
    ):
        raise ValueError("Заголовок статьи не находится в начале записи оглавления")

    section_children = [
        child for child in section_element.children
        if isinstance(child, _Element) or child.strip()
    ]

    # В выпуске 2019 № 3 рубрика заканчивается дополнительным переносом
    # внутри strong. Разрешаем только этот хвост, не скрывая другие узлы.
    if section_children and isinstance(section_children[-1], _Element):
        trailing_element = section_children[-1]

        if (
            trailing_element.tag == "br" and
            not trailing_element.attributes and not trailing_element.children
        ):
            section_children.pop()

    if any(not isinstance(child, str) for child in section_children):
        raise ValueError("Заголовок статьи не находится в начале записи оглавления")

    section = _normalized_text(section_element)

    if not section:
        raise ValueError("Начальная рубрика оглавления пуста")

    # Срез сохраняет исходные пробелы между названием, авторами и страницей.
    remainder = _Element("p", {}, paragraph.children[children[title_index][0]:])

    return _normalized_text(remainder), section


def parse_issue_articles(html: str, page_url: str) -> list[Document]:
    """Прочитать статьи, опубликованные в русском оглавлении прямыми PDF-ссылками."""

    if classify_url(page_url) != "issue":
        raise ValueError("Ожидается URL русского выпуска журнала")

    root = _parse_html(html)
    elements = root.elements()
    content = _with_class(elements, "entry-content")

    if content is None:
        raise ValueError("В выпуске нет основного блока entry-content")

    title_element = _with_class(elements, "entry-title")
    issue_title = _normalized_text(title_element) if title_element is not None else ""
    issue_match = re.fullmatch(
        r"([12][0-9]{3}),\s*том\s+([0-9]+),\s*номер\s+([0-9]+)", issue_title,
    )
    documents: list[Document] = []
    section: str | None = None

    for paragraph in content.children:
        if not isinstance(paragraph, _Element) or paragraph.tag != "p":
            continue

        pdf_links: list[tuple[_Element, str]] = []

        for element in paragraph.elements():
            if element.tag != "a":
                continue

            url = _explicit_url(element.attributes.get("href", ""), page_url)

            if url is not None and classify_url(url) == "pdf":
                pdf_links.append((element, url))

        if not pdf_links:
            if any(element.tag in {"b", "strong"} for element in paragraph.elements()):
                section = _normalized_text(paragraph) or None

            continue

        if len(pdf_links) != 1:
            raise ValueError("В одной записи оглавления несколько PDF: требуется проверка")

        element, pdf_url = pdf_links[0]
        title = _normalized_text(element)

        if (
            _metadata_language(title, "") != "ru" or
            re.search(r"\b(?:en|eng|english)\b", pdf_url.casefold().replace("_", "-"))
        ):
            continue

        if issue_match is None:
            raise ValueError("Не распознан русский заголовок выпуска с прямыми PDF")

        expected_path = f"/{issue_match[1]}_{issue_match[2]}_{issue_match[3]}"
        _validate_issue_path(page_url, expected_path)

        paragraph_text = _normalized_text(paragraph)
        leading_children = [
            child for child in paragraph.children
            if isinstance(child, _Element) or child.strip()
        ][:2]
        inline_section = not paragraph_text.startswith(title) or (
            len(leading_children) == 2 and
            isinstance(leading_children[0], _Element) and
            leading_children[0].tag in {"b", "strong"} and
            isinstance(leading_children[1], _Element) and leading_children[1].tag == "br"
        )

        if inline_section:
            paragraph_text, section = _split_issue_section(paragraph, element)

        if not paragraph_text.startswith(title):
            raise ValueError("Заголовок статьи не находится в начале записи оглавления")

        author_text = paragraph_text[len(title):].strip()
        first_page_match = re.search(r"\s+([0-9]+)$", author_text)

        if inline_section and first_page_match is None:
            # Новая форма допускает отсутствие авторов; прежние сохранённые
            # карточки разбираем без изменения их набора метаданных.
            first_page_match = re.fullmatch(r"([0-9]+)", author_text)

        extra: dict[str, object] = {
            "journal": "Квантовая электроника",
            "content_role": "metadata_only",
            "metadata_source": "issue_toc",
            "language_basis": "title_heuristic",
            "issue_url": page_url,
            "year": int(issue_match[1]),
            "volume": issue_match[2],
            "issue": issue_match[3],
        }

        if first_page_match:
            extra["first_page"] = int(first_page_match[1])
            author_text = author_text[:first_page_match.start()].strip()

        authors = []

        if re.match(r"[А-ЯЁA-Z]\.\s*(?:[А-ЯЁA-Z]\.\s*)?[А-ЯЁA-Z]", author_text):
            authors = [author.strip() for author in author_text.split(",") if author.strip()]

        documents.append(Document(
            source=SOURCE_ID,
            url=pdf_url,
            title=title,
            text="",
            authors=authors,
            section=section,
            pdf_url=pdf_url,
            language="ru",
            extra=extra,
        ))

    return documents


def _parse_obituary(root: _Element, page_url: str) -> Document | None:
    """Подтвердить карточку некролога по независимым признакам, не извлекая полный текст."""

    articles = [element for element in root.elements() if element.tag == "article"]

    if len(articles) != 1 or "category-nekrolog" not in articles[0].attributes.get("class", "").split():
        return

    elements = articles[0].elements()
    headings = [element for element in elements
                if "entry-title" in element.attributes.get("class", "").split()]
    contents = [element for element in elements
                if "entry-content" in element.attributes.get("class", "").split()]
    footers = [element for element in elements
               if element.tag == "footer" and "entry-meta" in element.attributes.get("class", "").split()]

    if len(headings) != 1 or headings[0].tag != "h1" or len(contents) != 1 or len(footers) != 1:
        return

    categories = [element for element in footers[0].elements()
                  if element.tag == "a" and "category" in element.attributes.get("rel", "").split()]

    if (
        len(categories) != 1 or _normalized_text(categories[0]) != "Некролог" or
        urljoin(page_url, categories[0].attributes.get("href", "")) !=
        "https://quantum-electronics.ru/category/nekrolog/"
    ):
        return

    title = _normalized_text(headings[0])
    paragraphs = [element for element in contents[0].children
                  if isinstance(element, _Element) and element.tag == "p"]

    if not title.startswith("Памяти ") or not paragraphs:
        return

    notice = _normalized_text(paragraphs[0])

    if (
        any(element.tag == "a" for element in paragraphs[0].elements()) or
        _metadata_language(title, notice) != "ru" or
        re.search(r"\b(?:кончин[а-яё]*|скончал(?:ся|ась))\b", notice.casefold()) is None
    ):
        return

    # Извещение не является аннотацией, редактор WordPress — автором,
    # а дата смерти или обновления страницы — годом публикации.
    return Document(
        source=SOURCE_ID,
        url=page_url,
        title=title,
        text="",
        authors=[],
        section="Некролог",
        pdf_url=None,
        language="ru",
        extra={
            "journal": "Квантовая электроника",
            "content_role": "metadata_only",
            "metadata_source": "obituary_card",
            "publication_type": "obituary",
            "language_basis": "title_and_obituary_notice_heuristic",
        },
    )


def parse_article(html: str, page_url: str) -> Document:
    """Получить метаданные карточки; аннотация не считается полным текстом."""

    if classify_url(page_url) != "article":
        raise ValueError("Ожидается URL русской карточки статьи")

    root = _parse_html(html)
    article = next(
        (element for element in root.elements() if element.tag == "article"), None,
    )

    if article is None:
        raise ValueError("На странице нет элемента статьи")

    elements = article.elements()
    title_element = _with_class(elements, "entry-title")
    content = _with_class(elements, "entry-content")
    title = _normalized_text(title_element) if title_element is not None else ""

    if not title or content is None:
        raise ValueError("В карточке отсутствует заголовок или содержимое статьи")

    abstract = _labeled_text(content, "Аннотация")
    citation = _labeled_text(content, "Образец цитирования")

    # Смешанные карточки уже могли быть сохранены со старым набором полей.
    # Их дополнение требует отдельной версии; новый формат включаем только
    # там, где прежний разбор не находил ни одного из двух основных полей.
    if not abstract and not citation:
        abstract = _labeled_text(content, "Аннотация", allow_plain=True)
        citation = _labeled_text(content, "Образец цитирования", allow_plain=True)

    language = _metadata_language(title, abstract)

    if not abstract and not citation:
        obituary = _parse_obituary(root, page_url)

        if obituary is not None:
            return obituary

        raise ValueError("Страница не содержит полей научной статьи")

    extra: dict[str, object] = {
        "journal": "Квантовая электроника",
        "content_role": "metadata_only",
        "language_basis": "title_and_abstract_heuristic",
    }
    keywords = _labeled_text(content, "Ключевые слова")

    if keywords:
        extra["keywords"] = [
            keyword.strip() for keyword in keywords.rstrip(".").split(",")
            if keyword.strip()
        ]

    if citation:
        extra["citation"] = citation
        match = re.search(
            r"Квантовая электроника\s*,\s*([0-9]+)\s*:\s*([0-9]+)\s*"
            r"\(([12][0-9]{3})\)", citation,
        )

        if match:
            extra.update(volume=match[1], issue=match[2], year=int(match[3]))

    doi_text = _labeled_text(content, "DOI")
    doi_match = re.fullmatch(r"(?:https://(?:dx\.)?doi\.org/)?(10\.[0-9]{4,9}/\S+)", doi_text)

    if doi_match:
        extra["doi"] = doi_match[1]

    section = next(
        (
            _normalized_text(element) for element in elements
            if element.tag == "a" and
            "category" in element.attributes.get("rel", "").split()
        ),
        None,
    )

    pdf_url = None

    if language == "ru":
        pdf_url = _article_pdf(content, page_url)

    else:
        extra["skipped_non_russian"] = True

    return Document(
        source=SOURCE_ID,
        url=page_url,
        title=title,
        text=abstract,
        authors=_article_authors(content),
        section=section,
        pdf_url=pdf_url,
        language=language,
        extra=extra,
    )
