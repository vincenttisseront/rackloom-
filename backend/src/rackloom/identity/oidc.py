"""SSO OIDC (lot 4) : Authlib code + PKCE, mapping groupes → rôles, step-up via IdP.

IdP local de test : Dex (Compose). IdP d'entreprise : non vérifié (D2).
Le client_secret éventuel est chiffré avec la clé API `oidc-client` ; jamais journalisé.
Les comptes locaux restent pour l'accès d'urgence (F14).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse
from uuid import UUID

import httpx
from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .. import audit, clock
from ..config import get_settings
from ..db import Conn
from ..errors import ApiError
from ..ids import uuid7
from ..redact import redact
from ..security import KEY_FILES, load_key, new_token, token_hash
from . import emergency
from . import service as identity
from .authz import GLOBAL_ROLES, ROLES, Principal
from .reauth import ACR_RANK, DEFAULT_MAX_AGE

log = logging.getLogger("rackloom.oidc")

FLOW_TTL = timedelta(minutes=10)
DISCOVERY_TIMEOUT = 5.0
TOKEN_TIMEOUT = 15.0
PROBE_TIMEOUT = 3.0

# ACR IdP courants mappés vers le vocabulaire Rackloom.
ACR_ALIASES = {
    "pwd": "pwd",
    "password": "pwd",
    "otp": "otp",
    "mfa": "mfa",
    "urn:okta:loa:2fa:any": "mfa",
    "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport": "pwd",
    "urn:rackloom:step-up": "urn:rackloom:step-up",
}


def _oidc_key() -> bytes:
    return load_key(get_settings().keys_dir, "oidc-client")


def encrypt_client_secret(plaintext: str) -> bytes:
    key = _oidc_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), b"rackloom-oidc-client-v1")
    return b"RLO1" + nonce + ct


def decrypt_client_secret(blob: bytes) -> str:
    if not blob.startswith(b"RLO1"):
        raise ValueError("secret OIDC non reconnu")
    key = _oidc_key()
    nonce, ct = blob[4:16], blob[16:]
    return AESGCM(key).decrypt(nonce, ct, b"rackloom-oidc-client-v1").decode()


def _ensure_keys_for_tests(keys_dir) -> None:
    """Les fixtures de test n'exécutent pas `keys init` Compose : crée oidc-client si absent."""
    path = keys_dir / KEY_FILES["oidc-client"]
    if not path.exists():
        keys_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(os.urandom(32))


def get_settings_row(conn: Conn) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM oidc_settings WHERE id = 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO oidc_settings (id) VALUES (1)")
        row = conn.execute("SELECT * FROM oidc_settings WHERE id = 1").fetchone()
    assert row is not None
    return dict(row)


def public_view(conn: Conn) -> dict[str, Any]:
    """État SSO exposable (pas de secret)."""
    row = get_settings_row(conn)
    return {
        "enabled": bool(row["enabled"]) and bool(row["issuer_url"]) and bool(row["client_id"]),
        "issuer_url": row["issuer_url"] or None,
        "sso_available": emergency.sso_available(conn),
        "login_path": "/api/v1/auth/oidc/login",
    }


def admin_view(conn: Conn) -> dict[str, Any]:
    row = get_settings_row(conn)
    mappings = [
        {
            "id": str(m["id"]),
            "idp_group": m["idp_group"],
            "role": m["role"],
            "project_id": str(m["project_id"]) if m["project_id"] else None,
            "environment_id": str(m["environment_id"]) if m["environment_id"] else None,
        }
        for m in conn.execute(
            "SELECT id, idp_group, role, project_id, environment_id FROM oidc_group_mappings ORDER BY idp_group, role"
        ).fetchall()
    ]
    return {
        "enabled": bool(row["enabled"]),
        "issuer_url": row["issuer_url"] or "",
        "client_id": row["client_id"] or "",
        "client_secret_configured": row["client_secret_enc"] is not None,
        "redirect_path": row["redirect_path"],
        "scopes": list(row["scopes"] or []),
        "email_claim": row["email_claim"],
        "groups_claim": row["groups_claim"],
        "name_claim": row["name_claim"],
        "step_up_acr": row["step_up_acr"],
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "mappings": mappings,
        "sso_available": emergency.sso_available(conn),
        "idp_reachable": None,  # renseigné à la demande via probe
    }


