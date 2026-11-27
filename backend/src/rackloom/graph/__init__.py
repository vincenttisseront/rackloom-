"""F04 : graphe de dépendances (VM, templates, réseaux, datastores, certificats, services).

Construit un instantané en lecture seule depuis l'inventaire, les hôtes, les révisions
résolues et le registre TLS. Chaque arête porte une provenance : confirmée, inférée ou
inconnue. Les vues d'impact sont filtrées par projet : une ressource hors périmètre
n'apparaît pas (404), jamais une fuite inter-projets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ..db import Conn, Jsonb
from ..errors import ApiError, not_found
from ..identity.authz import Principal
from ..ids import uuid7
from ..inventory import latest_snapshot


def _node_key(kind: str, stable_id: str) -> str:
    return f"{kind}:{stable_id}"


def build(conn: Conn, *, built_by: str = "system") -> dict[str, Any]:
    """Reconstruit le graphe (remplace l'instantané précédent)."""
    # Conservé un seul build actif : on archive en cascade via DELETE.
    conn.execute("DELETE FROM dependency_graph_builds")
    build_id = uuid7()
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def ensure_node(
        kind: str,
        stable_id: str,
        name: str,
        *,
        project_id: UUID | None = None,
        environment_id: UUID | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> UUID:
        key = _node_key(kind, stable_id)
        existing = nodes.get(key)
        if existing is not None:
            if project_id is not None and existing["project_id"] is None:
                existing["project_id"] = project_id
            if environment_id is not None and existing["environment_id"] is None:
                existing["environment_id"] = environment_id
            if attributes:
                existing["attributes"] = {**existing["attributes"], **attributes}
            return existing["id"]
        nid = uuid7()
        nodes[key] = {
            "id": nid,
            "kind": kind,
            "stable_id": stable_id,
            "name": name,
            "project_id": project_id,
            "environment_id": environment_id,
            "attributes": dict(attributes or {}),
        }
        return nid

    def add_edge(
        from_id: UUID,
        to_id: UUID,
        relation: str,
        provenance: str,
        source: str,
        project_id: UUID | None,
    ) -> None:
        edges.append(
            {
                "id": uuid7(),
                "from_node_id": from_id,
                "to_node_id": to_id,
                "relation": relation,
                "provenance": provenance,
                "source": source,
                "project_id": project_id,
            }
        )

    # Inventaire (réseaux, datastores, clusters, images) — parents inférés.
    snap = latest_snapshot(conn)
    items: list[dict[str, Any]] = []
    if snap is not None:
        items = list(
            conn.execute(
                "SELECT kind, stable_id, name, parent_stable_id, attributes FROM inventory_items "
                "WHERE snapshot_id = %s",
                (snap["id"],),
            ).fetchall()
        )
    by_stable = {i["stable_id"]: i for i in items}
    for item in items:
        kind = item["kind"]
        if kind not in ("network", "datastore", "cluster", "image", "folder", "datacenter"):
            continue
        mapped = kind if kind in ("network", "datastore", "cluster", "image") else None
        if mapped is None:
            continue
        nid = ensure_node(mapped, item["stable_id"], item["name"], attributes=dict(item["attributes"] or {}))
        parent = item.get("parent_stable_id")
        if parent and parent in by_stable:
            p = by_stable[parent]
            pk = p["kind"] if p["kind"] in ("network", "datastore", "cluster", "image") else None
            if pk:
                pid = ensure_node(pk, p["stable_id"], p["name"], attributes=dict(p["attributes"] or {}))
                add_edge(nid, pid, "inventory_parent", "inferred", "inventory", None)

    # Certificats TLS (F45) — nœuds certificat ; lien unknown vers endpoint si pas de VM.
    for cert in conn.execute(
        "SELECT c.id, c.fingerprint_sha256, c.subject, c.alert_status, "
        "e.scheme, e.host, e.port "
        "FROM tls_certificate_records c JOIN approved_endpoints e ON e.id = c.endpoint_id"
    ).fetchall():
        endpoint = f"{cert['scheme']}://{cert['host']}:{cert['port']}"
        ensure_node(
            "certificate",
            cert["fingerprint_sha256"],
            cert["subject"][:200],
            attributes={"alert_status": cert["alert_status"], "endpoint": endpoint},
        )

    # Hôtes / déploiements / templates / placement résolu.
    hosts = list(
        conn.execute(
            "SELECT h.*, d.template_slug, d.template_version_id, d.service_status, "
            "t.version AS template_version, t.contract AS template_contract "
            "FROM hosts h "
            "JOIN deployments d ON d.id = h.deployment_id "
            "JOIN template_versions t ON t.id = d.template_version_id "
            "ORDER BY h.name"
        ).fetchall()
    )
    # Dernière révision résolue par déploiement (placement confirmé).
    revisions = {
        str(r["deployment_id"]): r
        for r in conn.execute(
            """
            SELECT DISTINCT ON (c.deployment_id) c.deployment_id, rr.resolved
            FROM changes c
            JOIN request_revisions rr ON rr.change_id = c.id AND rr.revision_no = c.current_revision_no
            WHERE c.deployment_id IS NOT NULL AND rr.outcome = 'resolved'
            ORDER BY c.deployment_id, c.updated_at DESC
            """
        ).fetchall()
    }

    for host in hosts:
        vm_id = ensure_node(
            "vm",
            host["vm_stable_id"],
            host["name"],
            project_id=host["project_id"],
            environment_id=host["environment_id"],
            attributes={
                "host_id": str(host["id"]),
                "deployment_id": str(host["deployment_id"]),
                "composition_member": host.get("composition_member"),
                "deployment_state": host["deployment_state"],
            },
        )
        tpl_stable = f"{host['template_slug']}@{host['template_version']}"
        tpl_id = ensure_node(
            "template",
            tpl_stable,
            f"{host['template_slug']} {host['template_version']}",
            attributes={"slug": host["template_slug"], "version": host["template_version"]},
        )
        add_edge(vm_id, tpl_id, "uses_template", "confirmed", "host", host["project_id"])

        svc_stable = str(host["deployment_id"])
        svc_id = ensure_node(
            "service",
            svc_stable,
            f"{host['template_slug']}@{host['name']}",
            project_id=host["project_id"],
            environment_id=host["environment_id"],
            attributes={"service_status": host["service_status"]},
        )
        add_edge(vm_id, svc_id, "provides_service", "confirmed", "deployment", host["project_id"])

        # Dépendances inter-membres (composition) : confirmées depuis le contrat.
        contract = host.get("template_contract") or {}
        member = host.get("composition_member")
        if member and isinstance(contract, dict):
            from .. import composition as composition_mod

            m = composition_mod.member_by_id(contract, member)
            for dep in (m or {}).get("depends_on") or []:
                # Lien symbolique member→member via attribut ; arête VM→service du membre requis
                # matérialisée si un hôte du membre existe dans le même déploiement.
                dep_hosts = [
                    h
                    for h in hosts
                    if h["deployment_id"] == host["deployment_id"] and h.get("composition_member") == dep["member"]
                ]
                for dh in dep_hosts:
                    dep_vm = ensure_node(
                        "vm",
                        dh["vm_stable_id"],
                        dh["name"],
                        project_id=dh["project_id"],
                        environment_id=dh["environment_id"],
                    )
                    add_edge(vm_id, dep_vm, "depends_on_member", "confirmed", "composition", host["project_id"])

        rev = revisions.get(str(host["deployment_id"]))
        resolved = (rev or {}).get("resolved") or {}
        placement = resolved.get("placement") or {}
        # Placement par instance si présent.
        inst = next(
            (i for i in (resolved.get("instances") or []) if i.get("name") == host["name"]),
            None,
        )
        for kind, relation, place_key in (
            ("network", "uses_network", "network"),
            ("datastore", "uses_datastore", "datastore"),
            ("cluster", "uses_cluster", "cluster"),
            ("image", "uses_image", "image"),
        ):
            ref = None
            if inst and isinstance(inst.get(place_key if kind != "cluster" else "cluster"), dict):
                # instances portent cluster/datastore ; network/image restent globaux.
                if kind in ("cluster", "datastore"):
                    ref = inst.get(kind)
            if ref is None:
                ref = placement.get(place_key if kind != "cluster" else "cluster")
                if kind == "image":
                    ref = placement.get("image")
                elif kind == "network":
                    ref = placement.get("network")
            if not isinstance(ref, dict) or not ref.get("stable_id"):
                continue
            nid = ensure_node(kind, ref["stable_id"], ref.get("name") or ref["stable_id"])
            add_edge(vm_id, nid, relation, "confirmed", "resolution", host["project_id"])

    conn.execute(
        "INSERT INTO dependency_graph_builds (id, built_by, node_count, edge_count, note) VALUES (%s, %s, %s, %s, %s)",
        (build_id, built_by, len(nodes), len(edges), "reconstruction lecture seule"),
    )
    for n in nodes.values():
        conn.execute(
            "INSERT INTO dependency_nodes (id, build_id, kind, stable_id, name, project_id, "
            "environment_id, attributes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                n["id"],
                build_id,
                n["kind"],
                n["stable_id"],
                n["name"],
                n["project_id"],
                n["environment_id"],
                Jsonb(n["attributes"]),
            ),
        )
    # Dédupliquer arêtes (même from/to/relation).
    seen_e: set[tuple[UUID, UUID, str]] = set()
    edge_count = 0
    for e in edges:
        edge_key = (e["from_node_id"], e["to_node_id"], e["relation"])
        if edge_key in seen_e:
            continue
        seen_e.add(edge_key)
        conn.execute(
            "INSERT INTO dependency_edges (id, build_id, from_node_id, to_node_id, relation, "
            "provenance, source, project_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                e["id"],
                build_id,
                e["from_node_id"],
                e["to_node_id"],
                e["relation"],
                e["provenance"],
                e["source"],
                e["project_id"],
            ),
        )
        edge_count += 1
    conn.execute(
        "UPDATE dependency_graph_builds SET node_count = %s, edge_count = %s WHERE id = %s",
        (len(nodes), edge_count, build_id),
    )
    return {
        "build_id": str(build_id),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "node_count": len(nodes),
        "edge_count": edge_count,
    }


