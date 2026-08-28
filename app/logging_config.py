"""Logging setup, with a redaction filter on every handler.

**Why the filter exists.** `app/api/routes.py` logs the command as typed
and `app/core/router.py` logs it again, both with `%r`. Neither is doing
anything unusual — logging the request is ordinary — but the rotating
file they write to had no filter of any kind, so a sentence containing a
credential landed verbatim on disk and stayed there through three
rotations.

`app/launcher/server_process.py::redact_text()` did not help: it guards
the *child's piped stdout*, which is a different file
(`jarvis-server.log`) from the one this configures (`jarvis.log`).

Fixing the two call sites would have fixed those two call sites. A
filter on the handler fixes every call site there will ever be, which is
the property worth having — the next `logger.info("...%s", user_text)`
is written by somebody who is not thinking about this file.
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from app.config import settings


def _raising_site(traceback_obj) -> str:
    """`basename.py:lineno` of the innermost frame, or "" if unavailable.

    Deliberately the basename only. A full path under %LOCALAPPDATA% or
    C:\\Users\\ carries the account name, and these lines go into a log
    file a user may attach to a bug report.

    Never raises: this runs inside a logging filter.
    """
    try:
        frame = traceback_obj
        if frame is None:
            return ""
        while frame.tb_next is not None:
            frame = frame.tb_next
        name = os.path.basename(frame.tb_frame.f_code.co_filename)
        return f"{name}:{frame.tb_lineno}"
    except Exception:  # noqa: BLE001 - a filter must never break logging
        return ""


class _RedactingFilter(logging.Filter):
    """Masks credential-shaped values in a record before it is formatted.

    Rewrites `record.msg` and `record.args` rather than the formatted
    line, so the redaction survives whatever a handler's formatter does
    with them afterwards.

    Never raises. A logging filter that can throw turns every log call in
    the process into a potential crash, and this one runs on all of them.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging's own name
        try:
            from app.core.redaction import redact_message

            # `record.msg` is only redacted when there are no args.
            #
            # With args it is a *format string* — developer-written
            # literal text, never user data — and rewriting it is both
            # pointless and destructive. Caught by the suite rather than
            # by reasoning: "Could not remove the stored API key: %s"
            # reads as a credential noun followed by a value, so the
            # first version masked the whole string, took the `%s` with
            # it, and turned an ordinary warning into a TypeError.
            #
            # With no args, `msg` is the complete message and may well
            # be an f-string somebody built from user input, so it is
            # checked.
            if isinstance(record.msg, BaseException):
                record.msg = type(record.msg).__name__
                record.args = ()
            elif isinstance(record.msg, str) and not record.args:
                record.msg = redact_message(record.msg)

            def safe_arg(value):
                if isinstance(value, BaseException):
                    return type(value).__name__
                return redact_message(value) if isinstance(value, str) else value

            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        key: safe_arg(value) for key, value in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(safe_arg(value) for value in record.args)

            if record.exc_info:
                # Tracebacks include str(exc), which is handler-controlled
                # text and may quote credentials or request fragments.
                # Preserve the useful exception class, discard the message
                # and traceback, and format only already-sanitised args.
                #
                # The class alone is not enough to find the cause, though,
                # and losing that would undo the whole point of pairing a
                # correlation_id with a server-side log line. So the raising
                # site goes in too, as `basename.py:lineno` — that is code
                # location, chosen by us, not text chosen by an exception,
                # and the basename carries no directory and therefore no
                # Windows account name.
                exc_type = record.exc_info[0]
                class_name = getattr(exc_type, "__name__", "Exception")
                origin = _raising_site(record.exc_info[2])
                if origin:
                    class_name = f"{class_name} at {origin}"
                record.msg = f"{record.getMessage()} [{class_name}]"
                record.args = ()
                record.exc_info = None
                record.exc_text = None
        except Exception:  # noqa: BLE001 — logging must never fail because of redaction
            pass
        return True


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

    # Attached to each HANDLER, not to the logger.
    #
    # This is the subtlety that made the first attempt silently useless,
    # caught by testing it rather than reasoning about it: a filter on a
    # logger runs only for records logged *directly on that logger*.
    # Every call site here uses get_logger("x") -> "jarvis.x", a child,
    # whose records propagate to this logger's handlers without ever
    # consulting this logger's filters. Handler filters do run on
    # propagated records, so that is where the filter belongs.
    redactor = _RedactingFilter()

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
        console.addFilter(redactor)
        root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"jarvis.{name}")
