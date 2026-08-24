"""Patch-based file editing.

Every change JARVIS makes to a project goes through here. There is no
other write path, and specifically there is no shell redirection: a
coding agent that edits files by running `echo ... > file` has given
itself an arbitrary write primitive dressed up as a command.

Four properties this module exists to guarantee:

**Atomic.** Content is written to a temporary file in the same directory
and then `os.replace`d over the target — a rename within one filesystem,
which is atomic on both Windows and POSIX. A crash mid-write leaves the
original file, never a half-written one.

**Encoding and line endings survive.** A file read as UTF-8 with CRLF
line endings is written back as UTF-8 with CRLF. A coding agent that
silently rewrites every line ending in a file produces a diff where every
line changed, which is indistinguishable from having rewritten the file
and impossible to review. The BOM, if there was one, is preserved too.

**A stale base is refused, not overwritten.** Every patch names the hash
of the content it was computed against. If the file on disk no longer has
that hash — the user edited it in their editor while the task was running,
a build step regenerated it — the write is refused and the task re-reads
and re-plans. Forcing a patch onto a file that has changed underneath it
is how an agent silently discards someone's work.

**Nothing is hidden.** Every proposal produces a diff before it is
applied, and the before/after hashes are recorded. A change that does not
appear in the diff did not happen.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

from app.coding import limits
from app.coding.workspace import ResolvedPath, WorkspaceViolation, resolve
from app.logging_config import get_logger

logger = get_logger("coding.editing")

# Bytes that essentially never appear in text and reliably identify a
# binary file. A NUL in the first block is the classic signal and the one
# git itself uses.
_BINARY_SNIFF_BYTES = 8192


class EditKind(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    RENAME = "rename"
    DELETE = "delete"


@dataclass
class FileSnapshot:
    """What a file looked like when it was read, and everything needed to
    write it back in the same shape."""

    text: str
    sha256: str
    encoding: str
    newline: str
    had_bom: bool
    size_bytes: int
    existed: bool

    @property
    def short_hash(self) -> str:
        return self.sha256[:12]


@dataclass
class EditProposal:
    """A single proposed change. Not applied until `apply_all` runs, and
    never applied without its diff having been produced first."""

    kind: EditKind
    path: str                      # project-relative, forward slashes
    new_text: Optional[str] = None
    destination: Optional[str] = None   # RENAME only
    base_sha256: Optional[str] = None   # what the change was computed against
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "path": self.path,
            "destination": self.destination,
            "base_sha256": self.base_sha256,
            "reason": self.reason,
        }


@dataclass
class EditResult:
    proposal: EditProposal
    applied: bool
    message: str
    before_sha256: Optional[str] = None
    after_sha256: Optional[str] = None
    diff: str = ""
    lines_added: int = 0
    lines_removed: int = 0

    def as_dict(self) -> dict:
        return {
            **self.proposal.as_dict(),
            "applied": self.applied,
            "message": self.message,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
        }


class StaleBaseError(Exception):
    """The file changed between being read and being written."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def looks_binary(raw: bytes) -> bool:
    sample = raw[:_BINARY_SNIFF_BYTES]
    if b"\x00" in sample:
        return True
    if not sample:
        return False
    # A high proportion of bytes outside printable/whitespace ranges means
    # this is not text we can safely round-trip.
    text_bytes = bytes(range(0x20, 0x7F)) + b"\n\r\t\f\b"
    nontext = sum(1 for byte in sample if byte not in text_bytes and byte < 0x80)
    return (nontext / len(sample)) > 0.30


