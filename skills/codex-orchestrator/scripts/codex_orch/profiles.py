"""Load declarative execution defaults and resolve CLI routing."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path


DEFAULT_CONTEXT_HANDOFF: dict = {
    "default_mode": "propose",
    "noise_nudge": {
        "enabled": True,
        "remote_cmd_threshold": 4,
        "remote_streak_threshold": 3,
        "read_file_threshold": 6,
        "read_file_rearm_delta": 6,
        "read_file_min_tokens": 45000,
        "read_file_min_ratio": 0.12,
        "read_spike_tokens": 25000,
    },
    "soft_ratio": 0.45,
    "soft_cap_tokens": 200000,
    "hard_ratio": 0.65,
    "hard_cap_tokens": 300000,
    "emergency_ratio": 0.80,
    "checkpoint_fatigue_count": 2,
    "max_hops": 5,
    "default_context_limit": 160000,
    "context_limit_rules": [
        {
            "pattern": (
                "^gemini[-_ ]4[-_ ](?:neon|hydrogen|carbon|argon)"
                "|^gemini[-_ ]pro[-_ ]orchestrator|^gemini[-_ ]flash[-_ ]worker|^orchestrator"
            ),
            "limit": 512000,
        },
        {
            "pattern": "claude[-_ ](?:opus|sonnet)[-_ ]5[._-]5|^gemini[-_ ]3[._-]8[-_ ]flash",
            "limit": 256000,
        },
        {"pattern": "claude[-_ ](?:opus|sonnet)[-_ ]4[._-]6", "limit": 160000},
        {"pattern": "^gemini[-_ ]3[._-]1[-_ ]pro|^gemini[-_ ]pro", "limit": 128000},
        {"pattern": "gpt[-_ ]oss", "limit": 80000},
    ],
    "model_tier_fallbacks": [
        {"pattern": "neon|carbon|argon|opus|orchestrator|pro", "tier": "pro"},
        {"pattern": "lite", "tier": "flash_lite"},
        {"pattern": ".*", "tier": "flash"},
    ],
}
VERBOSITIES = ("low", "medium", "high", "inherit")
STDOUT_MODES = ("auto", "compact", "facts", "full")


_HANDOFF_MODES = ("propose", "auto", "off")
_MODEL_TIERS = ("pro", "flash", "flash_lite")


def default_profiles_path(script_dir: Path | None = None) -> Path:
    scripts = Path(__file__).resolve().parents[1] if script_dir is None else script_dir
    return scripts.parent / "config" / "profiles.json"


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _validate_context_handoff(config: object) -> None:
    if not isinstance(config, dict):
        raise ValueError("context_handoff must be an object")
    if config.get("default_mode") not in _HANDOFF_MODES:
        raise ValueError("invalid context_handoff.default_mode")
    soft, hard, emergency = (config.get(key) for key in ("soft_ratio", "hard_ratio", "emergency_ratio"))
    if (any(type(value) not in (float, int) for value in (soft, hard, emergency))
            or not 0 < soft < hard < emergency <= 1):
        raise ValueError("context_handoff ratios must increase from soft to hard to emergency within (0, 1]")
    for key in ("soft_cap_tokens", "hard_cap_tokens", "checkpoint_fatigue_count", "max_hops",
                "default_context_limit"):
        if not _positive_int(config.get(key)):
            raise ValueError(f"context_handoff.{key} must be a positive integer")
    if config["soft_cap_tokens"] > config["hard_cap_tokens"]:
        raise ValueError("context_handoff.soft_cap_tokens must not exceed hard_cap_tokens")

    if "noise_nudge" in config:
        noise = config["noise_nudge"]
        if not isinstance(noise, dict):
            raise ValueError("context_handoff.noise_nudge must be an object")
        noise = {**DEFAULT_CONTEXT_HANDOFF["noise_nudge"], **noise}
        if type(noise.get("enabled")) is not bool:
            raise ValueError("context_handoff.noise_nudge.enabled must be a boolean")
        for key in ("remote_cmd_threshold", "remote_streak_threshold", "read_file_threshold",
                    "read_file_rearm_delta", "read_file_min_tokens", "read_spike_tokens"):
            if not _positive_int(noise.get(key)):
                raise ValueError(f"context_handoff.noise_nudge.{key} must be a positive integer")

        ratio = noise["read_file_min_ratio"]
        if type(ratio) not in (float, int) or not 0 < ratio <= 1:
            raise ValueError("context_handoff.noise_nudge.read_file_min_ratio must be in (0, 1]")

    for key in ("context_limit_rules", "model_tier_fallbacks"):
        rules = config.get(key)
        if not isinstance(rules, list) or not rules:
            raise ValueError(f"context_handoff.{key} must be a non-empty list")
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str):
                raise ValueError(f"context_handoff.{key} must contain objects with regex patterns")
            try:
                re.compile(rule["pattern"], re.IGNORECASE)
            except (re.error, OverflowError, RecursionError) as exc:
                raise ValueError(f"invalid regex in context_handoff.{key}") from exc
            if key == "context_limit_rules" and not _positive_int(rule.get("limit")):
                raise ValueError("context_handoff context limit must be a positive integer")
            if key == "model_tier_fallbacks" and rule.get("tier") not in _MODEL_TIERS:
                raise ValueError("invalid context_handoff model tier")


def _validate_profiles(profiles: dict) -> None:
    if not isinstance(profiles, dict) or not _nonempty_string(profiles.get("version")):
        raise ValueError("expected a profile object with a version")
    for key in ("valid_modes", "valid_efforts", "valid_speeds"):
        values = profiles[key]
        if not isinstance(values, list) or not values or not all(map(_nonempty_string, values)):
            raise ValueError(f"{key} must be a non-empty list of strings")

    modes = set(profiles["valid_modes"])
    efforts = profiles["valid_efforts"]
    speeds = profiles["valid_speeds"]
    defaults = profiles["defaults"]
    fast = profiles["fast_preset"]
    if not _nonempty_string(fast["model"]) or fast["effort"] not in efforts:
        raise ValueError("invalid fast_preset model or effort")
    if defaults["effort"] not in efforts:
        raise ValueError("invalid default effort")

    for key, supported in (("valid_verbosities", VERBOSITIES), ("valid_stdout_modes", STDOUT_MODES)):
        if key in profiles:
            values = profiles[key]
            if (not isinstance(values, list) or not values
                    or any(value not in supported for value in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f"{key} must list distinct supported values")

    for key, allowed_keys, required_keys, values in (
        ("verbosity_by_mode", modes, modes, profiles.get("valid_verbosities", VERBOSITIES)),
        ("stdout_mode_by_mode", modes | {"default"}, {"default"},
         profiles.get("valid_stdout_modes", STDOUT_MODES)),
    ):
        if key in defaults:
            mapping = defaults[key]
            if (not isinstance(mapping, dict) or not required_keys.issubset(mapping)
                    or not mapping.keys() <= allowed_keys
                    or any(value not in values for value in mapping.values())):
                raise ValueError(f"invalid defaults.{key}")

    models_by_mode = defaults["model_by_mode"]
    budgets = defaults["max_cmds_by_mode"]
    schemas = defaults["schema_by_mode"]
    budget_modes = modes - {"review"}
    if "review" in modes:
        budget_modes.update(("review_exec", "review_native"))
    if not isinstance(models_by_mode, dict) or not modes.issubset(models_by_mode):
        raise ValueError("model_by_mode must cover every valid mode")
    if not all(map(_nonempty_string, models_by_mode.values())):
        raise ValueError("model_by_mode must contain non-empty model names")
    if not isinstance(budgets, dict) or not budget_modes.issubset(budgets):
        raise ValueError("max_cmds_by_mode must cover every valid mode and review engine")
    if any(type(value) is not int or value < 0 for value in budgets.values()):
        raise ValueError("max_cmds_by_mode must contain non-negative integers")
    if not isinstance(schemas, dict) or not _nonempty_string(schemas.get("default")):
        raise ValueError("schema_by_mode must include a default filename")
    if any(not _nonempty_string(value) or Path(value).name != value or value in (".", "..")
           for value in schemas.values()):
        raise ValueError("schema_by_mode must contain bundled filenames")

    models = profiles["models"]
    if not isinstance(models, dict) or not all(map(_nonempty_string, models)):
        raise ValueError("models must map model names to capabilities")
    for capability in (*models.values(), profiles["fallback_model_capabilities"]):
        if not isinstance(capability, dict) or type(capability.get("supports_ultrafast")) is not bool:
            raise ValueError("model capabilities must declare boolean supports_ultrafast")
        if "supports_verbosity" in capability and type(capability["supports_verbosity"]) is not bool:
            raise ValueError("model capabilities must declare boolean supports_verbosity")
        if any(capability.get(key) not in speeds for key in ("low_medium_speed", "high_speed")):
            raise ValueError("invalid model routing speed")
        if not capability["supports_ultrafast"] or "downgrade_ultrafast_to" in capability:
            downgrade = capability.get("downgrade_ultrafast_to")
            if downgrade not in speeds or downgrade == "ultrafast":
                raise ValueError("invalid model ultrafast downgrade speed")

    if "context_handoff" in profiles:
        _validate_context_handoff(profiles["context_handoff"])


def resolve_context_handoff_config(
    profiles: dict | None = None, *, environ: Mapping[str, str] | None = None
) -> dict:
    source = os.environ if environ is None else environ
    raw_context_handoff = (profiles or {}).get("context_handoff") or {}
    noise = raw_context_handoff.get("noise_nudge")
    config = deepcopy({
        **DEFAULT_CONTEXT_HANDOFF,
        **raw_context_handoff,
        "noise_nudge": {**DEFAULT_CONTEXT_HANDOFF["noise_nudge"], **(noise if isinstance(noise, dict) else {})},
    })

    mode = source.get("CODEX_HANDOFF_MODE", "").strip().lower()
    config["mode_explicit"] = mode in _HANDOFF_MODES
    config["mode"] = mode if config["mode_explicit"] else config["default_mode"]
    noise_override = source.get("CODEX_NOISE_NUDGE", "").strip().lower()
    if noise_override in ("1", "true", "on", "yes"):
        config["noise_nudge"]["enabled"] = True
    elif noise_override in ("0", "false", "off", "no"):
        config["noise_nudge"]["enabled"] = False
    config["archive_old"] = source.get("CODEX_HANDOFF_ARCHIVE_OLD", "").strip().lower() in ("1", "true", "yes")
    config["hook_metadata"] = source.get("CODEX_HANDOFF_HOOK_METADATA", "").strip().lower() in ("1", "true", "yes")
    config["model_override"] = source.get("CODEX_HANDOFF_MODEL", "").strip()
    config["soft_tokens_override"] = config["hard_tokens_override"] = None

    for suffix, key in (
        ("SOFT_TOKENS", "soft_tokens_override"),
        ("HARD_TOKENS", "hard_tokens_override"),
        ("MAX_HOPS", "max_hops"),
    ):
        try:
            value = int(source.get(f"CODEX_HANDOFF_{suffix}", ""))
        except ValueError:
            continue
        if value > 0:
            config[key] = value

    for suffix, key in (("SOFT_RATIO", "soft_ratio"), ("HARD_RATIO", "hard_ratio")):
        try:
            ratio = float(source.get(f"CODEX_HANDOFF_{suffix}", ""))
        except ValueError:
            continue
        if 0 < ratio < 1:
            config[key] = ratio
    return config


def load_profiles(path: str | Path | None = None, *, script_dir: Path | None = None) -> dict:
    override = os.environ.get("CODEX_ORCH_PROFILES_FILE")
    source = Path(path) if path is not None else (
        Path(override) if override else default_profiles_path(script_dir)
    )
    try:
        profiles = json.loads(source.read_text(encoding="utf-8"))
        _validate_profiles(profiles)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"invalid profiles file {source}: {exc}") from exc
    return profiles


def resolve_execution_profile(
    profiles: dict,
    *,
    mode: str,
    model: str | None = None,
    effort: str | None = None,
    speed: str | None = None,
    speed_explicit: bool = False,
    fast_mode: bool = False,
    profile_name: str | None = None,
    prior_model: str | None = None,
) -> tuple[str, str, str, list[str]]:
    if mode not in profiles["valid_modes"]:
        raise ValueError(f"invalid mode: {mode!r}")
    if effort and effort not in profiles["valid_efforts"]:
        raise ValueError(f"invalid effort: {effort!r}")
    if speed_explicit and speed not in profiles["valid_speeds"]:
        raise ValueError(f"invalid speed: {speed!r}")

    defaults = profiles["defaults"]
    if fast_mode:
        preset = profiles["fast_preset"]
        default_model, default_effort = preset["model"], preset["effort"]
    elif profile_name:
        default_model = default_effort = "inherit"
    else:
        default_model = defaults["model_by_mode"][mode]
        if mode == "resume" and prior_model:
            default_model = prior_model
        default_effort = defaults["effort"]

    model = model or default_model
    effort = effort or default_effort
    capabilities = profiles["models"].get(model, profiles["fallback_model_capabilities"])
    if not speed_explicit:
        if profile_name:
            speed = "inherit"
        elif fast_mode or effort in ("low", "medium"):
            speed = capabilities["low_medium_speed"]
        else:
            speed = capabilities["high_speed"]

    warnings = []
    if speed == "ultrafast" and not capabilities["supports_ultrafast"]:
        speed = capabilities["downgrade_ultrafast_to"]
        warnings.append(f"warning: {model} does not support ultrafast; downgrading speed to {speed}")
    return model, effort, speed, warnings


def resolve_verbosity(
    profiles: dict, *, mode: str, model: str, verbosity: str | None = None,
    profile_name: str | None = None,
) -> tuple[str, list[str]]:
    if verbosity is not None and verbosity not in profiles.get("valid_verbosities", VERBOSITIES):
        raise ValueError(f"invalid --verbosity: {verbosity!r}")
    if verbosity is None:
        defaults = profiles.get("defaults", {}).get("verbosity_by_mode", {})
        verbosity = "inherit" if profile_name else defaults.get(mode, "inherit")

    capabilities = profiles["models"].get(model, profiles["fallback_model_capabilities"])
    if verbosity != "inherit" and not capabilities.get("supports_verbosity", True):
        return "inherit", [f"warning: {model} does not support model verbosity; inheriting verbosity"]
    return verbosity, []


def default_stdout_mode(profiles: dict, *, mode: str) -> str:
    mapping = profiles.get("defaults", {}).get("stdout_mode_by_mode", {})
    return mapping.get(mode, mapping.get("default", "auto"))


def default_max_cmds(profiles: dict, *, mode: str, review_engine: str = "") -> int:
    key = mode
    if mode == "review":
        key = "review_native" if review_engine == "native-review" else "review_exec"
    return profiles["defaults"]["max_cmds_by_mode"][key]


def bundled_schema_filename(profiles: dict, mode: str) -> str:
    schemas = profiles["defaults"]["schema_by_mode"]
    return schemas.get(mode, schemas["default"])
