"""F60 : brouillon depuis un serveur de référence.

Collecteurs approuvés (champs en liste blanche), analyse en trois catégories, aucune écriture
sur l'hôte. Sans collecteur réel : observations fictives explicites (`simulated: true`).
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from .. import registry
from ..db import Conn
from ..errors import ApiError, not_found
from ..identity.authz import Principal
from ..ids import uuid7

# Collecteurs versionnés : seuls ces champs sont lus ; tout le reste est ignoré.
COLLECTORS: dict[str, dict[str, Any]] = {
    "os_facts": {
        "version": "1.0.0",
        "title": "Système",
        "fields": ("distribution", "version", "kernel"),
    },
    "mounts": {
        "version": "1.0.0",
        "title": "Volumes et montages",
        "fields": ("path", "fstype", "size_gb"),
    },
    "packages": {
        "version": "1.0.0",
        "title": "Paquets",
        "fields": ("name", "version"),
        # Paquets que les composants du catalogue savent interpréter.
        "allowlist": ("nginx", "fail2ban"),
    },
    "services": {
        "version": "1.0.0",
        "title": "Services",
        "fields": ("unit", "active", "enabled"),
        "allowlist": ("nginx", "fail2ban"),
    },
    "component_settings": {
        "version": "1.0.0",
        "title": "Réglages interprétables",
        "fields": ("component", "parameter", "value"),
    },
}

# Disposition que l'image std-v1 de la base fournit (pas de montage dédié /var/www).
IMAGE_LAYOUT_MOUNTS = frozenset({"/", "/boot", "/boot/efi"})

CATEGORY_LABELS = {
    "standardize": "À standardiser",
    "host_specific": "Propre à cet hôte",
    "confirm": "À confirmer",
}

# Fixture de démonstration : Ubuntu + Nginx + montage dédié + réglage inconnu + canaris d'hôte.
FIXTURE_ID = "ubuntu-nginx-dedicated-mount"
_CANARY_SECRET = "rl-canary-secret-f60-test"  # noqa: S105 — jeton de fuite de test, pas un secret réel
_VOLUME_UUID = "f60-volume-uuid-should-not-leak"
_HOST_ADDRESS = "10.233.0.42"
_HOSTNAME = "ref-web-01.demo.invalid"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def fixture_observation() -> dict[str, Any]:
    """Observations fictives explicites (D21) : badge simulé, canaris d'identité, montage dédié."""
    body = {
        "os": {"distribution": "Ubuntu", "version": "24.04", "kernel": "6.8.0-generic"},
        "mounts": [
            {"path": "/", "fstype": "ext4", "size_gb": 20},
            {"path": "/var/www", "fstype": "ext4", "size_gb": 10},
        ],
        "packages": {"nginx": "1.24.0-2ubuntu7", "fail2ban": None, "curl": "8.5.0-2ubuntu10"},
        "services": {
            "nginx": {"unit": "nginx", "active": True, "enabled": True},
            "ssh": {"unit": "ssh", "active": True, "enabled": True},
        },
        "settings": [
            {"component": "nginx", "parameter": "listen_port", "value": 8080},
            {"component": "nginx", "parameter": "server_tokens", "value": False},
            {"component": "nginx", "parameter": "security_headers", "value": True},
            # Non déclaré au contrat → « à confirmer », jamais appliqué.
            {"component": "nginx", "parameter": "geoip_country_block", "value": "FR"},
        ],
        "host_identity": {
            "hostname": _HOSTNAME,
            "address": _HOST_ADDRESS,
            "volume_uuid": _VOLUME_UUID,
            "canary": _CANARY_SECRET,
        },
    }
    return {
        "id": FIXTURE_ID,
        "label": "Ubuntu 24.04 + Nginx + montage /var/www (fictif)",
        "simulated": True,
        "simulation_label": "Observations fictives",
        "collected_at": _now(),
        "collectors": [
            {"slug": slug, "version": spec["version"], "title": spec["title"]} for slug, spec in COLLECTORS.items()
        ],
        "raw": body,
        "fingerprint": _fingerprint(body),
    }


