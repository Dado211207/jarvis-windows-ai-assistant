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

    # AI / Claude settings
    jarvis_ai_provider: str = "anthropic"
    jarvis_ai_model: str = "claude-haiku-4-5-20251001"
    jarvis_ai_max_tokens: int = 250
    jarvis_ai_timeout_seconds: int = 20

    # TTS / voice output settings (Phase 3)
    jarvis_tts_enabled: bool = False
    jarvis_tts_engine: str = "pyttsx3"
    jarvis_tts_rate: int = 175
    jarvis_tts_volume: float = 1.0
    jarvis_tts_voice: str = ""

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
