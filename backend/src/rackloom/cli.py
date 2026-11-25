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
    print(json.dumps({"projects": sorted(result["projects"]), "generated_passwords": result["generated_passwords"],
                      "credentials_file": str(credentials) if result["generated_passwords"] else None},
                     ensure_ascii=False))
    return 0


def cmd_catalog_load(args: argparse.Namespace) -> int:
    from . import registry

    with _conn() as conn:
        counts = registry.load(conn, Path(args.dir) if args.dir else get_settings().catalog_dir)
        conn.commit()
    print(json.dumps(counts))
    return 0


def cmd_jobs_list(args: argparse.Namespace) -> int:
    with _conn() as conn:
        query = "SELECT id, kind, state, attempts, lease_expires_at, blocked_reason, created_at FROM jobs"
        if args.stuck:
            query += (" WHERE (state IN ('leased', 'running') AND lease_expires_at < now()) "
                      "OR (state = 'queued' AND created_at < now() - interval '15 minutes')")
        rows = conn.execute(query + " ORDER BY created_at DESC LIMIT 200").fetchall()
    for r in rows:
        print(f"{r['id']} {r['kind']:<18} {r['state']:<10} tentatives={r['attempts']} "
              f"{r['blocked_reason'] or ''}")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    from .changes.service import change_ref
    from .executions import queue

    with _conn() as conn:
        rows = conn.execute("SELECT * FROM changes WHERE state = 'uncertain' "
                            + ("AND id = %s" if args.change else ""),
                            (args.change,) if args.change else ()).fetchall()
        for change in rows:
            queue.enqueue(conn, kind="reconcile", change=change, stack_id=None,
                          idempotency_key=f"reconcile:cli:{change['id']}:{change['row_version']}", payload={})
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
