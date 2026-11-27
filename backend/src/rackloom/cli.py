"""Commandes d'exploitation : `rackloom <commande>`.

Aucune commande n'affiche de secret, sauf `bootstrap-token` (jeton à usage unique, 30 min,
affiché une seule fois sur la console de l'appliance).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import get_settings


def _conn():
    from . import db

    return db.connect(autocommit=False)


def cmd_migrate(args: argparse.Namespace) -> int:
    from . import db

    db.migrate()
    print("migrations appliquées")
    return 0


def cmd_keys_init(args: argparse.Namespace) -> int:
    from . import security

    directory = Path(args.dir) if args.dir else get_settings().keys_dir
    created = security.generate_keys(directory)
    print(f"{len(created)} clé(s) créée(s) dans {directory} ; clés existantes conservées")
    return 0


def cmd_bootstrap_token(args: argparse.Namespace) -> int:
    from .identity.service import create_bootstrap_token

    with _conn() as conn:
        if conn.execute("SELECT 1 FROM role_bindings WHERE role = 'platform_admin' AND revoked_at IS NULL").fetchone():
            print("un administrateur existe déjà : jeton d'amorçage refusé", file=sys.stderr)
            return 1
        token = create_bootstrap_token(conn, minutes=30)
        conn.commit()
    print(token)
    return 0


def cmd_demo_seed(args: argparse.Namespace) -> int:
    from . import demo

    root = Path(args.root)
    credentials = Path(args.credentials_file) if args.credentials_file else root / ".lab" / "demo-credentials.txt"
    with _conn() as conn:
        try:
            result = demo.seed(conn, root, credentials_file=credentials)
        except demo.DemoRefused as exc:
            print(str(exc), file=sys.stderr)
            return 1
        conn.commit()
    print(
        json.dumps(
            {
                "projects": sorted(result["projects"]),
                "generated_passwords": result["generated_passwords"],
                "credentials_file": str(credentials) if result["generated_passwords"] else None,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_catalog_load(args: argparse.Namespace) -> int:
    from . import registry

    with _conn() as conn:
        counts = registry.load(conn, Path(args.dir) if args.dir else get_settings().catalog_dir)
        conn.commit()
    print(json.dumps(counts))
    return 0


def cmd_catalog_import_git(args: argparse.Namespace) -> int:
    """F01 : importe un catalogue depuis un dépôt Git local épinglé (non publié)."""
    from . import registry

    with _conn() as conn:
        result = registry.load_from_git(
            conn,
            Path(args.repo),
            args.commit,
            catalog_subdir=args.subdir or "",
        )
        conn.commit()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_catalog_review_pin(args: argparse.Namespace) -> int:
    from uuid import UUID

    from .registry import git_source

    with _conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = %s", (args.as_user,)).fetchone()
        if user is None:
            raise SystemExit(f"utilisateur inconnu : {args.as_user}")
        pin = git_source.review_pin(conn, UUID(args.pin_id), user["id"], args.reason)
        conn.commit()
    print(json.dumps({"id": str(pin["id"]), "reviewed": pin["reviewed"], "commit_sha": pin["commit_sha"]}))
    return 0


def cmd_jobs_list(args: argparse.Namespace) -> int:
    with _conn() as conn:
        query = "SELECT id, kind, state, attempts, lease_expires_at, blocked_reason, created_at FROM jobs"
        if args.stuck:
            query += (
                " WHERE (state IN ('leased', 'running') AND lease_expires_at < now()) "
                "OR (state = 'queued' AND created_at < now() - interval '15 minutes')"
            )
        rows = conn.execute(query + " ORDER BY created_at DESC LIMIT 200").fetchall()
    for r in rows:
        print(f"{r['id']} {r['kind']:<18} {r['state']:<10} tentatives={r['attempts']} {r['blocked_reason'] or ''}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from .changes.service import change_ref
    from .executions import queue

    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM changes WHERE state = 'uncertain' " + ("AND id = %s" if args.change else ""),
            (args.change,) if args.change else (),
        ).fetchall()
        for change in rows:
            queue.enqueue(
                conn,
                kind="reconcile",
                change=change,
                stack_id=None,
                idempotency_key=f"reconcile:cli:{change['id']}:{change['row_version']}",
                payload={},
            )
            print(f"réconciliation en file pour {change_ref(change)}")
        conn.commit()
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    from . import audit

    with _conn() as conn:
        result = audit.verify(conn)
    print(json.dumps(result))
    return 0 if result["ok"] else 2


def cmd_openapi_export(args: argparse.Namespace) -> int:
    from .api.app import create_app

    spec = create_app().openapi()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(spec['paths'])} chemins exportés vers {out}")
    return 0


def cmd_ci_submit(args: argparse.Namespace) -> int:
    import httpx

    from .ci_client import CiClient, CiError

    need: dict[str, Any] = json.loads(Path(args.need).read_text(encoding="utf-8"))
    password = Path(args.password_file).read_text(encoding="utf-8").strip()
    with httpx.Client(base_url=args.url, verify=args.ca or True, timeout=30) as http:
        client = CiClient(http)
        try:
            client.login(args.email, password)
            result = client.submit_need(args.project, need, submit=args.submit)
        except CiError as exc:
            print(json.dumps({"status": exc.status, **exc.body}, ensure_ascii=False), file=sys.stderr)
            return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_lab_keys(args: argparse.Namespace) -> int:
    from . import lab

    pub_dir = Path(args.pub_dir)
    with _conn() as conn:
        if args.if_missing and lab.identities_present(conn, pub_dir):
            print("identités SSH de labo déjà présentes ; conservées")
            return 0
        publics = lab.generate_identities(conn, pub_dir)
        conn.commit()
    print(f"{len(publics)} identité(s) SSH de labo régénérée(s) ; clés publiques dans {args.pub_dir}")
    return 0


def cmd_lab_anchor(args: argparse.Namespace) -> int:
    from . import lab

    line = sys.stdin.readline().strip()
    with _conn() as conn:
        row = lab.import_anchor(conn, args.address, line)
        conn.commit()
    print(f"ancre importée pour {args.address} : {row['fingerprint']}")
    return 0


def cmd_lab_nodes(args: argparse.Namespace) -> int:
    from . import lab

    nodes = []
    for item in args.node:
        name, _, address = item.partition("=")
        nodes.append({"name": name, "address": address or name})
    with _conn() as conn:
        lab.set_nodes(conn, nodes)
        conn.commit()
    print(f"{len(nodes)} nœud(s) de labo déclaré(s)")
    return 0


def cmd_grants(args: argparse.Namespace) -> int:
    from . import projects

    revoked = args.grants_command == "revoke"
    with _conn() as conn:
        ids = projects.set_grants_revoked(conn, args.project, args.environment, args.kind, revoked, args.reason)
        conn.commit()
    verb = "retiré(s)" if revoked else "rétabli(s)"
    print(
        f"{len(ids)} octroi(s) {args.kind} {verb} pour {args.project}/{args.environment}"
        + (f" : {', '.join(ids)}" if ids else "")
    )
    return 0


def cmd_artifacts_scan(args: argparse.Namespace) -> int:
    """T24 : déchiffre les artefacts de classe B et y cherche la valeur lue sur l'entrée standard."""
    from . import artifacts

    needle = sys.stdin.readline().strip().encode()
    if len(needle) < 16:
        print("valeur à rechercher absente ou trop courte (entrée standard)", file=sys.stderr)
        return 2
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM artifacts WHERE key_class = 'B' ORDER BY created_at").fetchall()
    found = [str(r["id"]) for r in rows if needle in artifacts.read(r)]
    print(
        f"{len(rows)} artefact(s) de classe B déchiffré(s), {len(found)} contenant la valeur"
        + (f" : {', '.join(found)}" if found else "")
    )
    return 1 if found else 0


