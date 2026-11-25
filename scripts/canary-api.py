"""T24 (réponses API et flux SSE) : parcourt l'API avec chaque identité de démonstration et cherche le canari.

Le canari est lu dans `.lab/e2e-canary` (jamais en argument). Seules les URL concernées sont affichées,
jamais la valeur. Lancer depuis la racine : `uv run --project backend python scripts/canary-api.py`.
`--plant` : l'administrateur saisit le canari comme mot de passe du connecteur vSphere fictif.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://localhost:8443"
API = f"{BASE}/api/v1"


def credentials() -> dict[str, str]:
    lines = (ROOT / ".lab" / "demo-credentials.txt").read_text(encoding="utf-8").splitlines()
    return dict(line.split()[:2] for line in lines if line.strip() and not line.startswith("#"))


class Crawler:
    def __init__(self, email: str, password: str, canary: str) -> None:
        self.c = httpx.Client(verify=str(ROOT / ".lab" / "ca.crt"), headers={"Origin": BASE}, timeout=30)
        r = self.c.post(f"{API}/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        self.canary, self.seen, self.leaks = canary, 0, []

    def get(self, path: str):
        r = self.c.get(f"{API}{path}")
        self.seen += 1
        if self.canary in r.text:
            self.leaks.append(path)
        return r.json() if r.status_code == 200 and "json" in r.headers.get("content-type", "") else None

    def stream(self, path: str, seconds: float = 3.0) -> None:
        body, end = [], time.monotonic() + seconds
        try:
            with self.c.stream("GET", f"{API}{path}", params={"after": 0}, timeout=seconds + 2) as r:
                for chunk in r.iter_text():
                    body.append(chunk)
                    if time.monotonic() > end:
                        break
        except httpx.ReadTimeout:
            pass
        self.seen += 1
        if self.canary in "".join(body):
            self.leaks.append(path)

    def crawl(self) -> None:
        for path in ("/session", "/assistant/status", "/admin/assistant", "/admin/components", "/connectors",
                     "/users", "/audit", "/platform/status"):
            self.get(path)
        for t in self.get("/admin/templates") or []:
            for v in t.get("versions", []):
                self.get(f"/admin/templates/{t['slug']}/versions/{v['version']}")
        for p in self.get("/projects") or []:
            slug = p["slug"]
            for sub in ("environments", "services", "deployments"):
                for item in self.get(f"/projects/{slug}/{sub}") or []:
                    if sub == "deployments":
                        self.get(f"/deployments/{item['id']}")
            for h in self.get(f"/projects/{slug}/hosts") or []:
                self.get(f"/hosts/{h['id']}")
                self.get(f"/hosts/{h['id']}/observations")
            for ch in self.get(f"/projects/{slug}/changes") or []:
                view = self.get(f"/changes/{ch['id']}") or {}
                for sub in ("history", "jobs", "events"):
                    self.get(f"/changes/{ch['id']}/{sub}")
                for no in range(1, (view.get("revision") or {}).get("revision_no", 0) + 1):
                    self.get(f"/changes/{ch['id']}/revisions/{no}")
                if view.get("plan"):
                    self.get(f"/changes/{ch['id']}/plans/{view['plan']['id']}")
            self.stream(f"/projects/{slug}/events/stream")


def plant(canary: str) -> None:
    """Le canari devient le mot de passe du connecteur vSphere fictif, saisi par l'administrateur."""
    c = httpx.Client(verify=str(ROOT / ".lab" / "ca.crt"), headers={"Origin": BASE}, timeout=30)
    r = c.post(f"{API}/auth/login", json={"email": "admin@demo.invalid",
                                         "password": credentials()["admin@demo.invalid"]})
    r.raise_for_status()
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    connector = next(x for x in c.get(f"{API}/connectors").json() if x["slug"] == "vsphere-fake")
    for method, path, body in (("PUT", f"/connectors/{connector['id']}/secrets/password", {"value": canary}),
                               ("POST", f"/connectors/{connector['id']}/test", None)):
        r = c.request(method, f"{API}{path}", json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
        if r.status_code != 200:
            sys.exit(f"{method} {path} → {r.status_code}")
    print("canari : mot de passe du connecteur vsphere-fake défini, test de connexion effectué")


def main() -> int:
    canary = (ROOT / ".lab" / "e2e-canary").read_text(encoding="utf-8").strip()
    if not canary.startswith("RLCANARY-"):
        sys.exit("canari absent : lancer scripts/test-e2e.sh")
    if sys.argv[1:] == ["--plant"]:
        plant(canary)
        return 0
    leaks, seen = [], 0
    for email, password in credentials().items():
        crawler = Crawler(email, password, canary)
        crawler.crawl()
        seen += crawler.seen
        leaks += [f"{email} {path}" for path in crawler.leaks]
    print(f"réponses API et flux SSE examinés : {seen}, contenant le canari : {len(leaks)}")
    for leak in leaks:
        print("  FUITE :", leak)
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