def list_reference_sources(conn: Conn, principal: Principal) -> dict[str, Any]:
    """Sources utilisables : fixtures toujours ; hôtes enrôlés seulement dans le périmètre visible."""
    fixtures = [
        {
            "kind": "fixture",
            "id": FIXTURE_ID,
            "label": "Ubuntu 24.04 + Nginx + montage /var/www (fictif)",
            "simulated": True,
            "simulation_label": "Observations fictives",
            "note": "Collecteur réel indisponible : chaîne construite avec des observations fictives explicites (D21).",
        }
    ]
    hosts: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT h.id, h.name, h.address, h.project_id, h.deployment_state, p.slug AS project_slug "
        "FROM hosts h JOIN projects p ON p.id = h.project_id "
        "WHERE h.enrolled_at IS NOT NULL ORDER BY p.slug, h.name"
    ).fetchall()
    for h in rows:
        if not principal.can_see_project(h["project_id"]):
            continue
        hosts.append(
            {
                "kind": "host",
                "id": str(h["id"]),
                "name": h["name"],
                "project": h["project_slug"],
                "deployment_state": h["deployment_state"],
                "simulated": False,
                "note": "Collecte réelle non branchée dans cette tranche : utilisez la fixture tant que le "
                "collecteur d'atelier n'est pas disponible.",
            }
        )
    return {"fixtures": fixtures, "hosts": hosts, "collectors": list_collectors()}


def list_collectors() -> list[dict[str, Any]]:
    return [
        {
            "slug": slug,
            "version": spec["version"],
            "title": spec["title"],
            "fields": list(spec["fields"]),
            **({"allowlist": list(spec["allowlist"])} if "allowlist" in spec else {}),
        }
        for slug, spec in COLLECTORS.items()
    ]


def load_observation(conn: Conn, principal: Principal, reference: dict[str, Any]) -> dict[str, Any]:
    """Charge une observation filtrée. fixture → fictif ; host → observations existantes filtrées ou refus."""
    if not isinstance(reference, dict):
        raise ApiError(422, "invalid_reference", "référence attendue : {fixture} ou {host_id}", field="/reference")
    keys = set(reference)
    if keys == {"fixture"}:
        if reference["fixture"] != FIXTURE_ID:
            raise ApiError(422, "unknown_fixture", f"fixture « {reference['fixture']} » inconnue", field="/reference")
        return fixture_observation()
    if keys == {"host_id"}:
        try:
            host_id = UUID(str(reference["host_id"]))
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid_reference", "host_id uuid attendu", field="/reference") from exc
        host = conn.execute("SELECT * FROM hosts WHERE id = %s", (host_id,)).fetchone()
        if host is None or not principal.can_see_project(host["project_id"]):
            raise not_found("machine")
        # Pas de collecteur d'atelier réel : on refuse plutôt que de lancer un job.
        raise ApiError(
            422,
            "collector_unavailable",
            "collecteur d'atelier indisponible pour un hôte réel : utilisez la fixture "
            f"« {FIXTURE_ID} » (observations fictives explicites)",
            field="/reference",
        )
    raise ApiError(422, "invalid_reference", "référence attendue : {fixture} ou {host_id}", field="/reference")


def _latest_component(conn: Conn, slug: str, os_name: str) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT * FROM component_versions WHERE slug = %s AND status IN ('validated', 'published')",
        (slug,),
    ).fetchall()
    usable = [r for r in rows if os_name in (r["contract"].get("supports") or {}).get("os", [])]
    if not usable:
        return None
    return max(usable, key=lambda r: registry.semver_key(r["version"]))


