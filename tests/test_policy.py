"""The tests that go in the pitch video. Pure domain — no DB, no network."""
from datetime import date
from services.app.domain.taxonomy import Cause
from services.app.domain.policy import Ctx, build_plan

CTX = Ctx(49_900, "card", date(2026, 8, 24),
          frozenset({"card", "upi_intent", "netbanking"}), True)

def test_expired_card_never_retries():
    assert build_plan(Cause.CARD_EXPIRED, CTX).retry_count == 0

def test_risk_block_escalates_only():
    p = build_plan(Cause.RISK_BLOCKED, CTX)
    assert p.retry_count == 0 and p.steps[0].action == "human_escalation"

def test_issuer_down_switches_rail_immediately():
    p = build_plan(Cause.TD_ISSUER_DOWN, CTX)
    assert p.steps[0].action == "rail_switch"
    assert p.steps[0].rail == "upi_intent" and p.steps[0].delay_s == 0

def test_insufficient_funds_waits_for_salary_window():
    p = build_plan(Cause.BD_INSUFFICIENT_FUNDS, CTX)
    assert p.steps[0].delay_s == 8 * 86_400   # Aug 24 -> Sep 1

def test_ambiguous_path_is_instrument_safe():
    p = build_plan(Cause.AMBIGUOUS, CTX)
    assert p.retry_count == 0
    assert all(s.action == "payment_link" for s in p.steps)

def test_policy_is_deterministic():
    assert build_plan(Cause.CARD_EXPIRED, CTX) == build_plan(Cause.CARD_EXPIRED, CTX)
