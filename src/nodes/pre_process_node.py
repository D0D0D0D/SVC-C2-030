"""AgentCore Platform v1.0 — SVC-C2-030 pre_process (InputParse / LanguageDetect / S-2)."""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants
#  - emit_trace_event() at least once inside execute()
#  - S-2: extend via _extra_security_gate_input() only (never override the @final gate).
#    The extra hook must NOT raise — reject by setting status=ERROR + error_log.

from __future__ import annotations

import re
from typing import ClassVar

from framework.nodes.function_node import FunctionNode, detect_pii, emit_trace_event
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.hospitality import DEFAULT_LANGUAGE, normalize_language

# Cap conversation history to the most recent N turns (checkpoint DB safety —
# do not accumulate guest PII indefinitely; docs/02_design.md F-03).
HISTORY_MAX_TURNS = 8
MAX_INPUT_CHARS = 4000

# Lightweight script-based language detection (JP/EN/ZH/KO). Deterministic and
# dependency-free; good enough to pick the answer language, with "ja" default.
_HIRAGANA_KATAKANA = re.compile(r"[぀-ヿ]")
_HANGUL = re.compile(r"[가-힣]")
_CJK = re.compile(r"[一-鿿]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> str:
    """Return one of ja/en/zh/ko by dominant script; defaults to ja."""
    if _HANGUL.search(text):
        return "ko"
    if _HIRAGANA_KATAKANA.search(text):
        return "ja"  # kana ⇒ Japanese even if kanji present
    if _CJK.search(text):
        return "zh"  # han only, no kana ⇒ Chinese
    if _LATIN.search(text):
        return "en"
    return DEFAULT_LANGUAGE


def _sanitize_pii(text: str) -> str:
    """Mask PII spans detected by the framework detector before storing in history."""
    if not text:
        return text
    spans = detect_pii(text)
    masked = text
    for span in sorted(spans, key=lambda s: s.get("start", 0), reverse=True):
        start, end = span.get("start"), span.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(masked):
            masked = masked[:start] + "[REDACTED]" + masked[end:]
        else:
            value = span.get("value") or span.get("text")
            if value:
                masked = masked.replace(str(value), "[REDACTED]")
    return masked


class PreProcessNode(FunctionNode):
    """Validate input, detect language, and append a PII-sanitized history turn."""

    # Guest-facing conversational operation — minimum for standard business use.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, history_max_turns: int = HISTORY_MAX_TURNS) -> None:
        super().__init__()
        # config-driven (config/agent.yaml: history_max_turns); module default fallback.
        self._history_max_turns = int(history_max_turns)

    def execute(self, state: dict) -> dict:
        user_input = state.get("user_input", "")

        if not user_input or not user_input.strip():
            emit_trace_event("concierge_input_rejected", {"reason": "empty_input"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        validated = user_input.strip()[:MAX_INPUT_CHARS]
        language = normalize_language(detect_language(validated))

        # Append a PII-sanitized turn and cap history length (never store raw PII).
        history = list(state.get("conversation_history", []))
        history.append({"role": "guest", "text": _sanitize_pii(validated)})
        history = history[-self._history_max_turns:]

        emit_trace_event(
            "concierge_input_parsed",
            {"language": language, "history_turns": len(history)},
            state,
        )

        return {
            "validated_input": validated,
            "detected_language": language,
            "conversation_history": history,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 domain check (runs after the framework @final PII scan).

        Contract: must NOT raise — reject by setting status=ERROR + error_log.
        Enforce a hard input size ceiling; the framework gate already masks PII.
        """
        raw = state.get("user_input", "") or ""
        if len(raw) > MAX_INPUT_CHARS * 4:
            state["status"] = AgentStatus.ERROR.value
            state.setdefault("error_log", [])
            state["error_log"] = state.get("error_log", []) + [
                f"PreProcessNode S-2: input exceeds hard size ceiling ({len(raw)} chars)"
            ]
        return state
