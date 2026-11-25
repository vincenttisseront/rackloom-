"""Client CI minimal : dépose un besoin (fichier JSON) par l'API publique, comme l'UI.

Le client ne contourne rien : session, jeton CSRF, `Idempotency-Key`, puis
`POST /projects/{p}/changes` et `POST /changes/{c}/versions`. Toute valeur technique
présente dans le fichier est refusée par le serveur (422 `locked_field`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Idempotency-Key": str(uuid.uuid4())} if method == "POST" else {}
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        response = self.http.request(method, API + path, json=body, headers=headers)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            raise CiError(response.status_code, data)
        return data

    def login(self, email: str, password: str) -> None:
        self.csrf = self._call("POST", "/auth/login", {"email": email, "password": password})["csrf_token"]

    def submit_need(self, project: str, need: dict[str, Any], title: str = "Demande CI",
                    provenance: dict[str, Any] | None = None, submit: bool = False) -> dict[str, Any]:
        change = self._call("POST", f"/projects/{project}/changes", {"title": title})
        version = self._call("POST", f"/changes/{change['id']}/versions",
                             {"need": need, "provenance": provenance or {}, "base_version_no": 0, "source": "ci"})
        result = {"change_id": change["id"], "revision_no": version["revision_no"],
                  "outcome": version["change"]["revision"]["outcome"]}
        if submit and result["outcome"] == "resolved":
            sub = self._call("POST", f"/changes/{change['id']}/submit", {"revision_no": version["revision_no"]})
            result["job_id"] = sub["job_id"]
        return result
