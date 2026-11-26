from __future__ import annotations

import errno
import os
import re
from collections.abc import Mapping
from pathlib import Path


_PRESERVED_KEYS = frozenset("""
    PATH HOME USER LOGNAME SHELL TERM COLORTERM TMPDIR TEMP TMP LANG TZ PWD
    SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE
    HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy
    GIT_CEILING_DIRECTORIES GIT_TERMINAL_PROMPT
    OPENAI_API_KEY OPENAI_BASE_URL OPENAI_ORG_ID OPENAI_PROJECT OPENAI_API_BASE
""".split())
_PRESERVED_PREFIXES = ("LC_", "XDG_", "PYTHON", "CODEX_", "TEST_CODEX_")

_URL_CREDENTIALS = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
_SECRET = re.compile(
    r"""(?i:(?<![\w-])(?:["']?x-antigravity-csrf-token["']?[ \t]*:|"""
    r"""["']?ANTIGRAVITY_CSRF_TOKEN["']?[ \t]*[:=])[ \t]*"""
    r"""(?:"(?:\\[^\r\n]|[^"\\\r\n])*"|'[^'\r\n]*'|[^\s,;}]+))"""
    r"|(?i:Authorization|X-API-Key|x-codeium-csrf-token)\s*:\s*"
    r"(?:(?i:Bearer|Basic)\s+[^\s,;]+|\S+)"
    r"|(?i:Bearer)\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+"
    r"|sk-[A-Za-z0-9_-]{12,}|lin_api_[A-Za-z0-9_-]+"
    r"|AIza[0-9A-Za-z_-]{20,}|xox[baprs]-[0-9A-Za-z-]+|AKIA[0-9A-Z]{16}"
)


def plugin_data_dir(
    environ: Mapping[str, str] | None = None, *, fallback_subdir: str = "plugin-data",
) -> Path:
    env = os.environ if environ is None else environ
    if env.get("PLUGIN_DATA"):
        return Path(env["PLUGIN_DATA"]).expanduser()
    if env.get("ANTIGRAVITY_APP_DATA_DIR"):
        return Path(env["ANTIGRAVITY_APP_DATA_DIR"]).expanduser() / "plugin_data" / "codex-orchestrator"
    return Path("/tmp/codex-orchestrator") / fallback_subdir


def ensure_private_dir(path: str | Path) -> Path:
    directory = Path(path)
    if directory.is_symlink():
        raise PermissionError(f"Artifact directory must not be a symlink: {directory}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise PermissionError(f"Artifact directory must not be a symlink: {directory}")
    try:
        directory.chmod(0o700)
    except OSError as exc:
        if exc.errno not in (errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
    if os.name == "posix":
        st = directory.stat()
        if st.st_uid != os.geteuid() or st.st_mode & 0o077:
            raise PermissionError(f"Artifact directory must be owned by the current user and private: {directory}")
    return directory


def build_child_env(
    base_env: Mapping[str, str] | None = None, *, inherit_all: bool = False
) -> dict[str, str]:
    source = os.environ if base_env is None or (inherit_all and not base_env) else base_env
    return {
        key: value
        for key, value in source.items()
        if not key.startswith("ANTIGRAVITY_")
        and (inherit_all or key in _PRESERVED_KEYS or key.startswith(_PRESERVED_PREFIXES))
    }


def redact_secrets(text: str) -> str:
    return _SECRET.sub("[REDACTED]", _URL_CREDENTIALS.sub("://[REDACTED]@", text))
