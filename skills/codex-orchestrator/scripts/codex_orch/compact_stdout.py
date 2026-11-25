import json
import re
from collections import Counter


DEFAULT_THRESHOLD_BYTES = 4096
DEFAULT_MAX_PREVIEW_BYTES = 2048


def utf8_truncate(text: str, max_bytes: int) -> str:
    return text.encode("utf-8", errors="replace")[:max(0, max_bytes)].decode(
        "utf-8", errors="ignore"
    )


def _format_footer(size: int, last_message_path: str, summary_path: str, budget: int, label: str) -> str:
    prefix = f"[{label} preview: {size} bytes; full output: "
    separator = "; summary: "
    space_for_paths = budget - len(prefix) - len(separator) - 1
    if space_for_paths < 0:
        return utf8_truncate(prefix + separator + "]", budget)

    last_message_size = len(last_message_path.encode("utf-8", errors="replace"))
    summary_size = len(summary_path.encode("utf-8", errors="replace"))
    last_message_space = max(space_for_paths // 2, space_for_paths - summary_size)
    summary_space = space_for_paths - min(last_message_size, last_message_space)
    last_message = utf8_truncate(last_message_path, last_message_space)
    summary = utf8_truncate(summary_path, summary_space)
    return f"{prefix}{last_message}{separator}{summary}]"


def _severity_breakdown(items: list) -> str:
    priorities = ("P0", "P1", "P2", "P3")
    distribution = Counter(
        item["severity"] for item in items
        if isinstance(item, dict) and item.get("severity") in priorities
    )
    return ", ".join(f"{key}={distribution[key]}" for key in priorities if distribution[key])


def _collection_count(key: str, value, *, facts: bool = False) -> str:
    count = f"{key}={len(value)}"
    if not isinstance(value, list):
        return count
    if key in ("findings", "challenges"):
        breakdown = _severity_breakdown(value)
        if breakdown:
            return f"{count} ({breakdown})"
    elif facts and key == "verification":
        codes = [item["exit_code"] for item in value
                 if isinstance(item, dict) and type(item.get("exit_code")) is int]
        passed = codes.count(0)
        if codes:
            return f"{count} (passed={passed}, failed={len(codes) - passed})"
    elif facts and key == "consistency_checks":
        statuses = ("match", "mismatch", "unverified")
        distribution = Counter(
            item["status"] for item in value
            if isinstance(item, dict) and item.get("status") in statuses
        )
        if distribution:
            breakdown = ", ".join(f"{status}={distribution[status]}" for status in statuses)
            return f"{count} ({breakdown})"
    return count


def _format_facts(instance, budget: int, *, parsed_ok: bool = True) -> str:
    if not parsed_ok:
        return utf8_truncate("format: non-json", budget)
    if isinstance(instance, list):
        return utf8_truncate(f"format: json-array, items={len(instance)}", budget)
    if not isinstance(instance, dict):
        if instance is None:
            kind = "null"
        elif isinstance(instance, bool):
            kind = "boolean"
        elif isinstance(instance, (int, float)):
            kind = "number"
        else:
            kind = "string"
        return utf8_truncate(f"format: json-{kind}", budget)

    def is_identifier(value):
        # A custom or invalid receipt can put prose in a verdict or field name.
        return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", value)

    verdict = instance.get("verdict")
    lines = [f"verdict: {verdict}"] if is_identifier(verdict) else []
    counts = [_collection_count(key, value, facts=True) for key, value in instance.items()
              if is_identifier(key) and isinstance(value, (list, dict))]
    if counts:
        lines.append(", ".join(counts))
    if not lines:
        lines.append(f"keys={json.dumps([key for key in instance if is_identifier(key)])}")
    return utf8_truncate("\n".join(lines), budget)


def _format_object(instance: dict, budget: int) -> str:
    verdict = instance.get("verdict")
    summary = instance.get("summary")
    verdict_line = f"verdict: {verdict}" if isinstance(verdict, str) else ""
    counts = [
        _collection_count(key, value) for key, value in instance.items()
        if isinstance(value, (list, dict))
    ]
    if not verdict_line and not isinstance(summary, str):
        counts.append(f"keys={json.dumps(list(instance), ensure_ascii=False)}")

    details = utf8_truncate(", ".join(counts), budget)
    budget -= len(details.encode("utf-8")) + bool(details)
    verdict_line = utf8_truncate(verdict_line, budget)
    budget -= len(verdict_line.encode("utf-8")) + bool(verdict_line)
    summary_line = utf8_truncate(f"summary: {summary}", budget) if isinstance(summary, str) else ""
    return "\n".join(filter(None, (verdict_line, summary_line, details)))


def format_last_message_preview(
    raw_bytes: bytes, *, mode: str = "auto", threshold_bytes: int = 4096,
    max_preview_bytes: int = 2048, last_message_path: str = "", summary_path: str = "",
) -> tuple[str, bool]:
    text = raw_bytes.decode("utf-8", errors="replace")
    if mode == "full" or (mode == "auto" and len(raw_bytes) <= threshold_bytes):
        return text, False
    if mode == "facts" and not raw_bytes:
        return "", False

    label = "facts" if mode == "facts" else "compact"
    footer = _format_footer(len(raw_bytes), last_message_path, summary_path, max_preview_bytes, label)
    body_budget = max_preview_bytes - len(footer.encode("utf-8")) - 1
    if body_budget <= 0:
        return footer, True
    parsed_ok: bool = True
    try:
        instance = json.loads(text)
    except (ValueError, RecursionError):
        instance = None
        parsed_ok = False
    if mode == "facts":
        body = _format_facts(instance, body_budget, parsed_ok=parsed_ok)
    elif isinstance(instance, dict):
        body = _format_object(instance, body_budget)
    else:
        body = utf8_truncate(text, body_budget)
    return "\n".join(filter(None, (body, footer))), True
