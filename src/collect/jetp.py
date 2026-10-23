"""Разбор публичного русского архива ЖЭТФ без сетевых запросов."""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .base import Document, HttpResponseSnapshot

SOURCE_ID = "jetp"
START_URL = "https://www.jetp.ras.ru/cgi-bin/r/index"
ALLOWED_ORIGINS = ("https://www.jetp.ras.ru",)
DEFAULT_ENCODING = "koi8-r"
JOURNAL_NAME = "Журнал экспериментальной и теоретической физики"

INDEX_PATH = "/cgi-bin/r/index"
NUMBER = r"[1-9][0-9]*"
ARTICLE_PATH = re.compile(rf"{INDEX_PATH}/(?:r/)?({NUMBER})/({NUMBER})/p({NUMBER})")
PDF_PATH = re.compile(rf"/cgi-bin/dn/r_({NUMBER})_([0-9]{{4,5}})\.pdf")
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
HIDDEN_TAGS = {"script", "style", "noscript"}
BLOCK_TAGS = {"br", "div", "p", "tr", "td", "li"}


@dataclass
class _Element:
    """Элемент опубликованной HTML-карточки и его текстовые потомки."""

    tag: str
    attributes: dict[str, str]
    children: list[_Element | str] = field(default_factory=list)
    duplicate_attributes: bool = False

    def text(self) -> str:
        """Собрать видимый текст без содержимого скриптов и стилей."""

        if self.tag in HIDDEN_TAGS:
            return ""

        text = "".join(
            child.text() if isinstance(child, _Element) else child
            for child in self.children
        )

        return f" {text} " if self.tag in BLOCK_TAGS else text


