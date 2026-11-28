"""Réauthentification forte pour actions sensibles (F12).

Preuve fraîche : mot de passe local (step-up), flux OIDC IdP (`acr` / `auth_time` via
`POST /auth/oidc/reauth` → callback), ou preuve simulée d'IdP de test (hors production).
Niveau et fraîcheur sont vérifiés côté serveur. Lire un plan n'exige aucune preuve fraîche.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from .. import audit, clock
from ..config import get_settings
from ..db import Conn
from ..errors import ApiError
from ..security import verify_password
from .authz import Principal

# Niveaux d'assurance croissants.
ACR_RANK = {"pwd": 1, "otp": 2, "mfa": 3, "urn:rackloom:step-up": 3}

# Actions sensibles : approbation, gel, secrets, rôles, urgence, exceptions, grants.
DEFAULT_MAX_AGE = timedelta(minutes=5)
SENSITIVE_MIN_ACR = "urn:rackloom:step-up"


def acr_ok(current: str, required: str) -> bool:
    return ACR_RANK.get(current, 0) >= ACR_RANK.get(required, 99)


def freshness(principal: Principal, *, max_age: timedelta = DEFAULT_MAX_AGE, at=None) -> bool:
    instant = at or clock.now()
    auth_time = principal.auth_time
    if auth_time.tzinfo is None:
        auth_time = auth_time.replace(tzinfo=UTC)
    return auth_time + max_age >= instant


def require_fresh(
    principal: Principal,
    *,
    min_acr: str = SENSITIVE_MIN_ACR,
    max_age: timedelta = DEFAULT_MAX_AGE,
    action: str = "action_sensible",
) -> None:
    """Refuse si la preuve est absente, trop faible ou trop ancienne."""
    current_acr = getattr(principal, "acr", None) or (
        "urn:rackloom:step-up" if principal.auth_method in ("step_up", "oidc_step_up") else "pwd"
    )
    if not acr_ok(current_acr, min_acr):
        raise ApiError(
            403,
            "reauth_required",
            f"réauthentification forte requise pour {action} (niveau {min_acr})",
            details={"required_acr": min_acr, "current_acr": current_acr},
        )
    if not freshness(principal, max_age=max_age):
        raise ApiError(
            403,
            "reauth_stale",
            f"preuve d'authentification trop ancienne pour {action} (max {int(max_age.total_seconds())}s)",
            details={"max_age_seconds": int(max_age.total_seconds())},
        )


def step_up(
    conn: Conn,
    principal: Principal,
    *,
    password: str | None = None,
    idp_proof: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Rafraîchit auth_time et élève l'acr de la session courante."""
    settings = get_settings()
    acr = "urn:rackloom:step-up"
    amr = ["pwd"]
    method = "step_up"

    if idp_proof is not None:
        # Preuve simulée : refusée en production. Avec OIDC réel, utiliser /auth/oidc/reauth.
        if settings.profile == "production":
            raise ApiError(403, "idp_proof_refused", "preuve IdP simulée refusée en production")
        from . import oidc as oidc_mod

        if oidc_mod.enabled(conn):
            raise ApiError(
                422,
                "use_oidc_reauth",
                "OIDC activé : utilisez POST /auth/oidc/reauth (redirection IdP) plutôt qu'une preuve simulée",
                field="/idp_proof",
            )
        if not isinstance(idp_proof, dict):
            raise ApiError(422, "invalid_proof", "objet idp_proof attendu", field="/idp_proof")
        claimed_acr = str(idp_proof.get("acr") or "")
        if claimed_acr not in ACR_RANK:
            raise ApiError(422, "invalid_acr", "acr IdP non reconnu", field="/idp_proof/acr")
        auth_time_raw = idp_proof.get("auth_time")
        try:
            if isinstance(auth_time_raw, (int, float)):
                claimed_time = datetime.fromtimestamp(float(auth_time_raw), tz=UTC)
            else:
                claimed_time = datetime.fromisoformat(str(auth_time_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ApiError(422, "invalid_auth_time", "auth_time IdP invalide", field="/idp_proof/auth_time") from exc
        max_age = timedelta(seconds=int(idp_proof.get("max_age") or DEFAULT_MAX_AGE.total_seconds()))
        if claimed_time + max_age < clock.now():
            raise ApiError(403, "reauth_stale", "preuve IdP trop ancienne")
        acr = claimed_acr
        amr = list(idp_proof.get("amr") or ["otp"])
        method = "oidc_step_up"
        new_auth_time = claimed_time
    else:
        if not password:
            raise ApiError(422, "password_required", "mot de passe requis pour le step-up", field="/password")
        user = conn.execute("SELECT password_hash FROM users WHERE id = %s", (principal.user_id,)).fetchone()
        if user is None or not verify_password(user["password_hash"], password):
            audit.record(
                conn,
                actor_type="user",
                actor_id=str(principal.user_id),
                action="auth.step_up",
                outcome="denied",
                request_id=request_id,
            )
            raise ApiError(401, "invalid_credentials", "identifiants invalides")
        new_auth_time = clock.now()

    conn.execute(
        """
        UPDATE sessions
        SET auth_time = %s, auth_method = %s, acr = %s, amr = %s, last_seen_at = now()
        WHERE id = %s AND revoked_at IS NULL
        """,
        (new_auth_time, method, acr, amr, principal.session_id),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="auth.step_up",
        outcome="ok",
        targets={"session_id": str(principal.session_id), "acr": acr},
        request_id=request_id,
    )
    return {
        "auth_method": method,
        "auth_time": new_auth_time.isoformat(),
        "acr": acr,
        "amr": amr,
    }
