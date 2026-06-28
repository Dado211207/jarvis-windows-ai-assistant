from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    anthropic_api_key: str = ""
    jarvis_host: str = "127.0.0.1"
    jarvis_port: int = 5555
    jarvis_db_path: str = "data/jarvis.db"
    jarvis_log_level: str = "INFO"
    jarvis_log_file: str = "data/logs/jarvis.log"

    @property
    def db_path(self) -> Path:
        return Path(self.jarvis_db_path)

    @property
    def log_file(self) -> Path:
        return Path(self.jarvis_log_file)

    @property
    def screenshots_dir(self) -> Path:
        return Path("data/screenshots")

    @property
    def has_anthropic_key(self) -> bool:
        return bool(self.anthropic_api_key.strip())


settings = Settings()
