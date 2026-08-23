"""Command classification — deciding what a proposed command is allowed
to be, before anything runs it.

This module never executes anything. It takes an argv list and returns a
verdict. `app/coding/runner.py` executes; keeping the two apart means the
decision can be tested exhaustively without a single process being
spawned, and means there is exactly one place to read to find out what is
permitted.

**argv, never a string.** A command is a list of arguments. There is no
parsing of a command line here, because there is no command line: nothing
in Coding Workspace ever builds a shell string, so shell metacharacters
have no meaning to defend against. `;`, `&&`, `|`, `>` and backticks are
literal characters in an argument, and an argument containing them is
still refused — not because they would be interpreted, but because a
proposal containing them means the model believed it was writing a shell
line, and acting on a misunderstanding that large is not safe even when
it is technically inert.

**Three tiers, and the third is not "ask harder".**

* AUTO — read-only or project-declared, runs once the user has started
  the task.
* APPROVAL — real consequences: network, installs, commit, deletion,
  anything unrecognised.
* BLOCKED — never, in this pass, at any approval level. A user cannot
  approve `powershell -c ...` here, because a coding workspace that can
  run arbitrary PowerShell is not a coding workspace, it is a remote
  shell with extra steps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence

from app.core.models import RiskLevel


class CommandTier(str, Enum):
    AUTO = "auto"
    APPROVAL = "approval"
    BLOCKED = "blocked"


@dataclass
class Verdict:
    tier: CommandTier
    reason: str
    risk: RiskLevel
    # What the user must be told before approving, when approval applies.
    disclosure: Dict[str, object] = field(default_factory=dict)

    @property
    def allowed_without_approval(self) -> bool:
        return self.tier is CommandTier.AUTO

    def as_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "reason": self.reason,
            "risk": self.risk.value,
            "disclosure": self.disclosure,
        }


# --------------------------------------------------------------------------
# Programs that may never be run from Coding Workspace in this pass.
#
# Matched on the executable's stem, case-insensitively, so `PowerShell.EXE`,
# `powershell`, and `C:\...\powershell.exe` are the same thing.
# --------------------------------------------------------------------------
BLOCKED_PROGRAMS = frozenset({
    # Shells and interpreters-as-shells
    "powershell", "pwsh", "cmd", "command", "wscript", "cscript", "bash",
    "sh", "zsh", "dash", "ksh", "csh", "fish", "busybox",
    # Remote execution / privilege
    "psexec", "psexec64", "runas", "sudo", "su", "gsudo", "elevate",
    "winrs", "wmic", "at", "schtasks", "sc", "net", "net1",
    # System modification
    "reg", "regedit", "regedt32", "bcdedit", "diskpart", "format",
    "vssadmin", "wbadmin", "cipher", "takeown", "icacls", "cacls",
    "netsh", "firewall", "defender", "mpcmdrun", "secedit", "gpupdate",
    "shutdown", "logoff", "tsdiscon",
    # Credential access
    "cmdkey", "vaultcmd", "mimikatz", "lsass",
    # Network reach / scanning
    "nmap", "masscan", "netcat", "nc", "ncat", "telnet", "ssh", "scp",
    "sftp", "rsync", "ftp", "tftp", "curl", "wget", "iwr", "invoke-webrequest",
    # Destructive file operations outside the patch engine
    "del", "erase", "rd", "rmdir", "rm", "move", "robocopy", "xcopy",
    "attrib", "fsutil",
    # Process control not owned by this task
    "taskkill", "tskill", "kill", "pkill", "killall",
})

# Programs that are legitimate development tools.
KNOWN_TOOLCHAIN = frozenset({
    "node", "npm", "npx", "pnpm", "pnpx", "yarn", "bun", "bunx", "deno",
    "python", "python3", "py", "pip", "pip3", "uv", "poetry", "pipenv",
    "pytest", "ruff", "black", "mypy", "pyright", "flake8", "isort",
    "tsc", "eslint", "prettier", "vite", "vitest", "jest", "webpack",
    "rollup", "esbuild", "swc", "playwright",
    "git",
})

# Git subcommands that only read. Anything not here needs approval, and a
# few are blocked outright further down.
GIT_READ_ONLY = frozenset({
    "status", "diff", "log", "show", "branch", "remote", "rev-parse",
    "ls-files", "ls-tree", "worktree", "config", "describe", "shortlog",
    "blame", "cat-file", "symbolic-ref", "stash",
})

# Git operations that are refused in this pass whatever the user approves,
# because each of them can destroy work that was never JARVIS's to destroy,
# or reaches a remote.
GIT_BLOCKED = frozenset({
    "push", "merge", "rebase", "reset", "clean", "checkout", "switch",
    "restore", "cherry-pick", "revert", "filter-branch", "filter-repo",
    "gc", "prune", "reflog", "am", "apply", "clone", "fetch", "pull",
    "submodule", "remote-add", "update-ref",
})

# Package-manager subcommands that install, i.e. execute third-party code.
INSTALL_SUBCOMMANDS = frozenset({
    "install", "i", "add", "ci", "update", "upgrade", "remove", "uninstall",
    "link", "unlink", "dlx", "create", "init", "exec",
})

# Arguments that reveal a proposal built as a shell line rather than argv.
_SHELL_ARTEFACTS = re.compile(r"[;&|><`$\n\r]|\|\||&&|\$\(")

# npm/pnpm/yarn lifecycle scripts that run automatically on install and are
# the classic supply-chain execution point.
LIFECYCLE_SCRIPTS = ("preinstall", "install", "postinstall", "prepare", "prepublish")


def _stem(program: str) -> str:
    """The comparable name of an executable: no directory, no extension,
    lower case. `C:\\Program Files\\nodejs\\npm.cmd` -> `npm`."""
    cleaned = (program or "").strip().strip('"').replace("\\", "/")
    tail = cleaned.rsplit("/", 1)[-1]
    for suffix in (".exe", ".cmd", ".bat", ".ps1", ".com", ".msi"):
        if tail.lower().endswith(suffix):
            tail = tail[: -len(suffix)]
            break
    return tail.lower()


def _has_shell_artefact(argv: Sequence[str]) -> Optional[str]:
    for argument in argv:
        if _SHELL_ARTEFACTS.search(argument or ""):
            return argument
    return None


def _suspicious_script_body(body: str) -> Optional[str]:
    """The first blocked program named inside a declared package script,
    or None.

    Package scripts chain commands with `&&` and `|` as a matter of
    routine, so the presence of shell syntax proves nothing. What matters
    is *which programs* the script names. `tsc && vite build` is fine;
    `curl … | sh` is not, and the difference is `curl` and `sh`, not the
    pipe between them.
    """
    if not body:
        return None
    for token in re.split(r"[\s;&|<>()`'\"]+", body):
        token = token.strip()
        if not token or token.startswith("-"):
            continue
        if _stem(token) in BLOCKED_PROGRAMS:
            return _stem(token)
    return None


def _network_flag(argv: Sequence[str]) -> bool:
    joined = " ".join(argv).lower()
    return bool(re.search(r"https?://|git@|--registry|--proxy", joined))


def classify(
    argv: Sequence[str],
    *,
    declared_commands: Optional[Dict[str, dict]] = None,
    approved_argvs: Optional[List[List[str]]] = None,
) -> Verdict:
    """The verdict for one proposed command.

    `declared_commands` is what the project itself declares (from
    `stacks.project_commands`). A command that matches one of those is
    AUTO — the project's own test script is the project's business.
    Anything else is APPROVAL at best.
    """
    if not argv or not isinstance(argv, (list, tuple)) or not str(argv[0]).strip():
        return Verdict(CommandTier.BLOCKED, "An empty command cannot be run.", RiskLevel.BLOCKED)

    argv = [str(a) for a in argv]
    program = _stem(argv[0])
    rest = argv[1:]

    artefact = _has_shell_artefact(argv)
    if artefact is not None:
        return Verdict(
            CommandTier.BLOCKED,
            "This command contains shell syntax. Coding Workspace runs argument "
            "lists, never shell lines, so a pipeline or redirection here would "
            "not do what it appears to — it is refused rather than run literally.",
            RiskLevel.BLOCKED,
        )

    if program in BLOCKED_PROGRAMS:
        return Verdict(
            CommandTier.BLOCKED,
            f"'{program}' is not available from Coding Workspace in this version.",
            RiskLevel.BLOCKED,
        )

    # An absolute or relative path to an executable is refused: the
    # toolchain policy is a list of *names* resolved on PATH, and a path
    # is how something outside that list gets run.
    first = argv[0].replace("\\", "/")
    if "/" in first or first.startswith("."):
        return Verdict(
            CommandTier.BLOCKED,
            "Only known development tools may be run, by name. A path to an "
            "executable is refused.",
            RiskLevel.BLOCKED,
        )

    if program == "git":
        return _classify_git(rest)

    if program not in KNOWN_TOOLCHAIN:
        return Verdict(
            CommandTier.APPROVAL,
            f"'{program}' is not one of the development tools JARVIS knows. It needs "
            "your approval, and it will run with your account's permissions.",
            RiskLevel.SENSITIVE,
            disclosure={"program": program, "unknown_tool": True},
        )

    # A project-declared command is checked BEFORE the package-manager
    # rules, because `npm run test` *is* how a project declares its test
    # command and treating it as a generic package-manager invocation
    # would put an approval prompt in front of the most ordinary action
    # in the entire feature.
    #
    # But `package.json` is untrusted project content (Zone 3), so the
    # script's *body* is inspected too: a project whose "test" script is
    # `curl https://… | sh` does not get to launder that through this
    # branch. What the body is checked for is a blocked program name, not
    # shell syntax — `tsc && vite build` is an ordinary, legitimate build
    # script and must not be treated as an attack.
    for intent, entry in (declared_commands or {}).items():
        if list(entry.get("argv") or []) != list(argv):
            continue
        suspicious = _suspicious_script_body(str(entry.get("declared") or ""))
        if suspicious is not None:
            return Verdict(
                CommandTier.APPROVAL,
                f"This project's '{intent}' script runs '{suspicious}', which JARVIS "
                "does not run automatically. Review the script before approving.",
                RiskLevel.SENSITIVE,
                disclosure={
                    "declared_script": str(entry.get("declared") or "")[:400],
                    "flagged_program": suspicious,
                    "intent": intent,
                },
            )
        return Verdict(
            CommandTier.AUTO,
            f"This is the project's own '{intent}' command ({entry.get('source', 'declared')}).",
            RiskLevel.REVERSIBLE,
        )

    for approved in (approved_argvs or []):
        if list(approved) == list(argv):
            return Verdict(
                CommandTier.AUTO,
                "You approved this exact command earlier in this task.",
                RiskLevel.REVERSIBLE,
            )

    if program in ("npm", "pnpm", "yarn", "bun", "pip", "pip3", "poetry", "uv", "pipenv"):
        return _classify_package_manager(program, rest)

    if program in ("npx", "pnpx", "bunx"):
        return Verdict(
            CommandTier.APPROVAL,
            f"'{program}' can download and execute a package that is not installed. "
            "It needs approval every time, and JARVIS will not run it with "
            "auto-confirm flags.",
            RiskLevel.SENSITIVE,
            disclosure={"program": program, "downloads_code": True, "argv": list(argv)},
        )

    if _network_flag(argv):
        return Verdict(
            CommandTier.APPROVAL,
            "This command reaches the network.",
            RiskLevel.SENSITIVE,
            disclosure={"network": True, "argv": list(argv)},
        )

    # Version queries are the one genuinely inert family.
    if rest and rest[-1] in ("--version", "-v", "-V", "--help", "-h"):
        return Verdict(
            CommandTier.AUTO,
            "A version or help query, which changes nothing.",
            RiskLevel.READ_ONLY,
        )

    return Verdict(
        CommandTier.APPROVAL,
        f"'{' '.join(argv)}' is a development tool, but it is not a command this "
        "project declares, so it needs your approval.",
        RiskLevel.SENSITIVE,
        disclosure={"argv": list(argv), "outside_project_scripts": True},
    )


def _classify_git(rest: Sequence[str]) -> Verdict:
    if not rest:
        return Verdict(CommandTier.APPROVAL, "A bare 'git' does nothing useful.", RiskLevel.SENSITIVE)

    subcommand = rest[0].lower()
    args = [a.lower() for a in rest[1:]]

    if subcommand in GIT_BLOCKED:
        return Verdict(
            CommandTier.BLOCKED,
            f"'git {subcommand}' is not available in this version. It can move, "
            "discard or publish work that is not JARVIS's to touch.",
            RiskLevel.BLOCKED,
        )

    if subcommand == "worktree":
        # Adding a worktree is how isolation happens; removing one could
        # discard task changes, so it is not auto.
        if args and args[0] in ("list",):
            return Verdict(CommandTier.AUTO, "Lists existing worktrees.", RiskLevel.READ_ONLY)
        return Verdict(
            CommandTier.APPROVAL,
            "Changing worktrees needs approval.",
            RiskLevel.SENSITIVE,
        )

    if subcommand == "config":
        if "--unset" in args or any(not a.startswith("-") for a in args[1:]):
            return Verdict(
                CommandTier.APPROVAL,
                "Changing Git configuration needs your approval.",
                RiskLevel.SENSITIVE,
            )
        return Verdict(CommandTier.AUTO, "Reads a Git configuration value.", RiskLevel.READ_ONLY)

    if subcommand == "stash":
        # Stashing moves the user's uncommitted work somewhere they did not
        # put it. Never automatic.
        return Verdict(
            CommandTier.APPROVAL,
            "Stashing moves your uncommitted changes. JARVIS never does that "
            "without asking.",
            RiskLevel.SENSITIVE,
        )

    if subcommand in GIT_READ_ONLY:
        return Verdict(CommandTier.AUTO, f"'git {subcommand}' only reads.", RiskLevel.READ_ONLY)

    if subcommand in ("add", "commit"):
        return Verdict(
            CommandTier.APPROVAL,
            f"'git {subcommand}' needs your approval. JARVIS shows the exact commit "
            "message and file list first, and never pushes.",
            RiskLevel.SENSITIVE,
            disclosure={"git": subcommand},
        )

    return Verdict(
        CommandTier.APPROVAL,
        f"'git {subcommand}' is not a read-only Git command, so it needs approval.",
        RiskLevel.SENSITIVE,
    )


def _classify_package_manager(program: str, rest: Sequence[str]) -> Verdict:
    if not rest:
        return Verdict(
            CommandTier.APPROVAL,
            f"A bare '{program}' may install dependencies.",
            RiskLevel.SENSITIVE,
        )

    subcommand = rest[0].lower()

    if program in ("npm", "pnpm", "yarn", "bun") and subcommand in ("run", "run-script"):
        return Verdict(
            CommandTier.APPROVAL,
            "Running a package script executes whatever that script contains. If it "
            "is one of the project's declared scripts, JARVIS proposes it directly "
            "instead and it runs without this prompt.",
            RiskLevel.SENSITIVE,
            disclosure={"runs_project_script": True, "script": rest[1] if len(rest) > 1 else ""},
        )

    if subcommand in INSTALL_SUBCOMMANDS:
        packages = [a for a in rest[1:] if not a.startswith("-")]
        return Verdict(
            CommandTier.APPROVAL,
            f"'{program} {subcommand}' installs or removes packages. Installing a "
            "package runs its lifecycle scripts, which is third-party code "
            "executing on this machine.",
            RiskLevel.SENSITIVE,
            disclosure={
                "installs_packages": True,
                "manager": program,
                "packages": packages,
                "lifecycle_scripts_may_run": list(LIFECYCLE_SCRIPTS),
                "network": True,
                "changes_lockfile": True,
            },
        )

    if subcommand in ("list", "ls", "outdated", "why", "view", "show", "--version"):
        return Verdict(
            CommandTier.AUTO,
            f"'{program} {subcommand}' only reports what is already installed.",
            RiskLevel.READ_ONLY,
        )

    return Verdict(
        CommandTier.APPROVAL,
        f"'{program} {subcommand}' needs approval.",
        RiskLevel.SENSITIVE,
    )


def describe_matrix() -> List[dict]:
    """The risk matrix as data, so the UI and the documentation show the
    same thing the classifier actually implements rather than a copy that
    drifts."""
    return [
        {
            "tier": "Automatic",
            "examples": [
                "git status / diff / log / branch",
                "the project's own test, lint, format, typecheck and build scripts",
                "tool --version",
                "npm ls / outdated",
            ],
            "why": "Read-only, or a command the project itself declares.",
        },
        {
            "tier": "Approval required",
            "examples": [
                "npm / pnpm / yarn / pip install, add, update, remove",
                "npx and anything that can download code",
                "git add / commit / config change / stash",
                "any command reaching the network",
                "any development tool not declared by the project",
                "any program JARVIS does not recognise",
            ],
            "why": "Real consequences: third-party code execution, network access, "
                   "or moving work that is not JARVIS's.",
        },
        {
            "tier": "Blocked outright",
            "examples": [
                "PowerShell, cmd, bash and every other shell",
                "git push / merge / rebase / reset / clean / checkout / clone",
                "registry, services, scheduled tasks, firewall, defender",
                "taskkill and other process control",
                "curl, wget, ssh, scp, nmap and network reach",
                "rm, del, move, robocopy and file operations outside the patch engine",
                "any executable named by path rather than by known name",
            ],
            "why": "Not approvable in this version at any level.",
        },
    ]
