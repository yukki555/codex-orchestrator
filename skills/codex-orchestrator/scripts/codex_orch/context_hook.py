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
    TranscriptTelemetry,
    _payload_marks_child,
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
SKIP_NOISE_NUDGE_RE = re.compile(
    r"不用子代理|别用子代理|不要子代理|主循环直跑|关闭降噪提醒|"
    r"skip\s+noise\s+nudge|no\s+noise\s+nudge|disable\s+noise\s+nudge|no\s+subagent",
    re.IGNORECASE,
)
BANDS = ("soft", "hard", "emergency")
AUTO_HANDOFF = (
    "Prepare a handoff packet with the current task, progress, and next steps. "
    "Run the plugin's `handoff.sh create --packet <packet> --resume-mode auto` with the current workspace. "
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


def _band_notice(
    assessment: ContextAssessment, band: str, mode: str, *, can_handoff: bool, hook_metadata: bool
) -> dict:
    if band == "checkpoint_fatigue":
        if can_handoff:
            message = "Recommend a clean `/handoff` now. Repeated in-place compactions have occurred."
            tip = "已多次压缩，建议用 /handoff 接力"
        else:
            message = (
                "Repeated in-place compactions have occurred and this session has reached max_hops. "
                "Ask the user whether to continue with `/compact` or explicitly raise `--max-hops`."
            )
            tip = "已多次压缩且已达接力跳数上限，请询问用户是否继续用 /compact 或提高 --max-hops"
    elif band == "soft":
        tip = "可用 /handoff 接力或 /compact 压缩"
        if not can_handoff:
            message = (
                "Context pressure is rising. Finish the current step and keep the next action clear. "
                "This session has reached max_hops. Offer `/compact` to stay in this session."
            )
            tip = "已达接力跳数上限，可用 /compact 压缩"
        elif mode == "auto":
            message = (
                "Context pressure is rising. Finish the current step and note the state needed for "
                "an automatic handoff if pressure increases. The user can use `/handoff` now."
            )
        else:
            message = (
                "Context pressure is rising. Finish the current step and keep the next action clear. "
                "Mention `/handoff` for a fresh session or `/compact` to stay in this session."
            )
    else:
        urgency = (
            "Context is near automatic compaction. Avoid starting additional work. "
            if band == "emergency" else "Context pressure is high. Pause at a safe point. "
        )
        if not can_handoff:
            message = urgency + (
                "This session has reached max_hops. Recommend `/compact` or wait for the user "
                "to explicitly raise `--max-hops`."
            )
            tip = "已达接力跳数上限，建议用 /compact 压缩"
        elif mode == "auto":
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


def _effective_mode(cfg: Mapping[str, object], state: Mapping[str, object]) -> str:
    chain_mode = state.get("chain_handoff_mode")
    if cfg.get("mode_explicit") is not True and chain_mode in ("auto", "propose"):
        return chain_mode
    return cfg["mode"]


def _noise_notice(
    nudge: str, telemetry: TranscriptTelemetry, assessment: ContextAssessment, *, hook_metadata: bool, read_spike: bool
) -> dict:
    if nudge == "remote_noise":
        status = (
            f"当前上下文中已有 {telemetry.remote_cmd_count} 次远程调用，"
            f"最近连续 {telemetry.remote_turn_streak} 个工具回合含远程操作。"
            '剩余 SSH、同步和轮询可交给 `remote-runner`（优先 Model="flash"）。'
        )
    elif nudge == "read_noise":
        unbounded = (f"（其中 {telemetry.read_file_unbounded_count} 次未限定范围）"
                     if telemetry.read_file_unbounded_count else "")
        spike = f"单步激增 +{telemetry.last_turn_delta_tokens:,} tokens。" if read_spike else ""
        status = (
            f"当前上下文已直接读取 {telemetry.read_file_count} 次文件{unbounded}，"
            f"已使用 {assessment.active_tokens:,} tokens。{spike}"
            '请停止继续整文件 read_file；把剩余跨文件排查直接委派给内置 research 子代理'
            '（invoke_subagent(TypeName="research")），主会话仅保留 <= 200 行带 StartLine/EndLine 的局部读取。'
        )
    else:
        status = (
            f"当前上下文累计直接读取文件 {telemetry.read_file_count} 次，"
            f"距上次委派 research 后直接读取 {telemetry.reads_since_last_research} 次"
            "（尚未委派时从本轮 checkpoint 开始计数）。"
            f"已使用 {assessment.active_tokens:,} tokens。"
            '请立即委派内置 research 子代理（invoke_subagent(TypeName="research")）'
            '或用 rg/python3 提取所需片段，以保留 200K soft budget。'
        )
    return _inject(
        f"[codex-context-handoff:{nudge}] {status}",
        hook_metadata=hook_metadata, nudge=nudge, user_markdown=status,
    )


def _pre_invocation(
    state: dict,
    user_hash: str,
    telemetry: TranscriptTelemetry,
    assessment: ContextAssessment | None,
    effective_mode: str,
    *,
    can_handoff: bool,
    noise_cfg: dict,
    hook_metadata: bool,
    suppress_handoff: bool,
    suppress_noise: bool,
) -> dict:
    if user_hash:
        state["last_seen_user_hash"] = user_hash
    if assessment is None:
        return {}

    epoch = assessment.checkpoint_epoch
    if (effective_mode != "off" and state.get("post_handoff_turn_active") is True
            and state.get("handed_off_epoch") == epoch):
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

    handoff_enabled = effective_mode != "off" and not suppress_handoff
    if state.get("checkpoint_epoch") != epoch:
        state.pop("read_noise_l1_reads", None)
        if handoff_enabled:
            state.update(checkpoint_epoch=epoch, fired_bands=[], fatigue_fired=False, fired_nudges=[])
        elif "checkpoint_epoch" in state:
            state.update(checkpoint_epoch=epoch, fired_nudges=[])
            if "fired_bands" in state:
                state["fired_bands"] = []
            if "fatigue_fired" in state:
                state["fatigue_fired"] = False

    if handoff_enabled:
        fired_bands = state.get("fired_bands", [])
        band = assessment.band
        if band in BANDS and band not in fired_bands:
            covered = BANDS[:BANDS.index(band) + 1]
            state["fired_bands"] = [name for name in BANDS if name in covered or name in fired_bands]
            return _band_notice(assessment, band, effective_mode,
                                can_handoff=can_handoff, hook_metadata=hook_metadata)
        if assessment.checkpoint_fatigue and not state.get("fatigue_fired"):
            state["fatigue_fired"] = True
            return _band_notice(assessment, "checkpoint_fatigue", effective_mode,
                                can_handoff=can_handoff, hook_metadata=hook_metadata)

    if not noise_cfg["enabled"] or suppress_noise:
        return {}
    fired_nudges = state.get("fired_nudges", [])
    read_min_tokens = min(int(noise_cfg["read_file_min_tokens"]),
                          int(assessment.context_limit * float(noise_cfg["read_file_min_ratio"])))
    has_research = "research" in telemetry.invoked_subagents
    read_threshold = noise_cfg["read_file_threshold"]
    rearm_delta = noise_cfg["read_file_rearm_delta"]
    read_spike = (telemetry.last_turn_had_read_file
                  and telemetry.last_turn_delta_tokens >= noise_cfg["read_spike_tokens"])
    if (
        "remote_noise" not in fired_nudges and "remote-runner" not in telemetry.invoked_subagents
        and (telemetry.remote_cmd_count >= noise_cfg["remote_cmd_threshold"]
             or telemetry.remote_turn_streak >= noise_cfg["remote_streak_threshold"])
    ):
        nudge = "remote_noise"
    elif (
        "read_noise" not in fired_nudges and not has_research
        and ((telemetry.read_file_count >= read_threshold and assessment.active_tokens >= read_min_tokens)
             or read_spike)
    ):
        nudge = "read_noise"
        state["read_noise_l1_reads"] = telemetry.read_file_count
    elif "read_noise_l2" not in fired_nudges and (
        ("read_noise" in fired_nudges and not has_research
         and telemetry.read_file_count >= int(state.get("read_noise_l1_reads") or 0) + rearm_delta)
        or (has_research and telemetry.reads_since_last_research >= read_threshold + rearm_delta
            and assessment.active_tokens >= read_min_tokens)
    ):
        nudge = "read_noise_l2"
    else:
        return {}
    state.update(checkpoint_epoch=epoch, fired_nudges=[*fired_nudges, nudge])
    return _noise_notice(nudge, telemetry, assessment, hook_metadata=hook_metadata, read_spike=read_spike)



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
        skip_handoff = bool(last_user and SKIP_HANDOFF_RE.search(last_user))

        environ = os.environ if environ is None else environ
        if profiles is None:
            profiles = load_profiles(environ.get("CODEX_ORCH_PROFILES_FILE") or default_profiles_path())
        cfg = resolve_context_handoff_config(profiles, environ=environ)
        noise_cfg = cfg["noise_nudge"]
        noise_enabled = bool(noise_cfg.get("enabled", True))
        if event == "PreInvocation" and cfg["mode_explicit"] and cfg["mode"] == "off" and not noise_enabled:
            return {}
        if event == "Stop" and (
            skip_handoff or (cfg["mode_explicit"] and cfg["mode"] != "auto")
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
        offline_child = telemetry.is_subagent or _payload_marks_child(payload, cid)
        assessment = evaluate_context_band(telemetry, cfg, model_hint=model_name) if telemetry.exists else None
        user_hash = hash_user_input(last_user)
        is_slash_cmd = last_user.lstrip().lower().startswith(("/compact", "/handoff"))
        suppress_handoff = skip_handoff or is_slash_cmd
        suppress_noise = bool(last_user and SKIP_NOISE_NUDGE_RE.search(last_user)) or is_slash_cmd
        needs_lookup = bool(environ.get("ANTIGRAVITY_LS_ADDRESS") and environ.get("ANTIGRAVITY_CSRF_TOKEN"))
        lookup = {}

        def update(state):
            if not isinstance(state.get("is_root"), bool):
                if offline_child:
                    state["is_root"] = False
                elif needs_lookup:
                    return None
                else:
                    state.update(lookup)
            if state.get("is_root") is False:
                return {}
            state.setdefault("conversation_id", cid)
            effective_mode = _effective_mode(cfg, state)
            can_handoff = int(state.get("hop_index") or 1) < int(cfg["max_hops"])
            if event == "PreInvocation":
                return _pre_invocation(
                    state, user_hash, telemetry, assessment, effective_mode,
                    can_handoff=can_handoff, noise_cfg=noise_cfg,
                    hook_metadata=cfg["hook_metadata"], suppress_handoff=suppress_handoff,
                    suppress_noise=suppress_noise,
                )
            if (
                effective_mode == "auto"
                and assessment is not None
                and assessment.band in ("hard", "emergency")
                and not state.get("handed_off")
                and can_handoff
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
