from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Erreur métier renvoyée telle quelle : code stable, pointeur de champ, message traduisible."""

    def __init__(self, status: int, code: str, message: str, field: str | None = None,
                 details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field
        self.details = details

    def to_dict(self) -> dict:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field:
            body["field"] = self.field
        if self.details is not None:
            body["details"] = self.details
        return body


def not_found(what: str = "ressource") -> ApiError:
    return ApiError(404, "not_found", f"{what} introuvable")


def forbidden(message: str = "droit insuffisant", code: str = "forbidden") -> ApiError:
    return ApiError(403, code, message)


def conflict(code: str, message: str, details: Any = None) -> ApiError:
    return ApiError(409, code, message, details=details)
