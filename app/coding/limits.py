"""Every hard bound in Coding Workspace, in one file.

Scattered limits are limits nobody can audit. A reviewer asking "what
stops this running forever" should be able to read one screen.

These are ceilings, not targets. Reaching one stops the task with a
stated reason — never a silent truncation that leaves the user believing
the work finished.
"""

from __future__ import annotations

from dataclasses import dataclass

# --------------------------------------------------------------------------
# The agent loop
# --------------------------------------------------------------------------
MAX_STEPS = 40                     # proposals executed in one task
MAX_ELAPSED_SECONDS = 900.0        # 15 minutes wall clock for a whole task
MAX_COMMANDS = 20                  # command executions in one task
MAX_FILES_READ = 60
MAX_FILES_EDITED = 25
MAX_RETRIES_PER_OPERATION = 2      # a third attempt at the same failing thing
MAX_CONCURRENT_PROCESSES = 2       # a preview plus one command

# --------------------------------------------------------------------------
# Bytes
# --------------------------------------------------------------------------
MAX_FILE_READ_BYTES = 256 * 1024          # one file into context
MAX_TOTAL_MODEL_BYTES = 1_200_000         # everything sent to a model, per task
MAX_COMMAND_OUTPUT_BYTES = 200 * 1024     # captured per command
MAX_PATCH_BYTES = 512 * 1024              # one patch
MAX_TOTAL_PATCH_BYTES = 2 * 1024 * 1024   # all patches in one task
MAX_EDITABLE_FILE_BYTES = 2 * 1024 * 1024  # refuse to edit anything larger

# --------------------------------------------------------------------------
# Commands and processes
# --------------------------------------------------------------------------
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0
MAX_COMMAND_TIMEOUT_SECONDS = 600.0
TERMINATE_GRACE_SECONDS = 3.0
KILL_GRACE_SECONDS = 3.0

# --------------------------------------------------------------------------
# Preview
# --------------------------------------------------------------------------
MAX_PREVIEW_LIFETIME_SECONDS = 1800.0     # 30 minutes, then it is stopped
PREVIEW_READY_TIMEOUT_SECONDS = 60.0
PREVIEW_POLL_INTERVAL_SECONDS = 0.5
PREVIEW_PORT_RANGE = (5180, 5279)         # searched for a free loopback port

# --------------------------------------------------------------------------
# Project walking
# --------------------------------------------------------------------------
MAX_PROJECT_FILES_LISTED = 4000
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_FILE_BYTES = 1024 * 1024


@dataclass
class TaskBudget:
    """The live remaining allowance for one task.

    Mutated as the task runs. `spend_*` returns the reason a limit was hit,
    or None when there was room — callers stop on a reason rather than
    testing a counter themselves and getting the comparison wrong.
    """

    steps: int = MAX_STEPS
    commands: int = MAX_COMMANDS
    files_read: int = MAX_FILES_READ
    files_edited: int = MAX_FILES_EDITED
    model_bytes: int = MAX_TOTAL_MODEL_BYTES
    patch_bytes: int = MAX_TOTAL_PATCH_BYTES

    def spend_step(self) -> str | None:
        if self.steps <= 0:
            return f"the {MAX_STEPS}-step limit for one task was reached"
        self.steps -= 1
        return None

    def spend_command(self) -> str | None:
        if self.commands <= 0:
            return f"the {MAX_COMMANDS}-command limit for one task was reached"
        self.commands -= 1
        return None

    def spend_file_read(self) -> str | None:
        if self.files_read <= 0:
            return f"the {MAX_FILES_READ}-file read limit for one task was reached"
        self.files_read -= 1
        return None

    def spend_file_edit(self) -> str | None:
        if self.files_edited <= 0:
            return f"the {MAX_FILES_EDITED}-file edit limit for one task was reached"
        self.files_edited -= 1
        return None

    def spend_model_bytes(self, count: int) -> str | None:
        if count > self.model_bytes:
            return (
                f"the {MAX_TOTAL_MODEL_BYTES:,}-byte limit on project content sent "
                "to the model was reached"
            )
        self.model_bytes -= count
        return None

    def spend_patch_bytes(self, count: int) -> str | None:
        if count > self.patch_bytes:
            return f"the {MAX_TOTAL_PATCH_BYTES:,}-byte limit on changes in one task was reached"
        self.patch_bytes -= count
        return None
