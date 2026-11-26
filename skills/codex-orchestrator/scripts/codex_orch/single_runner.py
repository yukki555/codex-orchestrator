from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from collections import deque
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import compact_stdout, env, schema_check, supervisor


SCRIPT_DIR = Path(__file__).resolve().parents[1]
RUNS_DIR = Path("/tmp/codex-orchestrator")
RESUME_BLOAT_TOKEN_THRESHOLD = 1_000_000
_SESSION_SEARCH_EXCLUDED_DIRS = {"attempts", "_worktrees", ".git", "__pycache__", "node_modules"}
MODES = ("consult", "implement", "review", "resume", "research", "critique", "analyze")

USAGE = """run_codex.sh v3.9.0

Usage:
  run_codex.sh consult   --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]
  run_codex.sh implement --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]
  run_codex.sh review    --dir <DIR> [--uncommitted | --base <BRANCH> | --commit <SHA>] [--spec <FILE> | --prompt <TEXT>] [options]
  run_codex.sh resume    --dir <DIR> (--id <SESSION_ID> | --last) (--spec <FILE> | --prompt <TEXT>) [--sandbox read-only|workspace-write] [options]
  run_codex.sh research  --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]
  run_codex.sh critique  --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]
  run_codex.sh analyze   --dir <DIR> (--spec <FILE> | --prompt <TEXT>) [options]

Common options:
  --dir <DIR>              Target directory (default: .)
  --spec <FILE> | --prompt <TEXT>
                           Prompt passed via stdin
  --model <MODEL|inherit>  Codex model override
  --effort <LEVEL>         low|medium|high|xhigh|max|ultra|inherit
  --verbosity <LEVEL>      low|medium|high|inherit (defaults by mode)
  --fast                   Use the configured fast preset for unset model/effort
  --speed <TIER>           default|fast|ultrafast|inherit
  --profiles-file <FILE>   Override the bundled execution profiles
  --profile <NAME>         Codex config profile
  --no-mcp | --with-mcp    Disable (default) or allow external MCP servers
  --ignore-user-config | --use-user-config
                           Ignore config by default unless using --profile or --with-mcp
  --ignore-rules | --use-rules
                           Ignore (default) or load Codex rules files
  --ephemeral              Run without persisting session files
  --worktree               Run in a managed Git worktree (unavailable for resume)
  -c, --config <k=v>       Additional Codex TOML configuration (repeatable)
  --timeout <SECONDS>      Wall timeout (exit 124)
  --stall-timeout <SECONDS> Event stream idle timeout (exit 125)
  --stdout-mode <MODE>     auto|compact|facts|full (implement defaults to facts, others to auto)
  --invocation-id <ID>     Non-empty run ID (defaults to a random UUID4 hex ID)
  --max-cmds <N>           Prompt command budget (0 disables the footer)
  --no-clarity-footer      Skip the Output Clarity Contract in exec prompts
  --add-dir <DIR>          Writable directory for implement or writable resume (repeatable)
  --structured | --schema <FILE>
                           Validate output with a bundled or custom JSON Schema
  -i, --image <FILE>       Attach a readable image (repeatable)
  --web-search             Enable live web search
  --out-dir <DIR>          Artifact directory (default: /tmp/codex-orchestrator/<mode>-<ts>-<pid>)
  -h, --help               Show this help message

Review uses native review for a bare target, and read-only exec with a prompt,
schema, image, or positive command budget. The default target is --uncommitted.
Resume uses a read-only sandbox by default. Writable resume by ID inherits
prior writable directories when --add-dir is omitted.

Host proof-of-work options (implement with a valid Git HEAD):
  --contract <FILE>        Override the contract stored at HEAD
  --allowed-path <GLOB>    Restrict changed paths (repeatable)
  --protected-path <GLOB>  Forbid changes to matching paths (repeatable)
  --verify-cmd <COMMAND> | --verify-preset <NAME>
                           Verify in an isolated worktree without a shell
  --verify-timeout <SEC>   Verification timeout (default: 120)
  --tdd                    Require an independent RED and GREEN verification
  --tdd-test-dir <DIR>     Overlay repo-relative test files before RED
  --tdd-test-path <GLOB>   Protect matching tests (repeatable)
  --tdd-from-working-tree  Copy matching tests from the working tree
  --tdd-allow-skips        Allow skipped tests during GREEN
"""

BUDGET_FOOTER = """
## Execution Budget
- Batch multi-file reads, searches, and JSON/log parsing into at most {max_cmds} batched python3 or shell commands instead of sequential per-file calls.
- Never run broad repo scans (rg --files, find, ls -R) when exact paths are already named above.
- Treat verified inline snapshots/metrics in this prompt as ground truth; do not re-read them unless checking a discrepancy.
- Keep total command stdout under 20 KB and stop once required findings are verified.
- Do NOT read ~/.codex/skills/* or probe/scan host disks for missing runtimes when environment facts are given in the Spec.
"""

OUTPUT_CLARITY_FOOTER = """
## Output Clarity Contract
- Write every summary, rationale, finding, and explanation in complete, self-contained sentences for a reader who has not seen your intermediate reasoning or tool logs.
- Never carry over internal step/slot codes, working shorthand, telegraphic fragments, or invented compound labels; cite exact repository file paths, symbols, commands, and numbers directly.
- Explicitly surface every skipped check, unverified assumption, or unresolved blocker rather than omitting it.
"""


DEFAULT_REVIEW = (
    "Review for correctness bugs, edge cases, regressions (including DDP, numerical, and shape "
    "behavior where relevant), backward-compatibility breaks, and missing tests. Prioritize "
    "actionable findings and cite file locations."
)


class UsageError(Exception):
    def __init__(self, message, *, show_usage=False, raw=False):
        super().__init__(message)
        self.show_usage = show_usage
        self.raw = raw


@dataclass
class Options:
    mode: str
    dir: str = "."
    spec: str = ""
    prompt: str = ""
    model: str = ""
    effort: str = ""
    speed: str = ""
    verbosity: str = ""
    fast: bool = False
    profile: str = ""
    profiles_file: str | None = None
    mcp_choice: str = ""
    config_choice: str = ""
    rules_choice: str = ""
    ephemeral: bool = False
    worktree: bool = False
    config: list[str] = field(default_factory=list)
    timeout: int = 0
    stall_timeout: int = 0
    stdout_mode: str = ""
    no_clarity_footer: bool = False
    invocation_id: str = ""
    max_cmds: int | None = None
    add_dir: list[str] = field(default_factory=list)
    structured: bool = False
    schema: str | None = None
    image: list[str] = field(default_factory=list)
    web_search: bool = False
    out_dir: str = ""
    review_target: str = ""
    review_arg: str = ""
    review_engine: str = ""
    id: str = ""
    last: bool = False
    sandbox: str = "read-only"
    contract: str | None = None
    allowed_path: list[str] = field(default_factory=list)
    protected_path: list[str] = field(default_factory=list)
    verify_cmd: str | None = None
    verify_preset: str | None = None
    verify_timeout: int | None = None
    tdd: bool = False
    tdd_test_dir: str | None = None
    tdd_test_path: list[str] = field(default_factory=list)
    tdd_from_working_tree: bool = False
    tdd_allow_skips: bool = False

    @property
    def no_mcp(self):
        return self.mcp_choice != "--with-mcp"

    @property
    def ignore_user_config(self):
        return self.config_choice != "--use-user-config" and not self.profile and self.no_mcp

    @property
    def sandbox_desc(self):
        if self.mode == "resume":
            return self.sandbox
        if self.mode == "implement":
            return "workspace-write"
        return "review-default" if self.review_engine == "native-review" else "read-only"


