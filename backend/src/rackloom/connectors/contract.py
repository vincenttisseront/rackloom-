"""F30 : suite de contrat pour adaptateurs de connecteurs.

Vérifie le descripteur (capacités, erreurs, réseau, événements) et les invariants
d'exécution : périmètre, refus des secrets, endpoints approuvés. Un adaptateur qui
omet `check_scope` ou annonce `enforces_scope=False` est rejeté.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import MATRIX, reference
from .sdk import (
    ADAPTER_EVENT_SOURCES,
    CAPABILITIES,
    ERROR_CODES,
    Adapter,
    AdapterDescriptor,
    Capability,
    NetworkConstraint,
)


@dataclass(frozen=True, slots=True)
class ContractIssue:
    code: str
    message: str
    path: str = ""


@dataclass(frozen=True, slots=True)
class ContractReport:
    kind: str
    ok: bool
    issues: tuple[ContractIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "ok": self.ok,
            "issues": [{"code": i.code, "message": i.message, "path": i.path} for i in self.issues],
        }


def descriptor_from_matrix(kind: str) -> AdapterDescriptor:
    """Construit un descripteur à partir de la matrice livrée (connecteurs lot 1)."""
    matrix = MATRIX[kind]
    caps = {
        name: Capability(
            available=bool(spec["available"]),
            mode=spec.get("mode"),
            note=str(spec.get("note") or ""),
        )
        for name, spec in matrix.items()
    }
    sources = {
        "vsphere": frozenset({"vcenter", "system"}),
        "terraform": frozenset({"terraform", "job", "system"}),
        "ansible": frozenset({"ansible", "job", "system"}),
        "model": frozenset({"system"}),
        "reference": frozenset({"system"}),
    }.get(kind, frozenset({"system"}))
    return AdapterDescriptor(
        kind=kind,
        title=f"Connecteur {kind}",
        capabilities=caps,
        network=NetworkConstraint(),
        event_sources=sources,
        enforces_scope=True,
        enforces_audit=True,
        requires_install_approval=kind == "reference",
        notes="descripteur dérivé de MATRIX",
    )


def validate_descriptor(desc: AdapterDescriptor) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    if not desc.kind or not desc.kind.replace("-", "").replace("_", "").isalnum():
        issues.append(ContractIssue("invalid_kind", "kind d'adaptateur invalide", "/kind"))
    for name in CAPABILITIES:
        if name not in desc.capabilities:
            issues.append(
                ContractIssue("missing_capability", f"capacité manquante : {name}", f"/capabilities/{name}")
            )
            continue
        cap = desc.capabilities[name]
        if cap.available and cap.mode is None:
            issues.append(
                ContractIssue(
                    "capability_mode_required",
                    f"mode requis lorsque available=true ({name})",
                    f"/capabilities/{name}/mode",
                )
            )
        if not cap.available and cap.mode is not None:
            issues.append(
                ContractIssue(
                    "capability_mode_forbidden",
                    f"mode interdit lorsque available=false ({name})",
                    f"/capabilities/{name}/mode",
                )
            )
        if cap.mode is not None and cap.mode not in ("real", "simulated", "request"):
            issues.append(
                ContractIssue("invalid_capability_mode", f"mode inconnu : {cap.mode}", f"/capabilities/{name}/mode")
            )
    if not desc.enforces_scope:
        issues.append(
            ContractIssue(
                "scope_not_enforced",
                "un adaptateur doit imposer le contrôle de périmètre (enforces_scope=true)",
                "/enforces_scope",
            )
        )
    if not desc.enforces_audit:
        issues.append(
            ContractIssue(
                "audit_not_enforced",
                "un adaptateur doit auditer ses mutations (enforces_audit=true)",
                "/enforces_audit",
            )
        )
    if desc.network.allow_user_supplied_url:
        issues.append(
            ContractIssue(
                "free_url_forbidden",
                "URL libre fournie par l'utilisateur interdite (endpoints approuvés uniquement)",
                "/network/allow_user_supplied_url",
            )
        )
    if not desc.network.require_hostname_verify:
        issues.append(
            ContractIssue(
                "hostname_verify_required",
                "la vérification du nom d'hôte TLS est obligatoire",
                "/network/require_hostname_verify",
            )
        )
    for src in desc.event_sources:
        if src not in ADAPTER_EVENT_SOURCES:
            issues.append(
                ContractIssue("invalid_event_source", f"source d'événement interdite : {src}", "/event_sources")
            )
    return issues


def validate_adapter_instance(adapter: Adapter) -> list[ContractIssue]:
    """Vérifie qu'une instance respecte le protocole et refuse un principal hors périmètre."""
    from ..identity.authz import Binding, Principal
    from ..ids import uuid7

    issues = validate_descriptor(adapter.descriptor())
    # Principal sans rôle connecteur → doit lever.
    outsider = Principal(
        user_id=uuid7(),
        email="outsider@demo.invalid",
        display_name="Outsider",
        session_id=uuid7(),
        auth_method="password",
        auth_time=__import__("datetime").datetime.now(__import__("datetime").UTC),
        bindings=[Binding(role="operator", project_id=uuid7(), environment_id=None)],
    )
    from ..errors import ApiError

    try:
        adapter.check_scope(outsider, "configure")
    except ApiError as exc:
        if exc.status not in (403, 401) and exc.code not in ("scope_denied", "forbidden"):
            issues.append(
                ContractIssue(
                    "scope_wrong_error",
                    f"check_scope doit refuser hors périmètre (403) ; obtenu {exc.code}",
                    "/check_scope",
                )
            )
    else:
        issues.append(
            ContractIssue(
                "scope_not_checked",
                "check_scope a accepté un principal hors périmètre",
                "/check_scope",
            )
        )
    # Secrets dans la config → refus.
    try:
        adapter.validate_config({"password": "x", "url": "https://lab.demo.invalid"})
    except ApiError as exc:
        if exc.code not in ("secret_in_config", "invalid_config"):
            issues.append(
                ContractIssue(
                    "secret_not_rejected",
                    f"validate_config doit refuser un secret ; code={exc.code}",
                    "/validate_config",
                )
            )
    else:
        issues.append(
            ContractIssue(
                "secret_not_rejected",
                "validate_config a accepté un champ secret",
                "/validate_config",
            )
        )
    return issues


def run_suite(descriptors: list[AdapterDescriptor] | None = None) -> list[ContractReport]:
    """Exécute la suite sur les descripteurs fournis (défaut : MATRIX + référence)."""
    if descriptors is None:
        descriptors = [descriptor_from_matrix(k) for k in MATRIX] + [reference.DESCRIPTOR]
    reports: list[ContractReport] = []
    seen: set[str] = set()
    for desc in descriptors:
        if desc.kind in seen:
            continue
        seen.add(desc.kind)
        issues = validate_descriptor(desc)
        reports.append(ContractReport(kind=desc.kind, ok=not issues, issues=tuple(issues)))
    return reports


def assert_suite_green(descriptors: list[AdapterDescriptor] | None = None) -> None:
    reports = run_suite(descriptors)
    failed = [r for r in reports if not r.ok]
    if failed:
        detail = "; ".join(f"{r.kind}:{r.issues[0].code}" for r in failed)
        raise AssertionError(f"suite de contrat échouée : {detail}")


# Codes d'erreur documentés (référence pour le guide).
DOCUMENTED_ERROR_CODES = sorted(ERROR_CODES)
