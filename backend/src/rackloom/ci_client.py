"""Client CI : dépose un besoin par l'API publique (même schéma que l'UI).

Deux modes :
- session utilisateur (démo / D2) : cookie + CSRF, routes classiques ;
- identité de service (F35) : `Authorization: Bearer`, `POST /ci/submissions` avec
  `Idempotency-Key` stable — un rejeu ne crée pas deux demandes.

Toute valeur technique est refusée par le serveur (422 `locked_field`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

API = "/api/v1"


class CiError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(f"{status} {body.get('code')}: {body.get('message')}")
        self.status = status
        self.body = body


@dataclass
class CiClient:
    http: httpx.Client
    csrf: str = ""
    service_token: str = ""
    idempotency_key: str = field(default_factory=lambda: str(uuid.uuid4()))

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
              *, idempotency_key: str | None = None) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if method == "POST":
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        if self.service_token:
            headers["Authorization"] = f"Bearer {self.service_token}"
        elif self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        response = self.http.request(method, API + path, json=body, headers=headers)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            raise CiError(response.status_code, data)
        return data

    def login(self, email: str, password: str) -> None:
        self.csrf = self._call("POST", "/auth/login", {"email": email, "password": password})["csrf_token"]
        self.service_token = ""

    def use_service_token(self, token: str, *, idempotency_key: str | None = None) -> None:
        self.service_token = token
        self.csrf = ""
        if idempotency_key:
            self.idempotency_key = idempotency_key

    def submit_need(self, project: str, need: dict[str, Any], title: str = "Demande CI",
                    provenance: dict[str, Any] | None = None, submit: bool = False) -> dict[str, Any]:
        if self.service_token:
            return self._call(
                "POST",
                "/ci/submissions",
                {
                    "project": project,
                    "title": title,
                    "need": need,
                    "provenance": provenance or {},
                    "submit": submit,
                },
                idempotency_key=self.idempotency_key,
            )
        change = self._call("POST", f"/projects/{project}/changes", {"title": title})
        version = self._call(
            "POST",
            f"/changes/{change['id']}/versions",
            {"need": need, "provenance": provenance or {}, "base_version_no": 0, "source": "ci"},
        )
        result = {
            "change_id": change["id"],
            "revision_no": version["revision_no"],
            "outcome": version["change"]["revision"]["outcome"],
        }
        if submit and result["outcome"] == "resolved":
            sub = self._call("POST", f"/changes/{change['id']}/submit", {"revision_no": version["revision_no"]})
            result["job_id"] = sub["job_id"]
        return result

    def get_submission(self, change_id: str) -> dict[str, Any]:
        if self.service_token:
            return self._call("GET", f"/ci/submissions/{change_id}")
        return self._call("GET", f"/changes/{change_id}")
