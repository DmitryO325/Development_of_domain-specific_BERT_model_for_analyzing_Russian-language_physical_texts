#!/usr/bin/env python3
"""
Сбор пилотного корпуса с сайтов физических журналов.

Примеры:
  python scripts/scrape.py ufn --max-docs 10 --text-source pdf
  python scripts/scrape.py ufn --text-source html --max-articles 3
  python scripts/scrape.py rss --feed https://quantum-electronics.ru/feed/ --limit 10
  python scripts/scrape.py pilot
"""

from __future__ import annotations

import argparse
import sys

from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Импорты проекта должны работать и при прямом запуске файла из scripts/.
sys.path.insert(0, str(PROJECT_ROOT))

from src.collect.base import Document, append_jsonl  # noqa: E402
from src.collect.rss_feed import DEFAULT_FEEDS, RssScraper  # noqa: E402
from src.collect.ufn import UfnScraper  # noqa: E402
from src.corpus.manifests import (  # noqa: E402
    ManifestConcurrencyError,
    ManifestStore,
)
from src.corpus.profiles import (  # noqa: E402
    SOURCE_PROFILES,
    SourceProfile,
    get_source_profile,
)
from src.corpus.registration import (  # noqa: E402
    ACQUISITION_SCOPES,
    CONTENT_ROLES,
    RegistrationOptions,
    plan_document,
    reconcile_document_plan,
    resolve_collection_rights,
)

DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "corpus.jsonl"
DEFAULT_PILOT_OUTPUT_PATH = PROJECT_ROOT / "data" / "raw" / "pilot.jsonl"
MAX_COMMIT_ATTEMPTS = 3


class DocumentRecorder:
    """Сохранять документы в прототипный JSONL или рабочие реестры."""

    def __init__(
        self,
        arguments: argparse.Namespace,
        profiles: Iterable[SourceProfile],
        *,
        project_root: Path = PROJECT_ROOT,
    ) -> None:
        """Проверить режим записи и права до начала сетевого сбора."""

        self.output_path = Path(arguments.output)
        self.registration_enabled = bool(
            getattr(arguments, "register_manifests", False)
        )
        self.profile_name = getattr(arguments, "profile", "auto")
        self.store: ManifestStore | None = None
        self.options_by_profile: dict[str, RegistrationOptions] = {}
        self._fresh_pending = False

        if not self.registration_enabled:
            self._fresh_pending = bool(arguments.fresh)
            return

        if arguments.fresh:
            raise ValueError(
                "--fresh несовместим с неизменяемой регистрацией в реестрах"
            )

        required_values = {
            "--content-role": getattr(arguments, "content_role", None),
            "--acquisition-scope": getattr(arguments, "acquisition_scope", None),
            "--extraction-method": getattr(arguments, "extraction_method", None),
            "--extraction-version": getattr(arguments, "extraction_version", None),
        }
        missing = [name for name, value in required_values.items() if not value]

        if missing:
            raise ValueError(
                "Для --register-manifests обязательны параметры: "
                + ", ".join(missing)
            )

        if arguments.content_role == "full_text":
            raise ValueError(
                "Текущие команды scrape.py ещё не передают исходный "
                "HttpResponseSnapshot; роль full_text здесь запрещена"
            )

        _validate_collection_semantics(arguments)

        rights_record_ids = tuple(
            getattr(arguments, "rights_record_id", None) or ()
        )
        manifest_dir = Path(arguments.manifest_dir)
        self.store = ManifestStore(
            project_root=project_root,
            manifest_dir=manifest_dir,
            schema_dir=PROJECT_ROOT / "manifests" / "schemas",
        )

        for profile in profiles:
            selected_rights = resolve_collection_rights(
                self.store,
                profile,
                acquisition_method=arguments.acquisition_method,
                acquisition_scope=arguments.acquisition_scope,
                allowed_rights_record_ids=rights_record_ids,
            )
            self.options_by_profile[profile.key] = RegistrationOptions(
                content_role=arguments.content_role,
                acquisition_method=arguments.acquisition_method,
                acquisition_scope=arguments.acquisition_scope,
                rights_record_ids=selected_rights,
                extraction_method=arguments.extraction_method,
                extraction_version=arguments.extraction_version,
                response_representation="plain_text",
                request_context_type="work",
            )

    @property
    def destination(self) -> Path:
        """Вернуть каталог реестров или путь прототипного JSONL."""

        if self.store is not None:
            return self.store.manifest_dir

        return self.output_path

    def save(self, document: Document) -> bool:
        """Сохранить один содержательный документ выбранным способом."""

        if document.extra.get("skipped") or len(document.text.strip()) < 30:
            return False

        if not self.registration_enabled:
            if self._fresh_pending:
                if self.output_path.exists():
                    self.output_path.unlink()

                self._fresh_pending = False

            append_jsonl(document, self.output_path)
            return True

        if self.store is None:
            raise RuntimeError("Хранилище реестров не было подготовлено")

        profile = get_source_profile(
            self.profile_name,
            source=document.source,
            url=document.url,
        )

        try:
            options = self.options_by_profile[profile.key]

        except KeyError as exception:
            raise ValueError(
                f"Профиль {profile.key!r} не был проверен до начала сбора"
            ) from exception

        collected_at = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        )
        plan = plan_document(
            document,
            profile,
            options,
            collected_at,
        )
        for attempt in range(MAX_COMMIT_ATTEMPTS):
            reconciled, expected_hashes = reconcile_document_plan(
                self.store,
                plan,
            )

            try:
                self.store.commit(
                    reconciled,
                    expected_snapshot_hashes=expected_hashes,
                )
                break

            except ManifestConcurrencyError as exception:
                if attempt + 1 == MAX_COMMIT_ATTEMPTS:
                    raise ManifestConcurrencyError(
                        "Реестры несколько раз изменились параллельно; "
                        "документ не записан"
                    ) from exception

        return True


