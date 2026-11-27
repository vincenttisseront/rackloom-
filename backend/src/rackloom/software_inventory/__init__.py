"""F20 : inventaire logiciel exportable et fin de support.

Versions installées (observations `packages`) reliées aux avis importés hors ligne
et aux échéances éditeur. Comparaison Debian/Ubuntu : un correctif rétroporté
(`fixed_version` de distribution) l'emporte sur toute comparaison amont naïve.
Provenance et résultats incertains sont toujours indiqués. Export filtré par projet.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from .. import audit, projects
from ..canonical import digest
from ..db import Conn, Jsonb
from ..errors import ApiError
from ..identity.authz import Principal, require_admin
from ..ids import uuid7

FORMAT = "rackloom.software_advisories.v1"
# Série Ubuntu 24.04 LTS.
SERIES_NOBLE = "noble"
PRODUCT_UBUNTU = "ubuntu"

# Verdicts possibles pour un avis vs version installée.
STATUS_NOT_AFFECTED = "not_affected"
STATUS_VULNERABLE = "vulnerable"
STATUS_UNFIXED = "unfixed"
STATUS_UNCERTAIN = "uncertain"

# État de support produit.
EOL_SUPPORTED = "supported"
EOL_ENDED = "ended"
EOL_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Comparaison de versions Debian / Ubuntu (Policy §5.6.12, sous-ensemble)
# ---------------------------------------------------------------------------

_DIGITS = re.compile(r"\d+")
_NON_DIGIT = re.compile(r"[^\d]+")


def _split_epoch(version: str) -> tuple[int, str]:
    if ":" in version:
        left, right = version.split(":", 1)
        if left.isdigit():
            return int(left), right
    return 0, version


def _split_debian(version: str) -> tuple[str, str]:
    """Sépare upstream et révision Debian (dernier tiret)."""
    if "-" in version:
        upstream, deb = version.rsplit("-", 1)
        return upstream, deb
    return version, ""


def _ord_char(ch: str) -> int:
    """Ordre Debian : ~ < fin < lettres < autres."""
    if ch == "~":
        return -1
    if ch.isalpha():
        return ord(ch)
    return ord(ch) + 256


def _cmp_nondigit_chars(ca: str | None, cb: str | None) -> int:
    """~ < fin de chaîne < lettres < autres (Policy Debian)."""
    if ca is None and cb is None:
        return 0
    if ca is None:
        return 1 if cb == "~" else -1
    if cb is None:
        return -1 if ca == "~" else 1
    oa, ob = _ord_char(ca), _ord_char(cb)
    if oa == ob:
        return 0
    return -1 if oa < ob else 1


def _cmp_version_part(a: str, b: str) -> int:
    """Compare une partie (upstream ou debian) selon les règles Debian."""
    ia = ib = 0
    while ia < len(a) or ib < len(b):
        ma = _NON_DIGIT.match(a, ia)
        mb = _NON_DIGIT.match(b, ib)
        sa = ma.group(0) if ma else ""
        sb = mb.group(0) if mb else ""
        ia += len(sa)
        ib += len(sb)
        for i in range(max(len(sa), len(sb))):
            ca = sa[i] if i < len(sa) else None
            cb = sb[i] if i < len(sb) else None
            c = _cmp_nondigit_chars(ca, cb)
            if c != 0:
                return c
        na = _DIGITS.match(a, ia)
        nb = _DIGITS.match(b, ib)
        va = int(na.group(0)) if na else 0
        vb = int(nb.group(0)) if nb else 0
        ia += len(na.group(0)) if na else 0
        ib += len(nb.group(0)) if nb else 0
        if va != vb:
            return -1 if va < vb else 1
        if not na and not nb:
            break
    return 0


def compare_debian_versions(left: str, right: str) -> int:
    """-1 si left < right, 0 si égal, 1 si left > right (ordre Debian)."""
    if not left or not right:
        raise ValueError("version Debian vide")
    e1, rest1 = _split_epoch(left.strip())
    e2, rest2 = _split_epoch(right.strip())
    if e1 != e2:
        return -1 if e1 < e2 else 1
    u1, d1 = _split_debian(rest1)
    u2, d2 = _split_debian(rest2)
    c = _cmp_version_part(u1, u2)
    if c != 0:
        return c
    return _cmp_version_part(d1, d2)


def debian_ge(installed: str, fixed: str) -> bool:
    return compare_debian_versions(installed, fixed) >= 0


# ---------------------------------------------------------------------------
# Évaluation d'un avis (jamais amont seule)
# ---------------------------------------------------------------------------


def evaluate_advisory(
    *,
    installed_version: str | None,
    fixed_version: str | None,
    upstream_fixed_version: str | None = None,
) -> dict[str, Any]:
    """Relie une version installée à un avis via la version de *distribution*.

    Un paquet rétroporté (installée >= fixed_version Debian) n'est **pas**
    vulnérable, même si la version amont est inférieure à `upstream_fixed_version`.
    """
    if installed_version is None or installed_version == "":
        return {
            "status": STATUS_UNCERTAIN,
            "reason": "version_installée_absente",
            "used_upstream_alone": False,
        }
    if fixed_version is None or fixed_version == "":
        # Pas de correctif publié pour cette série : pas de verdict « ok » inventé.
        return {
            "status": STATUS_UNFIXED,
            "reason": "pas_de_correctif_distribution",
            "used_upstream_alone": False,
            "upstream_fixed_version": upstream_fixed_version,
        }
    try:
        patched = debian_ge(installed_version, fixed_version)
    except ValueError:
        return {
            "status": STATUS_UNCERTAIN,
            "reason": "version_illisible",
            "used_upstream_alone": False,
        }
    # Piège amont : si on comparait naïvement amont, on pourrait conclure vulnérable
    # alors que le rétroportage couvre déjà. On documente sans l'utiliser.
    naive_upstream_vulnerable = False
    if upstream_fixed_version:
        # Heuristique illustrative : si le numéro amont extrait de l'installée
        # semble < amont corrigé (comparaison Debian sur la partie avant '-').
        inst_up, _ = _split_debian(_split_epoch(installed_version)[1])
        try:
            naive_upstream_vulnerable = compare_debian_versions(inst_up, upstream_fixed_version) < 0
        except ValueError:
            naive_upstream_vulnerable = False

    if patched:
        return {
            "status": STATUS_NOT_AFFECTED,
            "reason": "correctif_distribution_present",
            "used_upstream_alone": False,
            "naive_upstream_would_flag": naive_upstream_vulnerable,
            "fixed_version": fixed_version,
            "upstream_fixed_version": upstream_fixed_version,
        }
    return {
        "status": STATUS_VULNERABLE,
        "reason": "version_inferieure_au_correctif",
        "used_upstream_alone": False,
        "naive_upstream_would_flag": naive_upstream_vulnerable,
        "fixed_version": fixed_version,
        "upstream_fixed_version": upstream_fixed_version,
    }


# ---------------------------------------------------------------------------
# Fixtures Ubuntu (avis + EOL) — hors ligne, jamais un flux cloud
# ---------------------------------------------------------------------------


def ubuntu_fixture_payload() -> dict[str, Any]:
    """Fixture d'avis Ubuntu : correctif rétroporté openssl + avis encore ouvert curl."""
    return {
        "format": FORMAT,
        "source": "ubuntu-usn-fixture",
        "simulated": True,
        "notice": "Fixtures d'avis Ubuntu (hors ligne). Pas un flux éditeur réel.",
        "advisories": [
            {
                # Classic backport trap: upstream fixed at 3.0.13, Ubuntu noble
                # ships the fix in 3.0.2-0ubuntu1.18 without bumping upstream.
                "advisory_id": "USN-9999-1",
                "cve_ids": ["CVE-2099-0001"],
                "package_name": "openssl",
                "series": SERIES_NOBLE,
                "fixed_version": "3.0.2-0ubuntu1.18",
                "upstream_fixed_version": "3.0.13",
                "summary": "OpenSSL : correctif rétroporté dans la révision Ubuntu (fixture F20).",
                "published_on": "2024-06-01",
            },
            {
                "advisory_id": "USN-9999-2",
                "cve_ids": ["CVE-2099-0002"],
                "package_name": "curl",
                "series": SERIES_NOBLE,
                "fixed_version": "8.5.0-2ubuntu10.6",
                "upstream_fixed_version": "8.9.0",
                "summary": "curl : version installée de labo encore inférieure au correctif.",
                "published_on": "2024-07-15",
            },
            {
                "advisory_id": "USN-9999-3",
                "cve_ids": ["CVE-2099-0003"],
                "package_name": "nginx",
                "series": SERIES_NOBLE,
                "fixed_version": None,
                "upstream_fixed_version": "1.27.0",
                "summary": "nginx : avis sans correctif de distribution publié (incertain / non corrigé).",
                "published_on": "2024-08-01",
            },
        ],
        "eol": [
            {
                "product": PRODUCT_UBUNTU,
                "series": SERIES_NOBLE,
                "end_of_support": "2029-04-30",
                "source": "ubuntu-release-fixture",
            },
            {
                "product": PRODUCT_UBUNTU,
                "series": "focal",
                "end_of_support": "2025-04-30",
                "source": "ubuntu-release-fixture",
            },
        ],
    }