VALUE_FLAGS = frozenset("""
    dir spec prompt model effort speed verbosity profile profiles-file config timeout stall-timeout
    stdout-mode invocation-id max-cmds add-dir schema image out-dir base commit id sandbox
    contract allowed-path protected-path verify-cmd verify-preset verify-timeout tdd-test-dir
    tdd-test-path
""".split())
BOOL_FLAGS = frozenset("""
    fast ephemeral worktree structured no-clarity-footer web-search last tdd tdd-from-working-tree tdd-allow-skips
""".split())
REPEATABLE = {"config", "add-dir", "image", "allowed-path", "protected-path", "tdd-test-path"}
CHOICE_FLAGS = {
    "mcp_choice": ("--no-mcp", "--with-mcp"),
    "config_choice": ("--ignore-user-config", "--use-user-config"),
    "rules_choice": ("--ignore-rules", "--use-rules"),
}
ENUM_FLAGS = {
    "speed": ("default", "fast", "ultrafast", "inherit"),
    "stdout-mode": ("auto", "compact", "facts", "full"),
}
POSITIVE_FLAGS = {"timeout", "stall-timeout", "verify-timeout"}


def parse_args(argv):
    if not argv:
        raise UsageError("", show_usage=True)
    if argv[0] in ("-h", "--help"):
        return None
    if argv[0] not in MODES:
        raise UsageError(f"unknown mode '{argv[0]}'", show_usage=True)
    options = Options(argv[0])
    args = iter(argv[1:])
    for flag in args:
        if flag in ("-h", "--help"):
            return None
        name = {"-c": "config", "-i": "image"}.get(flag, flag[2:] if flag.startswith("--") else "")
        attr = name.replace("-", "_")
        if name in BOOL_FLAGS:
            setattr(options, attr, True)
            continue
        choice = next((key for key, pair in CHOICE_FLAGS.items() if flag in pair), None)
        if choice:
            if getattr(options, choice) not in ("", flag):
                first, second = CHOICE_FLAGS[choice]
                raise UsageError(f"{first} and {second} are mutually exclusive")
            setattr(options, choice, flag)
            continue
        if name not in VALUE_FLAGS and name != "uncommitted":
            raise UsageError(f"unknown option '{flag}'", show_usage=True)

        value = "" if name == "uncommitted" else next(args, None)
        if name == "invocation-id" and not value:
            raise UsageError("--invocation-id requires a non-empty ID")
        if value is None:
            expectation = "a value"
            if name in POSITIVE_FLAGS:
                expectation = "a positive integer (seconds)"
            elif name == "max-cmds":
                expectation = "a non-negative integer"
            elif name == "config":
                expectation = "a <key=value> argument"
            raise UsageError(f"{flag} requires {expectation}")
        if name == "verbosity" and not value:
            raise UsageError("--verbosity requires a non-empty level")
        if name in ENUM_FLAGS and value not in ENUM_FLAGS[name]:
            prefix = ", ".join(ENUM_FLAGS[name][:-1])
            raise UsageError(f"invalid {flag} '{value}'; expected {prefix}, or {ENUM_FLAGS[name][-1]}")
        if name in POSITIVE_FLAGS or name == "max-cmds":
            positive = name in POSITIVE_FLAGS
            if not re.fullmatch(r"[1-9][0-9]*" if positive else r"[0-9]+", value):
                expectation = "a positive integer (seconds)" if positive else "a non-negative integer"
                raise UsageError(f"{flag} must be {expectation}, got '{value}'")
            try:
                value = int(value)
            except ValueError as error:
                raise UsageError(f"{flag} integer is too large") from error
        if name in ("base", "commit", "uncommitted"):
            if options.review_target and options.review_target != ("uncommitted" if name == "uncommitted" else ""):
                raise UsageError("--uncommitted, --base, and --commit are mutually exclusive")
            options.review_target, options.review_arg = name, value
        elif name in REPEATABLE:
            if name == "image":
                if not Path(value).is_file() or not os.access(value, os.R_OK):
                    raise UsageError(f"image file is missing or unreadable: {value}")
                value = os.path.realpath(value)
            getattr(options, attr).append(value)
        else:
            setattr(options, attr, value)
    return options


def git(directory, *args, child_env=None):
    return subprocess.run(
        ["git", "-C", str(directory), *args], env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def git_checked(directory, *args):
    result = git(directory, *args)
    if result.returncode:
        detail = env.redact_secrets(os.fsdecode(result.stderr).strip())
        raise RuntimeError(detail or f"git {args[0]} exited {result.returncode}")
    return result.stdout


def read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


@dataclass
class PriorSession:
    model: str
    add_dirs: list[str]
    input_tokens: int
    status: str
    summary_path: str


def _session_summaries(root: Path, *, max_depth: int):
    if root.is_symlink():
        return
    for directory, dirnames, filenames in os.walk(root):
        path = Path(directory)
        if len(path.relative_to(root).parts) >= max_depth:
            dirnames.clear()
        else:
            dirnames[:] = [
                name for name in dirnames
                if name not in _SESSION_SEARCH_EXCLUDED_DIRS and not (path / name).is_symlink()
            ]
        if "summary.json" in filenames:
            yield path / "summary.json"


def _prior_input_tokens(data: dict) -> int:
    tokens = data.get("tokens")
    raw = tokens.get("input") if isinstance(tokens, dict) else None
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError, OverflowError):
        return 0


def _has_unsafe_session_path(path: Path) -> bool:
    path = path.absolute()
    if _SESSION_SEARCH_EXCLUDED_DIRS.intersection(path.parts):
        return True
    anchors = {RUNS_DIR, Path.home(), Path(tempfile.gettempdir()),
               Path("/tmp"), Path("/private/tmp"), Path("/var"), Path("/private/var"), Path("/")}
    try:
        for component in (path, *path.parents):
            if component == RUNS_DIR:
                return component.is_symlink()
            if component in anchors:
                break
            if component.is_symlink():
                return True
    except OSError:
        return True
    return False


def _immutable_summary_path(path: Path, data: dict) -> str:
    artifacts = data.get("attempt_artifacts")
    summary = artifacts.get("summary_json") if isinstance(artifacts, dict) else None
    attempt_dir = data.get("attempt_dir")
    if isinstance(summary, str) and summary:
        candidate = Path(summary)
    elif isinstance(attempt_dir, str) and attempt_dir:
        candidate = Path(attempt_dir) / "summary.json"
    else:
        return str(path)
    try:
        if (stat.S_ISREG(candidate.lstat().st_mode)
                and (path.parent / "attempts").resolve() in candidate.resolve().parents):
            return str(candidate)
    except (OSError, ValueError, RuntimeError):
        pass
    return str(path)


