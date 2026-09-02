"""Реестры, идентификаторы и проверки корпуса ruPhysBERT."""

from .identity import WorkIdentity, resolve_work_identity
from .extraction_registration import (
    find_registered_pdf_artifact,
    normalize_extraction_version,
    plan_extracted_text,
)
from .local_registration import (
    LocalFileRegistration,
    plan_local_file,
    read_local_file_registrations,
)
from .manifests import ManifestConcurrencyError, ManifestPlan, ManifestStore
from .profiles import SourceProfile, get_source_profile
from .registration import (
    RegistrationOptions,
    plan_document,
    reconcile_document_plan,
    resolve_collection_rights,
)

__all__ = [
    "ManifestConcurrencyError",
    "LocalFileRegistration",
    "ManifestPlan",
    "ManifestStore",
    "RegistrationOptions",
    "SourceProfile",
    "WorkIdentity",
    "find_registered_pdf_artifact",
    "get_source_profile",
    "normalize_extraction_version",
    "plan_document",
    "plan_extracted_text",
    "plan_local_file",
    "read_local_file_registrations",
    "reconcile_document_plan",
    "resolve_collection_rights",
    "resolve_work_identity",
]