def configure(
    conn: Conn,
    principal: Principal,
    *,
    enabled: bool | None = None,
    issuer_url: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    clear_client_secret: bool = False,
    scopes: list[str] | None = None,
    email_claim: str | None = None,
    groups_claim: str | None = None,
    name_claim: str | None = None,
    step_up_acr: str | None = None,
) -> dict[str, Any]:
    if not principal.is_platform_admin:
        raise ApiError(403, "forbidden", "rôle administrateur de plateforme requis")
    row = get_settings_row(conn)
    new_enabled = row["enabled"] if enabled is None else bool(enabled)
    new_issuer = row["issuer_url"] if issuer_url is None else issuer_url.strip().rstrip("/")
    new_client = row["client_id"] if client_id is None else client_id.strip()
    if new_issuer:
        ok_https = new_issuer.startswith("https://")
        # D2 / labo : http://127.0.0.1 ou localhost uniquement en profil demo (IdP de test).
        ok_loopback = get_settings().profile == "demo" and (
            new_issuer.startswith("http://127.0.0.1") or new_issuer.startswith("http://localhost")
        )
        if not (ok_https or ok_loopback):
            raise ApiError(
                422,
                "invalid_issuer",
                "issuer_url doit être https:// (ou http://127.0.0.1 en démo)",
                field="/issuer_url",
            )
    if new_client and len(new_client) > 200:
        raise ApiError(422, "invalid_client_id", "client_id trop long", field="/client_id")
    if new_enabled and (not new_issuer or not new_client):
        raise ApiError(422, "oidc_incomplete", "issuer_url et client_id requis pour activer OIDC")

    secret_enc = row["client_secret_enc"]
    if clear_client_secret:
        secret_enc = None
    elif client_secret is not None:
        secret = client_secret.strip()
        if not secret:
            secret_enc = None
        else:
            if len(secret) > 500:
                raise ApiError(422, "invalid_client_secret", "client_secret trop long", field="/client_secret")
            _ensure_keys_for_tests(get_settings().keys_dir)
            secret_enc = encrypt_client_secret(secret)

    new_scopes = list(row["scopes"] or []) if scopes is None else [s.strip() for s in scopes if s.strip()]
    if scopes is not None and ("openid" not in new_scopes):
        new_scopes = ["openid", *new_scopes]
    if not new_scopes:
        new_scopes = ["openid", "profile", "email", "groups"]

    def _claim(value: str | None, current: str, field: str) -> str:
        if value is None:
            return current
        text = value.strip()
        if not 1 <= len(text) <= 80:
            raise ApiError(422, "invalid_claim", f"{field} invalide", field=f"/{field}")
        return text

    email_c = _claim(email_claim, row["email_claim"], "email_claim")
    groups_c = _claim(groups_claim, row["groups_claim"], "groups_claim")
    name_c = _claim(name_claim, row["name_claim"], "name_claim")
    step_acr = row["step_up_acr"] if step_up_acr is None else step_up_acr.strip()
    if not 1 <= len(step_acr) <= 120:
        raise ApiError(422, "invalid_acr", "step_up_acr invalide", field="/step_up_acr")

    conn.execute(
        """
        UPDATE oidc_settings SET
            enabled = %s, issuer_url = %s, client_id = %s, client_secret_enc = %s,
            scopes = %s, email_claim = %s, groups_claim = %s, name_claim = %s,
            step_up_acr = %s, updated_at = now(), updated_by = %s
        WHERE id = 1
        """,
        (
            new_enabled,
            new_issuer,
            new_client,
            secret_enc,
            new_scopes,
            email_c,
            groups_c,
            name_c,
            step_acr,
            principal.user_id,
        ),
    )
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="oidc.configure",
        outcome="ok",
        targets={"enabled": new_enabled, "issuer": new_issuer, "client_id": new_client},
        policy_decision={"client_secret_configured": secret_enc is not None},
    )
    return admin_view(conn)