def _detect_newline(raw_text: str) -> str:
    """The dominant line ending, so it can be restored on write."""
    crlf = raw_text.count("\r\n")
    lf = raw_text.count("\n") - crlf
    cr = raw_text.count("\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    return "\n"


def read_snapshot(target: ResolvedPath) -> FileSnapshot:
    """Read a file in a form that can be written back unchanged.

    Refuses binaries and anything past the editable size cap: a patch
    engine that "edits" a PNG produces a corrupted PNG.
    """
    path = target.absolute
    if not path.exists():
        return FileSnapshot(
            text="", sha256=sha256_text(""), encoding="utf-8",
            newline=os.linesep if os.name == "nt" else "\n",
            had_bom=False, size_bytes=0, existed=False,
        )

    if not path.is_file():
        raise WorkspaceViolation(f"'{target.display}' is not a file.")

    size = path.stat().st_size
    if size > limits.MAX_EDITABLE_FILE_BYTES:
        raise WorkspaceViolation(
            f"'{target.display}' is {size:,} bytes, above the "
            f"{limits.MAX_EDITABLE_FILE_BYTES:,}-byte editing limit."
        )

    raw = path.read_bytes()
    if looks_binary(raw):
        raise WorkspaceViolation(
            f"'{target.display}' looks like a binary file. Coding Workspace edits text only."
        )

    had_bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if had_bom else raw
    encoding = "utf-8"
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        # A file that is not UTF-8 is still editable if it round-trips
        # through a single-byte codec without loss. Anything else is
        # refused rather than mangled.
        try:
            text = body.decode("cp1252")
            encoding = "cp1252"
        except UnicodeDecodeError:
            raise WorkspaceViolation(
                f"'{target.display}' is not text JARVIS can safely round-trip."
            ) from None

    return FileSnapshot(
        text=text,
        sha256=sha256_bytes(raw),
        encoding=encoding,
        newline=_detect_newline(text),
        had_bom=had_bom,
        size_bytes=size,
        existed=True,
    )


def _encode(text: str, snapshot: FileSnapshot) -> bytes:
    """Restore the file's original shape around new content."""
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    if snapshot.newline != "\n":
        normalised = normalised.replace("\n", snapshot.newline)
    raw = normalised.encode(snapshot.encoding, errors="strict")
    if snapshot.had_bom:
        raw = b"\xef\xbb\xbf" + raw
    return raw


def unified_diff(before: str, after: str, path: str, destination: str = "") -> str:
    """A unified diff, computed on normalised line endings so a pure
    CRLF/LF difference never shows as "every line changed"."""
    left = before.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    right = after.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True)
    return "".join(difflib.unified_diff(
        left, right,
        fromfile=f"a/{path}",
        tofile=f"b/{destination or path}",
        n=3,
    ))


def diff_counts(diff: str) -> Tuple[int, int]:
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return added, removed


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write via a temporary file in the same directory, then replace.

    Same directory matters: `os.replace` is only atomic within one
    filesystem, and a temp directory may be on another volume.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.jarvis-tmp"
    try:
        with open(temp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def atomic_write_bytes(target: ResolvedPath, payload: bytes) -> None:
    """Restore already-validated exact bytes with the normal atomic writer."""
    _atomic_write(target.absolute, payload)


def preview(root: Path, proposal: EditProposal) -> EditResult:
    """Produce the diff for a proposal without touching the disk.

    Every proposal is previewed before it is applied — the UI shows this,
    and `apply_all` recomputes it, so a change that was never shown cannot
    be applied.
    """
    try:
        if proposal.kind is EditKind.DELETE:
            target = resolve(root, proposal.path, must_exist=True)
            snapshot = read_snapshot(target)
            diff = unified_diff(snapshot.text, "", target.display)
            added, removed = diff_counts(diff)
            return EditResult(
                proposal=proposal, applied=False,
                message="Deletion proposed. Deleting a file always needs your approval.",
                before_sha256=snapshot.sha256, diff=diff,
                lines_added=added, lines_removed=removed,
            )

        if proposal.kind is EditKind.RENAME:
            source = resolve(root, proposal.path, must_exist=True)
            if not proposal.destination:
                return EditResult(proposal, False, "A rename needs a destination.")
            destination = resolve(root, proposal.destination)
            return EditResult(
                proposal=proposal, applied=False,
                message=f"Rename '{source.display}' to '{destination.display}'.",
            )

        target = resolve(root, proposal.path)
        snapshot = read_snapshot(target)
        if proposal.kind is EditKind.CREATE and snapshot.existed:
            return EditResult(
                proposal, False,
                f"'{target.display}' already exists. Propose an update instead of a create.",
                before_sha256=snapshot.sha256,
            )
        new_text = proposal.new_text or ""
        if len(new_text.encode("utf-8", errors="replace")) > limits.MAX_PATCH_BYTES:
            return EditResult(
                proposal, False,
                f"That change is larger than the {limits.MAX_PATCH_BYTES:,}-byte single-patch limit.",
            )
        diff = unified_diff(snapshot.text, new_text, target.display)
        added, removed = diff_counts(diff)
        if not diff:
            return EditResult(
                proposal, False, "No change — the file already has this content.",
                before_sha256=snapshot.sha256, after_sha256=snapshot.sha256,
            )
        return EditResult(
            proposal=proposal, applied=False,
            message=f"{added} line(s) added, {removed} removed.",
            before_sha256=snapshot.sha256, diff=diff,
            lines_added=added, lines_removed=removed,
        )
    except WorkspaceViolation as exc:
        return EditResult(proposal, False, exc.reason)


def apply(root: Path, proposal: EditProposal, *, approved_delete: bool = False) -> EditResult:
    """Apply one proposal, refusing a stale base.

    `approved_delete` is not a convenience flag — a deletion with it unset
    is refused outright, so an agent cannot delete a file by forgetting to
    ask.
    """
    try:
        if proposal.kind is EditKind.DELETE:
            if not approved_delete:
                return EditResult(proposal, False, "Deletion was not approved, so nothing was deleted.")
            target = resolve(root, proposal.path, must_exist=True)
            snapshot = read_snapshot(target)
            if proposal.base_sha256 and snapshot.sha256 != proposal.base_sha256:
                raise StaleBaseError(
                    f"'{target.display}' changed since it was read; it was not deleted."
                )
            target.absolute.unlink()
            return EditResult(
                proposal, True, f"Deleted '{target.display}'.",
                before_sha256=snapshot.sha256, after_sha256=None,
            )

        if proposal.kind is EditKind.RENAME:
            source = resolve(root, proposal.path, must_exist=True)
            snapshot = read_snapshot(source)
            if proposal.base_sha256 and snapshot.sha256 != proposal.base_sha256:
                raise StaleBaseError(
                    f"'{source.display}' changed since it was read; it was not renamed."
                )
            if not proposal.destination:
                return EditResult(proposal, False, "A rename needs a destination.")
            destination = resolve(root, proposal.destination)
            if destination.absolute.exists():
                return EditResult(
                    proposal, False,
                    f"'{destination.display}' already exists; the rename was refused rather than overwriting it.",
                )
            destination.absolute.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source.absolute, destination.absolute)
            return EditResult(
                proposal, True,
                f"Renamed '{source.display}' to '{destination.display}'.",
                before_sha256=snapshot.sha256,
                after_sha256=snapshot.sha256,
            )

        target = resolve(root, proposal.path)
        snapshot = read_snapshot(target)

        # The stale-base check. This is the whole reason base_sha256 exists.
        if proposal.base_sha256 is not None and snapshot.sha256 != proposal.base_sha256:
            raise StaleBaseError(
                f"'{target.display}' changed on disk after JARVIS read it. The change "
                "was NOT applied — your version is intact. JARVIS will re-read the file "
                "and plan again rather than overwrite what changed."
            )

        if proposal.kind is EditKind.CREATE and snapshot.existed:
            return EditResult(
                proposal, False,
                f"'{target.display}' already exists; the create was refused.",
                before_sha256=snapshot.sha256,
            )

        new_text = proposal.new_text or ""
        diff = unified_diff(snapshot.text, new_text, target.display)
        if not diff:
            return EditResult(
                proposal, False, "No change was needed.",
                before_sha256=snapshot.sha256, after_sha256=snapshot.sha256,
            )

        payload = _encode(new_text, snapshot)
        _atomic_write(target.absolute, payload)
        after = sha256_bytes(payload)
        added, removed = diff_counts(diff)
        logger.info(
            "Coding edit applied: %s (%s -> %s)",
            target.display, snapshot.short_hash, after[:12],
        )
        return EditResult(
            proposal=proposal, applied=True,
            message=f"Wrote '{target.display}' ({added} added, {removed} removed).",
            before_sha256=snapshot.sha256, after_sha256=after,
            diff=diff, lines_added=added, lines_removed=removed,
        )
    except StaleBaseError as exc:
        return EditResult(proposal, False, str(exc))
    except WorkspaceViolation as exc:
        return EditResult(proposal, False, exc.reason)
    except OSError as exc:
        return EditResult(proposal, False, f"The file could not be written ({type(exc).__name__}).")


