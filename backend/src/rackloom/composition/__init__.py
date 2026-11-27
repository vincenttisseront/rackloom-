"""F02 : compositions multi-VM — membres, dépendances et critères de disponibilité.

Une composition déclare des membres (rôles de VM) avec composants, contrôles de santé,
critères de disponibilité (`readiness`) et dépendances inter-membres. L'ordonnancement
réutilise les vagues d'exécution : un membre dépendant n'est configuré qu'après la
vérification réussie (et la readiness) du membre requis. Un échec partiel ne déclenche
aucune suppression (invariant 17) ; la reprise ciblée existante reprend les hôtes en
échec ou bloqués sans répéter ceux déjà vérifiés.
"""

from __future__ import annotations

from typing import Any


class CompositionError(ValueError):
    pass


MEMBER_ID_RE_MAX = 40


def has_composition(contract: dict[str, Any]) -> bool:
    return isinstance(contract.get("composition"), dict) and bool(contract["composition"].get("members"))


def members(contract: dict[str, Any]) -> list[dict[str, Any]]:
    if not has_composition(contract):
        return []
    return list(contract["composition"]["members"])


def member_by_id(contract: dict[str, Any], member_id: str) -> dict[str, Any] | None:
    for m in members(contract):
        if m["id"] == member_id:
            return m
    return None


def validate_composition(contract: dict[str, Any]) -> None:
    """Valide le bloc optionnel `composition` d'un template (DAG, quantités, refs)."""
    from ..registry import CHECK_TYPES, SLUG_RE, CatalogError

    raw = contract.get("composition")
    if raw is None:
        return
    if not isinstance(raw, dict) or "members" not in raw:
        raise CatalogError("template : composition attend {members: [...]}")
    mems = raw["members"]
    if not isinstance(mems, list) or len(mems) < 2:
        raise CatalogError("template : une composition exige au moins deux membres")
    if set(raw) - {"members"}:
        raise CatalogError("template : composition n'admet que « members »")
    ids: list[str] = []
    for m in mems:
        if not isinstance(m, dict):
            raise CatalogError("template : membre de composition invalide")
        mid = m.get("id")
        if not isinstance(mid, str) or not SLUG_RE.match(mid) or len(mid) > MEMBER_ID_RE_MAX:
            raise CatalogError(f"template : composition.member.id invalide ({mid!r})")
        if mid in ids:
            raise CatalogError(f"template : membre de composition dupliqué ({mid})")
        ids.append(mid)
    id_set = set(ids)
    for m in mems:
        mid = m["id"]
        if not m.get("title") or not isinstance(m["title"], str) or len(m["title"]) > 120:
            raise CatalogError(f"template : composition.{mid}.title manquant ou trop long")
        has_q = "quantity" in m
        has_from = "quantity_from" in m
        if has_q == has_from:
            raise CatalogError(f"template : composition.{mid} exige quantity XOR quantity_from")
        if has_q and (not isinstance(m["quantity"], int) or not 1 <= m["quantity"] <= 10):
            raise CatalogError(f"template : composition.{mid}.quantity hors bornes")
        if has_from and m["quantity_from"] != "quantity":
            raise CatalogError(f"template : composition.{mid}.quantity_from doit être « quantity »")
        if not isinstance(m.get("components"), list) or not m["components"]:
            raise CatalogError(f"template : composition.{mid}.components requis")
        for check in m.get("health_checks") or []:
            if check.get("type") not in CHECK_TYPES:
                raise CatalogError(f"template : composition.{mid} contrôle « {check.get('type')} » inconnu")
        for check in m.get("readiness") or []:
            if check.get("type") not in CHECK_TYPES:
                raise CatalogError(f"template : composition.{mid} readiness « {check.get('type')} » inconnu")
        for dep in m.get("depends_on") or []:
            if not isinstance(dep, dict) or dep.get("member") not in id_set:
                raise CatalogError(f"template : composition.{mid}.depends_on membre inconnu")
            if dep.get("readiness") not in (None, "ready"):
                raise CatalogError(f"template : composition.{mid}.depends_on.readiness doit être « ready »")
            if set(dep) - {"member", "readiness"}:
                raise CatalogError(f"template : composition.{mid}.depends_on n'admet que member/readiness")
            if dep["member"] == mid:
                raise CatalogError(f"template : composition.{mid} ne peut pas dépendre de lui-même")
        allowed = {
            "id",
            "title",
            "quantity",
            "quantity_from",
            "components",
            "health_checks",
            "readiness",
            "depends_on",
        }
        if set(m) - allowed:
            raise CatalogError(f"template : composition.{mid} champs inconnus : {sorted(set(m) - allowed)}")

    # DAG : pas de cycle.
    deps = {m["id"]: [d["member"] for d in (m.get("depends_on") or [])] for m in mems}
    visiting: set[str] = set()
    seen: set[str] = set()

    def walk(node: str) -> None:
        if node in seen:
            return
        if node in visiting:
            raise CatalogError("template : cycle dans composition.depends_on")
        visiting.add(node)
        for d in deps[node]:
            walk(d)
        visiting.remove(node)
        seen.add(node)

    for mid in ids:
        walk(mid)

    # Les composants top-niveau doivent couvrir l'union des membres (références catalogue).
    top_slugs = {(r["component"], r["version"]) for r in contract.get("components") or []}
    for m in mems:
        for ref in m["components"]:
            key = (ref["component"], ref["version"])
            if key not in top_slugs:
                raise CatalogError(
                    f"template : composition.{m['id']} composant {ref['component']}@"
                    f"{ref['version']} absent de components[]"
                )


