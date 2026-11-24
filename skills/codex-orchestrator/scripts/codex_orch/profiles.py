"""Load declarative execution defaults and resolve CLI routing."""

from __future__ import annotations

import json
import os
from pathlib import Path


def default_profiles_path(script_dir: Path | None = None) -> Path:
    scripts = Path(__file__).resolve().parents[1] if script_dir is None else script_dir
    return scripts.parent / "config" / "profiles.json"


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


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
        if any(capability.get(key) not in speeds for key in ("low_medium_speed", "high_speed")):
            raise ValueError("invalid model routing speed")
        if not capability["supports_ultrafast"] or "downgrade_ultrafast_to" in capability:
            downgrade = capability.get("downgrade_ultrafast_to")
            if downgrade not in speeds or downgrade == "ultrafast":
                raise ValueError("invalid model ultrafast downgrade speed")


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


def default_max_cmds(profiles: dict, *, mode: str, review_engine: str = "") -> int:
    key = mode
    if mode == "review":
        key = "review_native" if review_engine == "native-review" else "review_exec"
    return profiles["defaults"]["max_cmds_by_mode"][key]


def bundled_schema_filename(profiles: dict, mode: str) -> str:
    schemas = profiles["defaults"]["schema_by_mode"]
    return schemas.get(mode, schemas["default"])