def prior_session(thread_id, *, inherit_dirs, out_dir: Path | None = None) -> PriorSession | None:
    candidates = {}

    def consider(path):
        if _SESSION_SEARCH_EXCLUDED_DIRS.intersection(path.parent.parts):
            return
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                candidates[path.resolve()] = info.st_mtime
        except (OSError, RuntimeError):
            pass

    for path in _session_summaries(RUNS_DIR, max_depth=8):
        consider(path)
    if out_dir is not None and not _has_unsafe_session_path(out_dir):
        consider(out_dir / "summary.json")
    if out_dir is not None and not _has_unsafe_session_path(out_dir.parent):
        try:
            parent = out_dir.parent.resolve()
            runs = RUNS_DIR.resolve()
            broad_roots = {Path("/"), Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve(),
                           Path.home().resolve()}
        except (OSError, RuntimeError):
            pass
        else:
            if parent not in broad_roots and parent != runs and runs not in parent.parents and parent.is_dir():
                for path in _session_summaries(parent, max_depth=3):
                    consider(path)

    for path in sorted(candidates, key=lambda path: (candidates[path], path), reverse=True):
        try:
            data = read_json(path)
        except (OSError, ValueError, RecursionError):
            continue
        if isinstance(data, dict) and data.get("thread_id") == thread_id:
            model = data.get("model")
            if not isinstance(model, str) or model == "inherit" or "\0" in model:
                model = ""
            dirs = data.get("add_dirs")
            if not inherit_dirs or not isinstance(dirs, list):
                dirs = []
            add_dirs = list(dict.fromkeys(d for d in dirs if isinstance(d, str) and os.path.isdir(d)))
            status = data.get("status")
            return PriorSession(model, add_dirs, _prior_input_tokens(data),
                                status if isinstance(status, str) else "", _immutable_summary_path(path, data))
    return None


@dataclass
class HostPolicy:
    repo: Path
    base_oid: str
    cwd_rel: str
    source: str
    timeout: int
    verify_argv: list[str]
    verify_source: str
    cli_allowed: list[str]
    contract_allowed: list[str]
    protected: list[str]
    isolated: bool


def preflight_host(options, target, is_git, out):
    tdd_options = (options.tdd_test_dir is not None or options.tdd_test_path
                   or options.tdd_from_working_tree or options.tdd_allow_skips)
    explicit = (options.contract is not None or options.allowed_path or options.protected_path
                or options.verify_cmd is not None or options.verify_preset is not None
                or options.verify_timeout is not None or options.tdd or tdd_options)
    if options.mode != "implement" or not is_git:
        if explicit:
            raise UsageError("host verification and path policy options require implement in a Git repository with a valid HEAD")
        return None
    head = git(target, "rev-parse", "--verify", "HEAD^{commit}")
    if head.returncode:
        if explicit:
            raise UsageError("host verification and path policy options require implement in a Git repository with a valid HEAD")
        return None
    from . import verify

    base_oid = head.stdout.decode("ascii").strip()
    repo = Path(os.fsdecode(git_checked(target, "rev-parse", "--show-toplevel").removesuffix(b"\n")))
    if options.verify_cmd is not None and options.verify_preset is not None:
        raise UsageError("--verify-cmd and --verify-preset are mutually exclusive")
    if tdd_options and not options.tdd:
        raise UsageError("--tdd-test-dir, --tdd-test-path, --tdd-from-working-tree, and --tdd-allow-skips require --tdd")
    if options.tdd and options.tdd_test_dir is None and not options.tdd_test_path:
        raise UsageError("--tdd requires --tdd-test-dir or --tdd-test-path to freeze protected tests")
    if options.tdd and options.verify_cmd is None and options.verify_preset is None:
        raise UsageError("--tdd requires --verify-cmd or --verify-preset")
    if options.tdd_from_working_tree and (options.tdd_test_dir is not None or not options.tdd_test_path):
        raise UsageError("--tdd-from-working-tree requires --tdd-test-path and is mutually exclusive with --tdd-test-dir")
    for attr in ("contract", "verify_cmd", "verify_preset", "tdd_test_dir"):
        value = getattr(options, attr)
        if value is not None and not value.strip():
            raise UsageError(f"--{attr.replace('_', '-')} requires a non-empty value")
    if options.tdd_test_dir is not None:
        if not Path(options.tdd_test_dir).is_dir() or not os.access(options.tdd_test_dir, os.R_OK | os.X_OK):
            raise UsageError(f"--tdd-test-dir is not an accessible directory: {options.tdd_test_dir}")

    contract, source = verify.load_trusted_contract(repo, base_oid, contract_path=options.contract)
    cli_allowed = options.allowed_path
    contract_allowed = contract.get("allowed_paths", [])
    protected = list(dict.fromkeys([*contract.get("protected_paths", []), *options.protected_path]))
    verify.validate_path_globs(cli_allowed, "--allowed-path")
    verify.validate_path_globs(options.protected_path, "--protected-path")
    verify.validate_path_globs(options.tdd_test_path, "--tdd-test-path")
    limit = contract.get("max_timeout_s")
    timeout = options.verify_timeout or 120
    if limit is not None:
        if options.timeout > limit:
            raise UsageError(f"--timeout {options.timeout} exceeds contract max_timeout_s {limit}")
        timeout = min(timeout, limit)
        if not options.timeout:
            options.timeout = limit

    argv, verify_source = [], source
    if options.verify_cmd is not None or options.verify_preset is not None:
        argv, verify_source = verify.resolve_verify_argv(
            contract, verify_cmd=options.verify_cmd, verify_preset=options.verify_preset,
        )

    isolated = options.worktree or options.tdd
    if not isolated and contract.get("dirty_policy") == "fail":
        dirty = git_checked(repo, "status", "--porcelain", "--untracked-files=all")
        if dirty:
            raise UsageError("host verification requires a clean checkout; use --worktree for isolation")
    if not (argv or cli_allowed or contract_allowed or protected or options.tdd):
        return None
    if not isolated:
        try:
            out_rel = out.resolve().relative_to(repo)
        except ValueError:
            pass
        else:
            ignored = git(repo, "check-ignore", "-q", "--", f"{out_rel.as_posix()}/")
            tracked = git_checked(repo, "--literal-pathspecs", "ls-files", "-z", "--", out_rel.as_posix())
            # A candidate captured in place would include the runner's own artifacts.
            isolated = bool(ignored.returncode or tracked)
    return HostPolicy(repo, base_oid, target.relative_to(repo).as_posix(), source, timeout,
                      argv, verify_source, cli_allowed, contract_allowed, protected, isolated)


@dataclass
class Run:
    options: Options
    target: Path
    out: Path
    is_git: bool
    executable: str
    model: str
    effort: str
    speed: str
    speed_explicit: bool
    verbosity: str
    host: HostPolicy | None
    prior: PriorSession | None
    bloat_reasons: list[str]


