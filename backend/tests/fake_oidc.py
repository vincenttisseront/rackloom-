"""IdP OIDC minimal pour tests locaux (D2) : discovery, authorize, token, JWKS.

Écoute sur 127.0.0.1 ; émet des id_token RS256 avec acr/auth_time/groups contrôlables.
Aucun secret réel ; destiné uniquement à `make test`.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from authlib.jose import JsonWebKey, JsonWebToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeOidcIdP:
    def __init__(self, *, client_id: str = "rackloom-test") -> None:
        self.client_id = client_id
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._priv_pem = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        pub = self._key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self._jwk = JsonWebKey.import_key(pub, {"kty": "RSA", "kid": "test", "use": "sig", "alg": "RS256"})
        self._codes: dict[str, dict[str, Any]] = {}
        self.users: dict[str, dict[str, Any]] = {
            "admin@demo.invalid": {
                "sub": "admin-sub",
                "email": "admin@demo.invalid",
                "name": "Admin Démo",
                "groups": ["rackloom-admins"],
                "acr": "pwd",
            },
            "bruno@demo.invalid": {
                "sub": "bruno-sub",
                "email": "bruno@demo.invalid",
                "name": "Bruno",
                "groups": ["rackloom-approvers"],
                "acr": "pwd",
            },
        }
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> str:
        idp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
                return

            def _json(self, code: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path.endswith("/.well-known/openid-configuration"):
                    self._json(
                        200,
                        {
                            "issuer": idp.base_url,
                            "authorization_endpoint": f"{idp.base_url}/auth",
                            "token_endpoint": f"{idp.base_url}/token",
                            "jwks_uri": f"{idp.base_url}/keys",
                            "response_types_supported": ["code"],
                            "subject_types_supported": ["public"],
                            "id_token_signing_alg_values_supported": ["RS256"],
                        },
                    )
                    return
                if parsed.path.endswith("/keys"):
                    self._json(200, {"keys": [idp._jwk.as_dict()]})  # noqa: SLF001
                    return
                if parsed.path.endswith("/auth"):
                    qs = parse_qs(parsed.query)
                    redirect = qs["redirect_uri"][0]
                    state = qs["state"][0]
                    nonce = qs.get("nonce", [""])[0]
                    # login_hint ou login par défaut admin
                    email = (qs.get("login_hint") or ["admin@demo.invalid"])[0]
                    user = idp.users.get(email) or idp.users["admin@demo.invalid"]
                    code = secrets.token_urlsafe(16)
                    now = int(time.time())
                    idp._codes[code] = {  # noqa: SLF001
                        "user": user,
                        "nonce": nonce,
                        "acr": user.get("acr", "pwd"),
                        "auth_time": now,
                        "max_age": qs.get("max_age", [None])[0],
                        "prompt": (qs.get("prompt") or [None])[0],
                        "acr_values": (qs.get("acr_values") or [None])[0],
                    }
                    if qs.get("prompt") == ["login"] or qs.get("max_age") == ["0"]:
                        idp._codes[code]["acr"] = "urn:rackloom:step-up"  # noqa: SLF001
                        idp._codes[code]["auth_time"] = now  # noqa: SLF001
                    loc = redirect + ("&" if "?" in redirect else "?") + urlencode({"code": code, "state": state})
                    self.send_response(302)
                    self.send_header("Location", loc)
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if not parsed.path.endswith("/token"):
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = parse_qs(self.rfile.read(length).decode())
                code = (body.get("code") or [""])[0]
                entry = idp._codes.pop(code, None)  # noqa: SLF001
                if entry is None:
                    self._json(400, {"error": "invalid_grant"})
                    return
                user = entry["user"]
                now = int(time.time())
                claims = {
                    "iss": idp.base_url,
                    "sub": user["sub"],
                    "aud": idp.client_id,
                    "email": user["email"],
                    "name": user["name"],
                    "groups": user.get("groups", []),
                    "nonce": entry["nonce"],
                    "iat": now,
                    "exp": now + 120,
                    "auth_time": entry["auth_time"],
                    "acr": entry["acr"],
                    "amr": ["pwd"],
                }
                jwt = JsonWebToken(["RS256"])
                token = jwt.encode({"alg": "RS256", "kid": "test"}, claims, idp._priv_pem)  # noqa: SLF001
                if isinstance(token, bytes):
                    token = token.decode()
                self._json(
                    200,
                    {
                        "access_token": "opaque-test",
                        "token_type": "bearer",
                        "expires_in": 120,
                        "id_token": token,
                    },
                )

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