@dataclass
class EditBatch:
    """A set of proposals applied together, with a bounded total size.

    Applied one at a time and reported individually: a batch where three
    of five succeeded says exactly that, rather than "failed" or
    "succeeded".
    """

    results: List[EditResult] = field(default_factory=list)

    @property
    def applied(self) -> List[EditResult]:
        return [r for r in self.results if r.applied]

    @property
    def refused(self) -> List[EditResult]:
        return [r for r in self.results if not r.applied]

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.applied for r in self.results)

    def as_dict(self) -> dict:
        return {
            "applied_count": len(self.applied),
            "refused_count": len(self.refused),
            "results": [r.as_dict() for r in self.results],
        }


def apply_all(
    root: Path,
    proposals: List[EditProposal],
    budget: Optional[limits.TaskBudget] = None,
    *,
    approved_deletes: Optional[set] = None,
) -> EditBatch:
    batch = EditBatch()
    approved = approved_deletes or set()
    for proposal in proposals:
        if budget is not None:
            reason = budget.spend_file_edit()
            if reason is not None:
                batch.results.append(EditResult(proposal, False, f"Stopped: {reason}."))
                continue
            size = len((proposal.new_text or "").encode("utf-8", errors="replace"))
            reason = budget.spend_patch_bytes(size)
            if reason is not None:
                batch.results.append(EditResult(proposal, False, f"Stopped: {reason}."))
                continue
        batch.results.append(
            apply(root, proposal, approved_delete=proposal.path in approved)
        )
    return batch


def normalise_for_compare(text: str) -> str:
    """Unicode-normalise for comparison only — never for writing.

    Two paths that differ only by Unicode normalisation form look
    identical on screen and are different keys in a dict. Comparisons use
    this; the bytes written to disk never do.
    """
    return unicodedata.normalize("NFC", text)