def preflight(options):
    from . import profiles

    if not Path(options.dir).is_dir() or not options.dir or not os.access(options.dir, os.X_OK):
        raise UsageError(f"target directory does not exist: {options.dir}")
    target = Path(os.path.realpath(options.dir))
    if options.spec and options.prompt:
        raise UsageError("--spec and --prompt are mutually exclusive")
    if options.config_choice == "--ignore-user-config":
        if options.profile:
            raise UsageError("--ignore-user-config and --profile are mutually exclusive")
        if not options.no_mcp:
            raise UsageError("--ignore-user-config and --with-mcp are mutually exclusive")
    if options.profiles_file is not None and not options.profiles_file.strip():
        raise UsageError("--profiles-file requires a non-empty path")
    config = profiles.load_profiles(options.profiles_file, script_dir=SCRIPT_DIR)
    if options.mode not in config["valid_modes"]:
        raise UsageError(f"invalid mode: {options.mode!r}")
    if options.effort and options.effort not in config["valid_efforts"]:
        raise UsageError(f"invalid --effort '{options.effort}'. GPT-6.2 models require one of: "
                         "low, medium, high, xhigh, max, ultra (or 'inherit').")
    if options.stdout_mode and options.stdout_mode not in config.get("valid_stdout_modes", profiles.STDOUT_MODES):
        raise UsageError(f"invalid --stdout-mode: {options.stdout_mode!r}")
    if not options.stdout_mode:
        options.stdout_mode = profiles.default_stdout_mode(config, mode=options.mode)
    if options.spec and (Path(options.spec).is_dir() or not os.access(options.spec, os.R_OK)):
        raise UsageError(f"spec file is missing or unreadable: {options.spec}")
    if options.structured:
        if options.schema is not None:
            raise UsageError("--structured and --schema are mutually exclusive")
        options.schema = str(SCRIPT_DIR.parent / "references" / profiles.bundled_schema_filename(config, options.mode))
    if options.schema == "":
        raise UsageError("--schema requires a non-empty path")
    if options.schema is not None:
        _, error = schema_check.preflight_schema(options.schema)
        if error:
            path = Path(options.schema)
            if not path.is_file() or not os.access(path, os.R_OK):
                error += f"\nError: schema file is missing or unreadable: {options.schema}"
            raise UsageError(error, raw=True)
        if not options.structured:
            options.schema = os.path.realpath(options.schema)
    if options.mode != "review" and not (options.spec or options.prompt):
        raise UsageError(f"mode '{options.mode}' requires --spec <FILE> or --prompt <TEXT>")
    if options.mode == "resume":
        if options.worktree:
            raise UsageError("--worktree is not supported in 'resume' mode")
        if options.id and options.last:
            raise UsageError("--id and --last are mutually exclusive")
        if not options.id and not options.last:
            raise UsageError("'resume' mode requires --id <SESSION_ID> or --last")
        if options.sandbox not in ("read-only", "workspace-write"):
            raise UsageError("--sandbox must be 'read-only' or 'workspace-write'")
    if options.add_dir:
        if options.mode != "implement" and not (options.mode == "resume" and options.sandbox == "workspace-write"):
            raise UsageError("--add-dir requires implement or resume with --sandbox workspace-write")
        for directory in options.add_dir:
            if not directory or not Path(directory).is_dir() or not os.access(directory, os.X_OK):
                raise UsageError(f"--add-dir path is not an accessible directory: {directory}")
        options.add_dir = list(dict.fromkeys(map(os.path.realpath, options.add_dir)))

    if options.mode == "review":
        options.review_target = options.review_target or "uncommitted"
        custom = options.schema or options.spec or options.prompt or options.image or options.max_cmds
        options.review_engine = "read-only-exec" if custom else "native-review"
    if options.max_cmds is None:
        options.max_cmds = profiles.default_max_cmds(config, mode=options.mode, review_engine=options.review_engine)

    inside_worktree = False
    try:
        probe = git(target, "rev-parse", "--is-inside-work-tree")
        is_git = probe.returncode == 0
        inside_worktree = is_git and probe.stdout.strip() == b"true"
    except FileNotFoundError:
        is_git = False
    if options.mode == "review" and not is_git:
        raise UsageError(f"'{target}' is not inside a git repository. Use 'consult' mode for non-git review.")
    if options.worktree and not is_git:
        raise UsageError(f"--worktree requires '{target}' to be inside a git repository.")
    for choice, filename in ((options.config_choice, "config.toml"), (options.rules_choice, "rules")):
        if choice in ("--use-user-config", "--use-rules"):
            path = Path(os.environ.get("HOME", "")) / ".codex" / filename
            if path.exists() and not os.access(path, os.R_OK):
                raise UsageError(f"{choice} requires read access to {path}")
    if options.mode == "implement" and os.environ.get("CODEX_ORCH_ALLOW_INPLACE_SELF_EDIT") != "1":
        writable = [Path(d) for d in options.add_dir]
        if not (options.worktree or options.tdd):
            writable.insert(0, target)
        for directory in writable:
            common = Path(os.path.commonpath((directory, SCRIPT_DIR)))
            if common in (directory, SCRIPT_DIR):
                raise UsageError(f"the writable directory {directory} overlaps the running orchestrator scripts. "
                                 "Pass --worktree or run from a frozen /tmp copy of the scripts.")

    out = Path(options.out_dir or RUNS_DIR / f"{options.mode}-{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}").absolute()
    host = preflight_host(options, target, inside_worktree, out)
    prior = None
    bloat_reasons = []
    if options.mode == "resume" and options.id:
        prior = prior_session(
            options.id, inherit_dirs=options.sandbox == "workspace-write" and not options.add_dir,
            out_dir=out if options.out_dir else None,
        )
        if prior is not None:
            options.add_dir.extend(prior.add_dirs)
            if prior.input_tokens >= RESUME_BLOAT_TOKEN_THRESHOLD:
                bloat_reasons.append(
                    f"prior input tokens ({prior.input_tokens:,}) >= {RESUME_BLOAT_TOKEN_THRESHOLD:,}"
                )
            if prior.status in ("timeout", "stall_timeout"):
                bloat_reasons.append(f"prior session ended with status '{prior.status}'")
    speed_explicit = bool(options.speed)
    model, effort, speed, warnings = profiles.resolve_execution_profile(
        config, mode=options.mode, model=options.model or None, effort=options.effort or None,
        speed=options.speed or None, speed_explicit=speed_explicit, fast_mode=options.fast,
        profile_name=options.profile or None, prior_model=(prior.model or None) if prior else None,
    )
    verbosity, verbosity_warnings = profiles.resolve_verbosity(
        config, mode=options.mode, model=model, verbosity=options.verbosity or None,
        profile_name=options.profile or None,
    )
    executable = shutil.which("codex") or shutil.which("/opt/homebrew/bin/codex")
    if not executable:
        raise FileNotFoundError("'codex' binary not found in PATH or /opt/homebrew/bin/codex")
    for warning in [*warnings, *verbosity_warnings]:
        print(warning, file=sys.stderr)
    if bloat_reasons:
        print(f"warning: resuming thread {options.id} despite {', '.join(bloat_reasons)}; "
              "prefer a fresh implement/consult run to avoid context bloat.", file=sys.stderr)
    return Run(options, target, out, is_git, executable, model, effort, speed, speed_explicit, verbosity, host,
               prior, bloat_reasons)


@dataclass
class Attempt:
    number: int
    directory: Path

    def file(self, name):
        return self.directory / name


ARTIFACT_NAMES = {
    "last_message": "last_message.md",
    "events": "events.jsonl",
    "stderr": "stderr.log",
    "thread_id": "thread_id.txt",
    "summary_json": "summary.json",
    "supervisor_meta": "supervisor_meta.json",
    "review_prompt": "review_prompt.md",
    "prompt": "prompt.txt",
    "effective_prompt": "effective_prompt.md",
    "verification_json": "verification.json",
    "patch_file": "changes.patch",
    "impl_patch_file": "implementation.patch",
}


def allocate_attempt(out):
    env.ensure_private_dir(out)
    parent = env.ensure_private_dir(out / "attempts")
    number = 1
    for path in parent.iterdir():
        match = re.fullmatch(r"attempt-([0-9]+)", path.name)
        if match:
            number = max(number, int(match[1]) + 1)
    while True:
        path = parent / f"attempt-{number:03}"
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            number += 1
        else:
            path.chmod(0o700)
            return Attempt(number, path)


@contextmanager
def atomic_output(path):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        temp = Path(stream.name)
        try:
            yield stream
            stream.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def write_json(path, value):
    with atomic_output(path) as stream:
        stream.write((json.dumps(value, indent=2) + "\n").encode("utf-8"))