class _PageParser(HTMLParser):
    """Прочитать элементы HTML, не исполняя сценарии и не загружая ресурсы."""

    def __init__(self) -> None:
        """Подготовить корень документа и порядок его элементов."""

        super().__init__(convert_charrefs=True)
        self.root = _Element("root", {})
        self.elements: list[_Element] = []
        self.stack = [self.root]

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Добавить открывающий тег и его атрибуты."""

        attributes = {name: value or "" for name, value in attrs}
        element = _Element(tag, attributes, duplicate_attributes=len(attributes) != len(attrs))
        self.stack[-1].children.append(element)

        if not any(parent.tag in HIDDEN_TAGS for parent in self.stack):
            self.elements.append(element)

        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Обработать самозакрывающийся элемент."""

        self.handle_starttag(tag, attrs)

        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Закрыть соответствующий контейнер с учётом незакрытых HTML-тегов."""

        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """Сохранить текст текущего элемента."""

        self.stack[-1].children.append(data)


def _parse_html(html: str) -> _PageParser:
    """Проверить строковый вход и разобрать опубликованный HTML."""

    if not isinstance(html, str):
        raise ValueError("HTML ЖЭТФ должен быть строкой")

    parser = _PageParser()
    parser.feed(html)
    parser.close()

    return parser


def _has_unsafe_url_syntax(url: str) -> bool:
    """Выявить неоднозначное кодирование, управляющие символы и обход пути."""

    return (
        not url or
        any(character.isspace() or ord(character) < 32 for character in url) or
        any(character in url for character in "\\%#") or
        any(part in {".", ".."} for part in url.split("/"))
    )


def classify_url(url: str) -> str | None:
    """Распознать только проверенные публичные маршруты русской версии ЖЭТФ."""

    if not isinstance(url, str) or _has_unsafe_url_syntax(url):
        return

    try:
        parsed = urlsplit(url)

    except ValueError:
        return

    if f"{parsed.scheme}://{parsed.netloc}" not in ALLOWED_ORIGINS:
        return

    if PDF_PATH.fullmatch(parsed.path):
        return "pdf" if "?" not in url else None

    if parsed.query not in {"", "a=list"} or ("?" in url and not parsed.query):
        return

    if parsed.path == INDEX_PATH:
        return "archive"

    if re.fullmatch(rf"{INDEX_PATH}/r/{NUMBER}", parsed.path):
        return "archive"

    if re.fullmatch(rf"{INDEX_PATH}/(?:r/)?{NUMBER}/{NUMBER}", parsed.path):
        return "issue"

    if ARTICLE_PATH.fullmatch(parsed.path):
        return "article"

    return


def _resolve_link(reference: str, page_url: str) -> str | None:
    """Разрешить опубликованную ссылку без изменения узла или маршрута."""

    if _has_unsafe_url_syntax(reference):
        return

    try:
        url = urljoin(page_url, reference)

    except ValueError:
        return

    return url if classify_url(url) is not None else None


def extract_links(html: str, page_url: str) -> list[str]:
    """Собрать явные ссылки; не превращать POST-формы и номера томов в URL."""

    if classify_url(page_url) not in {"archive", "issue", "article"}:
        raise ValueError("Ожидается URL русской HTML-страницы архива ЖЭТФ")

    parser = _parse_html(html)
    urls: list[str] = []

    for element in parser.elements:
        if element.tag != "a":
            continue

        url = _resolve_link(element.attributes.get("href", ""), page_url)

        if url is not None and url not in urls:
            urls.append(url)

    return urls


def _has_class(element: _Element, class_name: str) -> bool:
    """Проверить отдельное имя класса HTML-элемента."""

    return class_name in element.attributes.get("class", "").split()


def _descendants(element: _Element) -> list[_Element]:
    """Получить вложенные элементы в порядке документа."""

    result: list[_Element] = []
    pending = list(reversed(element.children))

    while pending:
        child = pending.pop()

        if isinstance(child, _Element) and child.tag not in HIDDEN_TAGS:
            result.append(child)
            pending.extend(reversed(child.children))

    return result


def _validate_volume(volume: int) -> None:
    """Отклонить логическое значение и неположительный номер тома."""

    if type(volume) is not int or volume <= 0:
        raise ValueError("Номер тома ЖЭТФ должен быть положительным целым числом")


def _volume_options(select: _Element, volume: int) -> None:
    """Подтвердить единственный доступный вариант тома в опубликованном списке."""

    if select.attributes != {"name": "volume"}:
        raise ValueError("Неожиданные атрибуты списка томов ЖЭТФ")

    options: dict[str, _Element] = {}

    for child in select.children:
        if isinstance(child, str):
            if child.strip():
                raise ValueError("Неожиданный текст вне вариантов тома ЖЭТФ")

            continue

        value = child.attributes.get("value", "")

        if (
            child.tag != "option" or child.duplicate_attributes or
            set(child.attributes) - {"value", "selected", "disabled"} or
            re.fullmatch(NUMBER, value) is None or value in options or
            any(isinstance(part, _Element) for part in child.children)
        ):
            raise ValueError("Неоднозначные или неподдержанные варианты томов ЖЭТФ")

        options[value] = child

    selected = options.get(str(volume))

    if selected is None or "disabled" in selected.attributes:
        raise ValueError(f"Том {volume} отсутствует среди доступных вариантов формы ЖЭТФ")


def _validate_volume_form(form: _Element, volume: int) -> None:
    """Проверить только известные поля русской формы выбора тома."""

    if (
        form.duplicate_attributes or
        set(form.attributes) - {"action", "method", "enctype"} or
        form.attributes.get("action") not in {INDEX_PATH, START_URL} or
        form.attributes.get("method", "").lower() != "post" or
        form.attributes.get("enctype", "application/x-www-form-urlencoded").lower() !=
        "application/x-www-form-urlencoded"
    ):
        raise ValueError("Не подтверждены адрес, метод или формат формы ЖЭТФ")

    fields: dict[str, str] = {}
    selectors: list[_Element] = []
    buttons = 0

    for child in form.children:
        if isinstance(child, str):
            if child.strip():
                raise ValueError("Неожиданный текст в форме выбора тома ЖЭТФ")

            continue

        if child.duplicate_attributes:
            raise ValueError("Повторяющиеся атрибуты поля формы ЖЭТФ")

        if child.tag == "select":
            _volume_options(child, volume)
            selectors.append(child)
            continue

        if child.tag != "input":
            raise ValueError("Неподдержанный элемент формы выбора тома ЖЭТФ")

        attributes = child.attributes
        input_type = attributes.get("type", "").lower()

        if input_type == "image":
            if set(attributes) - {"type", "src", "align", "alt", "width", "height", "border"}:
                raise ValueError("Кнопка формы ЖЭТФ содержит дополнительные поля или переопределения")

            buttons += 1
            continue

        name = attributes.get("name", "")

        if (
            input_type != "hidden" or set(attributes) != {"type", "name", "value"} or
            name not in {"a", "issues_lang"} or name in fields
        ):
            raise ValueError("Неожиданное или повторяющееся поле формы ЖЭТФ")

        fields[name] = attributes["value"]

    if fields != {"a": "select", "issues_lang": "r"} or len(selectors) != 1 or buttons > 1:
        raise ValueError("Не найдены единственные поля русской формы выбора тома ЖЭТФ")


def _published_volume_form(html: str, page_url: str) -> _Element:
    """Найти единственную русскую форму, сохранив проверки её контекста."""

    if page_url != START_URL:
        raise ValueError("Форма томов ЖЭТФ должна происходить с начальной страницы архива")

    parser = _parse_html(html)
    forms: list[_Element] = []

    for element in parser.elements:
        if element.tag != "form":
            continue

        descendants = _descendants(element)

        if not any(child.tag == "select" and
                   (child.attributes.get("name") == "volume" or child.duplicate_attributes)
                   for child in descendants):
            continue

        languages = [child for child in descendants if child.attributes.get("name") == "issues_lang"]

        if (
            len(languages) == 1 and not languages[0].duplicate_attributes and
            languages[0].tag == "input" and
            languages[0].attributes.get("type", "").lower() == "hidden" and
            languages[0].attributes.get("value") == "e"
        ):
            continue

        forms.append(element)

    if len(forms) != 1:
        raise ValueError("Не найдена единственная русская форма выбора тома ЖЭТФ")

    form = forms[0]

    if any(
        element.tag == "base" or
        (element.tag == "fieldset" and "disabled" in element.attributes and
         any(child is form for child in _descendants(element)))
        for element in parser.elements
    ):
        raise ValueError("Контекст страницы изменяет адрес или доступность формы ЖЭТФ")

    return form


def build_volume_form(html: str, page_url: str, volume: int) -> bytes:
    """Сформировать POST только по единственной опубликованной русской форме."""

    _validate_volume(volume)
    form = _published_volume_form(html, page_url)
    _validate_volume_form(form, volume)

    return f"a=select&issues_lang=r&volume={volume}".encode("ascii")


def extract_volumes(html: str, page_url: str) -> list[int]:
    """Извлечь доступные русские тома без достраивания отсутствующих номеров."""

    form = _published_volume_form(html, page_url)
    selectors = [
        child for child in form.children
        if isinstance(child, _Element) and child.tag == "select"
    ]

    if len(selectors) != 1:
        raise ValueError("Не найден единственный список русских томов ЖЭТФ")

    volumes = [
        int(child.attributes["value"])
        for child in selectors[0].children
        if isinstance(child, _Element) and child.tag == "option" and
        "disabled" not in child.attributes and
        re.fullmatch(NUMBER, child.attributes.get("value", "")) is not None
    ]

    if not volumes:
        raise ValueError("Форма ЖЭТФ не содержит доступных русских томов")

    # Каждый номер подтверждается тем же строгим разбором, что и реальная
    # отправка формы: нельзя запланировать больше, чем разрешено сборщику.
    for volume in volumes:
        build_volume_form(html, page_url, volume)

    return sorted(volumes, reverse=True)


def volume_redirect(snapshot: HttpResponseSnapshot, volume: int) -> str:
    """Проверить единственный наблюдавшийся переход 302 к выбранному русскому тому."""

    _validate_volume(volume)

    if (
        not isinstance(snapshot, HttpResponseSnapshot) or
        snapshot.requested_url != START_URL or snapshot.final_url != START_URL or
        type(snapshot.status_code) is not int or snapshot.status_code != 302
    ):
        raise ValueError("Ожидался исходный HTTP 302 формы выбора тома ЖЭТФ")

    if not isinstance(snapshot.headers, tuple) or any(
        not isinstance(header, tuple) or len(header) != 2 or
        not all(isinstance(value, str) for value in header)
        for header in snapshot.headers
    ):
        raise ValueError("Некорректные заголовки ответа формы ЖЭТФ")

    locations = [value for name, value in snapshot.headers if name.lower() == "location"]

    if len(locations) != 1 or _has_unsafe_url_syntax(locations[0]) or "\x7f" in locations[0]:
        raise ValueError("Ожидался единственный безопасный заголовок Location формы ЖЭТФ")

    target = urljoin(START_URL, locations[0])

    if target != f"{START_URL}/r/{volume}?a=list":
        raise ValueError("Перенаправление формы ЖЭТФ не соответствует выбранному русскому тому")

    return target


def validate_volume_page(html: str, page_url: str, volume: int) -> None:
    """Сверить точный адрес и единственный заголовок выбранного русского тома."""

    _validate_volume(volume)

    if page_url != f"{START_URL}/r/{volume}?a=list":
        raise ValueError("Адрес оглавления ЖЭТФ не соответствует выбранному тому")

    parser = _parse_html(html)
    headings = [element for element in parser.elements if _has_class(element, "issue")]

    if len(headings) != 1 or re.match(
        rf"^ЖЭТФ,\s*Том\s+{volume},\s*[12][0-9]{{3}}(?:\s|$)",
        " ".join(headings[0].text().split()),
    ) is None:
        raise ValueError("Не найден единственный заголовок выбранного русского тома ЖЭТФ")


def _class_text(elements: list[_Element], class_name: str) -> str:
    """Прочитать единственное непустое значение заданного поля карточки."""

    values = list(dict.fromkeys(
        " ".join(element.text().split())
        for element in elements
        if _has_class(element, class_name) and element.text().strip()
    ))

    return values[0] if len(values) == 1 else ""


def _bibliographic_context(
    parser: _PageParser, volume: int, issue: int, first_page: int
) -> tuple[str | None, int | None]:
    """Проверить библиографическую строку, исключив год английского перевода."""

    heading = _class_text(parser.elements, "issue")
    expected = re.compile(
        rf"^ЖЭТФ,\s*Том\s+{volume},\s*Вып\.\s*{issue},\s*"
        rf"стр\.\s*{first_page}\s*\([^)]*?\b([12][0-9]{{3}})\)"
    )
    matched = expected.search(heading)

    if matched is None:
        return None, None

    return JOURNAL_NAME, int(matched.group(1))


def _authors(elements: list[_Element]) -> list[str]:
    """Собрать авторские ссылки только внутри авторского поля статьи."""

    authors: list[str] = []

    for container in elements:
        if not _has_class(container, "author"):
            continue

        for element in _descendants(container):
            if element.tag != "a" or not _has_class(element, "authorlink"):
                continue

            author = " ".join(element.text().split())

            if author and author not in authors:
                authors.append(author)

    return authors


def _doi(elements: list[_Element]) -> str | None:
    """Извлечь DOI из подписанного поля, не из аннотации или списка литературы."""

    values: set[str] = set()

    for element in elements:
        if not _has_class(element, "pacs"):
            continue

        text = " ".join(element.text().split())
        matched = re.fullmatch(r"DOI:\s*(10\.[0-9]{4,9}/[^\s<>\"']+)", text)

        if matched:
            values.add(matched.group(1))

    return next(iter(values)) if len(values) == 1 else None


def _pdf_url(
    elements: list[_Element], page_url: str, volume: int, first_page: int
) -> str | None:
    """Принять явный русский PDF с совпадающими томом и первой страницей."""

    urls: set[str] = set()

    for element in elements:
        if element.tag != "a":
            continue

        url = _resolve_link(element.attributes.get("href", ""), page_url)

        if url is None or classify_url(url) != "pdf":
            continue

        matched = PDF_PATH.fullmatch(urlsplit(url).path)

        if (
            matched is not None and
            int(matched.group(1)) == volume and
            int(matched.group(2)) == first_page
        ):
            urls.add(url)

    return next(iter(urls)) if len(urls) == 1 else None


def parse_article(html: str, page_url: str) -> Document:
    """Прочитать название и аннотацию карточки; не объявлять HTML полным текстом."""

    if classify_url(page_url) != "article":
        raise ValueError("Ожидается URL опубликованной русской статьи ЖЭТФ")

    matched = ARTICLE_PATH.fullmatch(urlsplit(page_url).path)

    if matched is None:
        raise ValueError("Не удалось определить том и страницу статьи ЖЭТФ")

    volume, issue, first_page = (int(value) for value in matched.groups())
    parser = _parse_html(html)
    containers = [
        element for element in parser.elements
        if element.tag == "p" and
        (_has_class(element, "article0") or _has_class(element, "article1"))
    ]

    if len(containers) != 1:
        raise ValueError("Не найдена единственная карточка статьи ЖЭТФ")

    elements = _descendants(containers[0])
    title = _class_text(elements, "atitle")

    if not title:
        raise ValueError("В карточке ЖЭТФ не найдено однозначное название статьи")

    journal, year = _bibliographic_context(parser, volume, issue, first_page)
    abstract = _class_text(elements, "abstract")
    language = "und"

    if re.search(r"[А-Яа-яЁё]", f"{title} {abstract}"):
        language = "ru"

    elif re.search(r"[A-Za-z]", f"{title} {abstract}"):
        language = "en"

    return Document(
        source=SOURCE_ID,
        url=page_url,
        title=title,
        text=abstract,
        authors=_authors(elements),
        published=str(year) if year is not None else None,
        pdf_url=(
            _pdf_url(elements, page_url, volume, first_page)
            if language == "ru" else None
        ),
        language=language,
        extra={
            "article_id": f"r/{volume}/{issue}/p{first_page}",
            "journal": journal,
            "year": year,
            "volume": volume,
            "issue": issue,
            "first_page": first_page,
            "doi": _doi(elements),
            "content_role": "metadata_only",
        },
    )