def topological_member_order(contract: dict[str, Any]) -> list[str]:
    """Ordre d'exécution des membres (dépendances d'abord)."""
    mems = members(contract)
    deps = {m["id"]: [d["member"] for d in (m.get("depends_on") or [])] for m in mems}
    order: list[str] = []
    ready = [m["id"] for m in mems if not deps[m["id"]]]
    remaining = {m["id"]: set(deps[m["id"]]) for m in mems}
    while ready:
        mid = ready.pop(0)
        order.append(mid)
        for other, waiting in remaining.items():
            if mid in waiting:
                waiting.remove(mid)
                if not waiting and other not in order and other not in ready:
                    ready.append(other)
    if len(order) != len(mems):
        raise CompositionError("cycle dans les dépendances de composition")
    return order


def member_quantity(member: dict[str, Any], need_quantity: int) -> int:
    if "quantity" in member:
        return int(member["quantity"])
    return int(need_quantity)


def plan_composition_waves(contract: dict[str, Any], instances: list[dict[str, Any]]) -> list[list[str]]:
    """Une vague par membre, dans l'ordre topologique ; hôtes déjà réussis exclus à la reprise."""
    by_member: dict[str, list[str]] = {}
    for inst in instances:
        mid = inst.get("member")
        if not mid:
            continue
        by_member.setdefault(mid, []).append(inst["name"])
    waves: list[list[str]] = []
    for mid in topological_member_order(contract):
        names = by_member.get(mid) or []
        if names:
            waves.append(names)
    return waves


def health_checks_for_host(
    contract: dict[str, Any], host: dict[str, Any], fallback: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    mid = host.get("composition_member")
    if not mid:
        return list(fallback)
    m = member_by_id(contract, mid)
    if m is None:
        return list(fallback)
    checks = m.get("health_checks") or m.get("readiness") or fallback
    return list(checks)


def readiness_for_member(contract: dict[str, Any], member_id: str) -> list[dict[str, Any]]:
    m = member_by_id(contract, member_id)
    if m is None:
        return []
    return list(m.get("readiness") or m.get("health_checks") or [])


def components_for_hosts(resolution: dict[str, Any], hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Composants Ansible à appliquer : union des membres des hôtes cibles."""
    member_comps = resolution.get("member_components") or {}
    if not member_comps:
        return list(resolution["components"])
    members_present = {h.get("composition_member") for h in hosts if h.get("composition_member")}
    if not members_present:
        return list(resolution["components"])
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for mid in members_present:
        for c in member_comps.get(mid) or []:
            key = (c["slug"], c["version"])
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out or list(resolution["components"])


def _check_dedupe_key(check: dict[str, Any]) -> tuple[Any, ...]:
    return (
        check.get("type"),
        check.get("package"),
        check.get("unit"),
        check.get("process"),
        check.get("jail"),
        check.get("port"),
        check.get("path"),
    )


def collect_checks_for_hosts(resolution: dict[str, Any], hosts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Contrôles à collecter pour un lot d'hôtes : union des checks des membres présents.

    Le verify d'une vague re-évalue aussi les hôtes des vagues précédentes (scope cumulé).
    La spec de collecte doit donc couvrir tous les membres ciblés, pas seulement le premier
    (sinon un socle Fail2ban re-vérifié avec les checks Nginx rate `fail2ban-server` absent).
    """
    fallback = list(resolution.get("health_checks") or [])
    members_meta = (resolution.get("composition") or {}).get("members") or []
    if not members_meta:
        return fallback
    by_id = {m["id"]: m for m in members_meta}
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    any_member = False
    for h in hosts:
        mid = h.get("composition_member")
        if not mid or mid not in by_id:
            continue
        any_member = True
        m = by_id[mid]
        for check in m.get("health_checks") or m.get("readiness") or []:
            key = _check_dedupe_key(check)
            if key not in seen:
                seen.add(key)
                out.append(check)
    return out if any_member else fallback
