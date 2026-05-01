from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

WHOOP_OAUTH_AUTH_DEFAULT = "https://api.prod.whoop.com/oauth/oauth2/auth"
WHOOP_OAUTH_TOKEN_DEFAULT = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_API_BASE_DEFAULT = "https://api.prod.whoop.com/developer"

# Uvicorn WorkingDirectory is often ``runtime/``; canonical deploy ``.env`` lives at repo root.
_SETTINGS_DIR = Path(__file__).resolve().parent
_REPO_ROOT_ENV = _SETTINGS_DIR.parent.parent / ".env"
_RUNTIME_ENV = _SETTINGS_DIR.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEMOWLAW_",
        env_file=(
            _REPO_ROOT_ENV,
            _RUNTIME_ENV,
        ),
        extra="ignore",
    )

    data_dir: Path = Path(__file__).resolve().parent.parent / "data"
    sqlite_path: Path | None = None
    artifact_log: Path | None = None

    confidence_commit_threshold: float = 0.65
    raw_event_retention_days: int = 90
    delegation_metadata_retention_days: int | None = None
    sqlite_busy_timeout_ms: int = 5000

    openrouter_api_key: str | None = None
    openrouter_api_base: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "deepseek/deepseek-v4-pro"

    whoop_client_id: str | None = None
    whoop_client_secret: str | None = None
    whoop_redirect_uri: str | None = None
    whoop_auth_url: str = WHOOP_OAUTH_AUTH_DEFAULT
    whoop_token_url: str = WHOOP_OAUTH_TOKEN_DEFAULT
    whoop_api_base: str = WHOOP_API_BASE_DEFAULT
    whoop_http_max_retries: int = 4
    whoop_default_sync_days: int = 7
    # If true, refetch each workout/sleep via GET-by-id after listing (more API calls; use if list payloads omit fields).
    whoop_fetch_activity_detail: bool = False

    dashboard_password: str | None = None
    session_secret: str | None = None
    # Optional Bearer token for POST /v1/jobs/* when dashboard_password is set (systemd/cron).
    job_token: str | None = None
    # Optional Bearer token for POST /v1/chat when dashboard_password is set (e.g. Telegram bot on EC2).
    chat_bearer_token: str | None = None

    def resolved_sqlite(self) -> Path:
        return self.sqlite_path if self.sqlite_path is not None else self.data_dir / "nemoclaw.sqlite"

    def resolved_artifact_log(self) -> Path:
        return (
            self.artifact_log
            if self.artifact_log is not None
            else self.data_dir / "artifacts" / "orchestration.jsonl"
        )

    def resolved_apple_imports_dir(self) -> Path:
        return self.data_dir / "apple_imports"

    def validate_whoop_oauth_urls(self) -> None:
        for name, url in (
            ("whoop_auth_url", self.whoop_auth_url),
            ("whoop_token_url", self.whoop_token_url),
        ):
            if "/developer" in url:
                raise ValueError(
                    f"{name} must not use /developer/ — OAuth lives on WHOOP's /oauth/ host. "
                    f"Got {url!r}."
                )


def get_settings() -> Settings:
    return Settings()
