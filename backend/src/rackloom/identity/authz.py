"""Autorisation côté serveur. Hors périmètre → 404 ; dans le périmètre sans le rôle → 403."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ..errors import forbidden, not_found

ROLES = (
    "platform_admin",
    "template_publisher",
    "template_author",
    "operator",
    "approver",
    "auditor",
    "project_admin",
    "connector_admin",
)


GLOBAL_ROLES = ("platform_admin", "template_publisher", "template_author", "auditor")

# F13 : administration déléguée par projet (catalogue / connecteurs / politiques).
PROJECT_ADMIN_ROLES = ("project_admin", "connector_admin")


@dataclass(frozen=True)
class Binding:
    role: str
    project_id: UUID | None
    environment_id: UUID | None
    source: str = "permanent"  # permanent | temporary (F11)


@dataclass
class Principal:
    user_id: UUID
    email: str
    display_name: str
    session_id: UUID
    auth_method: str
    auth_time: datetime
    bindings: list[Binding] = field(default_factory=list)
    csrf_token: str = ""
    kind: str = "human"  # human | service (F35)
    service_project_id: UUID | None = None
    acr: str = "pwd"  # F12
    amr: list[str] = field(default_factory=lambda: ["pwd"])

    @property
    def is_platform_admin(self) -> bool:
        return any(b.role == "platform_admin" for b in self.bindings)

    @property
    def is_service(self) -> bool:
        return self.kind == "service" or self.auth_method == "service_token"

    @property
    def is_global_auditor(self) -> bool:
        return any(b.role == "auditor" and b.project_id is None for b in self.bindings)

    def visible_project_ids(self, all_project_ids: list[UUID] | None = None) -> set[UUID] | None:
        """None signifie « tous les projets » (auditeur global). L'administrateur de plateforme ne lit
        les données d'un projet qu'avec un rôle sur ce projet (décision D7)."""
        if self.is_global_auditor:
            return None
        return {b.project_id for b in self.bindings if b.project_id is not None}

    def can_see_project(self, project_id: UUID) -> bool:
        visible = self.visible_project_ids()
        return visible is None or project_id in visible

    def has_role(self, role: str, project_id: UUID | None = None, environment_id: UUID | None = None) -> bool:
        for b in self.bindings:
            if b.role != role:
                continue
            if b.project_id is None and role in GLOBAL_ROLES:
                return True
            if project_id is not None and b.project_id == project_id:
                if b.environment_id is None or environment_id is None or b.environment_id == environment_id:
                    return True
        return False

    def roles_for(self, project_id: UUID) -> list[str]:
        return sorted(
            {
                b.role
                for b in self.bindings
                if b.project_id == project_id or (b.project_id is None and b.role == "auditor")
            }
        )


def require_project(principal: Principal, project_id: UUID, what: str = "projet") -> None:
    if not principal.can_see_project(project_id):
        raise not_found(what)


def require_role(
    principal: Principal, role: str, project_id: UUID, environment_id: UUID | None = None, what: str = "ressource"
) -> None:
    require_project(principal, project_id, what)
    if not principal.has_role(role, project_id, environment_id):
        raise forbidden(f"rôle {role} requis sur ce projet")


def require_admin(principal: Principal) -> None:
    if not principal.is_platform_admin:
        raise forbidden("rôle administrateur de plateforme requis")


def require_publisher(principal: Principal) -> None:
    if not principal.has_role("template_publisher"):
        raise forbidden("rôle template_publisher requis pour publier, déprécier ou retirer un template")


def require_author(principal: Principal) -> None:
    if not principal.has_role("template_author"):
        raise forbidden("rôle template_author requis pour concevoir un template")


def require_auditor(principal: Principal, project_id: UUID | None = None) -> None:
    if principal.is_global_auditor:
        return
    if project_id is None and principal.is_platform_admin:
        return
    if project_id is not None and principal.has_role("auditor", project_id):
        return
    raise forbidden("rôle auditeur requis")


def require_project_admin(principal: Principal, project_id: UUID) -> None:
    """F13 : admin de plateforme ou project_admin du projet."""
    require_project(principal, project_id)
    if principal.is_platform_admin or principal.has_role("project_admin", project_id):
        return
    raise forbidden("rôle project_admin requis sur ce projet")


def require_connector_admin(principal: Principal, project_id: UUID | None = None) -> None:
    """F13 : configurer un connecteur (écriture seule des secrets)."""
    if principal.is_platform_admin:
        return
    if project_id is not None and (
        principal.has_role("connector_admin", project_id) or principal.has_role("project_admin", project_id)
    ):
        return
    raise forbidden("rôle connector_admin ou project_admin requis")


def permanent_bindings(conn, user_id: UUID) -> list[Binding]:
    rows = conn.execute(
        "SELECT role, project_id, environment_id FROM role_bindings WHERE user_id = %s AND revoked_at IS NULL",
        (user_id,),
    ).fetchall()
    return [Binding(r["role"], r["project_id"], r["environment_id"], source="permanent") for r in rows]


def load_bindings(conn, user_id: UUID) -> list[Binding]:
    """Bindings permanents + droits temporaires encore valides (F11)."""
    from . import temporary as temp_rights

    permanent = permanent_bindings(conn, user_id)
    temporary = [
        Binding(b.role, b.project_id, b.environment_id, source="temporary")
        for b in temp_rights.as_bindings(conn, user_id)
    ]
    return permanent + temporary


def has_permanent_role(
    conn,
    user_id: UUID,
    role: str,
    project_id: UUID | None = None,
    environment_id: UUID | None = None,
) -> bool:
    from datetime import UTC

    p = Principal(
        user_id=user_id,
        email="",
        display_name="",
        session_id=user_id,
        auth_method="check",
        auth_time=datetime.min.replace(tzinfo=UTC),
        bindings=permanent_bindings(conn, user_id),
    )
    return p.has_role(role, project_id, environment_id)