def analyze(conn: Conn, observation: dict[str, Any], base_contract: dict[str, Any]) -> dict[str, Any]:
    """Produit les propositions classées ; n'écrit rien dans le brouillon ni sur l'hôte."""
    raw = observation["raw"]
    collected_at = observation["collected_at"]
    os_name = base_contract["system_profile"]["os"]
    layout_id = (base_contract["system_profile"].get("layout") or {}).get("layout_id", "std-v1")
    items: list[dict[str, Any]] = []

    # Identité d'hôte → propre à cet hôte (jamais recopiable).
    identity = raw.get("host_identity") or {}
    for key, label in (
        ("hostname", "nom d'hôte"),
        ("address", "adresse"),
        ("volume_uuid", "UUID de volume"),
        ("canary", "identifiant / secret d'hôte"),
    ):
        if key not in identity:
            continue
        items.append(
            {
                "id": str(uuid7()),
                "category": "host_specific",
                "status": "noted",
                "path": None,
                "value": None,
                "label": f"{label} observé",
                # Jamais la valeur brute : elle ne doit pas fuir vers l'UI, le brouillon ni le modèle.
                "observation": f"{label} présent sur l'hôte (valeur masquée)",
                "observed_at": collected_at,
                "deduction": "Valeur propre à cet hôte : ne sera jamais recopiée dans le brouillon ni publiée.",
                "decidable": False,
            }
        )

    # Montages : hors image → à confirmer / layout incompatible.
    observed_mounts = {m["path"] for m in raw.get("mounts") or [] if isinstance(m, dict) and "path" in m}
    dedicated = sorted(observed_mounts - IMAGE_LAYOUT_MOUNTS)
    layout_warning = None
    if dedicated:
        layout_warning = (
            f"Montage(s) dédié(s) observé(s) ({', '.join(dedicated)}) : l'image actuelle "
            f"(layout « {layout_id} ») ne les fournit pas. Aucun repartitionnement implicite ; "
            "choisir une image compatible ou une installation automatisée (AT02)."
        )
        items.append(
            {
                "id": str(uuid7()),
                "category": "confirm",
                "status": "pending",
                "path": None,
                "value": None,
                "label": "Disposition des volumes incompatible avec l'image",
                "observation": f"montages = {sorted(observed_mounts)}",
                "observed_at": collected_at,
                "deduction": layout_warning,
                "decidable": True,
                "extension": {
                    "kind": "new_component",
                    "subject": "layout-volumes",
                    "summary": layout_warning,
                },
            }
        )

    # Paquets / services connus → composants à standardiser.
    packages = raw.get("packages") or {}
    services = raw.get("services") or {}
    for slug in COLLECTORS["packages"]["allowlist"]:
        pkg_ver = packages.get(slug)
        svc = services.get(slug) or {}
        if not pkg_ver and not (isinstance(svc, dict) and svc.get("active")):
            continue
        row = _latest_component(conn, slug, os_name)
        if row is None:
            items.append(
                {
                    "id": str(uuid7()),
                    "category": "confirm",
                    "status": "pending",
                    "path": None,
                    "value": None,
                    "label": f"Paquet / service « {slug} » observé sans composant catalogue",
                    "observation": f"package={pkg_ver!r} service={svc!r}",
                    "observed_at": collected_at,
                    "deduction": "Aucun composant publié ne couvre ce paquet : proposition d'extension à revoir.",
                    "decidable": True,
                    "extension": {
                        "kind": "new_component",
                        "subject": slug,
                        "summary": f"Paquet ou service « {slug} » observé sur le serveur de référence, "
                        "sans composant catalogue correspondant.",
                    },
                }
            )
            continue
        obs_bits = []
        if pkg_ver:
            obs_bits.append(f"paquet {slug}={pkg_ver}")
        if isinstance(svc, dict) and svc.get("active"):
            obs_bits.append(f"service {slug} actif")
        items.append(
            {
                "id": str(uuid7()),
                "category": "standardize",
                "status": "pending",
                "path": f"/components/{slug}",
                "value": {"version": row["version"]},
                "label": f"Ajouter le composant {slug} {row['version']}",
                "observation": ", ".join(obs_bits),
                "observed_at": collected_at,
                "deduction": f"Le catalogue couvre « {slug} » : proposer la version {row['version']} "
                "dans le brouillon (à confirmer par l'auteur).",
                "decidable": True,
            }
        )

    # Réglages : déclarés → standardiser ; sinon → confirmer.
    for setting in raw.get("settings") or []:
        if not isinstance(setting, dict):
            continue
        comp = setting.get("component")
        param = setting.get("parameter")
        value = setting.get("value")
        if not isinstance(comp, str) or not isinstance(param, str):
            continue
        row = _latest_component(conn, comp, os_name)
        spec = (row["contract"].get("parameters") if row else {}) or {}
        if row is None or param not in spec:
            items.append(
                {
                    "id": str(uuid7()),
                    "category": "confirm",
                    "status": "pending",
                    "path": None,
                    "value": None,
                    "label": f"Réglage non pris en charge : {comp}.{param}",
                    "observation": f"{comp}.{param} = {json.dumps(value, ensure_ascii=False)}",
                    "observed_at": collected_at,
                    "deduction": "Réglage hors contrat du composant : à confirmer comme extension, jamais appliqué.",
                    "decidable": True,
                    "extension": {
                        "kind": "component_parameter",
                        "subject": comp,
                        "summary": f"Paramètre « {param} » observé ({json.dumps(value, ensure_ascii=False)}) "
                        f"mais non déclaré au contrat de {comp}" + (f"@{row['version']}" if row else "") + ".",
                    },
                }
            )
            continue
        # secret_ref jamais recopié
        if spec[param].get("type") == "secret_ref":
            items.append(
                {
                    "id": str(uuid7()),
                    "category": "host_specific",
                    "status": "noted",
                    "path": None,
                    "value": None,
                    "label": f"Référence de secret {comp}.{param}",
                    "observation": f"{comp}.{param} (type secret_ref)",
                    "observed_at": collected_at,
                    "deduction": "Une référence de secret ne se règle pas dans l'atelier.",
                    "decidable": False,
                }
            )
            continue
        error = registry.check_parameter(spec[param], value)
        if error:
            items.append(
                {
                    "id": str(uuid7()),
                    "category": "confirm",
                    "status": "pending",
                    "path": None,
                    "value": None,
                    "label": f"Valeur hors bornes : {comp}.{param}",
                    "observation": f"{comp}.{param} = {json.dumps(value, ensure_ascii=False)} ({error})",
                    "observed_at": collected_at,
                    "deduction": "Valeur observée hors bornes du contrat : à confirmer, non appliquée.",
                    "decidable": True,
                    "extension": {
                        "kind": "component_parameter",
                        "subject": comp,
                        "summary": f"Valeur observée pour {param} hors bornes : {error}.",
                    },
                }
            )
            continue
        items.append(
            {
                "id": str(uuid7()),
                "category": "standardize",
                "status": "pending",
                "path": f"/components/{comp}/config/{param}",
                "value": value,
                "label": f"{comp} · {param} = {json.dumps(value, ensure_ascii=False)}",
                "observation": f"{comp}.{param} = {json.dumps(value, ensure_ascii=False)}",
                "observed_at": collected_at,
                "deduction": "Réglage déclaré par le composant : proposer la valeur observée dans le brouillon.",
                "decidable": True,
            }
        )

    # Empreinte avant/après : la collecte fictive ne mute pas le raw.
    after = _fingerprint(raw)
    return {
        "id": str(uuid7()),
        "kind": "reference_analysis",
        "source": {
            "kind": "fixture" if observation.get("simulated") else "host",
            "id": observation["id"],
            "label": observation.get("label") or observation["id"],
            "simulated": bool(observation.get("simulated")),
            "simulation_label": observation.get("simulation_label"),
        },
        "collected_at": collected_at,
        "collectors": observation.get("collectors") or [],
        "fingerprint_before": observation["fingerprint"],
        "fingerprint_after": after,
        "host_unchanged": observation["fingerprint"] == after,
        "layout_warning": layout_warning,
        "items": items,
        # Snapshot filtré pour l'UI : pas d'identité d'hôte, pas de canari.
        "snapshot": {
            "os": raw.get("os"),
            "mounts": [
                {"path": m.get("path"), "fstype": m.get("fstype"), "size_gb": m.get("size_gb")}
                for m in (raw.get("mounts") or [])
                if isinstance(m, dict)
            ],
            "packages": {
                k: v for k, v in (raw.get("packages") or {}).items() if k in COLLECTORS["packages"]["allowlist"]
            },
            "services": {
                k: {"active": (v or {}).get("active"), "enabled": (v or {}).get("enabled")}
                for k, v in (raw.get("services") or {}).items()
                if k in COLLECTORS["services"]["allowlist"] and isinstance(v, dict)
            },
        },
    }


