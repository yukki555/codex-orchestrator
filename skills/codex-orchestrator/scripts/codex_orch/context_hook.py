"""Evaluate Antigravity lifecycle events for context handoff."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .context_telemetry import (
    ContextAssessment,
    chain_position_from_tags,
    evaluate_context_band,
    hash_user_input,
    read_transcript_telemetry,
    update_conversation_state,
)
from .profiles import default_profiles_path, load_profiles, resolve_context_handoff_config

SKIP_HANDOFF_RE = re.compile(
    r"不用接力|别接力|不要接力|不要开新会话|跳过接力|"
    r"skip\s+handoff|no\s+handoff|disable\s+handoff",
    re.IGNORECASE,
)
BANDS = ("soft", "hard", "emergency")
AUTO_HANDOFF = (
    "Prepare a handoff packet with the current task, progress, and next steps. "
    "Run the plugin's `handoff.sh create --packet <packet>` with the current workspace. "
    "Once handoff succeeds, output the handoff receipt and end this turn."
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _resolve_is_root(cid: str, lookup: dict, environ: Mapping[str, str]) -> bool:
    address = environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    token = environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if not address or not token:
        return True

    if "://" not in address:
        address = "http://" + address
    try:
        target = urlsplit(address)
        if (target.scheme not in ("http", "https") or not target.hostname
                or target.username is not None or "?" in address or "#" in address):
            return True
        request = Request(
            address.rstrip("/") + "/product.v1.ConversationService/GetConversation",
            data=json.dumps({"conversationId": cid}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                     "x-antigravity-csrf-token": token},
            method="POST",
        )
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=0.5) as response:
            result = json.load(response)
    except HTTPError as exc:
        exc.close()
        return True
    except (HTTPException, OSError, ValueError, RecursionError):
        return True

    if not isinstance(result, dict):
        return True
    conversation = result.get("conversationMetadata", result.get("conversation", result))
    if not isinstance(conversation, dict):
        return True
    metadata = conversation.get("metadata", conversation)
    if not isinstance(metadata, dict):
        return True
    position = chain_position_from_tags(metadata.get("tags"))
    if not position and not (
        metadata.keys() | conversation.keys()
    ) & {"conversationId", "id", "parentConversationId", "rootConversationId"}:
        return True

    parent_id = metadata.get("parentConversationId", conversation.get("parentConversationId"))
    root_id = metadata.get("rootConversationId", conversation.get("rootConversationId"))
    if any(value is not None and not isinstance(value, str) for value in (parent_id, root_id)):
        return True
    is_root = not parent_id and (not root_id or root_id == cid)
    lookup.update(position, is_root=is_root)
    return is_root


def _inject(message: str, *, hook_metadata: bool, **metadata: str) -> dict:
    system_message = {"systemMessage": message}
    # Antigravity v2.35.1's ScrubInternalFields panics on non-empty systemMessage.metadata.
    if hook_metadata:
        system_message["metadata"] = {"source": "codex-context-handoff", **metadata}
    return {"injectSteps": [{"systemMessage": system_message}]}


def _band_notice(assessment: ContextAssessment, band: str, mode: str, *, hook_metadata: bool) -> dict:
    if band == "checkpoint_fatigue":
        message = "Recommend a clean `/handoff` now. Repeated in-place compactions have occurred."
        tip = "已多次压缩，建议用 /handoff 接力"
    elif band == "soft":
        message = (
            "Context pressure is rising. Finish the current step and keep the next action clear. "
            "Mention `/handoff` for a fresh session or `/compact` to stay in this session."
        )
        if mode == "auto":
            message = (
                "Context pressure is rising. Finish the current step and note the state needed for "
                "an automatic handoff if pressure increases. The user can use `/handoff` now."
            )
        tip = "可用 /handoff 接力或 /compact 压缩"
    else:
        urgency = (
            "Context is near automatic compaction. Avoid starting additional work. "
            if band == "emergency" else "Context pressure is high. Pause at a safe point. "
        )
        if mode == "auto":
            message = urgency + AUTO_HANDOFF
            tip = "将自动接力，也可用 /handoff"
        else:
            message = urgency + (
                "Recommend `/handoff` to the user and wait for their choice before creating a new "
                "conversation. Offer `/compact` if they want to stay in this conversation."
            )
            tip = "建议用 /handoff 接力，也可用 /compact 压缩"

    status = (
        f"上下文 {assessment.active_tokens:,} / {assessment.context_limit:,} tokens"
        f"（{assessment.utilization_ratio:.1%}），{tip}。"
    )
    return _inject(
        f"[codex-context-handoff:{band}] {status} {message}",
        hook_metadata=hook_metadata, band=band, user_markdown=status,
    )


def _pre_invocation(
    state: dict,
    user_hash: str,
    assessment: ContextAssessment | None,
    mode: str,
    *,
    hook_metadata: bool,
    suppress_notice: bool = False,
) -> dict:
    if user_hash:
        state["last_seen_user_hash"] = user_hash
    if assessment is None:
        return {}

    epoch = assessment.checkpoint_epoch
    if state.get("post_handoff_turn_active") is True and state.get("handed_off_epoch") == epoch:
        if not user_hash or user_hash == state.get("handed_off_turn_hash", ""):
            if state.get("post_handoff_reminded"):
                return {}
            state["post_handoff_reminded"] = True
            return _inject(
                "[codex-context-handoff:post_handoff] "
                f"Handoff to {state['handed_off_to']} already succeeded. "
                "Do not invoke any more mutating tools. Output the final handoff receipt now.",
                hook_metadata=hook_metadata,
            )
        state.update(post_handoff_turn_active=False, post_handoff_reminded=False)

    if suppress_notice:
        return {}

    if state.get("checkpoint_epoch") != epoch:
        state.update(checkpoint_epoch=epoch, fired_bands=[], fatigue_fired=False)

    fired_bands = state.get("fired_bands", [])
    band = assessment.band
    if band in BANDS and band not in fired_bands:
        covered = BANDS[:BANDS.index(band) + 1]
        state["fired_bands"] = [name for name in BANDS if name in covered or name in fired_bands]
        return _band_notice(assessment, band, mode, hook_metadata=hook_metadata)
    if assessment.checkpoint_fatigue and not state.get("fatigue_fired"):
        state["fatigue_fired"] = True
        return _band_notice(assessment, "checkpoint_fatigue", mode, hook_metadata=hook_metadata)
    return {}


def evaluate_context_hook(
    payload: object,
    *,
    environ: Mapping[str, str] | None = None,
    profiles: dict | None = None,
) -> dict:
    try:
        if not isinstance(payload, dict):
            return {}
        event = payload.get("hookEventName")
        if event not in ("PreInvocation", "Stop"):
            return {}
        raw_user = payload.get("lastUserInput")
        last_user = raw_user if isinstance(raw_user, str) else ""
        skip = bool(last_user and SKIP_HANDOFF_RE.search(last_user))

        environ = os.environ if environ is None else environ
        if profiles is None:
            profiles = load_profiles(environ.get("CODEX_ORCH_PROFILES_FILE") or default_profiles_path())
        cfg = resolve_context_handoff_config(profiles, environ=environ)
        mode = cfg["mode"]
        if mode == "off":
            return {}
        if event == "Stop" and (
            skip or mode != "auto"
            or payload.get("terminationReason") != "NO_TOOL_CALL"
            or payload.get("fullyIdle") is not True
            or payload.get("continuationActive") is not False
        ):
            return {}

        cid = payload.get("conversationId")
        transcript_path = payload.get("transcriptPath")
        model_name = payload.get("modelName", payload.get("model", ""))
        if (
            not isinstance(cid, str) or not cid.strip()
            or not isinstance(transcript_path, str) or not transcript_path.strip()
            or not isinstance(model_name, str)
        ):
            return {}

        telemetry = read_transcript_telemetry(transcript_path)
        assessment = evaluate_context_band(telemetry, cfg, model_hint=model_name) if telemetry.exists else None
        user_hash = hash_user_input(last_user)
        suppress_notice = skip or last_user.lstrip().lower().startswith(("/compact", "/handoff"))
        needs_lookup = bool(environ.get("ANTIGRAVITY_LS_ADDRESS") and environ.get("ANTIGRAVITY_CSRF_TOKEN"))
        lookup = {}

        def update(state):
            if not isinstance(state.get("is_root"), bool):
                if needs_lookup:
                    return None
                state.update(lookup)
            if state.get("is_root") is False:
                return {}
            state.setdefault("conversation_id", cid)
            if event == "PreInvocation":
                return _pre_invocation(
                    state, user_hash, assessment, mode,
                    hook_metadata=cfg["hook_metadata"], suppress_notice=suppress_notice,
                )
            if (
                assessment is not None
                and assessment.band in ("hard", "emergency")
                and not state.get("handed_off")
                and int(state.get("hop_index") or 1) < int(cfg["max_hops"])
                and state.get("stop_nudged_epoch") != assessment.checkpoint_epoch
            ):
                state["stop_nudged_epoch"] = assessment.checkpoint_epoch
                return {"decision": "continue", "reason": "Context pressure is high. " + AUTO_HANDOFF}
            return {}

        _, result = update_conversation_state(cid, update, environ=environ)
        if result is None:
            # Re-read under the lock after HTTP to include any intervening handoff.
            _resolve_is_root(cid, lookup, environ)
            needs_lookup = False
            _, result = update_conversation_state(cid, update, environ=environ)
        return result
    except Exception:
        # A hook failure must leave the host lifecycle free to proceed.
        return {}


def run_context_hook_cli(environ: Mapping[str, str] | None = None) -> int:
    try:
        result = evaluate_context_hook(json.load(sys.stdin), environ=environ)
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        try:
            print("{}")
        except Exception:
            pass
    return 0
