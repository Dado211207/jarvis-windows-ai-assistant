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

    # sys.stdout is None in a --windowed/console=False PyInstaller build
    # (no console to attach to) — a StreamHandler bound to None doesn't
    # raise on construction, and a later emit() failure is caught and
    # silently swallowed by logging's own Handler.handleError() (which
    # itself checks `if sys.stderr` before doing anything, and
    # sys.stderr is also None here) — so this couldn't crash startup,
    # but it also cannot ever produce real output, and constructing it
    # anyway is pointless. Skip it outright in that case rather than
    # silently dropping every console-handler record.
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
