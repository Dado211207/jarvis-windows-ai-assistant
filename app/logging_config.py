import logging
import logging.handlers
import sys
from pathlib import Path

from app.config import settings


def setup_logging() -> logging.Logger:
    log_level = getattr(logging, settings.jarvis_log_level.upper(), logging.INFO)

    log_file: Path = settings.log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger("jarvis")
    root.setLevel(log_level)
    root.handlers.clear()

    # sys.stdout is None in a PyInstaller --windowed/--noconsole build (there is
    # no console to write to) — skip the console handler rather than crashing
    # on the first log call.
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(log_level)
        console.setFormatter(formatter)
        root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"jarvis.{name}")
