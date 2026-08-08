from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.app_paths import default_db_path, default_log_file, default_screenshots_dir


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
    # Empty means "not explicitly overridden" — db_path/log_file below then
    # resolve through app_paths (repo-relative in dev, %LOCALAPPDATA%\JARVIS
    # in the packaged app). Set JARVIS_DB_PATH/JARVIS_LOG_FILE to override
    # either mode explicitly, e.g. CI pointing the DB at a temp path.
    jarvis_db_path: str = ""
    jarvis_log_level: str = "INFO"
    jarvis_log_file: str = ""

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

    # STT / push-to-talk voice input settings (v0.2). Disabled by default,
    # matching TTS's own opt-in pattern. No wake word, no always-listening
    # — see app/voice/stt.py.
    jarvis_stt_enabled: bool = False
    jarvis_stt_model_size: str = "tiny"  # CPU-safe default if downloading is allowed
    jarvis_stt_model_path: str = ""  # a local model dir/path — never triggers a download
    jarvis_stt_allow_download: bool = False  # must be explicit; faster-whisper otherwise refuses to fetch a model
    jarvis_stt_timeout_seconds: int = 30

    # Native desktop window (packaging release-candidate pass). "quit":
    # the window's close (X) button shuts JARVIS down — the default, and
    # what most people expect an X button to do. "tray": close hides the
    # window instead and JARVIS keeps running in the system tray.
    #
    # "quit" is the default for a real, CI-verified reason, not just
    # convention: a graceful `taskkill` (which is what Windows, and the
    # Inno Setup uninstaller's CloseApplications, use to ask this process
    # to exit) delivers the very same WM_CLOSE message a user's X click
    # does, and they cannot be told apart at the pywebview level. With
    # "tray" as the default, the app cancelled that close and refused to
    # exit — caught on windows-latest CI. See
    # app/launcher/webview_window.py::request_shutdown(), which keeps the
    # opt-in "tray" mode stoppable too.
    #
    # Invalid values fall back to "tray" (see resolve_close_action()) —
    # that fallback is about an unparseable *user setting*, where not
    # silently killing a running server is the safer reading.
    jarvis_close_action: str = "quit"

    @property
    def db_path(self) -> Path:
        return Path(self.jarvis_db_path) if self.jarvis_db_path else default_db_path()

    @property
    def log_file(self) -> Path:
        return Path(self.jarvis_log_file) if self.jarvis_log_file else default_log_file()

    @property
    def screenshots_dir(self) -> Path:
        return default_screenshots_dir()

    @property
    def effective_api_key(self) -> str:
        """The Anthropic API key actually in effect. ANTHROPIC_API_KEY
        (env var / .env) always wins when set — this is what development
        and CI use, and it must never be shadowed by a credential store
        that may not even exist in that context. Only when it's unset
        does this fall back to the OS credential store (set via the
        packaged app's onboarding/settings UI — see
        app/core/credentials.py, which never raises even if the store
        is unavailable)."""
        if self.anthropic_api_key.strip():
            return self.anthropic_api_key
        from app.core.credentials import get_stored_api_key
        return get_stored_api_key()

    @property
    def has_anthropic_key(self) -> bool:
        return bool(self.effective_api_key.strip())


settings = Settings()