def publish(run, attempt, summary):
    # Attempt summaries refer to their own files. The compatibility summary
    # and its aliases can advance without changing that evidence.
    historical = dict(summary)
    for key in ("last_message", "events", "stderr", "summary_json"):
        historical[key] = str(attempt.file(ARTIFACT_NAMES[key]))
    write_json(attempt.file("summary.json"), historical)
    with (attempt.directory.parent / ".publish.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            info = read_json(run.out / "summary.json")
        except (OSError, ValueError, RecursionError):
            info = None
        if isinstance(info, dict) and type(info.get("attempt_num")) is int and info["attempt_num"] > attempt.number:
            return
        previous = info.get("attempt_artifacts", {}) if isinstance(info, dict) else {}
        for key, name in ARTIFACT_NAMES.items():
            if name == "summary.json":
                continue
            source, alias = attempt.file(name), run.out / name
            if source.is_file():
                with source.open("rb") as src, atomic_output(alias) as dest:
                    shutil.copyfileobj(src, dest)
            elif key in ("last_message", "events", "stderr", "thread_id", "supervisor_meta") or (
                isinstance(previous, dict) and key in previous
            ):
                alias.unlink(missing_ok=True)
        write_json(run.out / "summary.json", summary)


def shell_quote(word):
    # Bash printf %q used by the legacy review prompt.
    if not word:
        return "''"
    if any(not char.isprintable() for char in word):
        return "$'" + "".join(
            {"\a": r"\a", "\b": r"\b", "\t": r"\t", "\n": r"\n", "\v": r"\v",
             "\f": r"\f", "\r": r"\r", "\x1b": r"\E", "\\": r"\\", "'": r"\'"}.get(
                char, char if char.isprintable() else f"\\{ord(char):03o}",
            ) for char in word
        ) + "'"
    return "".join("\\" + char if char in " |&;()<>!{}[]*?$`\"'\\~#" else char for char in word)


def review_prompt(run):
    target, arg = run.options.review_target, run.options.review_arg
    if target == "uncommitted":
        unborn = git(run.target, "rev-parse", "--verify", "HEAD").returncode != 0
        if unborn:
            instructions = "Inspect with `git status --short`, `git diff`, and `git diff --cached`.\n"
            commands = [("diff", "--stat"), ("diff", "--cached", "--stat")]
        else:
            instructions = "Inspect with `git status --short` and `git diff HEAD`.\n"
            commands = [("diff", "--stat", "HEAD")]
        description = "Review uncommitted changes.\n"
    elif target == "base":
        ref = f"{arg}...HEAD"
        description = f"Review the diff against base branch {arg}.\n"
        instructions = f"Inspect with `git status --short` and `git diff {shell_quote(ref)}`.\n"
        commands = [("diff", "--stat", "--end-of-options", ref)]
    else:
        description = f"Review commit {arg}.\n"
        instructions = f"Inspect with `git status --short` and `git show {shell_quote(arg)}`.\n"
        commands = [("show", "--stat", "--format=medium", "--end-of-options", arg)]
    snapshot = []
    for command in [("status", "--short"), *commands]:
        child_env = dict(os.environ, GIT_OPTIONAL_LOCKS="0") if command[0] == "status" else None
        with subprocess.Popen(
            ["git", "-C", str(run.target), *command], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=child_env,
        ) as child:
            for line in child.stdout:
                if len(snapshot) < 80:
                    snapshot.append(b"    " + line.removesuffix(b"\n") + b"\n")
    header = "# Code Review Target\n\n" + description + instructions + "\nCompact Git snapshot\n\n"
    precedence = ("\n## Instruction Precedence\n\nThis prompt overrides repository AGENTS.md and "
                  "~/.codex/skills/*. You MUST NOT read ~/.codex/skills/*.\n\n")
    return os.fsencode(header) + b"".join(snapshot) + precedence.encode("utf-8")


def make_prompt(run, attempt, protected=()):
    opts = run.options
    if opts.review_engine == "native-review":
        return None
    if opts.spec:
        path = Path(opts.spec)
        if not (opts.review_engine or opts.max_cmds or protected or not opts.no_clarity_footer):
            return str(path)
        body = path.read_bytes()
    else:
        body = os.fsencode((opts.prompt or DEFAULT_REVIEW) + "\n")
        if not opts.review_engine:
            attempt.file("prompt.txt").write_bytes(body)

    if protected:
        footer = "\n## Protected Test Paths\n\nDo not modify, remove, or rename these test paths:\n"
        footer += "".join(f"- {json.dumps(path, ensure_ascii=False)}\n" for path in protected)
        body += os.fsencode(footer)
    if opts.max_cmds:
        body += BUDGET_FOOTER.format(max_cmds=opts.max_cmds).encode("utf-8")
    if not opts.no_clarity_footer:
        body += OUTPUT_CLARITY_FOOTER.encode("utf-8")
    if opts.review_engine:
        path = attempt.file("review_prompt.md")
        body = review_prompt(run) + body
    elif opts.max_cmds or protected or not opts.no_clarity_footer:
        path = attempt.file("effective_prompt.md")
    else:
        return str(attempt.file("prompt.txt"))
    path.write_bytes(body)
    return str(path)


def codex_command(run, attempt, target):
    opts = run.options
    command = [run.executable]
    if opts.web_search:
        command.append("--search")
    command.append("exec")
    profile = ["-p", opts.profile] if opts.profile else []
    if opts.review_engine == "native-review":
        command += ["-C", str(target), *profile, "review"]
    elif opts.mode == "resume":
        command += ["-C", str(target), "-s", opts.sandbox, *profile, "resume"]
    else:
        command += ["-s", opts.sandbox_desc, "-C", str(target)]
        if not run.is_git:
            command.append("--skip-git-repo-check")
    if opts.ephemeral:
        command.append("--ephemeral")
    if opts.worktree and target == run.target:
        command.append("--worktree")
    if opts.ignore_user_config:
        command.append("--ignore-user-config")
    if opts.rules_choice != "--use-rules":
        command.append("--ignore-rules")
    if opts.mode == "resume" and not run.is_git:
        command.append("--skip-git-repo-check")
    if opts.review_engine == "native-review":
        command.append(f"--{opts.review_target}")
        if opts.review_target != "uncommitted":
            command.append(opts.review_arg)
    if run.model != "inherit":
        command += ["-m", run.model]
    if opts.mode != "resume" and opts.review_engine != "native-review":
        command += profile

    configs = []
    if opts.no_mcp and not opts.ignore_user_config:
        configs.append("mcp_servers={}")
    if run.effort != "inherit":
        configs.append(f'model_reasoning_effort="{run.effort}"')
    if run.speed in ("fast", "ultrafast") or (run.speed_explicit and run.speed == "default"):
        configs.append(f'service_tier="{run.speed}"')
    if opts.mode == "resume" and opts.sandbox == "workspace-write" and opts.add_dir:
        roots = list(dict.fromkeys([str(target), *opts.add_dir]))
        toml_roots = json.dumps(roots, ensure_ascii=False).replace("\x7f", "\\u007f")
        configs.append(f"sandbox_workspace_write.writable_roots={toml_roots}")
    if not opts.web_search:
        configs.append('web_search="disabled"')
    if run.verbosity != "inherit":
        configs.append(f'model_verbosity="{run.verbosity}"')
    for config in [*configs, *opts.config]:
        command += ["-c", config]
    if opts.mode == "implement":
        for directory in opts.add_dir:
            command += ["--add-dir", directory]
    if opts.schema:
        command += ["--output-schema", opts.schema]
    for image in opts.image:
        command += ["-i", image]
    command += ["--json", "-o", str(attempt.file("last_message.md"))]
    if opts.mode == "resume":
        command.append("--last" if opts.last else opts.id)
    if opts.review_engine != "native-review":
        command.append("-")
    return command


@contextmanager
def test_overlay(run, parent):
    from . import verify

    opts, host = run.options, run.host
    if not opts.tdd_from_working_tree:
        yield Path(opts.tdd_test_dir).resolve() if opts.tdd_test_dir else None
        return
    with tempfile.TemporaryDirectory(dir=parent, prefix="test-overlay-") as directory:
        overlay = Path(directory)
        copy_working_tree_tests(host.repo, opts.tdd_test_path, overlay, verify.match_repo_glob)
        yield overlay


def copy_working_tree_tests(repo, test_paths, overlay, match_glob):
    entries = git_checked(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    matched = set()
    for name in dict.fromkeys(os.fsdecode(entries).split("\0")):
        if not name:
            continue
        patterns = [path for path in test_paths if match_glob(name, path)]
        if not patterns:
            continue
        src = repo / name
        if not src.is_file():
            continue
        if not src.resolve().is_relative_to(repo) or (src.is_symlink() and os.path.isabs(os.readlink(src))):
            raise ValueError(f"working-tree test symlink escapes the repository: {name}")
        dst = overlay / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)
        matched.update(patterns)
    missing = [path for path in test_paths if path not in matched]
    if missing:
        raise ValueError(f"working-tree test paths matched no files: {', '.join(missing)}")


class ProofOfWork:
    def __init__(self, run, attempt):
        self.run = run
        self.attempt = attempt
        self.host = run.host
        self.parent = None
        self.worker = None
        self.red = None
        self.green = None
        self.verification = None
        self.tdd = None
        if run.options.tdd:
            self.tdd = {"base_oid": self.host.base_oid, "red_ok": False, "green_ok": False,
                        "verification_json": str(attempt.file("verification.json"))}
        self.metadata = {"contract_source": self.host.source, "base_oid": self.host.base_oid}

    def verify_tree(self, *, base_oid, patches, tree, artifact):
        from . import verify

        return verify.run_host_verification(
            self.host.repo, base_oid=base_oid, patches=patches, expected_tree_oid=tree,
            verify_argv=self.host.verify_argv, out_path=self.attempt.file(artifact),
            worktrees_parent=self.parent, cwd_rel=self.host.cwd_rel, timeout_s=self.host.timeout,
            policy_source=self.host.verify_source,
            test_snapshot_tree=self.tdd.get("test_snapshot_tree") if self.tdd else None,
        )

    def prepare(self):
        from . import verify

        opts, host = self.run.options, self.host
        self.parent = Path(tempfile.mkdtemp(prefix="codex_orch_worktrees_", dir="/tmp"))
        anchor, protected = host.base_oid, []
        if self.tdd is not None:
            with test_overlay(self.run, self.parent) as overlay:
                snapshot = verify.create_test_snapshot(
                    host.repo, host.base_oid, test_overlay_dir=overlay,
                    test_paths=opts.tdd_test_path, worktrees_parent=self.parent,
                )
            self.tdd.update(snapshot)
            anchor = snapshot["test_snapshot_commit"]
            tree = snapshot["test_snapshot_tree"]
            protected = snapshot["protected_test_paths"]
            self.red = self.verify_tree(base_oid=anchor, patches=[], tree=tree, artifact="verification-red.json")
            code = self.red.get("exit_code")
            self.red["outcome"] = verify.analyze_test_outcome(
                code if type(code) is int else 0, self.red.get("stdout_tail", ""), self.red.get("stderr_tail", ""),
            )
            self.tdd["red_ok"] = (self.red["outcome"]["red_valid"] and type(code) is int
                                  and 0 < code < 126 and not self.red.get("timed_out")
                                  and self.red.get("reconstructed_tree_oid") == tree)
            if not self.tdd["red_ok"]:
                return None, protected
        if host.isolated:
            worker = self.parent / "worker"
            git_checked(host.repo, "worktree", "add", "--detach", str(worker), anchor)
            self.worker = worker
            self.metadata["worktree_dir"] = str(worker)
            return worker / host.cwd_rel, protected
        return self.run.target, protected

    def finish(self):
        from . import verify

        host, opts = self.host, self.run.options
        root = self.worker or host.repo
        base = host.base_oid
        if self.tdd is not None:
            base = self.tdd["test_snapshot_tree"]
        primary_patch = "implementation.patch" if self.tdd is not None else "changes.patch"
        export = verify.export_and_verify_patch(root, base, self.attempt.file(primary_patch))
        expected = export["final_tree_oid"]
        if not export["custody_verified"] or export["reconstructed_tree_oid"] != expected:
            raise RuntimeError("the exported patch did not reconstruct the candidate tree")
        if self.tdd is not None:
            self.tdd.update(impl_patch=export["patch_file"], impl_changed_files=export["changed_files"],
                            final_tree_oid=expected)
        else:
            self.metadata["patch"] = export

        # CLI restrictions further constrain a trusted contract allowlist.
        snapshot = self.tdd or {}
        policy = verify.check_path_policies(
            export["changed_files"], allowed_paths=host.contract_allowed or host.cli_allowed,
            protected_paths=[*host.protected, *snapshot.get("protected_test_globs", ())],
            exact_protected_paths=snapshot.get("protected_test_exact", ()),
        )
        if host.contract_allowed and host.cli_allowed:
            cli_policy = verify.check_path_policies(export["changed_files"], allowed_paths=host.cli_allowed)
            policy["allowed_violations"] = sorted(set(policy["allowed_violations"] + cli_policy["allowed_violations"]))
            policy["ok"] = policy["ok"] and cli_policy["ok"]
        self.metadata["path_policy"] = policy
        if not policy["ok"]:
            status = "protected_path_violation" if policy["protected_violations"] or self.tdd else "scope_violation"
            return status, 5

        if self.tdd is not None:
            export = verify.export_and_verify_patch(root, host.base_oid, self.attempt.file("changes.patch"))
            if (not export["custody_verified"] or export["final_tree_oid"] != expected
                    or export["reconstructed_tree_oid"] != expected):
                raise RuntimeError("the combined patch did not reconstruct the implementation tree")
            self.tdd["patch_file"] = export["patch_file"]
            self.metadata["patch"] = export
        if not host.verify_argv:
            return "ok", 0

        artifact = "verification-green.json" if self.tdd is not None else "verification.json"
        patches = [export["patch_file"]] if export["patch_bytes"] else []
        result = self.verify_tree(base_oid=host.base_oid, patches=patches, tree=expected, artifact=artifact)
        valid = bool(result["ok"] and not result.get("timed_out") and result["reconstructed_tree_oid"] == expected)
        if self.tdd is not None:
            self.green = result
            self.green["outcome"] = verify.analyze_test_outcome(
                result.get("exit_code", 1), result.get("stdout_tail", ""), result.get("stderr_tail", ""),
                allow_skips=opts.tdd_allow_skips,
            )
            self.tdd["green_ok"] = valid and self.green["outcome"]["green_valid"]
            valid = self.tdd["green_ok"]
        else:
            self.verification = result
        return ("ok", 0) if valid else ("verification_failed", 6)

    def close(self, status, error=None):
        if self.worker is not None and self.tdd is not None and status == "ok":
            try:
                git_checked(self.host.repo, "worktree", "remove", "--force", str(self.worker))
                git_checked(self.host.repo, "worktree", "prune")
            except (OSError, RuntimeError) as exc:
                status, error = "verification_failed", str(exc)
        if self.parent is not None:
            try:
                self.parent.rmdir()
            except OSError:
                pass  # A preserved worker or verifier worktree still owns this directory.
        if self.worker is not None:
            self.metadata["worktree_preserved"] = self.worker.is_dir()
        if self.tdd is not None:
            self.verification = {"ok": status == "ok", "red": self.red, "green": self.green,
                                 "base_oid": self.host.base_oid, "status": status,
                                 "red_ok": self.tdd["red_ok"], "green_ok": self.tdd["green_ok"]}
        if error is not None:
            if self.verification is None:
                self.verification = {"ok": False, "status": status}
            self.verification["error"] = env.redact_secrets(error)
            print(f"Error: {self.verification['error']}", file=sys.stderr)
        if self.verification is not None:
            path = self.attempt.file("verification.json")
            self.verification["verification_json"] = str(path)
            write_json(path, self.verification)
            self.metadata["verification"] = self.verification
        if self.tdd is not None:
            self.metadata["tdd"] = self.tdd
        return status


def reject_json_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def event_telemetry(events_path):
    thread_id = ""
    commands = cmd_output_bytes = malformed = 0
    tokens = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    event_errors, seen_errors = [], set()
    modified_files, in_flight = {}, {}
    failed_commands = deque(maxlen=5)
    last_agent_messages = deque(maxlen=3)

    event_stream = events_path.open("rb") if events_path.is_file() else nullcontext(())
    with event_stream as events:
        for raw_line in events:
            if not raw_line.strip():
                continue
            try:
                obj = json.loads(raw_line.decode("utf-8"), parse_constant=reject_json_constant)
            except (ValueError, RecursionError):
                obj = None
            if not isinstance(obj, dict):
                malformed += 1
                continue

            if not thread_id:
                ids = [obj.get(key) for key in ("thread_id", "session_id", "conversation_id")]
                for parent in (obj.get("thread"), obj.get("session")):
                    if isinstance(parent, dict):
                        ids.append(parent.get("id"))
                thread_id = next((value for value in ids if isinstance(value, str) and value), "")

            etype = obj.get("type")
            if etype in ("item.started", "item.completed"):
                item = obj.get("item")
                if not isinstance(item, dict):
                    continue
                item_type, item_id = item.get("type"), item.get("id")
                if etype == "item.started":
                    command = item.get("command")
                    if item_type == "command_execution" and isinstance(item_id, str) and isinstance(command, str):
                        in_flight[item_id] = env.redact_secrets(command)[:200]
                    continue
                if isinstance(item_id, str):
                    in_flight.pop(item_id, None)
                if item_type == "command_execution":
                    commands += 1
                    output = item.get("aggregated_output")
                    if not isinstance(output, str):
                        output = ""
                    cmd_output_bytes += len(output.encode("utf-8", errors="replace"))
                    exit_code = item.get("exit_code")
                    if exit_code is not None and exit_code != 0:
                        command = item.get("command")
                        failed_commands.append({
                            "exit_code": exit_code,
                            "command": env.redact_secrets(command)[:200] if isinstance(command, str) else "",
                            "output_tail": env.redact_secrets(output)[-300:],
                        })
                elif item_type == "file_change" and isinstance(item.get("changes"), list):
                    for change in item["changes"]:
                        if isinstance(change, dict) and isinstance(change.get("path"), str):
                            modified_files[change["path"]] = None
                elif item_type == "agent_message" and isinstance(item.get("text"), str):
                    last_agent_messages.append(item["text"][:400])
            elif etype == "turn.completed" and isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
                try:
                    tokens = {key: int(usage.get(source) or 0) for key, source in (
                        ("input", "input_tokens"), ("cached", "cached_input_tokens"),
                        ("output", "output_tokens"), ("reasoning", "reasoning_output_tokens"),
                    )}
                except (ValueError, TypeError, OverflowError):
                    malformed += 1
            elif etype in ("error", "turn.failed"):
                error = obj.get("message") if etype == "error" else obj.get("error")
                if etype == "turn.failed" and isinstance(error, dict):
                    error = error.get("message")
                if isinstance(error, str) and error and error not in seen_errors:
                    seen_errors.add(error)
                    event_errors.append(f"{etype}: {env.redact_secrets(error)}")

    telemetry = {"thread_id": thread_id or "unknown", "commands": commands, "malformed_event_lines": malformed,
                 "cmd_output_kb": round(cmd_output_bytes / 1024.0, 2), "tokens": tokens}
    progress = {"modified_files": list(modified_files), "last_agent_messages": list(last_agent_messages),
                "failed_commands": list(failed_commands), "in_flight_commands": list(in_flight.values())[:5]}
    return thread_id, telemetry, progress, event_errors


def codex_result(meta, structured_error):
    if meta["cancelled"]:
        return "cancelled", 128 + (meta["signal"] or signal.SIGTERM)
    if meta["stall_timed_out"]:
        return "stall_timeout", 125
    if meta["timed_out"]:
        return "timeout", 124
    if meta["codex_exit_code"]:
        return ("paused_quota" if meta["paused_quota"] else "error"), meta["codex_exit_code"]
    if structured_error:
        return "schema_error", 3
    return "ok", 0


def empty_metadata(options):
    return {"codex_exit_code": None, "duration_s": 0.0, "timed_out": False,
            "stall_timed_out": False, "paused_quota": False, "cancelled": False, "signal": None,
            "timeout_s": options.timeout, "stall_timeout_s": options.stall_timeout}


def supervise(run, attempt, target, protected):
    try:
        return supervisor.run_supervised(
            codex_command(run, attempt, target), prompt_path=make_prompt(run, attempt, protected),
            events_path=attempt.file("events.jsonl"), stderr_path=attempt.file("stderr.log"),
            timeout_s=run.options.timeout, stall_timeout_s=run.options.stall_timeout,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error: unable to read supervisor metadata: {error}", file=sys.stderr)
        return dict(empty_metadata(run.options), codex_exit_code=1)


def announce(summary, preview, schema_error, event_errors, stderr_tail):
    def print_field(key):
        value = summary[key]
        print(f"{key}: {json.dumps(value) if isinstance(value, bool) else value}")

    for key in ("status", "exit_code", "codex_exit_code", "mode", "model", "effort", "fast", "speed",
                "verbosity", "no_mcp",
                "ignore_user_config", "ignore_rules", "structured", "web_search"):
        print_field(key)
    if summary["review_engine"]:
        print_field("review_engine")
    if summary["structured_valid"] is not None:
        print_field("structured_valid")
        if schema_error:
            flag = "--structured" if summary["structured"] else "--schema"
            print(f"warning: {flag} output in last_message.md {env.redact_secrets(schema_error)}", file=sys.stderr)
    for key in ("duration_s", "commands", "cmd_output_kb", "budget_exceeded", "stdout_truncated"):
        print_field(key)
    tokens = summary["tokens"]
    print(f"tokens: in={tokens['input']} cached={tokens['cached']} out={tokens['output']} reason={tokens['reasoning']}")
    for key in ("stderr_errors", "dir", "sandbox", "thread_id", "out_dir", "summary_json", "last_message", "events", "stderr"):
        print_field(key)

    if summary["stderr_errors"] and not summary["no_mcp"]:
        print(f"tip: stderr reported {summary['stderr_errors']} error(s) with --with-mcp enabled; "
              "retry with default --no-mcp or --ignore-user-config")
    if summary.get("resume_bloat_warning") is True:
        print(f"tip: resume bloat warning ({', '.join(summary['resume_bloat_reasons'])}); "
              "prefer a fresh implement/consult run")
    patch = summary.get("patch")
    if isinstance(patch, dict):
        paths = patch.get("changed_files", [])
        suffix = f" (+{len(paths) - 10} more)" if len(paths) > 10 else ""
        print(f"host_changed_files: {len(paths)} {json.dumps(paths[:10])}{suffix}")
        print(f"patch_file: {patch.get('patch_file')}")
        print(f"patch_bytes: {patch.get('patch_bytes')}")
    verification = summary.get("verification")
    if isinstance(verification, dict):
        ok = json.dumps(verification.get("ok") is True)
        print(f"host_verification: ok={ok} exit_code={verification.get('exit_code')} "
              f"file={verification.get('verification_json')}")

    facts_only = summary["stdout_mode"] == "facts"
    progress = {key: entries for key, entries in summary.get("partial_progress", {}).items()
                if key != "last_agent_messages" and entries}
    if progress:
        print("--- partial_progress ---")
        for key, entries in progress.items():
            suffix = f" (+{len(entries) - 5} more in summary.json)" if len(entries) > 5 else ""
            if facts_only and key == "failed_commands":
                entries = [{name: entry[name] for name in ("exit_code", "command")} for entry in entries]
            print(f"{key}: {json.dumps(entries[:5])}{suffix}")
    if preview:
        print("--- last_message ---")
        print(preview, end="" if preview.endswith("\n") else "\n")
    if not facts_only and summary["status"] != "ok" and event_errors:
        print("--- event_errors ---")
        print("\n".join(event_errors))
    if not facts_only and summary["exit_code"] and stderr_tail:
        print("--- stderr (tail) ---")
        print(env.redact_secrets("".join(stderr_tail)), end="")


def summarize(run, attempt, result, meta, proof, last_message, structured_valid, structured_error):
    opts = run.options
    status, exit_code = result
    thread_id, telemetry, progress, event_errors = event_telemetry(attempt.file("events.jsonl"))
    if thread_id:
        attempt.file("thread_id.txt").write_text(thread_id + "\n", encoding="utf-8", errors="backslashreplace")
    stderr_errors = 0
    stderr_tail = deque(maxlen=30)
    with attempt.file("stderr.log").open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            stderr_tail.append(line)
            stderr_errors += "ERROR" in line

    out_dir = opts.out_dir or str(run.out)
    aliases = {key: os.path.join(out_dir, ARTIFACT_NAMES[key]) for key in ("last_message", "events", "stderr", "summary_json")}
    preview, truncated = "", False
    if last_message:
        preview, truncated = compact_stdout.format_last_message_preview(
            last_message, mode=opts.stdout_mode, threshold_bytes=4096, max_preview_bytes=2048,
            last_message_path=aliases["last_message"], summary_path=aliases["summary_json"],
        )
    commands = telemetry["commands"]
    modified = progress["modified_files"]
    summary = {
        "status": status, "exit_code": exit_code, **meta,
        "invocation_id": opts.invocation_id or uuid.uuid4().hex,
        "mode": opts.mode, "model": run.model, "effort": run.effort, "speed": run.speed,
        "verbosity": run.verbosity,
        "clarity_footer": opts.review_engine != "native-review" and not opts.no_clarity_footer,
        "fast": run.speed in ("fast", "ultrafast"), "fast_preset": opts.fast,
        "no_mcp": opts.no_mcp, "ignore_user_config": opts.ignore_user_config,
        "ignore_rules": opts.rules_choice != "--use-rules",
        "structured": opts.structured, "structured_valid": structured_valid, "schema_file": opts.schema or "",
        "web_search": opts.web_search, "images": opts.image, "ephemeral": opts.ephemeral,
        "worktree": opts.worktree or bool(run.host and run.host.isolated),
        "review_engine": opts.review_engine, "add_dirs": opts.add_dir,
        "modified_files": modified, "max_cmds": opts.max_cmds,
        "budget_exceeded": opts.max_cmds > 0 and commands > opts.max_cmds,
        "budget_utilization": commands / opts.max_cmds if opts.max_cmds else None,
        "cmd_per_modified_file": commands / len(modified) if modified else None,
        **telemetry, "stderr_errors": stderr_errors,
        "dir": str(run.target), "sandbox": opts.sandbox_desc, "out_dir": out_dir,
        "last_message_bytes": len(last_message), "stdout_mode": opts.stdout_mode, "stdout_truncated": truncated,
        **aliases, "attempt_num": attempt.number, "attempt_dir": str(attempt.directory),
        "attempt_artifacts": {key: str(attempt.file(name)) for key, name in ARTIFACT_NAMES.items()
                              if key == "summary_json" or attempt.file(name).is_file()},
    }
    if opts.mode == "resume":
        summary["resume_bloat_warning"] = bool(run.bloat_reasons)
        if run.bloat_reasons:
            summary["resume_bloat_reasons"] = run.bloat_reasons
        if run.prior is not None:
            summary["prior_session"] = {
                "summary_json": run.prior.summary_path, "status": run.prior.status,
                "input_tokens": run.prior.input_tokens,
            }
    if proof is not None:
        summary.update(proof.metadata)
    if status != "ok":
        summary["partial_progress"] = progress
    publish(run, attempt, summary)
    announce(summary, preview, structured_error, event_errors, stderr_tail)
    return exit_code


def execute(run):
    received_signal = None

    def receive_signal(signum, _frame):
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum

    handlers = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            handlers[signum] = signal.signal(signum, receive_signal)
        return execute_attempt(run, lambda: received_signal)
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)


def execute_attempt(run, pending_signal):
    attempt = allocate_attempt(run.out)
    for name in ("events.jsonl", "stderr.log"):
        attempt.file(name).touch()
    proof = ProofOfWork(run, attempt) if run.host else None
    opts = run.options
    meta = empty_metadata(opts)
    target, protected = run.target, []
    error = None
    result = "ok", 0
    if proof is not None and not pending_signal():
        try:
            target, protected = proof.prepare()
            if target is None:
                result = "tdd_red_failed", 4
        except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
            if opts.tdd and not proof.tdd["red_ok"]:
                result = "tdd_red_failed", 4
            else:
                result = "verification_failed", 6
            error = str(exc)

    last_message = b""
    structured_valid = structured_error = None
    if result[1] == 0 and not pending_signal():
        meta = supervise(run, attempt, target, protected)
        last_path = attempt.file("last_message.md")
        last_message = last_path.read_bytes() if last_path.is_file() else b""
        if (opts.schema and meta["codex_exit_code"] == 0 and not pending_signal()
                and not any(meta[key] for key in ("cancelled", "timed_out", "stall_timed_out", "paused_quota"))):
            structured_valid, structured_error, _ = schema_check.validate_last_message(last_message, opts.schema)
        result = codex_result(meta, structured_valid is False)
        if result[1] == 0 and proof is not None and not pending_signal():
            try:
                result = proof.finish()
            except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
                result, error = ("verification_failed", 6), str(exc)

    signum = meta["signal"] if meta["cancelled"] else pending_signal()
    if signum is not None:
        meta.update(cancelled=True, signal=signum)
        result = "cancelled", 128 + signum
    if proof is not None:
        status = proof.close(result[0], error)
        if status != result[0]:
            result = status, 6
    write_json(attempt.file("supervisor_meta.json"), meta)
    return summarize(run, attempt, result, meta, proof, last_message, structured_valid, structured_error)


def main(argv=None):
    try:
        options = parse_args(sys.argv[1:] if argv is None else argv)
        if options is None:
            print(USAGE, end="")
            return 0
        run = preflight(options)
    except UsageError as error:
        if str(error):
            print(str(error) if error.raw else f"Error: {error}", file=sys.stderr)
        if error.show_usage:
            print(USAGE, file=sys.stderr, end="")
        return 2
    except FileNotFoundError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 127
    except (OSError, ValueError, RuntimeError, ImportError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    try:
        return execute(run)
    except OSError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
