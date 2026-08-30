"""Детерминированные идентификаторы работ и файлов корпуса."""

from __future__ import annotations

import re
import unicodedata
import uuid

from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

# UUID получен один раз как uuid5(NAMESPACE_DNS,
# "ruphysbert.manifests.work.v1"). Его нельзя менять внутри works-v1: иначе
# одна работа получит разные резервные work_id.
WORK_ID_NAMESPACE = uuid.UUID("1d018193-a92a-56b0-bcd2-2b29309b7a96")


@dataclass(frozen=True)
class WorkIdentity:
    """Результат выбора work_id и уровень надёжности этого выбора."""

    work_id: str
    confidence: str
    basis: str


def canonicalize_url(value: str) -> str:
    """Нормализовать URL и убрать фрагмент и известные параметры слежения."""

    raw = value.strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()

    port = parts.port

    default_http_port = scheme == "http" and port == 80
    default_https_port = scheme == "https" and port == 443

    if port and not (default_http_port or default_https_port):
        netloc = f"{hostname}:{port}"

    else:
        netloc = hostname

    path = re.sub(r"/{2,}", "/", parts.path or "/")

    if path != "/":
        path = path.rstrip("/")

    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in {"fbclid", "gclid"}
        ),
        doseq=True,
    )

    return urlunsplit((scheme, netloc, path, query, ""))


def normalize_doi(value: str | None) -> str | None:
    """Нормализовать DOI без doi.org, регистра, query и fragment."""

    if not value:
        return

    raw = unquote(value.strip())
    raw = re.sub(r"^doi\s*:\s*", "", raw, flags=re.IGNORECASE)

    if re.match(r"^https?://", raw, flags=re.IGNORECASE):
        parts = urlsplit(raw)

        if (parts.hostname or "").lower() not in {"doi.org", "dx.doi.org"}:
            return

        raw = parts.path.lstrip("/")

    else:
        raw = raw.split("#", 1)[0].split("?", 1)[0]

    raw = raw.strip().rstrip(".,;").casefold()

    if not re.fullmatch(r"10\.\d{4,9}/\S+", raw):
        return

    return raw


def normalize_native_id(value: str | None) -> str | None:
    """Привести собственный ID источника к безопасному устойчивому виду."""

    if not value:
        return

    normalized = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = re.sub(r"[^a-zа-яё0-9._:/-]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-:/")

    return normalized or None


def normalize_identity_text(value: str | None) -> str:
    """Точная нормализация поля резервного UUIDv5."""

    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = re.sub(r"[^a-zа-яё0-9]+", " ", normalized)

    return " ".join(normalized.split())


def resolve_work_identity(
    *,
    source_id: str,
    title: str,
    authors: list[str],
    year: str | int | None,
    doi: str | None = None,
    native_id: str | None = None,
) -> WorkIdentity:
    """Выбрать work_id по правилу DOI → ID источника → UUIDv5."""

    normalized_doi = normalize_doi(doi)

    if normalized_doi:
        return WorkIdentity(
            work_id=f"doi:{normalized_doi}",
            confidence="high",
            basis="doi",
        )

    normalized_native_id = normalize_native_id(native_id)

    if normalized_native_id:
        return WorkIdentity(
            work_id=f"source:{source_id}:{normalized_native_id}",
            confidence="medium",
            basis="source_native_id",
        )

    identity_key = "\x1f".join(
        (
            source_id,
            normalize_identity_text(title),
            normalize_identity_text(authors[0] if authors else ""),
            str(year or "").strip(),
        )
    )

    fallback = uuid.uuid5(WORK_ID_NAMESPACE, identity_key)

    return WorkIdentity(
        work_id=f"uuid:{fallback}",
        confidence="low",
        basis="uuid5_fallback",
    )
