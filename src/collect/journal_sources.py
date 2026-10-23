"""Единый интерфейс проверенных страниц русских физических журналов."""

from __future__ import annotations

import importlib
import re

from dataclasses import dataclass
from types import ModuleType
from urllib.parse import urlsplit

from .base import Document, HttpResponseSnapshot

SOURCE_MODULES = {
    "jetp": "jetp",
    "jetp_letters": "jetp_letters",
    "quantum": "quantum_electronics",
    "ufn": "ufn_journal",
}


@dataclass(frozen=True)
class JournalSource:
    """Связать чистый разбор сайта с ограничениями адресов общего сборщика."""

    module: ModuleType

    @property
    def source_id(self) -> str:
        """Вернуть внутреннее имя источника для каталогов партии."""

        return self.module.SOURCE_ID

    @property
    def start_url(self) -> str:
        """Вернуть подтверждённый адрес русского архива."""

        return self.module.START_URL

    @property
    def default_encoding(self) -> str:
        """Вернуть кодировку сайта для ответа без явного charset."""

        return self.module.DEFAULT_ENCODING

    def validate_url(self, url: str) -> None:
        """Запретить посторонние узлы и неоднозначные адреса до обращения к сети."""

        if not isinstance(url, str) or not url or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in url
        ):
            raise ValueError("Адрес должен быть непустой строкой без пробелов")

        parsed = urlsplit(url)

        if (
            parsed.scheme not in {"https", "http"} or
            f"{parsed.scheme}://{parsed.netloc}" not in self.module.ALLOWED_ORIGINS or
            parsed.username is not None or parsed.password is not None or
            "#" in url or "\\" in url or "%" in url or
            any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise ValueError(f"Адрес вне проверенной публичной области источника: {url}")

        if parsed.path == "/robots.txt" and "?" not in url:
            return

        if self.module.classify_url(url) not in {"archive", "issue", "article", "pdf"}:
            raise ValueError(f"Неизвестный или служебный путь журнала: {url}")

    def classify_url(self, url: str) -> str | None:
        """Определить роль страницы только после проверки адреса."""

        self.validate_url(url)

        return self.module.classify_url(url)

    def extract_links(self, html: str, page_url: str) -> list[str]:
        """Получить явные ссылки, повторно проверив границы источника."""

        links = self.module.extract_links(html, page_url)

        for url in links:
            self.validate_url(url)

        return list(dict.fromkeys(links))

    def parse_article(self, html: str, page_url: str) -> Document:
        """Получить метаданные; PDF останется отдельным исходным артефактом."""

        return self.module.parse_article(html, page_url)

    def decode_body(self, body: bytes, encoding: str) -> str:
        """Прочитать HTML строго либо применить подтверждённое исключение источника."""

        decoder = getattr(self.module, "decode_body", None)

        return decoder(body, encoding) if decoder is not None else body.decode(encoding)

    def parse_issue_articles(self, html: str, page_url: str) -> list[Document]:
        """Разобрать оглавление, когда сайт публикует PDF без отдельных карточек."""

        parser = getattr(self.module, "parse_issue_articles", None)

        return parser(html, page_url) if parser is not None else []

    def parse_unlinked_issue(self, html: str, page_url: str) -> list[dict[str, object]]:
        """Распознать оглавление без ссылок, если адаптер умеет подтверждать такой формат."""

        parser = getattr(self.module, "parse_unlinked_issue", None)

        return parser(html, page_url) if parser is not None else []

    def accepted_statuses(self, url: str) -> frozenset[int]:
        """Признать отсутствие robots.txt и явно описанные ответы адаптера."""

        self.validate_url(url)

        if urlsplit(url).path == "/robots.txt":
            return frozenset({200, 404, 410})

        policy = getattr(self.module, "accepted_statuses", None)

        return policy(url) if policy is not None else frozenset({200})

    def resolve_article_redirect(self, snapshot: HttpResponseSnapshot) -> str | None:
        """Проверить разрешённое адаптером назначение, не выполняя сетевой переход."""

        self.validate_url(snapshot.requested_url)
        resolver = getattr(self.module, "resolve_article_redirect", None)
        target = resolver(snapshot) if resolver is not None else None

        if target is None:
            return

        if target == snapshot.requested_url or self.classify_url(target) != "article":
            raise ValueError("Перенаправление должно вести на другую публичную карточку")

        return target


def get_source(source_id: str) -> JournalSource:
    """Загрузить только явно поддержанный адаптер без сетевых обращений."""

    if source_id not in SOURCE_MODULES:
        raise ValueError(f"Неизвестный источник: {source_id}")

    source = JournalSource(importlib.import_module(f"src.collect.{SOURCE_MODULES[source_id]}"))

    if source.source_id != source_id or re.fullmatch(r"[a-z][a-z0-9_]*", source.source_id) is None:
        raise ValueError("Адаптер вернул несовместимое имя источника")

    source.validate_url(source.start_url)

    return source
