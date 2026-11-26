"""F01 : import de catalogue depuis un dépôt Git local épinglé.

Seuls les dépôts locaux de laboratoire sont acceptés (pas d'URL réseau). Le commit doit
exister et être épinglé explicitement ; la revue est un acte distinct avant publication.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from uuid import UUID

from .. import audit
from ..db import Conn
from ..errors import ApiError
from ..ids import uuid7

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _run_git(repo: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise ApiError(500, "git_unavailable", "git n'est pas disponible sur l'appliance") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()[:300]
        raise ApiError(422, "git_error", f"opération git refusée : {detail or 'échec'}") from exc
    return completed.stdout.strip()


def resolve_local_repo(path: str | Path) -> Path:
    """Refuse les URL et chemins hors labo ; exige un dépôt Git local."""
    raw = path if isinstance(path, str) else str(path)
    # Path("https://…") normalise en "https:/…" : inspecter la forme d'origine quand c'est une str.
    if "://" in raw or raw.startswith("git@") or raw.startswith("ssh://") or raw.startswith("https:/") \
            or raw.startswith("http:/"):
        raise ApiError(422, "remote_git_forbidden", "seuls les dépôts Git locaux de laboratoire sont acceptés")
    repo = Path(path).expanduser().resolve()
    if not repo.is_dir():
        raise ApiError(404, "repo_not_found", "dépôt Git introuvable")
    if not (repo / ".git").exists() and not (repo / "HEAD").exists():
        git_dir = _run_git(repo, "rev-parse", "--git-dir")
        if not git_dir:
            raise ApiError(422, "not_a_git_repo", "le chemin n'est pas un dépôt Git")
    return repo


def resolve_commit(repo: Path, commit: str | None) -> str:
    """Épingle un commit : obligatoire ; HEAD seul est refusé (non épinglé)."""
    if not commit or not commit.strip():
        raise ApiError(422, "unpinned", "un commit SHA-1 (40 hex) est obligatoire pour importer depuis Git")
    sha = commit.strip().lower()
    if not COMMIT_RE.match(sha):
        # Accepte une forme courte uniquement si elle se résout en objet unique, puis refuse
        # si ce n'est pas 40 hex après résolution — on exige l'épingle complète.
        if re.fullmatch(r"[0-9a-f]{7,39}", sha):
            full = _run_git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
            if COMMIT_RE.match(full):
                raise ApiError(
                    422,
                    "unpinned",
                    f"indiquez le commit complet ({full}) : une forme courte n'est pas une épingle",
                )
        raise ApiError(422, "invalid_commit", "commit SHA-1 attendu (40 caractères hexadécimaux)")
    try:
        resolved = _run_git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    except ApiError:
        raise ApiError(422, "unknown_commit", "commit inconnu dans le dépôt local") from None
    if resolved != sha:
        raise ApiError(422, "commit_mismatch", "le commit fourni ne correspond pas à un objet du dépôt")
    return sha


def create_pin(
    conn: Conn,
    *,
    source_kind: str,
    repo_label: str,
    commit_sha: str | None = None,
    artifact_digest: str | None = None,
    actor_id: UUID | None = None,
) -> dict[str, Any]:
    if source_kind == "git_commit":
        if not commit_sha or not COMMIT_RE.match(commit_sha):
            raise ApiError(422, "unpinned", "commit SHA-1 (40 hex) obligatoire")
    elif source_kind == "artifact_digest":
        if not artifact_digest or not DIGEST_RE.match(artifact_digest):
            raise ApiError(422, "unpinned", "empreinte d'artefact (64 hex) obligatoire")
    elif source_kind != "filesystem":
        raise ApiError(422, "invalid_source", "source_kind invalide")
    pin_id = uuid7()
    conn.execute(
        "INSERT INTO catalog_source_pins (id, source_kind, repo_label, commit_sha, artifact_digest, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (pin_id, source_kind, repo_label[:200], commit_sha, artifact_digest, actor_id),
    )
    return conn.execute("SELECT * FROM catalog_source_pins WHERE id = %s", (pin_id,)).fetchone()


def review_pin(conn: Conn, pin_id: UUID, actor_id: UUID, reason: str) -> dict[str, Any]:
    if not reason or not reason.strip():
        raise ApiError(422, "reason_required", "un motif de revue est obligatoire", field="/reason")
    row = conn.execute("SELECT * FROM catalog_source_pins WHERE id = %s FOR UPDATE", (pin_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", "épingle de catalogue introuvable")
    if row["source_kind"] == "filesystem":
        raise ApiError(409, "not_reviewable", "une source filesystem n'exige pas de revue Git distincte")
    if row["reviewed"]:
        return row
    conn.execute(
        "UPDATE catalog_source_pins SET reviewed = true, reviewed_by = %s, reviewed_at = now(), "
        "review_reason = %s WHERE id = %s",
        (actor_id, reason.strip(), pin_id),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(actor_id),
        action="registry.catalog.pin_reviewed",
        outcome="ok",
        targets={"pin_id": str(pin_id), "source_kind": row["source_kind"]},
        versions={"commit_sha": row["commit_sha"], "artifact_digest": row["artifact_digest"]},
        policy_decision={"reason": reason.strip()},
    )
    return conn.execute("SELECT * FROM catalog_source_pins WHERE id = %s", (pin_id,)).fetchone()


def require_publishable_pin(conn: Conn, pin_id: UUID | None) -> None:
    """Refuse la publication si la source Git/artefact n'est pas épinglée et revue."""
    if pin_id is None:
        return
    pin = conn.execute("SELECT * FROM catalog_source_pins WHERE id = %s", (pin_id,)).fetchone()
    if pin is None:
        raise ApiError(409, "pin_missing", "épingle de source introuvable pour cette version")
    if pin["source_kind"] == "filesystem":
        return
    if pin["source_kind"] == "git_commit" and not pin["commit_sha"]:
        raise ApiError(409, "unpinned", "commit non épinglé : publication refusée")
    if pin["source_kind"] == "artifact_digest" and not pin["artifact_digest"]:
        raise ApiError(409, "unpinned", "artefact non épinglé : publication refusée")
    if not pin["reviewed"]:
        raise ApiError(
            409,
            "unreviewed_commit",
            "la source Git/artefact doit être revue avant publication",
        )


def checkout_tree(repo: Path, commit_sha: str, dest: Path) -> Path:
    """Exporte l'arbre du commit vers un répertoire jetable (lecture seule logique)."""
    dest.mkdir(parents=True, exist_ok=True)
    # archive | tar : pas de checkout qui modifierait le worktree du labo
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", commit_sha],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run(["tar", "-xf", "-", "-C", str(dest)], input=archive.stdout, check=True, timeout=60)
    return dest
