"""Validate handoff packets and create successor Antigravity sessions."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .context_telemetry import (
    chain_position_from_tags,
    conversation_state_lock,
    save_conversation_state,
    update_conversation_state,
)
from .env import build_child_env, ensure_private_dir, redact_secrets

_HANDOFFS = Path("/tmp/codex-orchestrator/handoffs")
_SERVICE = "product.v1.ConversationService"
_TIERS = ("pro", "flash", "flash_lite")
_MIN_PACKET_BYTES = 400
_MAX_PACKET_BYTES = 24576
_SECTIONS = re.compile(r"^##[ \t]+(?:(\d+)\.[ \t]+)?[^\n]*", re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_PATH_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?|#L\d+(?:-L?\d+)?)$")
_LOCAL_PATH = r"(?:file://(?=/)|~(?=/))?/(?!/)"
_PATH_REFERENCE = re.compile(
    r"\[[^\]\n]*\]\((?:<(" + _LOCAL_PATH + r"[^>\n]+)>|("
    + _LOCAL_PATH + r"(?:[^()\n]|\([^()\n]*\))+))\)"
    r"|`(" + _LOCAL_PATH + r"[^`\n]+)`"
    r"|(?<![\w.:/\\~?=&%#@-])(" + _LOCAL_PATH + r"[^\s`<>\[\](),;:]+"
    r"(?::\d+(?:-\d+)?)?)"
)
_ARTIFACTS = ("summary.json", "parallel-summary.json", "verification.json",
              "changes.patch", "spec.md", "contract.md", "corpus")
_ID = re.compile(r"[A-Za-z0-9_.-]+")
_CONFLICT = re.compile(rb"^(?:<<<<<<< |>>>>>>> )", re.MULTILINE)


class _PreflightError(ValueError):
    pass


class _ServiceUnavailable(Exception):
    pass


class _CreateTimeout(_ServiceUnavailable):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _PreflightError(message)


def _secret_safe(text, environ):
    token = environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if token:
        text = text.replace(token, "[REDACTED]")
    return redact_secrets(text)


def _safe_data(value, environ):
    if isinstance(value, str):
        return _secret_safe(value, environ)
    if isinstance(value, dict):
        return {_secret_safe(key, environ): _safe_data(item, environ)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_data(item, environ) for item in value]
    return value


def _json(value):
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _write(path, text):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".handoff-", delete=False) as handle:
        pending = Path(handle.name)
        try:
            handle.write(text)
            handle.close()
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)


def _git(workspace, *args, check=True):
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(workspace), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check,
        env={**build_child_env(), "LC_ALL": "C"},
    )


def _git_text(workspace, *args):
    return _git(workspace, *args).stdout.decode("utf-8", errors="backslashreplace").rstrip("\n")


def _git_root(workspace):
    result = _git(workspace, "rev-parse", "--show-toplevel", check=False)
    if result.returncode == 0:
        return Path(os.fsdecode(result.stdout.removesuffix(b"\n")))
    if b"not a git repository" not in result.stderr and workspace.is_dir():
        result.check_returncode()
    return None


def _workspace_errors(workspace):
    if not workspace.is_dir():
        return [f"Workspace is missing or is not a directory: {workspace}"]
    root = _git_root(workspace)
    if root is None:
        return []
    errors = []
    if _git(root, "ls-files", "-u", "-z").stdout:
        errors.append(f"Git index has unmerged entries: {root}")
    changed = set()
    for command in (("diff",), ("diff", "--cached")):
        changed.update(_git(root, *command, "--no-renames", "--name-only",
                            "--diff-filter=ACMRTU", "-z", "--").stdout.split(b"\0"))
    changed.update(_git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split(b"\0"))
    for name in sorted(changed - {b""}):
        path = root / os.fsdecode(name)
        if path.is_symlink() or not path.is_file():
            continue
        content = path.read_bytes()
        if b"\0" not in content and _CONFLICT.search(content):
            errors.append(f"Git conflict markers in {path}")
    return errors


def _referenced_paths(text):
    for match in _PATH_REFERENCE.finditer(text):
        angle, linked, quoted, bare = match.groups()
        path = bare.rstrip(".!?*\"'") if bare else (angle or linked or quoted).strip()
        path = _PATH_SUFFIX.sub("", path)
        if path.startswith("file://"):
            path = unquote(path[len("file://"):])
        elif path.startswith("/"):
            first_segment, _, remainder = path[1:].partition("/")
            if not remainder.strip("/") or any(char.isspace() for char in first_segment):
                continue
        yield Path(path).expanduser()


def _validate_packet(packet_path, workspaces, environ):
    errors = []
    info = {"packet_path": str(packet_path), "size_bytes": 0, "sections": [],
            "referenced_paths": []}
    try:
        with Path(packet_path).expanduser().open("rb") as handle:
            info["size_bytes"] = os.fstat(handle.fileno()).st_size
            content = handle.read(_MAX_PACKET_BYTES + 1)
        if not _MIN_PACKET_BYTES <= info["size_bytes"] <= _MAX_PACKET_BYTES:
            return [f"Packet must be {_MIN_PACKET_BYTES}–{_MAX_PACKET_BYTES} bytes"], info, ""
        text = content.decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        return [f"Cannot read UTF-8 packet: {exc}"], info, ""

    if "<!--" in text or re.search(r"<(?:REPLACE_ME|TODO)>", text, re.IGNORECASE):
        errors.append("Packet contains unresolved HTML comments or placeholders")
    token = environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if redact_secrets(text) != text or (len(token) >= 8 and token in text):
        errors.append("Packet contains an unredacted secret")

    # Ignore headings and references embedded in comments when checking content.
    clean = _COMMENT.sub("", text)
    headings = list(_SECTIONS.finditer(clean))
    sections = {}
    for index, heading in enumerate(headings):
        number = heading.group(1)
        if number is None or len(number) != 1 or number not in "12345678":
            continue
        if number in sections:
            errors.append(f"Duplicate packet section {number}")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(clean)
        sections[number] = clean[heading.end():end].strip()

    info["sections"] = sorted(int(number) for number in sections)
    for number in "12345678":
        if number not in sections:
            errors.append(f"Missing packet section {number}")
        elif len(sections[number]) < 10:
            errors.append(f"Packet section {number} must have at least 10 characters of content")

    paths = sorted(set(_referenced_paths(sections.get("6", "") + "\n" + sections.get("7", ""))))
    info["referenced_paths"] = [str(path) for path in paths]
    for path in paths:
        if not path.exists():
            errors.append(f"Referenced path does not exist: {path}")
    for workspace in workspaces:
        errors.extend(_workspace_errors(Path(workspace).expanduser()))
    return errors, info, text


def validate_handoff_packet(packet_path, *, workspaces=(), environ=None) -> tuple[bool, list[str], dict]:
    """Return validity, redacted errors and packet metadata."""
    environ = os.environ if environ is None else environ
    errors, info, _ = _validate_packet(packet_path, workspaces, environ)
    return not errors, _safe_data(errors, environ), _safe_data(info, environ)


def _worktrees(root):
    output = _git(root, "worktree", "list", "--porcelain", "-z").stdout
    worktrees = []
    for block in output.decode("utf-8", errors="backslashreplace").split("\0\0"):
        fields = dict(field.partition(" ")[::2] for field in block.split("\0") if field)
        if "worktree" in fields:
            worktrees.append({
                "path": fields["worktree"], "head_oid": fields.get("HEAD", ""),
                "branch": fields.get("branch", "").removeprefix("refs/heads/"),
            })
    return sorted(worktrees, key=lambda item: item["path"])


def _workspace_snapshot(workspace):
    root = _git_root(workspace)
    snapshot = dict(path=str(workspace), is_git_repo=root is not None, branch="", head_oid="",
                    head_subject="", status_porcelain="", diff_stat="", worktrees=[])
    if root is None:
        return snapshot
    head = _git(root, "log", "-1", "--format=%H%n%s", check=False)
    oid, _, subject = head.stdout.decode("utf-8", errors="backslashreplace").rstrip("\n").partition("\n")
    diff_base = ("HEAD",) if oid else ()
    diff_stat = _git_text(root, "diff", "--no-ext-diff", "--stat", *diff_base, "--")
    if not oid:
        staged = _git_text(root, "diff", "--cached", "--no-ext-diff", "--stat", "--")
        diff_stat = "\n".join(filter(None, (staged, diff_stat)))
    snapshot.update(
        branch=_git_text(root, "rev-parse", "--abbrev-ref", "HEAD") if oid else
        _git_text(root, "symbolic-ref", "--short", "HEAD"),
        head_oid=oid, head_subject=subject,
        status_porcelain=_git_text(root, "status", "--porcelain=v1", "--untracked-files=all"),
        diff_stat=diff_stat, worktrees=_worktrees(root),
    )
    return snapshot


def _artifact_fields(data):
    if not isinstance(data, dict):
        return {}
    result = {}
    groups = ((str, ("status", "thread_id")), (int, ("exit_code", "attempt_num")),
              (bool, ("merge_ok", "verification_ok")))
    for expected, keys in groups:
        result.update((key, data[key]) for key in keys if type(data.get(key)) is expected)
    tokens = data.get("tokens")
    if type(tokens) is int:
        result["tokens"] = tokens
    elif isinstance(tokens, dict):
        result["tokens"] = {key: value for key, value in tokens.items() if type(value) is int}
    files = data.get("changed_files", data.get("modified_files"))
    if isinstance(files, list) and all(isinstance(path, str) for path in files):
        result["changed_files"] = sorted(set(files))
    if "verification_ok" not in result and type(data.get("ok")) is bool:
        result["verification_ok"] = data["ok"]
    for key in ("tasks", "verification"):
        if isinstance(data.get(key), list):
            result[key] = [fields for row in data[key] if (fields := _artifact_fields(row))]
    return result


def _artifact_snapshot(directory):
    artifacts = []
    if not directory.is_dir():
        return {"path": str(directory), "artifacts": artifacts}
    children = sorted(path for path in directory.iterdir()
                      if path.is_dir() and not path.is_symlink() and path.name != "corpus")
    for parent in (directory, *children):
        for name in _ARTIFACTS:
            path = parent / name
            if not (path.is_dir() if name == "corpus" else path.is_file()):
                continue
            entry = {"path": str(path)}
            if path.suffix == ".json":
                try:
                    entry.update(_artifact_fields(json.loads(path.read_text(encoding="utf-8"))))
                except (OSError, ValueError):
                    entry["readable"] = False
            artifacts.append(entry)
    return {"path": str(directory), "artifacts": artifacts}


def _brain_dir(conversation_id, app_data_dir):
    if app_data_dir and _safe_id(conversation_id):
        return Path(app_data_dir).expanduser().resolve() / "brain" / conversation_id
    return None


def collect_runtime_snapshot(*, workspaces=(), artifact_dirs=(), conversation_id="", app_data_dir="") -> dict:
    """Collect path-sorted local evidence for the runtime snapshot."""
    workspace_paths = sorted({Path(path).expanduser().resolve() for path in workspaces})
    artifact_paths = sorted({Path(path).expanduser().resolve() for path in artifact_dirs})
    brain = _brain_dir(conversation_id, app_data_dir)
    snapshot = {
        "workspaces": [_workspace_snapshot(path) for path in workspace_paths],
        "artifact_dirs": [_artifact_snapshot(path) for path in artifact_paths],
        "conversation_artifacts": sorted(str(path) for path in brain.glob("*.md") if path.is_file())
        if brain is not None else [],
    }
    return _safe_data(snapshot, os.environ)


def normalize_handoff_title(raw_title: str, hop_index: int) -> str:
    title = " ".join(raw_title.split())
    suffix = r"(?:接力\s+#\d+|hop\s+\d+)"
    title = re.sub(rf"(?:\s*(?:[|·—-]\s*)?(?:\[{suffix}\]|\({suffix}\)))+$",
                   "", title, flags=re.IGNORECASE).strip()
    return f"{title or 'Codex Orchestrator Session'} [接力 #{hop_index}]"


def build_bootstrap_prompt(*, packet_path: Path, manifest_path: Path, prev_conversation_id: str,
                           prev_transcript_path: str, chain_id: str, hop_index: int,
                           max_hops: int, resume_mode: str) -> str:
    if resume_mode == "auto":
        action = (
            "auto：遵守第 1 节约束，立即执行第 2 节的 Immediate Next Atomic Action，无需重复确认。"
        )
    else:
        action = "pause：用不超过 5 行中文确认就绪状态并说明第 8 节待决事项，然后等待用户确认。"
    return (
        f"[Codex Orchestrator Session Handoff — Chain {chain_id} · Hop {hop_index}/{max_hops}]\n"
        f"上一会话：conversation://{prev_conversation_id}\n\n"
        f"1. 并行用 `read_file` 读取 `{packet_path}`、`{manifest_path}`，并读取第 6 节"
        "（Active Working Set）列在最前面的文件。禁止预先扫描整个仓库或通读上一会话 transcript。"
        f"只在需要时查询 `{prev_transcript_path or '上一会话 transcript（路径未记录）'}` 的相关片段。\n"
        "2. 在 3 秒内运行 `git rev-parse HEAD` 和 `git status --short`，与第 9 节及清单的快照核对。\n"
        f"3. {action}\n"
        + json.dumps({"prev_conversation_id": prev_conversation_id,
                      "prev_transcript_path": prev_transcript_path, "chain_id": chain_id,
                      "hop_index": hop_index, "max_hops": max_hops, "resume_mode": resume_mode},
                     ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


def _safe_id(value):
    return isinstance(value, str) and value not in (".", "..") and bool(_ID.fullmatch(value))


def _rpc(method, payload, environ):
    address = environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip().rstrip("/")
    token = environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if not address or not token:
        raise _ServiceUnavailable("Language Server address or credentials are missing")
    if "://" not in address:
        address = "http://" + address
    target = urlsplit(address)
    if (target.scheme not in ("http", "https") or not target.hostname or
            target.username is not None or target.query or target.fragment):
        raise ValueError("Invalid Language Server address")
    request = Request(
        f"{address}/{_SERVICE}/{method}", method="POST",
        data=_json(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                 "x-antigravity-csrf-token": token},
    )
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=10) as response:
            data = json.load(response)
    except HTTPError as exc:
        if exc.fp is not None:
            exc.close()
        if exc.code == 403:
            raise _ServiceUnavailable(f"{method} was forbidden by the Language Server") from None
        if method == "CreateConversation" and exc.code in (408, 504):
            raise _CreateTimeout(f"{method} timed out") from None
        raise
    except OSError as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        code = getattr(reason, "errno", None)
        if isinstance(reason, TimeoutError) or code == errno.ETIMEDOUT:
            failure = _CreateTimeout if method == "CreateConversation" else _ServiceUnavailable
            raise failure(f"{method} timed out") from None
        if isinstance(reason, ConnectionRefusedError) or code in (errno.ECONNREFUSED, errno.EPERM, errno.EACCES):
            raise _ServiceUnavailable(f"{method} could not reach the Language Server") from None
        raise
    if not isinstance(data, dict):
        raise ValueError(f"{method} returned an invalid response")
    return data


def _conversation(response):
    conversation = response.get("conversation", response)
    if not isinstance(conversation, dict) or not conversation:
        raise ValueError("Invalid ConversationService conversation")
    return conversation


def _object(value):
    return value if isinstance(value, dict) else {}


def _chain_position(cid, source, state):
    position = chain_position_from_tags(_object(source.get("metadata")).get("tags"))
    saved_chain = state.get("chain_id")
    saved_hop = state.get("hop_index")
    chain = position.get("chain_id", saved_chain if _safe_id(saved_chain) else cid[:8] or "manual")
    hop = position.get("hop_index", saved_hop if type(saved_hop) is int and saved_hop > 0 else 1)
    return chain, hop


def _successor_id(conversation, cid, environ, *, fallback=None):
    new_id = conversation.get("conversationId") or conversation.get("id") or fallback
    token = environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if _safe_id(new_id) and new_id != cid and (len(token) < 8 or token not in new_id):
        return new_id
    return None


def _reconcile_create(attempt_tag, cid, environ):
    if not isinstance(attempt_tag, str) or not re.fullmatch(r"attempt:[0-9a-f]{12}", attempt_tag):
        return None
    try:
        result = _rpc("ListConversations", {
            "tags": [attempt_tag], "pageSize": 5, "includeArchived": True,
        }, environ)
    except (HTTPException, OSError, ValueError, RecursionError, _ServiceUnavailable):
        return None
    conversations = result.get("conversations")
    if isinstance(conversations, list):
        for row in conversations:
            conversation = _object(_object(row).get("conversation", row))
            tags = _object(conversation.get("metadata")).get("tags", conversation.get("tags"))
            if tags is not None and (not isinstance(tags, list) or attempt_tag not in tags):
                continue
            new_id = _successor_id(conversation, cid, environ)
            if new_id:
                return new_id
    return None


def _commit_handoff(cid, state, new_id, *, epoch, chain, hop, is_root, created_at, environ):
    state.update(conversation_id=cid, handed_off=True, handed_off_epoch=epoch,
                 handed_off_to=new_id, chain_id=chain, hop_index=hop - 1,
                 post_handoff_turn_active=True,
                 handed_off_turn_hash=state.get("last_seen_user_hash", ""),
                 post_handoff_reminded=False, updated_at=created_at)
    state.pop("pending_handoff", None)
    if is_root is not None:
        state["is_root"] = is_root
    save_conversation_state(cid, _safe_data(state, environ), environ=environ)
    try:
        update_conversation_state(
            new_id, lambda successor: successor.update(
                conversation_id=new_id, is_root=True, chain_id=chain, hop_index=hop,
            ), environ=environ,
        )
    except OSError:
        pass


def _app_data_dir(environ):
    return environ.get("ANTIGRAVITY_APP_DATA_DIR") or "~/.gemini/antigravity"


def _transcript_path(explicit, cid, environ):
    path = explicit or environ.get("ANTIGRAVITY_TRANSCRIPT_PATH")
    if path:
        return str(Path(path).expanduser().resolve())
    brain = _brain_dir(cid, _app_data_dir(environ))
    if brain is None:
        return ""
    candidates = (brain / "transcript.jsonl",
                  brain / ".system_generated" / "logs" / "transcript.jsonl")
    return next((str(candidate) for candidate in candidates if candidate.is_file()), "")


def _settings(args, environ):
    from .profiles import load_profiles, resolve_context_handoff_config

    return resolve_context_handoff_config(load_profiles(args.profiles_file), environ=environ)


def _inspect(args, environ):
    from .context_telemetry import (evaluate_context_band, load_conversation_state,
                                    read_transcript_telemetry)

    cid = args.conversation_id or environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
    cfg = _settings(args, environ)
    telemetry = read_transcript_telemetry(_transcript_path(args.transcript, cid, environ) or None)
    assessment = evaluate_context_band(telemetry, cfg, model_hint=args.model)
    state = load_conversation_state(cid, environ=environ) if cid else {}
    state_keys = ("is_root", "checkpoint_epoch", "fired_bands", "fatigue_fired", "stop_nudged_epoch",
                  "handed_off", "handed_off_epoch", "handed_off_to", "post_handoff_reminded",
                  "chain_id", "hop_index", "updated_at")
    return {
        "exit_code": 0, "conversation_id": cid, "telemetry": vars(telemetry),
        "assessment": assessment.to_dict(), "config": {key: cfg[key] for key in ("mode", "max_hops")},
        "state": {key: state[key] for key in state_keys if key in state},
    }


def _output_dir(requested, chain, hop, *, cid="", environ=None):
    if requested:
        directory = Path(requested).expanduser().absolute()
        if directory.is_symlink():
            raise ValueError("Handoff output directory must not be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return directory.resolve()
    if cid:
        environ = os.environ if environ is None else environ
        brain = _brain_dir(cid, _app_data_dir(environ))
        if brain is None:
            raise _PreflightError("Invalid source conversation ID")
        return ensure_private_dir(brain / "handoff").resolve()
    parent = ensure_private_dir(ensure_private_dir(_HANDOFFS) / chain)
    return Path(tempfile.mkdtemp(prefix=f"hop-{hop:02d}-", dir=parent)).resolve()


def _create(args, environ, packet_text, cid):
    from .context_telemetry import (evaluate_context_band, load_conversation_state,
                                    read_transcript_telemetry, resolve_model_tier_fallback)

    cfg = _settings(args, environ)
    available = bool(environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip() and
                     environ.get("ANTIGRAVITY_CSRF_TOKEN"))
    max_hops = cfg["max_hops"] if args.max_hops is None else args.max_hops
    if max_hops < 1:
        raise _PreflightError("--max-hops must be positive")
    token = environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if cid and not _safe_id(cid):
        raise _PreflightError("Invalid source conversation ID")
    if not cid and available and not args.dry_run:
        raise _PreflightError("Source conversation ID is required")
    if len(token) >= 8 and any(token in value for value in (cid, args.out_dir or "")):
        raise _PreflightError("Conversation ID or output directory contains a secret")

    transcript = _transcript_path(args.transcript, cid, environ)
    telemetry = read_transcript_telemetry(transcript or None)
    state = load_conversation_state(cid, environ=environ) if cid else {}
    if (state.get("handed_off") is True and state.get("handed_off_epoch") == telemetry.checkpoint_epoch
            and not args.force):
        raise _PreflightError("A handoff already exists for this checkpoint epoch")
    pending = _object(state.get("pending_handoff"))
    if pending.get("epoch") == telemetry.checkpoint_epoch and not args.force:
        found = None if args.dry_run else _reconcile_create(pending.get("attempt_tag"), cid, environ)
        if found:
            _commit_handoff(
                cid, state, found, epoch=telemetry.checkpoint_epoch, chain=pending["chain_id"],
                hop=pending["hop_index"], is_root=state.get("is_root"),
                created_at=pending["created_at"], environ=environ,
            )
            raise _PreflightError(f"A prior handoff attempt already created successor {found}")
        raise _PreflightError(
            "A prior handoff attempt for this checkpoint epoch has an unknown outcome; "
            "verify existing conversations or pass --force"
        )

    source = {}
    unavailable = ""
    if not args.dry_run:
        try:
            source = _conversation(_rpc("GetConversation", {"conversationId": cid}, environ))
        except _ServiceUnavailable as exc:
            unavailable = str(exc)
    metadata = _object(source.get("metadata"))
    root_id = metadata.get("rootConversationId", source.get("rootConversationId"))
    parent_id = metadata.get("parentConversationId", source.get("parentConversationId"))
    is_root = (not parent_id and
               (not root_id or root_id == cid)) if source else state.get("is_root")
    if is_root is False and not args.allow_child:
        raise _PreflightError("Child conversations require --allow-child")
    chain, prev_hop = _chain_position(cid, source, state)
    hop = prev_hop + 1
    if hop > max_hops:
        raise _PreflightError(f"Next hop {hop} exceeds the maximum of {max_hops}")
    if not _safe_id(chain) or (len(token) >= 8 and token in chain):
        raise _PreflightError("Invalid or sensitive chain ID")

    out = _output_dir(args.out_dir, chain, hop, cid=cid, environ=environ)
    packet_path = out / f"handoff-hop-{hop:02d}.md"
    manifest_path = out / "handoff-manifest.json"
    receipt_path = out / "handoff-receipt.json"
    prompt_path = out / "handoff-bootstrap.txt"
    annotations = _object(source.get("annotations"))
    title = normalize_handoff_title(args.title or annotations.get("title") or
                                   _object(source.get("state")).get("summary") or "", hop)
    explicit = args.model.strip()
    model = (explicit if explicit.lower() != "auto" else "") or cfg["model_override"] or telemetry.active_model or "pro"
    snapshot = collect_runtime_snapshot(
        workspaces=args.workspace, artifact_dirs=args.artifact_dir, conversation_id=cid,
        app_data_dir=_app_data_dir(environ),
    )
    assessment = evaluate_context_band(telemetry, cfg, model_hint=model)
    manifest = {
        "chain_id": chain, "hop_index": hop, "max_hops": max_hops, "resume_mode": args.resume_mode,
        "source_conversation_id": cid, "source_transcript_path": transcript,
        "checkpoint_epoch": telemetry.checkpoint_epoch, "title": title, "model_requested": model,
        "packet_path": str(packet_path), "manifest_path": str(manifest_path),
        "runtime_snapshot": snapshot, "assessment": assessment.to_dict(),
    }
    _write(packet_path, _secret_safe(packet_text.rstrip(), environ) +
           "\n\n## 9. Deterministic Runtime Snapshot\n\n```json\n" + _json(snapshot) + "```\n")
    _write(manifest_path, _json(_safe_data(manifest, environ)))
    prompt = _secret_safe(build_bootstrap_prompt(
        packet_path=packet_path, manifest_path=manifest_path, prev_conversation_id=cid,
        prev_transcript_path=transcript, chain_id=chain, hop_index=hop, max_hops=max_hops,
        resume_mode=args.resume_mode,
    ), environ)
    receipt = dict(manifest, receipt_path=str(receipt_path), model_used=model, model_fallback_applied=False)
    if args.dry_run:
        _write(prompt_path, prompt + "\n")
        receipt.update(status="dry_run", exit_code=0, fallback_prompt_path=str(prompt_path))
        _write(receipt_path, _json(_safe_data(receipt, environ)))
        return receipt

    attempt_tag = f"attempt:{uuid.uuid4().hex[:12]}"
    payload = {
        "title": title, "model": model, "tags": ["codex-handoff", f"chain:{chain}", f"hop:{hop}",
                                                f"from:{cid[:8]}", attempt_tag],
        "prompt": [{"text": prompt}],
    }
    project_id = metadata.get("projectId", source.get("projectId")) or environ.get("ANTIGRAVITY_PROJECT_ID")
    environment_id = metadata.get("environmentId", source.get("environmentId"))
    if project_id:
        payload["projectId"] = project_id
    if environment_id:
        payload["environmentId"] = environment_id

    attempt_recorded = False
    try:
        if unavailable:
            raise _ServiceUnavailable(unavailable)
        state["pending_handoff"] = {
            "epoch": telemetry.checkpoint_epoch, "chain_id": chain, "hop_index": hop,
            "attempt_tag": attempt_tag, "created_at": datetime.now(timezone.utc).isoformat(),
        }
        save_conversation_state(cid, state, environ=environ)
        attempt_recorded = True
        try:
            response = _rpc("CreateConversation", _safe_data(payload, environ), environ)
        except HTTPError as exc:
            if exc.code not in (400, 422) or model in _TIERS:
                raise
            payload["model"] = resolve_model_tier_fallback(model, cfg)
            receipt.update(model_used=payload["model"], model_fallback_applied=True)
            response = _rpc("CreateConversation", _safe_data(payload, environ), environ)
        new_id = _successor_id(_conversation(response), cid, environ, fallback=response.get("conversationId"))
        if not new_id:
            raise ValueError("CreateConversation returned no valid successor ID")
    except _CreateTimeout:
        new_id = _reconcile_create(attempt_tag, cid, environ)
        if not new_id:
            receipt.update(
                status="create_outcome_unknown", exit_code=7, reconciliation_required=True,
                attempt_tag=attempt_tag,
                error="CreateConversation timed out. Check ListConversations for this attempt tag before retrying.",
            )
            _write(receipt_path, _json(_safe_data(receipt, environ)))
            return receipt
        receipt["reconciled_after_timeout"] = True
    except _ServiceUnavailable as exc:
        if attempt_recorded:
            state.pop("pending_handoff")
            save_conversation_state(cid, state, environ=environ)
        _write(prompt_path, prompt + "\n")
        receipt.update(status="ls_api_unreachable", exit_code=7, error=str(exc),
                       fallback_prompt_path=str(prompt_path))
        _write(receipt_path, _json(_safe_data(receipt, environ)))
        return receipt
    except (HTTPError, ValueError):
        if attempt_recorded:
            state.pop("pending_handoff")
            save_conversation_state(cid, state, environ=environ)
        raise

    receipt.update(status="ok", exit_code=0, new_conversation_id=new_id,
                   created_at=datetime.now(timezone.utc).isoformat())
    receipt_text = _json(_safe_data(receipt, environ))
    try:
        try:
            _write(receipt_path, receipt_text)
        finally:
            _commit_handoff(
                cid, state, new_id, epoch=telemetry.checkpoint_epoch, chain=chain,
                hop=hop, is_root=is_root, created_at=receipt["created_at"], environ=environ,
            )
        chain_dir = ensure_private_dir(ensure_private_dir(_HANDOFFS) / chain)
        _write(chain_dir / "latest.json", receipt_text)
    except OSError as exc:
        receipt.update(status="error", exit_code=1, error=f"Successor created but local persistence failed: {exc}")

    updates = []
    if annotations.get("conversationGroupId"):
        updates.append({"conversationId": new_id, "annotations": {
            "conversationGroupId": annotations["conversationGroupId"],
        }})
    tags = annotations.get("tags")
    tags = [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []
    if "handoff-completed" not in tags:
        tags.append("handoff-completed")
    old_update = {"conversationId": cid, "annotations": {"tags": tags}}
    if args.archive_old or cfg["archive_old"]:
        old_update["annotations"]["archived"] = True
    updates.append(old_update)
    for update in updates:
        try:
            _rpc("UpdateConversation", _safe_data(update, environ), environ)
        except (OSError, ValueError, _ServiceUnavailable):
            continue
    return receipt


def _run_create(args, environ, packet_text):
    cid = args.conversation_id or environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
    if args.dry_run or not cid:
        return _create(args, environ, packet_text, cid)

    if not _safe_id(cid):
        raise _PreflightError("Invalid source conversation ID")
    token = environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if len(token) >= 8 and token in cid:
        raise _PreflightError("Conversation ID contains a secret")
    with conversation_state_lock(cid, environ=environ):
        return _create(args, environ, packet_text, cid)


def _parser():
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Inspect context telemetry and handoff state")
    validate = commands.add_parser("validate", help="Validate a packet and its workspaces")
    create = commands.add_parser("create", help="Create a successor conversation")
    for command in (inspect, create):
        command.add_argument("--transcript")
        command.add_argument("--conversation-id")
        command.add_argument("--model", default="")
        command.add_argument("--profiles-file")
    for command in (validate, create):
        command.add_argument("--packet", required=True)
        command.add_argument("--workspace", action="append", default=[])
        command.add_argument("--artifact-dir", action="append", default=[])
    create.add_argument("--title")
    create.add_argument("--resume-mode", choices=("auto", "pause"), default="pause")
    create.add_argument("--archive-old", action="store_true")
    create.add_argument("--max-hops", type=int)
    create.add_argument("--force", action="store_true")
    create.add_argument("--allow-child", action="store_true")
    create.add_argument("--dry-run", action="store_true")
    create.add_argument("--out-dir")
    return parser


def main(argv=None) -> int:
    environ = os.environ
    try:
        args = _parser().parse_args(argv)
        if args.command == "inspect":
            result = _inspect(args, environ)
        else:
            errors, metadata, packet_text = _validate_packet(args.packet, args.workspace, environ)
            if errors or args.command == "validate":
                result = {"status": "invalid" if errors else "ok", "valid": not errors,
                          "errors": errors, "exit_code": 2 if errors else 0, **metadata}
            else:
                result = _run_create(args, environ, packet_text)
    except _PreflightError as exc:
        result = {"status": "preflight_failed", "exit_code": 2, "error": str(exc)}
    except Exception as exc:
        result = {"status": "error", "exit_code": 1, "error": f"{type(exc).__name__}: {exc}"}
    print(_json(_safe_data(result, environ)), end="")
    return result["exit_code"]