def latest_build(conn: Conn) -> dict[str, Any] | None:
    return conn.execute("SELECT * FROM dependency_graph_builds ORDER BY built_at DESC LIMIT 1").fetchone()


def _visible_project_ids(principal: Principal) -> set[UUID] | None:
    """None = auditeur global (tous les projets) ; sinon l'ensemble des projets du principal (D7)."""
    return principal.visible_project_ids()


def _node_visible(node: dict[str, Any], allowed: set[UUID] | None) -> bool:
    if allowed is None:
        return True
    if node["project_id"] is None:
        # Ressource d'inventaire partagée : visible seulement via une arête vers une VM du périmètre
        # (filtrée à l'affichage impact) ; en liste graphe on l'expose sans attributs projet.
        return True
    return node["project_id"] in allowed


def get_graph(conn: Conn, principal: Principal) -> dict[str, Any]:
    build_row = latest_build(conn)
    if build_row is None:
        return {"build": None, "nodes": [], "edges": [], "notice": "Aucun graphe construit."}
    allowed = _visible_project_ids(principal)
    nodes = list(
        conn.execute(
            "SELECT * FROM dependency_nodes WHERE build_id = %s ORDER BY kind, name",
            (build_row["id"],),
        ).fetchall()
    )
    visible_ids = {n["id"] for n in nodes if _node_visible(n, allowed)}
    # Masquer les VM / services hors périmètre.
    if allowed is not None:
        visible_ids = {n["id"] for n in nodes if n["project_id"] is None or n["project_id"] in allowed}
    edges = list(
        conn.execute(
            "SELECT * FROM dependency_edges WHERE build_id = %s",
            (build_row["id"],),
        ).fetchall()
    )
    edges = [
        e
        for e in edges
        if e["from_node_id"] in visible_ids
        and e["to_node_id"] in visible_ids
        and (allowed is None or e["project_id"] is None or e["project_id"] in allowed)
    ]
    # Nœuds d'inventaire uniquement s'ils apparaissent dans une arête visible.
    linked = {e["from_node_id"] for e in edges} | {e["to_node_id"] for e in edges}
    out_nodes = [
        _node_view(n)
        for n in nodes
        if n["id"] in visible_ids and (n["project_id"] is not None or n["id"] in linked or n["kind"] != "vm")
    ]
    return {
        "build": {
            "id": str(build_row["id"]),
            "built_at": build_row["built_at"].isoformat(),
            "node_count": build_row["node_count"],
            "edge_count": build_row["edge_count"],
        },
        "nodes": out_nodes,
        "edges": [_edge_view(e) for e in edges],
    }