def _validate_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise ApiError(422, "invalid_advisory_feed", "flux d'avis invalide")
    extra = set(payload) - {"format", "source", "simulated", "notice", "advisories", "eol", "provenance"}
    if extra:
        raise ApiError(422, "unknown_field", f"champ « {sorted(extra)[0]} » refusé", field=f"/{sorted(extra)[0]}")
    if payload.get("format") != FORMAT:
        raise ApiError(422, "invalid_advisory_format", f"format attendu {FORMAT}")
    source = payload.get("source")
    if not isinstance(source, str) or not (1 <= len(source) <= 120):
        raise ApiError(422, "invalid_advisory_source", "source d'avis manquante ou invalide")
    advisories = payload.get("advisories")
    if not isinstance(advisories, list) or not advisories:
        raise ApiError(422, "empty_advisories", "aucun avis dans le flux")
    for i, adv in enumerate(advisories):
        if not isinstance(adv, dict):
            raise ApiError(422, "invalid_advisory", f"avis[{i}] invalide")
        for key in ("advisory_id", "package_name", "series", "summary"):
            if not isinstance(adv.get(key), str) or not adv[key]:
                raise ApiError(422, "invalid_advisory", f"avis[{i}].{key} requis")
    eol = payload.get("eol") or []
    if not isinstance(eol, list):
        raise ApiError(422, "invalid_eol", "liste eol invalide")


