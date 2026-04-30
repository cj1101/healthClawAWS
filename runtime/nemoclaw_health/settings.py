from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEMOWLAW_", env_file=".env", extra="ignore")

    data_dir: Path = Path(__file__).resolve().parent.parent / "data"
    sqlite_path: Path | None = None
    artifact_log: Path | None = None

    confidence_commit_threshold: float = 0.65
    raw_event_retention_days: int = 90

    openrouter_api_key: str | None = None
    openrouter_model: str = "deepseek/deepseek-v4-pro"

    def resolved_sqlite(self) -> Path:
        return self.sqlite_path if self.sqlite_path is not None else self.data_dir / "nemoclaw.sqlite"

    def resolved_artifact_log(self) -> Path:
        return (
            self.artifact_log
            if self.artifact_log is not None
            else self.data_dir / "artifacts" / "orchestration.jsonl"
        )


def get_settings() -> Settings:
    return Settings()
