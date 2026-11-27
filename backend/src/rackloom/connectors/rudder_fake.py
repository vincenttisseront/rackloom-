"""Point d'entrée connecteur pour l'adaptateur Rudder fictif (F24 / RAC-36)."""

from __future__ import annotations

from ..compliance.rudder_fake import DEFAULT_REPORT, NOTICE, SOURCE, capabilities, import_observations

__all__ = [
    "DEFAULT_REPORT",
    "NOTICE",
    "SOURCE",
    "capabilities",
    "import_observations",
]