def _save_documents(
    documents: Iterable[Document],
    recorder: DocumentRecorder,
) -> int:
    """Сохранить содержательные документы и вернуть их число."""

    saved_count = 0

    for document in documents:
        if recorder.save(document):
            saved_count += 1

    return saved_count


def _validate_collection_semantics(arguments: argparse.Namespace) -> None:
    """Сверить заявленный способ и масштаб с реальным обходом."""

    if arguments.acquisition_method != "crawler":
        raise ValueError(
            "scrape.py выполняет автоматический обход; "
            "--acquisition-method должен быть crawler"
        )

    if arguments.acquisition_scope == "single":
        raise ValueError(
            "scrape.py выполняет пакетный обход; "
            "--acquisition-scope single для него неверен"
        )

    command = getattr(arguments, "command", None)
    expected_scope: str | None = None

    if command in {"rss", "pilot"}:
        expected_scope = "sample"

    elif command == "ufn":
        bounded = (
            getattr(arguments, "max_docs", None) is not None
            or getattr(arguments, "max_issues", None) is not None
        )
        expected_scope = "sample" if bounded else "bulk"

    if expected_scope and arguments.acquisition_scope != expected_scope:
        raise ValueError(
            f"Для команды {command!r} фактический масштаб получения "
            f"равен {expected_scope!r}, а не {arguments.acquisition_scope!r}"
        )


def _ufn_profiles(arguments: argparse.Namespace) -> tuple[SourceProfile, ...]:
    """Вернуть заранее известный профиль команды УФН."""

    profile_name = getattr(arguments, "profile", "auto")
    profile = get_source_profile(
        profile_name,
        source="ufn.ru",
        url="https://ufn.ru/",
    )

    return (profile,)


def _rss_profiles(arguments: argparse.Namespace) -> tuple[SourceProfile, ...]:
    """Определить все профили RSS-команды до сетевых запросов."""

    profile_name = getattr(arguments, "profile", "auto")

    if arguments.feed:
        source = urlsplit(arguments.feed).hostname or ""
        return (
            get_source_profile(
                profile_name,
                source=source,
                url=arguments.feed,
            ),
        )

    profiles: dict[str, SourceProfile] = {}

    for name, url in DEFAULT_FEEDS.items():
        profile = get_source_profile(
            profile_name,
            source=name,
            url=url,
        )
        profiles[profile.key] = profile

    return tuple(profiles[key] for key in sorted(profiles))


