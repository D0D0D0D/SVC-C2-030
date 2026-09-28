"""AgentCore Platform v1.0 — SVC-C2-030 hospitality/localization + disclaimer text.

We do NOT ship a 4-language × N-intent template set (that would balloon the
maintained surface). Instead the おもてなし tone / honorific register / language
match is instructed to the LLM at generation time via a system prompt
(docs/02_design.md §保守アセットの範囲). This module holds only:

  - the localization system prompt builder, and
  - the single fixed medical-disclaimer + front-desk-escalation phrases,

which are the small, stable, maintained pieces.
"""

from __future__ import annotations

# Supported guest languages. "ja" is the safe default when detection is unsure.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"ja", "en", "zh", "ko"})
DEFAULT_LANGUAGE = "ja"

_LANG_NAME = {
    "ja": "Japanese (日本語)",
    "en": "English",
    "zh": "Chinese (中文)",
    "ko": "Korean (한국어)",
}

# Marker phrase that MUST survive in any health-adjacent onsen answer. The S-3
# preservation check (post_process) rejects a health answer that drops it.
DISCLAIMER_MARKER = "[医療免責]"

# The single maintained disclaimer text (per language). Fixed, no diagnosis,
# routes the decision to a doctor / the front desk.
MEDICAL_DISCLAIMER = {
    "ja": "[医療免責] これは一般的なご案内であり、診断や医学的助言ではありません。ご不安な場合は医師またはフロントにご相談ください。",
    "en": "[医療免責] This is general information only, not a diagnosis or medical advice. If you have any concerns, please consult a doctor or the front desk.",
    "zh": "[医療免責] 这只是一般性说明，并非诊断或医疗建议。如有疑虑，请咨询医生或前台。",
    "ko": "[医療免責] 이는 일반적인 안내이며 진단이나 의학적 조언이 아닙니다. 우려가 있으시면 의사나 프런트에 문의해 주세요.",
}

# Front-desk escalation wording (not-in-KB / complaint / booking / law).
# We ship this instead of maintaining 旅館業法 legal templates.
FRONT_DESK_ESCALATION = {
    "ja": "恐れ入りますが、この件はフロントにてご案内しております。フロントにお繋ぎいたします。",
    "en": "I'm sorry, but the front desk handles this matter. Let me connect you to the front desk.",
    "zh": "非常抱歉，此事项由前台处理。我为您转接前台。",
    "ko": "죄송하지만 이 사항은 프런트에서 안내해 드립니다. 프런트로 연결해 드리겠습니다.",
}


def normalize_language(lang: str | None) -> str:
    """Map a detected language tag to a supported one, defaulting to Japanese."""
    if lang and lang.lower() in SUPPORTED_LANGUAGES:
        return lang.lower()
    return DEFAULT_LANGUAGE


def disclaimer_for(lang: str) -> str:
    return MEDICAL_DISCLAIMER.get(normalize_language(lang), MEDICAL_DISCLAIMER[DEFAULT_LANGUAGE])


def escalation_for(lang: str) -> str:
    return FRONT_DESK_ESCALATION.get(normalize_language(lang), FRONT_DESK_ESCALATION[DEFAULT_LANGUAGE])


def localization_system_prompt(lang: str) -> str:
    """System prompt that instructs おもてなし tone + language match (no template set)."""
    lang_name = _LANG_NAME.get(normalize_language(lang), _LANG_NAME[DEFAULT_LANGUAGE])
    return (
        "You are a lodging concierge. Rewrite the given answer in "
        f"{lang_name}, using a warm, polite おもてなし (hospitality) register with "
        "appropriate honorifics. Preserve all facts and any disclaimer markers "
        "exactly; do not add new facts. Keep it concise and guest-friendly. "
        # Measured with a live LLM (2026-08-20): "appropriate honorifics" alone
        # makes the model open with a salutation ("Dear Guest, ..."), which the
        # S-3 non-disclosure gate correctly reads as a personal name and blocks.
        # The register must come from the wording, never from a form of address.
        # Ban the OPENING VOCATIVE only. Ordinary polite second-person wording
        # (you / 您 / お客様のご都合 …) is what the おもてなし register is made of
        # and must stay allowed — over-banning it would trade F-02 for S-3.
        "Do NOT open with a vocative or salutation line addressing the reader: "
        'no "Dear ...", no "Dear Guest", no 尊敬的宾客 / 客户先生 / 女士, no 고객님 '
        "/ 손님, no お客様 / 様 / さん as an opening address, and never a personal "
        "name. Begin directly with the information itself; the politeness must "
        "come from the sentence-level register, not from a form of address."
    )