def public_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Vue API : catégories groupées, sans données d'identité."""
    groups: dict[str, list[dict[str, Any]]] = {cat: [] for cat in CATEGORY_LABELS}
    for item in analysis.get("items") or []:
        entry = {
            "id": item["id"],
            "category": item["category"],
            "category_label": CATEGORY_LABELS[item["category"]],
            "status": item["status"],
            "label": item["label"],
            "observation": item["observation"],
            "observed_at": item["observed_at"],
            "deduction": item["deduction"],
            "decidable": item.get("decidable", False),
            "path": item.get("path"),
        }
        groups[item["category"]].append(entry)
    return {
        "id": analysis["id"],
        "source": analysis["source"],
        "collected_at": analysis["collected_at"],
        "collectors": analysis["collectors"],
        "fingerprint_before": analysis["fingerprint_before"],
        "fingerprint_after": analysis["fingerprint_after"],
        "host_unchanged": analysis["host_unchanged"],
        "layout_warning": analysis.get("layout_warning"),
        "snapshot": analysis.get("snapshot"),
        "categories": [{"id": cat, "label": CATEGORY_LABELS[cat], "items": groups[cat]} for cat in CATEGORY_LABELS],
        "pending_standardize": sum(
            1 for i in analysis.get("items") or [] if i["category"] == "standardize" and i["status"] == "pending"
        ),
    }


