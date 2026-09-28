"""AgentCore Platform v1.0 — SVC-C2-030 minimal lodging intent ontology.

Maintained asset (deliberately MINIMAL, see docs/02_design.md §保守アセットの範囲):
we ship the escalate-only intent set + the onsen health signal. Fine-grained
intent classification is delegated to the LLM (few-shot in IntentResolveNode);
this table only carries the *decisions we must not leave to the LLM*:

  - which intents MUST escalate to the front desk (never answered in-chat), and
  - which intents carry an onsen health signal (routed via the S-5 disclaimer gate).

This is NOT an exhaustive lodging-intent dictionary — keeping it small is the
whole point (no 4-language × N-intent template maintenance).
"""

from __future__ import annotations

# Intents that must NOT be answered in-chat — always routed to the front desk.
# Complaints, booking changes, and law-adjacent requests (旅館業法: passport
# check, guest register) are front-desk concerns. We ship the escalate wording,
# NOT the legal templates (法改正追従・法的責任を負わないため).
ESCALATE_INTENTS: frozenset[str] = frozenset(
    {
        "complaint",           # 苦情
        "reservation_change",  # 予約変更
        "booking_cancel",      # キャンセル
        "payment",             # 決済
        "law_passport",        # 旅券確認（旅館業法） → フロント案内
        "law_guest_register",  # 宿泊者名簿（旅館業法） → フロント案内
        "emergency",           # 緊急・トラブル
    }
)

# Intents that carry an onsen health signal. When the KB has a hit AND the
# intent is one of these, the answer is routed through the MedicalDisclaimerGate
# (S-5): disclaimer added, no diagnosis, escalation offered.
HEALTH_INTENTS: frozenset[str] = frozenset(
    {
        "onsen_contraindication",  # 禁忌症
        "onsen_pregnancy",         # 妊娠中の入浴可否
        "onsen_skin",              # 皮膚疾患等
        "onsen_health_general",    # その他の健康関連
    }
)

# A small set of well-known answerable intents. Used only as few-shot anchors
# for the LLM classifier — NOT an exhaustive list. Any intent the LLM returns
# that is not recognised falls through to the KB-grounded path (and, if the KB
# has no hit, to escalation).
KNOWN_ANSWERABLE_INTENTS: frozenset[str] = frozenset(
    {
        "check_in_time",
        "check_out_time",
        "late_checkout_charge",
        "breakfast",
        "parking",
        "wifi",
        "luggage_hold",
        "bath_rotation",       # 大浴場入替
        "amenities",
        "nearby",
        "onsen_general",       # 泉質・入り方（健康signalなし）
    }
)


def is_escalate_intent(intent: str) -> bool:
    """True if the intent must be routed to the front desk (never answered in-chat)."""
    return intent in ESCALATE_INTENTS


def is_health_intent(intent: str) -> bool:
    """True if the intent carries an onsen health signal (S-5 disclaimer gate)."""
    return intent in HEALTH_INTENTS


def all_intent_labels() -> list[str]:
    """Flat list of anchor labels, used to build the LLM few-shot prompt."""
    return sorted(
        ESCALATE_INTENTS | HEALTH_INTENTS | KNOWN_ANSWERABLE_INTENTS
    )