def cmd_ufn(arguments: argparse.Namespace) -> None:
    """Выполнить сбор статей и RSS-карточек УФН."""

    profiles = (
        _ufn_profiles(arguments)
        if getattr(arguments, "register_manifests", False)
        else ()
    )
    recorder = DocumentRecorder(arguments, profiles)
    scraper = UfnScraper(
        delay_seconds=arguments.delay,
        text_mode=arguments.text_source,
        pdf_dir=Path(arguments.pdf_dir),
        pdf_text_dir=Path(arguments.pdf_text_dir),
        min_pdf_chars=arguments.min_pdf_chars,
        try_ocr=not arguments.no_ocr,
    )

    saved_count = 0

    if arguments.rss:
        documents = scraper.parse_rss(limit=arguments.rss_limit)
        saved_count += _save_documents(documents, recorder)
        print(f"RSS: сохранено {saved_count} записей → {recorder.destination}")

    max_issues = arguments.max_issues

    if arguments.max_docs is not None and max_issues is None:
        print(
            f"Цель: {arguments.max_docs} статей — обход выпусков "
            "без лимита по числу номеров"
        )

    elif max_issues:
        print(f"Лимит: не более {max_issues} последних выпусков")

    for document in scraper.iter_articles(
        max_issues=max_issues,
        max_articles_per_issue=arguments.max_articles,
        max_docs=arguments.max_docs,
    ):
        if not recorder.save(document):
            continue

        saved_count += 1
        text_source = document.extra.get("text_source", "?")

        print(
            f"  [{saved_count}] {document.title[:60]}… "
            f"[{text_source}, {len(document.text)} симв.]"
        )

    print(f"Итого UFN: {saved_count} документов → {recorder.destination}")


def cmd_rss(arguments: argparse.Namespace) -> None:
    """Выполнить сбор из одной или всех штатных RSS-лент."""

    profiles = (
        _rss_profiles(arguments)
        if getattr(arguments, "register_manifests", False)
        else ()
    )
    recorder = DocumentRecorder(arguments, profiles)
    scraper = RssScraper(delay_seconds=arguments.delay)

    if arguments.feed:
        documents = scraper.parse_feed(arguments.feed, limit=arguments.limit)
        saved_count = _save_documents(documents, recorder)
        source_name = urlsplit(arguments.feed).hostname or arguments.feed
        print(f"  {source_name}: {len(documents)} элементов ленты")

    else:
        documents = list(
            scraper.iter_default_feeds(limit_per_feed=arguments.limit)
        )

        for document in documents:
            if document.extra.get("skipped"):
                error_message = document.extra.get("error", "неизвестная ошибка")
                print(f"  {document.source}: {error_message}")

        saved_count = _save_documents(documents, recorder)

    print(f"Итого RSS: {saved_count} документов → {recorder.destination}")


def cmd_pilot(arguments: argparse.Namespace) -> None:
    """Небольшой прогон для проверки: 1 выпуск УФН + RSS."""

    arguments.max_issues = 1
    arguments.max_articles = 4
    arguments.max_docs = 4
    arguments.rss = False
    arguments.rss_limit = 0
    arguments.text_source = "pdf+html"
    arguments.pdf_dir = str(PROJECT_ROOT / "data" / "raw" / "pdf")
    arguments.pdf_text_dir = str(PROJECT_ROOT / "data" / "raw" / "pdf_text")
    arguments.min_pdf_chars = 500
    arguments.no_ocr = False
    arguments.feed = None
    arguments.limit = 8

    if getattr(arguments, "register_manifests", False):
        profiles = {
            profile.key: profile
            for profile in (
                *_ufn_profiles(arguments),
                *_rss_profiles(arguments),
            )
        }
        DocumentRecorder(
            arguments,
            (profiles[key] for key in sorted(profiles)),
        )

    cmd_ufn(arguments)

    arguments.fresh = False
    cmd_rss(arguments)

    if getattr(arguments, "register_manifests", False):
        print(f"\nПилот готов: рабочие реестры в {arguments.manifest_dir}")
        return

    # Статистика прототипного JSONL.
    output_path = Path(arguments.output)

    content = (
        output_path.read_text(encoding="utf-8").strip()
        if output_path.exists()
        else ""
    )

    line_count = len(content.splitlines()) if content else 0
    print(f"\nПилот готов: {line_count} записей в {output_path}")