def find_item(sources: list[dict[str, Any]], item_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for analysis in sources:
        if analysis.get("kind") != "reference_analysis":
            continue
        for item in analysis.get("items") or []:
            if item["id"] == item_id:
                return analysis, item
    return None


def leak_tokens() -> tuple[str, ...]:
    """Jetons qui ne doivent jamais apparaître dans un brouillon ni un contexte modèle."""
    return (_CANARY_SECRET, _VOLUME_UUID, _HOST_ADDRESS, _HOSTNAME)


def sanitized_for_assistant(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Contexte assistant : uniquement catégories et déductions, jamais d'identité d'hôte."""
    out = []
    for analysis in sources:
        if analysis.get("kind") != "reference_analysis":
            continue
        out.append(
            {
                "source": analysis["source"]["label"],
                "simulated": analysis["source"]["simulated"],
                "layout_warning": analysis.get("layout_warning"),
                "items": [
                    {
                        "category": CATEGORY_LABELS[i["category"]],
                        "label": i["label"],
                        "deduction": i["deduction"],
                        "status": i["status"],
                    }
                    for i in analysis.get("items") or []
                    if i["category"] != "host_specific"
                ],
            }
        )
    return out


def assert_no_host_leaks(payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    for token in leak_tokens():
        if token in text:
            raise AssertionError(f"fuite d'identité d'hôte : {token!r}")


def clone_sources_without_raw(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Persistance : analyses sans le raw (identité déjà absente du snapshot)."""
    return copy.deepcopy(sources)