def impact(
    conn: Conn,
    principal: Principal,
    *,
    kind: str,
    stable_id: str,
) -> dict[str, Any]:
    """VM (et services) qui dépendent d'une ressource ; hors périmètre → 404."""
    if kind not in ("network", "datastore", "cluster", "image", "template", "certificate", "service", "vm"):
        raise ApiError(422, "invalid_kind", "type de ressource non pris en charge", field="/kind")
    build_row = latest_build(conn)
    if build_row is None:
        raise not_found("graphe de dépendances")
    target = conn.execute(
        "SELECT * FROM dependency_nodes WHERE build_id = %s AND kind = %s AND stable_id = %s",
        (build_row["id"], kind, stable_id),
    ).fetchone()
    if target is None:
        raise not_found("ressource")
    allowed = _visible_project_ids(principal)
    # Ressource projet-scopée hors périmètre → 404 (pas 403).
    if target["project_id"] is not None and allowed is not None and target["project_id"] not in allowed:
        raise not_found("ressource")

    # Arêtes entrantes vers la cible (VM uses_network → network).
    incoming = list(
        conn.execute(
            "SELECT e.*, n.kind AS from_kind, n.stable_id AS from_stable_id, n.name AS from_name, "
            "n.project_id AS from_project_id, n.attributes AS from_attributes "
            "FROM dependency_edges e "
            "JOIN dependency_nodes n ON n.id = e.from_node_id "
            "WHERE e.build_id = %s AND e.to_node_id = %s",
            (build_row["id"], target["id"]),
        ).fetchall()
    )
    dependents: list[dict[str, Any]] = []
    hidden = 0
    for row in incoming:
        if row["from_kind"] != "vm":
            continue
        if allowed is not None and (row["from_project_id"] is None or row["from_project_id"] not in allowed):
            # Compte masqué sans révéler l'identité.
            hidden += 1
            continue
        if allowed is not None and row["project_id"] is not None and row["project_id"] not in allowed:
            hidden += 1
            continue
        dependents.append(
            {
                "kind": "vm",
                "stable_id": row["from_stable_id"],
                "name": row["from_name"],
                "project_id": str(row["from_project_id"]) if row["from_project_id"] else None,
                "relation": row["relation"],
                "provenance": row["provenance"],
                "host_id": (row["from_attributes"] or {}).get("host_id"),
                "deployment_id": (row["from_attributes"] or {}).get("deployment_id"),
            }
        )
    return {
        "resource": _node_view(target),
        "dependents": dependents,
        "hidden_count": hidden,
        "notice": (f"{hidden} dépendance(s) hors de votre périmètre sont masquées." if hidden else None),
    }


def _node_view(n: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(n["id"]),
        "kind": n["kind"],
        "stable_id": n["stable_id"],
        "name": n["name"],
        "project_id": str(n["project_id"]) if n.get("project_id") else None,
        "environment_id": str(n["environment_id"]) if n.get("environment_id") else None,
        "attributes": n.get("attributes") or {},
    }


def _edge_view(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(e["id"]),
        "from_node_id": str(e["from_node_id"]),
        "to_node_id": str(e["to_node_id"]),
        "relation": e["relation"],
        "provenance": e["provenance"],
        "source": e["source"],
        "project_id": str(e["project_id"]) if e.get("project_id") else None,
    }
