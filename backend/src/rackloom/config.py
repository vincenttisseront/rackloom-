from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _with_password(url: str, password_file: Path | None) -> str:
    if not password_file or not password_file.exists():
        return url
    password = password_file.read_text().strip()
    parts = urlsplit(url)
    user = parts.username or "rackloom"
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{quote(user)}:{quote(password, safe='')}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class Settings(BaseSettings):
    """Configuration lue depuis l'environnement (préfixe RACKLOOM_).

    Aucun secret n'a de valeur par défaut : mots de passe et clés viennent de fichiers
    montés (secrets Compose, dossier `.lab/` en développement).
    """

    model_config = SettingsConfigDict(env_prefix="RACKLOOM_", env_file=None)

    profile: str = Field(default="demo", pattern="^(demo|production)$")
    public_origin: str = "https://localhost:8443"

    database_url: str = "postgresql://rackloom_api@localhost:5432/rackloom"
    database_password_file: Path | None = None
    migrator_url: str | None = None
    migrator_password_file: Path | None = None
    db_pool_size: int = 10

    data_dir: Path = Path("/var/lib/rackloom")
    keys_dir: Path = Path("/run/rackloom-keys")
    catalog_dir: Path = Path("/opt/rackloom/catalog")
    fixtures_dir: Path = Path("/opt/rackloom/lab/fixtures")

    session_idle_minutes: int = 60
    session_absolute_minutes: int = 600
    cookie_secure: bool = True
    login_max_failures: int = 5
    login_window_seconds: int = 300

    job_lease_seconds: int = 60
    job_heartbeat_seconds: int = 15
    reaper_interval_seconds: int = 10
    worker_slots: int = 2
    worker_poll_seconds: float = 1.0
    worker_id: str | None = None

    launcher_socket: Path = Path("/run/rackloom/launcher.sock")
    # "socket" en Compose ; "inprocess" pour les tests et le développement natif.
    launcher_mode: str = Field(default="socket", pattern="^(socket|inprocess)$")
    runner_timeout_seconds: int = 1800

    guest_ready_timeout_seconds: int = 120
    sse_keepalive_seconds: int = 15
    sse_session_recheck_seconds: int = 30
    inventory_max_age_hours: int = 24

    chat_adapter: str = Field(default="fake", pattern="^(fake|ollama|disabled)$")
    ollama_url: str | None = None
    ollama_model: str = "llama3.1:8b"
    ollama_timeout_seconds: float = 30.0

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    def resolved_database_url(self) -> str:
        return _with_password(self.database_url, self.database_password_file)

    def resolved_migrator_url(self) -> str:
        return _with_password(self.migrator_url or self.database_url, self.migrator_password_file)


@lru_cache
def get_settings() -> Settings:
    return Settings()