def import_feed(
    conn: Conn,
    principal: Principal,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Importe un flux d'avis hors ligne (admin). Réimport du même digest = idempotent."""
    require_admin(principal)
    body = payload if payload is not None else ubuntu_fixture_payload()
    _validate_payload(body)
    content_digest = digest(body)
    existing = conn.execute(
        "SELECT * FROM software_advisory_imports WHERE content_digest = %s", (content_digest,)
    ).fetchone()
    if existing is not None:
        return import_view(conn, dict(existing))

    provenance = {
        "kind": "offline_fixture" if body.get("simulated") else "offline_import",
        "source": body["source"],
        "simulated": bool(body.get("simulated", False)),
        "notice": body.get("notice") or "Import hors ligne d'avis éditeur.",
    }
    import_id = uuid7()
    advisories = body["advisories"]
    eol_rows = body.get("eol") or []
    conn.execute(
        """
        INSERT INTO software_advisory_imports (
            id, source, format, content_digest, advisory_count, eol_count, provenance, imported_by
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            import_id,
            body["source"],
            FORMAT,
            content_digest,
            len(advisories),
            len(eol_rows),
            Jsonb(provenance),
            principal.user_id,
        ),
    )
    for adv in advisories:
        published = adv.get("published_on")
        pub_date: date | None
        if isinstance(published, str) and published:
            pub_date = date.fromisoformat(published)
        else:
            pub_date = None
        cves = adv.get("cve_ids") or []
        if not isinstance(cves, list):
            cves = []
        conn.execute(
            """
            INSERT INTO software_advisories (
                id, import_id, advisory_id, cve_ids, package_name, series,
                fixed_version, upstream_fixed_version, summary, published_on
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                uuid7(),
                import_id,
                adv["advisory_id"],
                [str(c) for c in cves],
                adv["package_name"],
                adv["series"],
                adv.get("fixed_version"),
                adv.get("upstream_fixed_version"),
                adv["summary"][:500],
                pub_date,
            ),
        )
    for row in eol_rows:
        if not isinstance(row, dict):
            continue
        conn.execute(
            """
            INSERT INTO software_eol_products (
                id, import_id, product, series, end_of_support, source
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                uuid7(),
                import_id,
                str(row["product"]),
                str(row["series"]),
                date.fromisoformat(str(row["end_of_support"])),
                str(row.get("source") or body["source"])[:120],
            ),
        )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="software_advisories.import",
        outcome="ok",
        targets={"import_id": str(import_id), "digest": content_digest, "count": len(advisories)},
    )
    row = conn.execute("SELECT * FROM software_advisory_imports WHERE id = %s", (import_id,)).fetchone()
    assert row is not None
    return import_view(conn, dict(row))


def import_view(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "source": row["source"],
        "format": row["format"],
        "content_digest": row["content_digest"],
        "advisory_count": row["advisory_count"],
        "eol_count": row["eol_count"],
        "provenance": row["provenance"],
        "imported_at": row["imported_at"].isoformat() if row.get("imported_at") else None,
        "imported_by": str(row["imported_by"]) if row.get("imported_by") else None,
    }


def list_imports(conn: Conn, principal: Principal) -> list[dict[str, Any]]:
    require_admin(principal)
    rows = conn.execute("SELECT * FROM software_advisory_imports ORDER BY imported_at DESC LIMIT 50").fetchall()
    return [import_view(conn, dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Inventaire projet (observations + avis + EOL)
# ---------------------------------------------------------------------------


def _series_from_facts(facts: dict[str, Any] | None) -> str | None:
    if not facts:
        return None
    # Observations labo : facts.os = "Ubuntu 24.04.x …" ou codename.
    codename = facts.get("os_codename") or facts.get("ubuntu_codename")
    if isinstance(codename, str) and codename:
        return codename.lower()
    os_label = facts.get("os") or facts.get("distribution")
    if isinstance(os_label, str):
        lower = os_label.lower()
        if "24.04" in lower or "noble" in lower:
            return SERIES_NOBLE
        if "22.04" in lower or "jammy" in lower:
            return "jammy"
        if "20.04" in lower or "focal" in lower:
            return "focal"
    return None


def _latest_obs(conn: Conn, host_id: UUID, kind: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM host_observations WHERE host_id = %s AND kind = %s ORDER BY collected_at DESC LIMIT 1",
        (host_id, kind),
    ).fetchone()


def _eol_for(conn: Conn, product: str, series: str, *, today: date) -> dict[str, Any]:
    row = conn.execute(
        "SELECT e.* FROM software_eol_products e "
        "JOIN software_advisory_imports i ON i.id = e.import_id "
        "WHERE e.product = %s AND e.series = %s "
        "ORDER BY i.imported_at DESC LIMIT 1",
        (product, series),
    ).fetchone()
    if row is None:
        return {
            "status": EOL_UNKNOWN,
            "product": product,
            "series": series,
            "end_of_support": None,
            "reason": "echeance_absente",
        }
    eos: date = row["end_of_support"]
    status = EOL_ENDED if eos < today else EOL_SUPPORTED
    return {
        "status": status,
        "product": product,
        "series": series,
        "end_of_support": eos.isoformat(),
        "source": row["source"],
        "reason": "support_termine" if status == EOL_ENDED else "dans_support",
    }


def _advisories_for(conn: Conn, package_name: str, series: str) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT DISTINCT ON (a.advisory_id, a.package_name, a.series)
            a.*
        FROM software_advisories a
        JOIN software_advisory_imports i ON i.id = a.import_id
        WHERE a.package_name = %s AND a.series = %s
        ORDER BY a.advisory_id, a.package_name, a.series, i.imported_at DESC
        """,
        (package_name, series),
    ).fetchall()


def build_inventory(
    conn: Conn,
    principal: Principal,
    project_ref: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Inventaire logiciel du projet : paquets × avis × fin de support."""
    project = projects.get_project(conn, principal, project_ref)
    moment = now or datetime.now(UTC)
    today = moment.date()

    hosts = conn.execute("SELECT * FROM hosts WHERE project_id = %s ORDER BY name", (project["id"],)).fetchall()

    import_count = conn.execute("SELECT count(*) AS n FROM software_advisory_imports").fetchone()
    has_feed = bool(import_count and import_count["n"])

    host_rows: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    package_total = 0
    uncertain_hosts = 0

    for host in hosts:
        pkg_obs = _latest_obs(conn, host["id"], "packages")
        facts_obs = _latest_obs(conn, host["id"], "facts")
        facts = dict(facts_obs["data"]) if facts_obs and isinstance(facts_obs["data"], dict) else None
        series = _series_from_facts(facts)
        packages_data: dict[str, Any] = {}
        if pkg_obs and isinstance(pkg_obs["data"], dict):
            packages_data = dict(pkg_obs["data"])

        if not packages_data:
            uncertain_hosts += 1

        eol = (
            _eol_for(conn, PRODUCT_UBUNTU, series, today=today)
            if series
            else {
                "status": EOL_UNKNOWN,
                "product": PRODUCT_UBUNTU,
                "series": None,
                "end_of_support": None,
                "reason": "serie_inconnue",
            }
        )

        pkg_entries: list[dict[str, Any]] = []
        for name, raw_ver in sorted(packages_data.items()):
            if not isinstance(name, str):
                continue
            version = raw_ver if isinstance(raw_ver, str) else (None if raw_ver is None else str(raw_ver))
            package_total += 1
            entry: dict[str, Any] = {
                "name": name,
                "version": version,
                "advisories": [],
            }
            if not series:
                entry["advisories"].append(
                    {
                        "status": STATUS_UNCERTAIN,
                        "reason": "serie_os_inconnue",
                        "used_upstream_alone": False,
                    }
                )
            elif not has_feed:
                entry["advisories"].append(
                    {
                        "status": STATUS_UNCERTAIN,
                        "reason": "aucun_avis_importe",
                        "used_upstream_alone": False,
                    }
                )
            else:
                for adv in _advisories_for(conn, name, series):
                    verdict = evaluate_advisory(
                        installed_version=version,
                        fixed_version=adv["fixed_version"],
                        upstream_fixed_version=adv["upstream_fixed_version"],
                    )
                    item = {
                        "advisory_id": adv["advisory_id"],
                        "cve_ids": list(adv["cve_ids"] or []),
                        "summary": adv["summary"],
                        "fixed_version": adv["fixed_version"],
                        "upstream_fixed_version": adv["upstream_fixed_version"],
                        **verdict,
                    }
                    entry["advisories"].append(item)
                    if verdict["status"] in (STATUS_VULNERABLE, STATUS_UNFIXED, STATUS_UNCERTAIN):
                        findings.append(
                            {
                                "host_id": str(host["id"]),
                                "host_name": host["name"],
                                "package": name,
                                "version": version,
                                "advisory_id": adv["advisory_id"],
                                "status": verdict["status"],
                                "reason": verdict["reason"],
                                "naive_upstream_would_flag": verdict.get("naive_upstream_would_flag", False),
                            }
                        )
            pkg_entries.append(entry)

        host_rows.append(
            {
                "id": str(host["id"]),
                "name": host["name"],
                "series": series,
                "packages_observed": bool(packages_data),
                "observation": {
                    "packages_at": pkg_obs["collected_at"].isoformat() if pkg_obs else None,
                    "facts_at": facts_obs["collected_at"].isoformat() if facts_obs else None,
                    "source": (pkg_obs["source"] if pkg_obs else (facts_obs["source"] if facts_obs else None)),
                    "status": "ok" if packages_data else "missing",
                },
                "eol": eol,
                "packages": pkg_entries,
            }
        )

    provenance = {
        "observations": "host_observations.kind=packages|facts",
        "advisories": "software_advisories (import hors ligne)",
        "comparison": "debian_version_vs_fixed_version",
        "never_upstream_alone": True,
        "feed_imported": has_feed,
    }
    return {
        "project_id": str(project["id"]),
        "project_slug": project["slug"],
        "generated_at": moment.isoformat(),
        "hosts": host_rows,
        "summary": {
            "host_count": len(host_rows),
            "package_count": package_total,
            "finding_count": len(findings),
            "uncertain_host_count": uncertain_hosts,
            "feed_imported": has_feed,
        },
        "findings": findings,
        "provenance": provenance,
        "notice": (
            "Comparaison sur la version de distribution (rétroportages). "
            "Un paquet n'est jamais marqué vulnérable sur la seule version amont."
        ),
    }


def export_inventory(
    conn: Conn,
    principal: Principal,
    project_ref: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Export JSON filtré au projet ; enregistrement d'audit ; hors périmètre → 404."""
    inventory = build_inventory(conn, principal, project_ref, now=now)
    project = projects.get_project(conn, principal, project_ref)
    content_digest = digest(
        {
            "project_id": inventory["project_id"],
            "hosts": [
                {
                    "id": h["id"],
                    "packages": [{"name": p["name"], "version": p["version"]} for p in h["packages"]],
                    "findings": [f for f in inventory["findings"] if f["host_id"] == h["id"]],
                }
                for h in inventory["hosts"]
            ],
        }
    )
    export_id = uuid7()
    conn.execute(
        """
        INSERT INTO software_inventory_exports (
            id, project_id, actor_id, content_digest, host_count, package_count, finding_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            export_id,
            project["id"],
            principal.user_id,
            content_digest,
            inventory["summary"]["host_count"],
            inventory["summary"]["package_count"],
            inventory["summary"]["finding_count"],
        ),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="software_inventory.export",
        outcome="ok",
        project_id=project["id"],
        targets={"export_id": str(export_id), "digest": content_digest},
    )
    return {
        "export_id": str(export_id),
        "content_digest": content_digest,
        "inventory": inventory,
    }