def main() -> None:
    """Разобрать аргументы командной строки и запустить выбранный сбор."""

    parser = argparse.ArgumentParser(description="Сбор текстов с сайтов НИР")
    common_parser = argparse.ArgumentParser(add_help=False)

    common_parser.add_argument(
        "--output",
        "-o",
        default=str(DEFAULT_OUTPUT_PATH),
        help=(
            "JSONL-файл (data/raw/corpus.jsonl; "
            "для pilot — data/raw/pilot.jsonl)"
        ),
    )

    common_parser.add_argument(
        "--delay", type=float, default=1.0, help="Пауза между запросами (с)"
    )

    common_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Очистить выходной файл перед первой успешной записью",
    )

    common_parser.add_argument(
        "--register-manifests",
        action="store_true",
        help=(
            "Записывать новые результаты прямо в рабочие реестры вместо "
            "прототипного JSONL"
        ),
    )

    common_parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=PROJECT_ROOT / "manifests",
        help="Каталог рабочих реестров для --register-manifests",
    )

    common_parser.add_argument(
        "--rights-record-id",
        action="append",
        help=(
            "Явно разрешённый ID записи rights; параметр можно повторять"
        ),
    )

    common_parser.add_argument(
        "--content-role",
        choices=sorted(CONTENT_ROLES - {"full_text"}),
        help=(
            "Явная роль текста при прямой регистрации; full_text появится "
            "после подключения исходных HTTP-снимков"
        ),
    )

    common_parser.add_argument(
        "--acquisition-method",
        choices=["crawler"],
        default="crawler",
        help="Фактический способ получения: crawler",
    )

    common_parser.add_argument(
        "--acquisition-scope",
        choices=sorted(ACQUISITION_SCOPES - {"single"}),
        help="Фактический масштаб получения при прямой регистрации",
    )

    common_parser.add_argument(
        "--extraction-method",
        help="Явное имя метода извлечения текста",
    )

    common_parser.add_argument(
        "--extraction-version",
        help="Версия метода извлечения текста",
    )

    common_parser.add_argument(
        "--profile",
        choices=["auto", *sorted(SOURCE_PROFILES)],
        default="auto",
        help="Явный профиль источника или его однозначный автовыбор",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    ufn_parser = subparsers.add_parser(
        "ufn", parents=[common_parser], help="ufn.ru — выпуски и HTML-статьи"
    )

    ufn_parser.add_argument(
        "--max-issues",
        type=int,
        default=None,
        help=(
            "Сколько последних выпусков (по умолчанию без лимита; "
            "с --max-docs идём по архиву до N статей)"
        ),
    )

    ufn_parser.add_argument(
        "--max-articles",
        type=int,
        default=None,
        help="Статей на выпуск (по умолчанию все)",
    )

    ufn_parser.add_argument(
        "--max-docs",
        type=int,
        default=None,
        help="Остановиться после N статей (например 100)",
    )

    ufn_parser.add_argument(
        "--rss", action="store_true", help="Дополнительно RSS ufn.ru"
    )

    ufn_parser.add_argument("--rss-limit", type=int, default=20)

    ufn_parser.add_argument(
        "--text-source",
        choices=["pdf", "html", "pdf+html"],
        default="pdf+html",
        help=(
            "pdf+html (по умолч.): PDF если кириллица ок, иначе HTML; "
            "ufn.ru PDF часто без Unicode"
        ),
    )

    ufn_parser.add_argument(
        "--pdf-dir",
        default=str(PROJECT_ROOT / "data" / "raw" / "pdf"),
        help="Куда сохранять PDF",
    )

    ufn_parser.add_argument(
        "--min-pdf-chars",
        type=int,
        default=500,
        help="Мин. длина текста из PDF",
    )

    ufn_parser.add_argument(
        "--pdf-text-dir",
        default=str(PROJECT_ROOT / "data" / "raw" / "pdf_text"),
        help="Куда писать .txt из PDF (видно, что распарсилось)",
    )

    ufn_parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Без Tesseract (на ufn.ru PDF без OCR обычно нечитаемы)",
    )
    ufn_parser.set_defaults(func=cmd_ufn)

    rss_parser = subparsers.add_parser(
        "rss", parents=[common_parser], help="RSS-ленты журналов"
    )

    rss_parser.add_argument("--feed", help="URL одной ленты")
    rss_parser.add_argument("--limit", type=int, default=20)
    rss_parser.set_defaults(func=cmd_rss)

    pilot_parser = subparsers.add_parser(
        "pilot",
        parents=[common_parser],
        help="Пилотный прогон (1 выпуск + RSS)",
    )

    pilot_parser.set_defaults(
        func=cmd_pilot,
        output=str(DEFAULT_PILOT_OUTPUT_PATH),
    )

    arguments = parser.parse_args()
    arguments.func(arguments)


if __name__ == "__main__":
    main()