def cmd_chat_eval(args: argparse.Namespace) -> int:
    """F46 : banc d'évaluation Ollama (conversations fictives, aucun job)."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from .chat import eval as chat_eval
    from .identity.authz import Principal, load_bindings

    with _conn() as conn:
        admin = conn.execute(
            "SELECT u.* FROM users u JOIN role_bindings b ON b.user_id = u.id "
            "WHERE b.role = 'platform_admin' AND b.revoked_at IS NULL LIMIT 1"
        ).fetchone()
        if admin is None:
            print("aucun administrateur de plateforme : banc refusé", file=sys.stderr)
            return 1
        principal = Principal(
            user_id=admin["id"],
            email=admin["email"],
            display_name=admin["display_name"],
            session_id=uuid4(),
            auth_method="cli",
            auth_time=datetime.now(UTC),
            bindings=load_bindings(conn, admin["id"]),
        )
        from .errors import ApiError

        try:
            report = chat_eval.run_bench(
                conn, principal, adapter=args.adapter, scenario=args.scenario, project_slug=args.project
            )
        except ApiError as exc:
            print(json.dumps({"code": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
            return 1
        conn.commit()
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("id", "verdict", "adapter", "model", "prompt_id", "scenario", "failure_codes", "metrics")
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["verdict"] == "passed" else 2


def cmd_emergency_activate(args: argparse.Namespace) -> int:
    """F14 : active une intervention d'urgence depuis la console."""
    from .errors import ApiError
    from .identity import emergency

    with _conn() as conn:
        user_ids = []
        for email in args.users:
            row = conn.execute(
                "SELECT id FROM users WHERE email = %s AND status = 'active' AND kind = 'human'",
                (email.lower(),),
            ).fetchone()
            if row is None:
                print(f"utilisateur inconnu : {email}", file=sys.stderr)
                return 1
            user_ids.append(row["id"])
        try:
            result = emergency.activate(
                conn,
                reason=args.reason,
                minutes=args.minutes,
                user_ids=user_ids,
                actor="console",
                simulate_idp_down=not args.keep_sso,
            )
        except ApiError as exc:
            print(json.dumps({"code": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
            return 1
        conn.commit()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_emergency_status(args: argparse.Namespace) -> int:
    from .identity import emergency

    with _conn() as conn:
        print(json.dumps(emergency.status(conn), ensure_ascii=False))
    return 0


def cmd_emergency_close(args: argparse.Namespace) -> int:
    from .errors import ApiError
    from .identity import emergency

    with _conn() as conn:
        try:
            result = emergency.close(conn, reason=args.reason, actor="console", restore_sso=True)
        except ApiError as exc:
            print(json.dumps({"code": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
            return 1
        conn.commit()
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_bundle_build(args: argparse.Namespace) -> int:
    """F17 : construit un lot signé de démo (charges utiles fictives) vers un fichier."""
    from . import offline_bundles

    private_raw, public_raw = offline_bundles.ensure_signing_keypair()
    package = offline_bundles.build_package(
        slug=args.slug,
        version=args.version,
        title=args.title,
        items=offline_bundles.demo_items(),
        private_raw=private_raw,
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(package)
    out.chmod(0o600)
    print(
        json.dumps(
            {
                "path": str(out),
                "size_bytes": len(package),
                "signer_key_id": offline_bundles.key_id_for(public_raw),
                "notice": "lot signé ; clé privée hors fichier",
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_bundle_verify(args: argparse.Namespace) -> int:
    """F17 : vérifie intégrité et signature d'un lot (sans importer)."""
    from . import offline_bundles

    data = Path(args.path).read_bytes()
    _, public_raw = offline_bundles.ensure_signing_keypair()
    manifest, signature, signer_key_id, payloads = offline_bundles.parse_package(data)
    digest = offline_bundles.verify_integrity(manifest, signature, signer_key_id, payloads, public_raw)
    offline_bundles.check_compatibility(manifest)
    print(
        json.dumps(
            {
                "ok": True,
                "slug": manifest["slug"],
                "version": manifest["version"],
                "content_digest": digest,
                "items": [
                    {
                        "kind": i["kind"],
                        "name": i["name"],
                        "version": i["version"],
                        "digest": i["digest"],
                        "size_bytes": i["size_bytes"],
                    }
                    for i in manifest["items"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_bundle_import(args: argparse.Namespace) -> int:
    """F17 : importe un lot après vérifications (console admin plateforme)."""
    from datetime import UTC, datetime

    from . import offline_bundles
    from .identity.authz import Binding, Principal
    from .ids import uuid7

    data = Path(args.path).read_bytes()
    with _conn() as conn:
        offline_bundles.ensure_trust_key(conn)
        admin = conn.execute(
            "SELECT u.id, u.email, u.display_name FROM users u "
            "JOIN role_bindings b ON b.user_id = u.id "
            "WHERE b.role = 'platform_admin' AND b.revoked_at IS NULL LIMIT 1"
        ).fetchone()
        if admin is None:
            print("aucun platform_admin", file=sys.stderr)
            return 2
        principal = Principal(
            user_id=admin["id"],
            email=admin["email"],
            display_name=admin["display_name"],
            session_id=uuid7(),
            auth_method="cli",
            auth_time=datetime.now(UTC),
            bindings=[Binding(role="platform_admin", project_id=None, environment_id=None)],
        )
        view = offline_bundles.import_package(conn, principal, data)
        conn.commit()
        print(
            json.dumps(
                {"id": view["id"], "status": view["status"], "items": view["items"]},
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


def cmd_emergency_idp_down(args: argparse.Namespace) -> int:
    from .identity import emergency

    with _conn() as conn:
        emergency.set_sso_available(conn, False, actor="console")
        conn.commit()
    print(json.dumps({"sso_available": False}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rackloom")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate").set_defaults(func=cmd_migrate)
    keys = sub.add_parser("keys").add_subparsers(dest="keys_command", required=True)
    k = keys.add_parser("init")
    k.add_argument("--dir")
    k.set_defaults(func=cmd_keys_init)
    sub.add_parser("bootstrap-token").set_defaults(func=cmd_bootstrap_token)
    demo = sub.add_parser("demo").add_subparsers(dest="demo_command", required=True)
    d = demo.add_parser("seed")
    d.add_argument("--root", default=".")
    d.add_argument("--credentials-file")
    d.set_defaults(func=cmd_demo_seed)
    catalog = sub.add_parser("catalog").add_subparsers(dest="catalog_command", required=True)
    c = catalog.add_parser("load")
    c.add_argument("--dir")
    c.set_defaults(func=cmd_catalog_load)
    cg = catalog.add_parser("import-git", help="F01 : charge un catalogue depuis un dépôt Git local épinglé")
    cg.add_argument("--repo", required=True, help="chemin local du dépôt (pas d'URL)")
    cg.add_argument("--commit", required=True, help="SHA-1 complet (40 hex)")
    cg.add_argument("--subdir", default="", help="sous-répertoire du catalogue dans le dépôt")
    cg.set_defaults(func=cmd_catalog_import_git)
    cr = catalog.add_parser("review-pin", help="F01 : marque une épingle Git comme revue")
    cr.add_argument("--pin-id", required=True)
    cr.add_argument("--reason", required=True)
    cr.add_argument("--as-user", required=True, help="email du relecteur (template_publisher)")
    cr.set_defaults(func=cmd_catalog_review_pin)
    jobs = sub.add_parser("jobs").add_subparsers(dest="jobs_command", required=True)
    j = jobs.add_parser("list")
    j.add_argument("--stuck", action="store_true")
    j.set_defaults(func=cmd_jobs_list)
    r = sub.add_parser("reconcile")
    r.add_argument("--change")
    r.set_defaults(func=cmd_reconcile)
    audit = sub.add_parser("audit").add_subparsers(dest="audit_command", required=True)
    audit.add_parser("verify").set_defaults(func=cmd_audit_verify)
    openapi = sub.add_parser("openapi").add_subparsers(dest="openapi_command", required=True)
    o = openapi.add_parser("export")
    o.add_argument("--output", default="openapi/openapi.json")
    o.set_defaults(func=cmd_openapi_export)
    lab = sub.add_parser("lab").add_subparsers(dest="lab_command", required=True)
    lk = lab.add_parser("keys", help="régénère les identités SSH de labo (clés privées scellées en base)")
    lk.add_argument("--pub-dir", default=".lab/lab-keys")
    lk.add_argument("--if-missing", action="store_true", help="ne régénère pas des identités déjà présentes")
    lk.set_defaults(func=cmd_lab_keys)
    la = lab.add_parser("anchor", help="importe la clé d'hôte d'un nœud (ligne OpenSSH sur l'entrée standard)")
    la.add_argument("--address", required=True)
    la.set_defaults(func=cmd_lab_anchor)
    ln = lab.add_parser("nodes", help="déclare les nœuds de labo (nom=adresse)")
    ln.add_argument("node", nargs="+")
    ln.set_defaults(func=cmd_lab_nodes)
    grants = sub.add_parser("grants", help="octrois de ressources d'un environnement").add_subparsers(
        dest="grants_command", required=True
    )
    for name in ("revoke", "restore"):
        g = grants.add_parser(name)
        g.add_argument("--project", required=True)
        g.add_argument("--environment", required=True)
        g.add_argument("--kind", required=True, choices=["network", "image", "cluster", "datastore", "folder"])
        g.add_argument("--reason", required=True)
        g.set_defaults(func=cmd_grants)
    arts = sub.add_parser("artifacts").add_subparsers(dest="artifacts_command", required=True)
    arts.add_parser(
        "scan", help="T24 : cherche une valeur (entrée standard) dans les artefacts de classe B"
    ).set_defaults(func=cmd_artifacts_scan)
    ci = sub.add_parser("ci").add_subparsers(dest="ci_command", required=True)
    s = ci.add_parser("submit", help="dépose un besoin (JSON, catégorie A) par l'API")
    s.add_argument("--url", required=True)
    s.add_argument("--ca", help="CA de l'appliance (PEM) ; la vérification TLS ne se désactive pas")
    s.add_argument("--email", required=True)
    s.add_argument("--password-file", required=True)
    s.add_argument("--project", required=True)
    s.add_argument("--need", required=True)
    s.add_argument("--submit", action="store_true")
    s.set_defaults(func=cmd_ci_submit)
    chat = sub.add_parser("chat").add_subparsers(dest="chat_command", required=True)
    ce = chat.add_parser("eval", help="F46 : banc d'évaluation (conversations fictives, aucun job)")
    ce.add_argument("--adapter", choices=["fake", "ollama"])
    ce.add_argument("--scenario", help="scénario de l'adaptateur fictif à qualifier")
    ce.add_argument("--project", default="vitrine", help="projet dont le contexte sert de banc")
    ce.set_defaults(func=cmd_chat_eval)
    emergency = sub.add_parser("emergency").add_subparsers(dest="emergency_command", required=True)
    ea = emergency.add_parser(
        "activate",
        help="F14 : active l'accès d'urgence local (console) si le SSO est indisponible",
    )
    ea.add_argument("--reason", required=True)
    ea.add_argument("--minutes", type=int, default=60)
    ea.add_argument(
        "--user",
        action="append",
        dest="users",
        required=True,
        help="email local autorisé (répétable)",
    )
    ea.add_argument(
        "--keep-sso",
        action="store_true",
        help="ne simule pas la panne IdP (SSO reste disponible)",
    )
    ea.set_defaults(func=cmd_emergency_activate)
    emergency.add_parser("status", help="F14 : état SSO et intervention").set_defaults(func=cmd_emergency_status)
    ec = emergency.add_parser("close", help="F14 : ferme l'intervention et retire l'accès")
    ec.add_argument("--reason", required=True)
    ec.set_defaults(func=cmd_emergency_close)
    es = emergency.add_parser("simulate-idp-down", help="F14 : marque le SSO indisponible (laboratoire / démo)")
    es.set_defaults(func=cmd_emergency_idp_down)
    bundle = sub.add_parser("bundle").add_subparsers(dest="bundle_command", required=True)
    bb = bundle.add_parser("build", help="F17 : construit un lot signé de démo (hors ligne)")
    bb.add_argument("--slug", default="lab-offline")
    bb.add_argument("--version", default="1.0.0")
    bb.add_argument("--title", default="Lot de démo hors ligne")
    bb.add_argument("--output", "-o", required=True)
    bb.set_defaults(func=cmd_bundle_build)
    bv = bundle.add_parser("verify", help="F17 : vérifie intégrité et compatibilité d'un lot")
    bv.add_argument("path")
    bv.set_defaults(func=cmd_bundle_verify)
    bi = bundle.add_parser("import", help="F17 : importe un lot après vérifications")
    bi.add_argument("path")
    bi.set_defaults(func=cmd_bundle_import)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
