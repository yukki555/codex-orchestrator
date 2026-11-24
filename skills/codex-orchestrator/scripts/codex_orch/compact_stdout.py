import json
from collections import Counter


DEFAULT_THRESHOLD_BYTES = 4096
DEFAULT_MAX_PREVIEW_BYTES = 2048


def utf8_truncate(text: str, max_bytes: int) -> str:
    return text.encode("utf-8", errors="replace")[:max(0, max_bytes)].decode(
        "utf-8", errors="ignore"
    )


def _format_footer(size: int, last_message_path: str, summary_path: str, budget: int) -> str:
    prefix = f"[compact preview: {size} bytes; full output: "
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


def _format_object(instance: dict, budget: int) -> str:
    verdict = instance.get("verdict")
    summary = instance.get("summary")
    verdict_line = f"verdict: {verdict}" if isinstance(verdict, str) else ""
    counts = [
        f"{key}={len(value)}" for key, value in instance.items()
        if isinstance(value, (list, dict))
    ]
    challenges = instance.get("challenges")
    if isinstance(challenges, list):
        priorities = ("P0", "P1", "P2", "P3")
        distribution = Counter(
            item.get("severity") for item in challenges
            if isinstance(item, dict) and item.get("severity") in priorities
        )
        if distribution:
            index = counts.index(f"challenges={len(challenges)}")
            breakdown = ", ".join(f"{key}={distribution[key]}" for key in priorities if distribution[key])
            counts[index] += f" ({breakdown})"
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

    footer = _format_footer(len(raw_bytes), last_message_path, summary_path, max_preview_bytes)
    body_budget = max_preview_bytes - len(footer.encode("utf-8")) - 1
    if body_budget <= 0:
        return footer, True
    try:
        instance = json.loads(text)
    except (ValueError, RecursionError):
        instance = None
    if isinstance(instance, dict):
        body = _format_object(instance, body_budget)
    else:
        body = utf8_truncate(text, body_budget)
    return "\n".join(filter(None, (body, footer))), True
