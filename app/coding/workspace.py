"""The workspace boundary — the one place that decides whether a path may
be touched at all.

Every filesystem operation in Coding Workspace goes through `resolve()`.
There is no second path-checking function and there must never be one:
two implementations of a containment rule is one implementation and one
liability.

**Refuse, do not repair.** This is the house rule inherited from
`app/desktop/notes.py`. A path of `../../.ssh/id_rsa` is not a typo to be
sanitised into something valid — it is precisely what this module exists
to stop, and quietly rewriting it into a legal path would hide the
attempt from the person who most needs to see it.

**Why a string prefix check is not enough**, each of these having been
written as a test rather than a comment:

* `C:\\Projects\\app` vs `c:\\projects\\app` — Windows drive letters and
  path components are case-insensitive, so a prefix comparison on raw
  strings both over-rejects and, with a crafted case, under-rejects.
* `C:\\Projects\\app-evil` starts with `C:\\Projects\\app`. A prefix test
  passes it. It is a different directory.
* A junction or symlink *inside* the root pointing outside it resolves
  outside the root, and a check performed on the pre-resolution string
  never notices.
* `\\\\?\\C:\\...` and `\\\\.\\PIPE\\...` are alternate representations
  the OS accepts and a naive comparison does not recognise.
* `file.txt:hidden` is an NTFS alternate data stream: a second, invisible
  file behind a name that looks contained.

So containment is decided on `Path.resolve()` output using
`os.path.commonpath`-equivalent *component* comparison, after the string
form has been screened for representations that must never be accepted
at all.

**Time-of-check/time-of-use.** `resolve()` is called immediately before
the operation, never cached across a step, and file writes additionally
verify the base content hash (`app/coding/editing.py`). That closes the
practical window. It does not make the operation atomic with respect to a
local attacker who already has the user's privileges — see
docs/coding-workspace-architecture.md §6, which says so plainly rather
than implying a guarantee this cannot make.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Iterable, Optional, Tuple

# --------------------------------------------------------------------------
# Protected paths — content is never read, never sent to a model, never
# logged, never put in a diff or a screenshot.
#
# These are matched on the *resolved, relative-to-root* path, so a project
# that legitimately contains a directory called "ssh" is unaffected: what
# is protected is `.ssh`, not any path containing those letters.
# --------------------------------------------------------------------------

# Exact filenames, matched case-insensitively against the final component.
PROTECTED_FILENAMES = frozenset({
    ".env",
    ".envrc",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "client_secret.json",
    "service-account.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "known_hosts",
    "authorized_keys",
    "secring.gpg",
    "trustdb.gpg",
    "keychain.db",
    "login.keychain",
    ".htpasswd",
    "terraform.tfstate",
    "terraform.tfvars",
})

# Suffixes that carry private key or certificate material.
PROTECTED_SUFFIXES = frozenset({
    ".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".asc", ".gpg",
    ".ppk", ".crt.key", ".kdbx", ".ovpn",
})

# Any path having one of these as a *component* is protected wholesale.
PROTECTED_DIR_COMPONENTS = frozenset({
    ".git",          # internals: config holds remote credentials, hooks are code
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".config/gcloud",  # handled component-wise below as "gcloud"
    "gcloud",
    ".kube",
    ".docker",
    ".netlify",
    ".vercel",
    ".terraform",
    ".vscode-server",
    # Browser profile data — cookies and saved passwords live here.
    "user data",
    "default profile",
    "profiles",
    ".mozilla",
    "chrome user data",
})

# Filename *patterns*: `.env.local`, `.env.production`, `secrets.yaml`, ...
PROTECTED_PATTERNS = (
    re.compile(r"^\.env($|\..+)", re.IGNORECASE),
    re.compile(r"^secrets?($|[._-])", re.IGNORECASE),
    re.compile(r".*[._-]secrets?\.(ya?ml|json|toml|ini|env)$", re.IGNORECASE),
    re.compile(r"^.*\.private\..+$", re.IGNORECASE),
)

# Path forms that are never acceptable, whatever they resolve to.
_DEVICE_PATH_PREFIXES = ("\\\\.\\", "//./")
_WIN32_NAMESPACE_PREFIXES = ("\\\\?\\", "//?/")
_RESERVED_WINDOWS_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


class WorkspaceViolation(Exception):
    """A path was refused. The message names the rule, never the secret."""

    def __init__(self, reason: str, shown: str = "") -> None:
        self.reason = reason
        self.shown = shown
        super().__init__(reason)


@dataclass(frozen=True)
class ResolvedPath:
    """A path that has been proved to be inside the workspace.

    Holding this type is the proof. Functions that touch the filesystem
    take a ResolvedPath, not a str — so "did anyone check this?" is
    answered by the signature instead of by reading the body.
    """

    absolute: Path
    relative: PurePath
    root: Path

    @property
    def display(self) -> str:
        """The project-relative form, with forward slashes. This is the
        ONLY form that may appear in a task record, a log line, an event,
        a screenshot name or anything shown to a model: an absolute path
        carries the account name."""
        return self.relative.as_posix()


def _reject_unsafe_representation(raw: str) -> None:
    """Screen the *string* before the filesystem is consulted at all.

    These forms are refused outright rather than resolved, because
    resolving them is itself the risk: `\\\\.\\PhysicalDrive0` is a device,
    not a file, and opening it to find out is not an option.
    """
    if not raw or not raw.strip():
        raise WorkspaceViolation("An empty path is not a location.")

    if "\x00" in raw:
        raise WorkspaceViolation("A path containing a null byte is refused.")

    normalised = raw.replace("/", "\\") if os.name == "nt" else raw

    for prefix in _DEVICE_PATH_PREFIXES:
        if raw.startswith(prefix) or normalised.startswith(prefix):
            raise WorkspaceViolation(
                "Device paths are refused: this names a device, not a file in the project."
            )

    for prefix in _WIN32_NAMESPACE_PREFIXES:
        if raw.startswith(prefix) or normalised.startswith(prefix):
            raise WorkspaceViolation(
                "Extended-length (\\\\?\\) paths are refused: they bypass the "
                "normalisation this check depends on."
            )

    # UNC — treated conservatively, meaning refused. A project on a network
    # share is a real thing; supporting it safely needs share-level checks
    # this pass does not have, and guessing is how a boundary becomes a
    # suggestion.
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise WorkspaceViolation(
            "Network (UNC) paths are refused in this version. Copy the project "
            "to a local drive to work on it."
        )

    # NTFS alternate data streams: `file.txt:hidden`. A bare drive letter
    # (`C:\...`) is legal and must not trip this, so only a colon appearing
    # after the drive-letter position counts.
    tail = raw[2:] if re.match(r"^[A-Za-z]:", raw) else raw
    if ":" in tail:
        raise WorkspaceViolation(
            "Alternate data streams are refused: a name containing ':' can hide "
            "a second file behind a contained-looking one."
        )

    for component in re.split(r"[\\/]+", raw):
        stem = component.split(".")[0].strip().lower()
        if stem in _RESERVED_WINDOWS_NAMES:
            raise WorkspaceViolation(
                f"'{component}' is a reserved Windows device name and is refused."
            )


def _components(path: Path) -> Tuple[str, ...]:
    """Path components, case-folded on platforms where paths are
    case-insensitive. Comparing these, rather than strings, is what makes
    `app` and `app-evil` different and `C:\\P` and `c:\\p` the same."""
    parts = path.parts
    if os.name == "nt":
        return tuple(p.lower() for p in parts)
    return parts


def _is_within(child: Path, root: Path) -> bool:
    """True when *child* is *root* or lies beneath it.

    Component-wise, never a string prefix. `/a/b-evil` is not inside
    `/a/b`, and a string prefix test says it is.
    """
    child_parts = _components(child)
    root_parts = _components(root)
    if len(child_parts) < len(root_parts):
        return False
    return child_parts[: len(root_parts)] == root_parts


def _has_link_escape(candidate: Path, root: Path) -> Optional[str]:
    """Walk from the root down to *candidate*; report the first component
    that is a link leading outside the root.

    `Path.resolve()` already follows links, so a fully-resolved path that
    lands outside the root is caught by `_is_within`. This exists for the
    other half: a link that resolves *inside* the root today but whose
    presence means the path is not what it appears to be, and for
    reporting *which* component was the problem instead of a bare refusal.
    """
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return "the path is not inside the project root"

    walked = root
    for part in relative.parts:
        walked = walked / part
        try:
            if not walked.is_symlink():
                # Windows junctions are reparse points, not symlinks, and
                # older Pythons report them inconsistently. lstat's
                # st_reparse_tag (Windows-only) is the reliable signal.
                st = walked.lstat()
                tag = getattr(st, "st_reparse_tag", 0)
                if not tag:
                    continue
        except (OSError, ValueError):
            # Does not exist yet (a file about to be created) — nothing to
            # follow, so nothing to escape through.
            continue

        try:
            target = walked.resolve(strict=False)
        except (OSError, RuntimeError):
            return f"'{part}' could not be resolved"
        if not _is_within(target, root):
            return f"'{part}' is a link that points outside the project"
    return None


def canonical_root(raw: str) -> Path:
    """The canonical form of a workspace root the user selected.

    Called once when a project is added, and again — from `resolve()` —
    every single time a path is checked, because a root that has been
    replaced by a link since it was registered is no longer the directory
    the user chose.
    """
    _reject_unsafe_representation(raw)
    candidate = Path(raw).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceViolation(f"That folder could not be opened: {type(exc).__name__}.") from None
    if not resolved.is_dir():
        raise WorkspaceViolation("A project root must be a folder.")
    return resolved


def protected_summary() -> dict:
    """What is protected, for the UI to render.

    Derived from the same frozensets `is_protected()` consults, so the
    page cannot describe a protection the code does not enforce, or miss
    one it does. A hand-written list in a template drifts the first time
    somebody adds an entry here and does not think to update the page.
    """
    return {
        "filenames": sorted(PROTECTED_FILENAMES),
        "suffixes": sorted(PROTECTED_SUFFIXES),
        "directories": sorted(PROTECTED_DIR_COMPONENTS),
        "pattern_examples": [
            ".env.local, .env.production and anything else beginning .env",
            "secrets.yaml, secret.json and similar",
            "anything named *.private.*",
        ],
        "note": (
            "JARVIS reports that these files exist and how big they are, and never "
            "reads a byte of their contents. Nothing from them reaches the AI model, "
            "the activity log, a diff, a screenshot or a task record."
        ),
    }


def is_protected(relative: PurePath) -> Optional[str]:
    """Why this project-relative path is protected, or None if it is not.

    Protected means: JARVIS may report that the file exists and how big it
    is, and may never read its content, show it, send it to a model or put
    it in a diff.
    """
    parts_lower = [p.lower() for p in relative.parts]
    name = relative.name.lower()

    for component in parts_lower:
        if component in PROTECTED_DIR_COMPONENTS:
            return f"'{component}' holds credentials or repository internals"

    if name in PROTECTED_FILENAMES:
        return "this file normally holds credentials or private key material"

    for suffix in PROTECTED_SUFFIXES:
        if name.endswith(suffix):
            return "this file extension normally holds private key or certificate material"

    for pattern in PROTECTED_PATTERNS:
        if pattern.match(name):
            return "this filename pattern normally holds environment secrets"

    return None


def resolve(
    root: Path,
    candidate: str,
    *,
    must_exist: bool = False,
    allow_protected: bool = False,
) -> ResolvedPath:
    """Prove that *candidate* is inside *root*, and return the proof.

    Raises WorkspaceViolation for anything else. This is the only way a
    path enters the rest of Coding Workspace.

    `allow_protected` exists for exactly one caller: the metadata reader
    that reports "`.env` exists, 412 bytes, not read" without opening it.
    Nothing that returns content may pass it.
    """
    _reject_unsafe_representation(candidate)

    # Re-canonicalize the root on every call. Registering a project does
    # not freeze the filesystem, and a root swapped for a link afterwards
    # must not still be honoured.
    try:
        live_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorkspaceViolation("The project folder is no longer available.") from None

    raw = Path(candidate).expanduser()
    joined = raw if raw.is_absolute() else (live_root / raw)

    # strict=False: the target of a create may legitimately not exist yet.
    # Resolution still collapses `..` and follows every link that does
    # exist, which is what the containment check needs.
    try:
        resolved = joined.resolve(strict=False)
    except (OSError, RuntimeError):
        raise WorkspaceViolation("That path could not be resolved.") from None

    if not _is_within(resolved, live_root):
        raise WorkspaceViolation(
            "That path is outside the project folder. Coding Workspace can only "
            "read and change files inside the folder you selected."
        )

    escape = _has_link_escape(resolved, live_root)
    if escape is not None:
        raise WorkspaceViolation(f"Refused: {escape}.")

    relative = PurePath(resolved.relative_to(live_root))

    if not allow_protected:
        why = is_protected(relative)
        if why is not None:
            raise WorkspaceViolation(
                f"'{relative.as_posix()}' is a protected file — {why}. Its contents "
                "are never read, shown or sent to a model.",
                shown=relative.as_posix(),
            )

    if must_exist and not resolved.exists():
        raise WorkspaceViolation(f"'{relative.as_posix()}' does not exist.")

    return ResolvedPath(absolute=resolved, relative=relative, root=live_root)


def safe_metadata(root: Path, candidate: str) -> dict:
    """What may be said about a protected file: that it is there, how big
    it is, and why it was not read. Never its content.
    """
    resolved = resolve(root, candidate, allow_protected=True)
    why = is_protected(resolved.relative)
    exists = resolved.absolute.exists()
    size = None
    if exists:
        try:
            size = resolved.absolute.stat().st_size
        except OSError:
            size = None
    return {
        "path": resolved.display,
        "exists": exists,
        "size_bytes": size,
        "protected": why is not None,
        "protected_reason": why,
        "content_read": False,
    }


def iter_project_files(
    root: Path,
    *,
    max_entries: int = 4000,
    skip_dirs: Iterable[str] = (
        "node_modules", ".git", "dist", "build", ".venv", "venv", "__pycache__",
        ".next", ".nuxt", ".svelte-kit", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", "coverage", ".tox", "target", ".idea",
    ),
) -> list:
    """Walk the project, bounded, skipping the directories that make a
    walk meaningless (a `node_modules` has more files than the project).

    Never follows a link out of the tree: `os.walk(followlinks=False)` is
    the default and is relied on deliberately.
    """
    skip = {d.lower() for d in skip_dirs}
    found: list = []
    root = root.resolve(strict=False)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in skip)
        here = Path(dirpath)
        for filename in sorted(filenames):
            if len(found) >= max_entries:
                return found
            full = here / filename
            try:
                relative = PurePath(full.relative_to(root))
            except ValueError:
                continue
            try:
                st = full.lstat()
                if stat.S_ISLNK(st.st_mode):
                    continue
                size = st.st_size
            except OSError:
                continue
            found.append({
                "path": relative.as_posix(),
                "size_bytes": size,
                "protected": is_protected(relative) is not None,
            })
    return found