def add_group_mapping(
    conn: Conn,
    principal: Principal,
    *,
    idp_group: str,
    role: str,
    project_id: UUID | None = None,
    environment_id: UUID | None = None,
) -> dict[str, Any]:
    if not principal.is_platform_admin:
        raise ApiError(403, "forbidden", "rôle administrateur de plateforme requis")
    group = idp_group.strip()
    if not 1 <= len(group) <= 200:
        raise ApiError(422, "invalid_group", "groupe IdP invalide", field="/idp_group")
    if role not in ROLES:
        raise ApiError(422, "invalid_role", "rôle inconnu", field="/role")
    if role in GLOBAL_ROLES and role != "auditor" and project_id is not None:
        raise ApiError(422, "invalid_scope", "ce rôle est global", field="/project_id")
    if role not in GLOBAL_ROLES and project_id is None:
        raise ApiError(422, "project_required", "projet requis pour ce rôle", field="/project_id")
    mid = uuid7()
    try:
        conn.execute(
            """
            INSERT INTO oidc_group_mappings (id, idp_group, role, project_id, environment_id, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (mid, group, role, project_id, environment_id, principal.user_id),
        )
    except Exception as exc:  # noqa: BLE001 — contrainte UNIQUE
        raise ApiError(409, "duplicate_mapping", "mapping déjà présent") from exc
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="oidc.mapping.create",
        outcome="ok",
        targets={"id": str(mid), "idp_group": group, "role": role},
    )
    return admin_view(conn)


def delete_group_mapping(conn: Conn, principal: Principal, mapping_id: UUID) -> dict[str, Any]:
    if not principal.is_platform_admin:
        raise ApiError(403, "forbidden", "rôle administrateur de plateforme requis")
    deleted = conn.execute("DELETE FROM oidc_group_mappings WHERE id = %s RETURNING id", (mapping_id,)).fetchone()
    if deleted is None:
        raise ApiError(404, "not_found", "mapping introuvable")
    audit.record(
        conn,
        actor_type="user",
        actor_id=str(principal.user_id),
        action="oidc.mapping.delete",
        outcome="ok",
        targets={"id": str(mapping_id)},
    )
    return admin_view(conn)


def enabled(conn: Conn) -> bool:
    row = get_settings_row(conn)
    return bool(row["enabled"] and row["issuer_url"] and row["client_id"])


def _discovery_url(issuer: str) -> str:
    """URL de discovery : base interne Compose si configurée, sinon issuer public."""
    internal = get_settings().oidc_internal_base
    if internal:
        # issuer https://localhost:8443/dex → http://dex:5556/dex/.well-known/...
        path = urlparse(issuer).path.rstrip("/") or ""
        return internal.rstrip("/") + path + "/.well-known/openid-configuration"
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def _internalize_endpoint(url: str, issuer: str) -> str:
    """Réécrit token/jwks vers la base interne ; laisse authorization_endpoint public.

    Réécrit par *chemin* (pas seulement si le préfixe public matche) : la discovery
    frappée via `http://dex:5556` peut renvoyer des hôtes internes ou publics.
    """
    internal = get_settings().oidc_internal_base
    if not internal:
        return url
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.path:
        return url
    # Conserves path+query ; force host interne HTTP (TLS seulement sur Caddy).
    q = f"?{parsed.query}" if parsed.query else ""
    return internal.rstrip("/") + parsed.path + q


def probe_idp(conn: Conn, *, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    """Vérifie la joignabilité de l'IdP (discovery). N'écrit aucun secret dans les logs."""
    row = get_settings_row(conn)
    if not row["issuer_url"]:
        return {"reachable": False, "code": "not_configured"}
    url = _discovery_url(row["issuer_url"])
    try:
        # Dex en Compose parle HTTP en interne ; TLS seulement sur Caddy.
        verify = not url.startswith("http://")
        with httpx.Client(timeout=timeout, verify=verify) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return {"reachable": False, "code": "http_error", "status": resp.status_code}
        data = resp.json()
        if "authorization_endpoint" not in data or "token_endpoint" not in data:
            return {"reachable": False, "code": "invalid_discovery"}
        return {"reachable": True, "code": "ok", "issuer": data.get("issuer")}
    except httpx.HTTPError:
        log.warning("oidc probe failed for configured issuer (details redacted)")
        return {"reachable": False, "code": "unreachable"}


def _discovery(issuer: str) -> dict[str, Any]:
    url = _discovery_url(issuer)
    verify = not url.startswith("http://")
    with httpx.Client(timeout=DISCOVERY_TIMEOUT, verify=verify) as client:
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
    # Issuer public configuré (celui des id_token) — pas l'hôte interne de discovery.
    data["issuer"] = issuer.rstrip("/")
    internal = get_settings().oidc_internal_base
    # token + jwks : joignables depuis l'API ; authorization : navigateur (issuer public).
    if "token_endpoint" in data:
        data["token_endpoint"] = _internalize_endpoint(str(data["token_endpoint"]), issuer)
    if "jwks_uri" in data:
        data["jwks_uri"] = _internalize_endpoint(str(data["jwks_uri"]), issuer)
    # En Compose : forcer authorization via l'origine publique (Caddy → Dex).
    # Hors Compose (FakeOidc en tests) : conserver l'URL renvoyée par l'IdP.
    if internal:
        auth_ep = data.get("authorization_endpoint")
        if isinstance(auth_ep, str) and auth_ep:
            path = urlparse(auth_ep).path
            if path.startswith("/"):
                data["authorization_endpoint"] = get_settings().public_origin.rstrip("/") + path
    return data


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _redirect_uri(row: dict[str, Any]) -> str:
    return urljoin(get_settings().public_origin.rstrip("/") + "/", row["redirect_path"].lstrip("/"))


def begin_flow(
    conn: Conn,
    *,
    purpose: str,
    session_id: UUID | None = None,
    return_to: str | None = None,
) -> dict[str, str]:
    """Crée state/PKCE et renvoie l'URL d'autorisation IdP."""
    row = get_settings_row(conn)
    if not enabled(conn):
        raise ApiError(404, "oidc_disabled", "SSO OIDC non configuré ou désactivé")
    if not emergency.sso_available(conn) and purpose == "login":
        raise ApiError(
            503,
            "idp_unavailable",
            "SSO indisponible : utilisez l'accès d'urgence (comptes locaux) si une intervention est ouverte",
        )
    if purpose == "reauth" and session_id is None:
        raise ApiError(422, "session_required", "session requise pour le step-up OIDC")
    if return_to is not None:
        if not return_to.startswith("/") or return_to.startswith("//"):
            raise ApiError(422, "invalid_return_to", "return_to doit être un chemin relatif", field="/return_to")

    try:
        disco = _discovery(row["issuer_url"])
    except Exception as exc:
        log.warning("oidc discovery failed")
        raise ApiError(503, "idp_unreachable", "IdP injoignable") from exc

    auth_ep = disco.get("authorization_endpoint")
    if not auth_ep:
        raise ApiError(502, "idp_misconfigured", "authorization_endpoint absent")

    verifier, challenge = _pkce_pair()
    state = new_token(24)
    nonce = new_token(24)
    expires = clock.now() + FLOW_TTL
    conn.execute("DELETE FROM oidc_flow_states WHERE expires_at < now()")
    conn.execute(
        """
        INSERT INTO oidc_flow_states (state, code_verifier, nonce, purpose, session_id, return_to, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (state, verifier, nonce, purpose, session_id, return_to, expires),
    )

    params: dict[str, str] = {
        "response_type": "code",
        "client_id": row["client_id"],
        "redirect_uri": _redirect_uri(row),
        "scope": " ".join(row["scopes"] or ["openid"]),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if purpose == "reauth":
        params["prompt"] = "login"
        params["max_age"] = "0"
        if row["step_up_acr"]:
            params["acr_values"] = row["step_up_acr"]

    url = auth_ep + ("&" if "?" in auth_ep else "?") + urlencode(params)
    audit.record(
        conn,
        actor_type="user" if session_id else "anonymous",
        actor_id=str(session_id) if session_id else None,
        action="oidc.begin",
        outcome="ok",
        targets={"purpose": purpose},
    )
    return {"authorization_url": url, "state": state}


def _exchange_code(row: dict[str, Any], disco: dict[str, Any], code: str, verifier: str) -> dict[str, Any]:
    token_ep = disco["token_endpoint"]
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(row),
        "client_id": row["client_id"],
        "code_verifier": verifier,
    }
    headers = {"Accept": "application/json"}
    verify = not str(token_ep).startswith("http://")
    with httpx.Client(timeout=TOKEN_TIMEOUT, verify=verify) as client:
        if row["client_secret_enc"]:
            _ensure_keys_for_tests(get_settings().keys_dir)
            secret = decrypt_client_secret(bytes(row["client_secret_enc"]))
            resp = client.post(
                token_ep,
                data=data,
                headers=headers,
                auth=httpx.BasicAuth(str(row["client_id"]), secret),
            )
        else:
            resp = client.post(token_ep, data=data, headers=headers)
        if resp.status_code >= 400:
            # Ne jamais journaliser le corps (peut contenir des indices).
            log.warning("oidc token exchange failed status=%s", resp.status_code)
            raise ApiError(401, "oidc_token_failed", "échange de code OIDC refusé par l'IdP")
        return resp.json()


def _load_jwks(disco: dict[str, Any]) -> Any:
    jwks_uri = disco.get("jwks_uri")
    if not jwks_uri:
        raise ApiError(502, "idp_misconfigured", "jwks_uri absent")
    verify = not str(jwks_uri).startswith("http://")
    with httpx.Client(timeout=DISCOVERY_TIMEOUT, verify=verify) as client:
        resp = client.get(jwks_uri)
        resp.raise_for_status()
        return JsonWebKey.import_key_set(resp.json())


def _verify_id_token(
    id_token: str,
    *,
    disco: dict[str, Any],
    client_id: str,
    nonce: str,
) -> dict[str, Any]:
    try:
        key_set = _load_jwks(disco)
        jwt = JsonWebToken(["RS256", "ES256", "PS256", "EdDSA", "HS256"])
        claims = jwt.decode(id_token, key_set)
        claims.validate(
            now=int(clock.now().timestamp()),
            leeway=30,
        )
    except JoseError as exc:
        log.warning("oidc id_token verification failed")
        raise ApiError(401, "oidc_invalid_token", "jeton d'identité IdP invalide") from exc

    iss = str(claims.get("iss") or "")
    # Comparer à l'issuer de discovery (forcé sur l'URL configurée en base).
    expected_iss = str(disco.get("issuer") or "")
    if iss.rstrip("/") != expected_iss.rstrip("/"):
        raise ApiError(401, "oidc_issuer_mismatch", "émetteur IdP inattendu")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if client_id not in auds:
        raise ApiError(401, "oidc_audience_mismatch", "audience IdP inattendue")
    if str(claims.get("nonce") or "") != nonce:
        raise ApiError(401, "oidc_nonce_mismatch", "nonce IdP invalide")
    return dict(claims)


def _normalize_acr(raw: Any, *, purpose: str, step_up_acr: str, auth_time: datetime) -> str:
    claimed = ACR_ALIASES.get(str(raw or "").strip(), None)
    if purpose == "reauth":
        # Step-up réussi (prompt=login + auth_time frais) : élever au niveau configuré.
        if claimed and ACR_RANK.get(claimed, 0) >= ACR_RANK.get(step_up_acr, 3):
            return claimed if claimed in ACR_RANK else step_up_acr
        if auth_time + DEFAULT_MAX_AGE >= clock.now():
            return step_up_acr if step_up_acr in ACR_RANK else "urn:rackloom:step-up"
        raise ApiError(403, "reauth_stale", "preuve IdP trop ancienne")
    if claimed and claimed in ACR_RANK:
        return claimed
    return "pwd"


def _groups_from_claims(claims: dict[str, Any], groups_claim: str) -> list[str]:
    raw = claims.get(groups_claim)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(g) for g in raw if str(g).strip()]
    return []


def sync_group_bindings(conn: Conn, user_id: UUID, groups: list[str]) -> list[dict[str, Any]]:
    """Remplace les bindings `binding_source=oidc` par ceux des mappings correspondants."""
    group_set = set(groups)
    mappings = conn.execute("SELECT idp_group, role, project_id, environment_id FROM oidc_group_mappings").fetchall()
    desired: set[tuple[str, UUID | None, UUID | None]] = set()
    applied: list[dict[str, Any]] = []
    for m in mappings:
        if m["idp_group"] not in group_set:
            continue
        key = (m["role"], m["project_id"], m["environment_id"])
        desired.add(key)
        applied.append(
            {
                "idp_group": m["idp_group"],
                "role": m["role"],
                "project_id": str(m["project_id"]) if m["project_id"] else None,
                "environment_id": str(m["environment_id"]) if m["environment_id"] else None,
            }
        )

    existing = conn.execute(
        """
        SELECT id, role, project_id, environment_id FROM role_bindings
        WHERE user_id = %s AND revoked_at IS NULL AND binding_source = 'oidc'
        """,
        (user_id,),
    ).fetchall()
    for row in existing:
        key = (row["role"], row["project_id"], row["environment_id"])
        if key not in desired:
            conn.execute(
                "UPDATE role_bindings SET revoked_at = now(), revoked_by = %s WHERE id = %s",
                (user_id, row["id"]),
            )

    have = {(r["role"], r["project_id"], r["environment_id"]) for r in existing}
    for role, project_id, environment_id in desired:
        if (role, project_id, environment_id) in have:
            continue
        conn.execute(
            """
            INSERT INTO role_bindings (id, user_id, role, project_id, environment_id, granted_by, binding_source)
            VALUES (%s, %s, %s, %s, %s, %s, 'oidc')
            """,
            (uuid7(), user_id, role, project_id, environment_id, None),
        )
    return applied


def _upsert_user(conn: Conn, email: str, display_name: str, subject: str) -> dict[str, Any]:
    email = email.strip().lower()
    row = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
    if row:
        if row["status"] != "active":
            raise ApiError(403, "identity_disabled", "compte désactivé")
        if row["kind"] == "service":
            raise ApiError(403, "service_identity", "identité de service refusée pour OIDC")
        updates = []
        params: list[Any] = []
        if display_name and display_name != row["display_name"]:
            updates.append("display_name = %s")
            params.append(display_name)
        if subject and row.get("external_id") != subject:
            updates.append("external_id = %s")
            params.append(subject)
        if updates:
            params.append(row["id"])
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = %s", params)
        refreshed = conn.execute("SELECT * FROM users WHERE id = %s", (row["id"],)).fetchone()
        assert refreshed is not None
        return dict(refreshed)
    # Nouveau compte : mot de passe aléatoire non communiqué (secours F14 uniquement si un admin le réinitialise).
    user_id = identity.create_user(conn, email, display_name or email, new_token(48), is_demo=False)
    if subject:
        conn.execute("UPDATE users SET external_id = %s WHERE id = %s", (subject, user_id))
    created = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
    assert created is not None
    return dict(created)


def complete_callback(
    conn: Conn,
    *,
    code: str,
    state: str,
    previous_token: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Échange le code, crée/élève la session. Renvoie (NewSession|None, meta).

    Pour purpose=login : NewSession. Pour purpose=reauth : None + meta (session mise à jour).
    """
    flow = conn.execute(
        "SELECT * FROM oidc_flow_states WHERE state = %s FOR UPDATE",
        (state,),
    ).fetchone()
    if flow is None or flow["expires_at"] <= clock.now():
        conn.execute("DELETE FROM oidc_flow_states WHERE state = %s", (state,))
        raise ApiError(400, "oidc_state_invalid", "état OIDC absent ou expiré")
    conn.execute("DELETE FROM oidc_flow_states WHERE state = %s", (state,))

    row = get_settings_row(conn)
    if not enabled(conn):
        raise ApiError(404, "oidc_disabled", "SSO OIDC désactivé")

    try:
        disco = _discovery(row["issuer_url"])
        token_resp = _exchange_code(row, disco, code, flow["code_verifier"])
    except ApiError:
        raise
    except Exception as exc:
        log.warning("oidc callback exchange failed")
        raise ApiError(503, "idp_unreachable", "IdP injoignable lors de l'échange") from exc

    id_token = token_resp.get("id_token")
    if not id_token or not isinstance(id_token, str):
        raise ApiError(401, "oidc_missing_id_token", "id_token absent")

    claims = _verify_id_token(
        id_token,
        disco=disco,
        client_id=row["client_id"],
        nonce=flow["nonce"],
    )

    email = str(claims.get(row["email_claim"]) or claims.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise ApiError(422, "oidc_email_required", "revendication e-mail absente côté IdP")
    display = str(claims.get(row["name_claim"]) or claims.get("name") or email).strip()
    subject = str(claims.get("sub") or "").strip()

    auth_time_raw = claims.get("auth_time")
    if auth_time_raw is not None:
        try:
            auth_time = datetime.fromtimestamp(float(auth_time_raw), tz=UTC)
        except (TypeError, ValueError) as exc:
            raise ApiError(401, "oidc_invalid_auth_time", "auth_time IdP invalide") from exc
    else:
        # Certains IdP (Dex) omettent auth_time hors max_age ; iat sert de borne.
        try:
            auth_time = datetime.fromtimestamp(float(claims["iat"]), tz=UTC)
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError(401, "oidc_invalid_auth_time", "auth_time/iat IdP invalide") from exc

    acr = _normalize_acr(
        claims.get("acr"),
        purpose=flow["purpose"],
        step_up_acr=row["step_up_acr"],
        auth_time=auth_time,
    )
    amr = claims.get("amr")
    if not isinstance(amr, list) or not amr:
        amr = ["oidc"] if flow["purpose"] == "login" else ["oidc", "pwd"]

    if flow["purpose"] == "reauth":
        session_id = flow["session_id"]
        sess = conn.execute(
            """
            SELECT s.id, s.user_id, u.email, u.status FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = %s AND s.revoked_at IS NULL
            """,
            (session_id,),
        ).fetchone()
        if sess is None or sess["status"] != "active":
            raise ApiError(401, "unauthenticated", "session absente pour le step-up")
        if sess["email"].lower() != email:
            raise ApiError(403, "oidc_subject_mismatch", "le compte IdP ne correspond pas à la session")
        conn.execute(
            """
            UPDATE sessions
            SET auth_time = %s, auth_method = 'oidc_step_up', acr = %s, amr = %s, last_seen_at = now()
            WHERE id = %s
            """,
            (auth_time, acr, amr, session_id),
        )
        audit.record(
            conn,
            actor_type="user",
            actor_id=str(sess["user_id"]),
            action="auth.step_up",
            outcome="ok",
            targets={"session_id": str(session_id), "acr": acr, "via": "oidc"},
        )
        return None, {
            "purpose": "reauth",
            "auth_method": "oidc_step_up",
            "auth_time": auth_time.isoformat(),
            "acr": acr,
            "amr": amr,
            "return_to": flow["return_to"] or "/",
        }

    # Login
    if get_settings().profile == "production" and email.endswith(".invalid"):
        raise ApiError(403, "demo_identity_refused", "domaine .invalid refusé en production")

    user = _upsert_user(conn, email, display, subject)
    groups = _groups_from_claims(claims, row["groups_claim"])
    applied = sync_group_bindings(conn, user["id"], groups)

    if previous_token:
        conn.execute(
            "UPDATE sessions SET revoked_at = now(), revoked_reason = 'rotation' "
            "WHERE id_hash = %s AND revoked_at IS NULL",
            (token_hash(previous_token),),
        )

    session = identity._create_session(  # noqa: SLF001 — création session partagée
        conn,
        user["id"],
        user["privilege_epoch"],
        "oidc",
        acr=acr,
        amr=list(amr),
    )
    # auth_time IdP (pas l'horloge locale de création).
    conn.execute("UPDATE sessions SET auth_time = %s WHERE id = %s", (auth_time, session.session_id))

    audit.record(
        conn,
        actor_type="user",
        actor_id=str(user["id"]),
        action="auth.login",
        outcome="ok",
        targets={
            "session_id": str(session.session_id),
            "via": "oidc",
            "groups": redact(groups),
            "mappings_applied": len(applied),
        },
    )
    return session, {
        "purpose": "login",
        "auth_method": "oidc",
        "auth_time": auth_time.isoformat(),
        "acr": acr,
        "amr": amr,
        "return_to": flow["return_to"] or "/",
        "email": email,
    }


def safe_return_to(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        # Évite les open-redirect ; chemins relatifs uniquement.
        parsed = urlparse(value)
        if parsed.scheme or parsed.netloc:
            return "/"
        return value
    return "/"
