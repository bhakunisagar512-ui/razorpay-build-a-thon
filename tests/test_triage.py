"""
Tier-3 safety tests. ZERO API calls — a fake provider is injected.

The point: the LLM is untrusted input. These prove that every way it can
misbehave lands on AMBIGUOUS rather than on an unsafe retry.
"""
import pytest

from services.app.adapters import llm_triage as T
from services.app.domain.taxonomy import Cause

AMBIG = str(Cause.AMBIGUOUS)


@pytest.fixture(autouse=True)
def _clean():
    T.reset_stats()
    yield
    T.set_provider(None)
    T.reset_stats()


def fake(text):
    """Install a provider that always returns `text`."""
    T.set_provider(lambda _prompt: text)


# ------------------------------------------------------- malformed responses --
@pytest.mark.parametrize("payload", [
    "not json at all",
    "",
    "[]",                                        # array, not object
    '{"cause": "BD_INSUFFICIENT_FUNDS"',         # truncated
    '{"confidence": 0.9}',                       # no cause
    '{"cause": null, "confidence": 0.9}',
    '{"cause": 123, "confidence": 0.9}',
])
def test_malformed_response_abstains(payload):
    fake(payload)
    r = T.triage(gw_error_code="X", gw_error_source="y", gw_error_step="z", method="card")
    assert r.ok is False and r.cause == AMBIG


def test_hallucinated_cause_rejected():
    """A cause outside the taxonomy must never escape the parser."""
    fake('{"cause": "CUSTOMER_CHANGED_MIND", "confidence": 0.99, "rationale": "x"}')
    r = T.triage(gw_error_code="X", gw_error_source="y", gw_error_step="z", method="card")
    assert r.ok is False and r.cause == AMBIG


@pytest.mark.parametrize("conf", ["high", None, -0.5, 1.7])
def test_bad_confidence_rejected(conf):
    fake('{"cause": "TD_ISSUER_DOWN", "confidence": %s, "rationale": "x"}'
         % ("null" if conf is None else f'"{conf}"' if isinstance(conf, str) else conf))
    r = T.triage(gw_error_code="X", gw_error_source="y", gw_error_step="z", method="card")
    assert r.ok is False and r.cause == AMBIG


def test_low_confidence_below_floor_abstains():
    fake('{"cause": "BD_INSUFFICIENT_FUNDS", "confidence": 0.55, "rationale": "guess"}')
    r = T.triage(gw_error_code="X", gw_error_source="y", gw_error_step="z", method="card")
    assert r.ok is False and r.cause == AMBIG


def test_explicit_ambiguous_is_not_ok():
    fake('{"cause": "AMBIGUOUS", "confidence": 0.95, "rationale": "cannot tell"}')
    r = T.triage(gw_error_code="X", gw_error_source="y", gw_error_step="z", method="card")
    assert r.ok is False


def test_provider_exception_abstains():
    T.set_provider(lambda _p: (_ for _ in ()).throw(TimeoutError("slow")))
    r = T.triage(gw_error_code="X", gw_error_source="y", gw_error_step="z", method="card")
    assert r.ok is False and r.cause == AMBIG
    assert T.STATS["errors"] == 1


def test_markdown_fenced_json_is_accepted():
    fake('```json\n{"cause": "TD_ISSUER_DOWN", "confidence": 0.9, "rationale": "issuer"}\n```')
    r = T.triage(gw_error_code="X", gw_error_source="y", gw_error_step="z", method="card")
    assert r.ok is True and r.cause == str(Cause.TD_ISSUER_DOWN)


def test_disabled_without_provider():
    T.set_provider(None)
    T.API_KEY = ""
    assert T.available() is False
    r = T.triage(gw_error_code="X", gw_error_source="y", gw_error_step="z", method="card")
    assert r.ok is False and r.cause == AMBIG


def test_identical_tuples_hit_the_cache():
    fake('{"cause": "TD_ISSUER_DOWN", "confidence": 0.9, "rationale": "x"}')
    for _ in range(5):
        T.triage(gw_error_code="A", gw_error_source="b", gw_error_step="c", method="upi")
    assert T.STATS["calls"] == 1
    assert T.STATS["cache_hits"] == 4


# ------------------------------------------ the gate, at the classifier level --
class _Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _row():
    return _Row(gw_error_code="UNSEEN_CODE", gw_error_source="bank",
                gw_error_step="authorization", method="card", instrument_hint="na")


def test_retryable_proposal_rejected_when_hard_mass_is_high():
    """THE safety property: the LLM proposes, the hard-decline gate disposes."""
    from eval.classifier import CauseClassifier, HARD_DECLINE_MASS_LIMIT
    fake('{"cause": "BD_INSUFFICIENT_FUNDS", "confidence": 0.99, "rationale": "bank"}')
    cause, _conf, stage = CauseClassifier._tier3(
        _row(), hard_mass=HARD_DECLINE_MASS_LIMIT + 0.2, fb_conf=0.4,
        fb_stage="gated:low_conf")
    assert cause == AMBIG
    assert stage.endswith("llm_gated")


def test_hard_decline_proposal_survives_high_hard_mass():
    """Proposing a hard decline is the conservative direction — allow it."""
    from eval.classifier import CauseClassifier, HARD_DECLINE_MASS_LIMIT
    fake('{"cause": "CARD_EXPIRED", "confidence": 0.93, "rationale": "expired"}')
    cause, _conf, stage = CauseClassifier._tier3(
        _row(), hard_mass=HARD_DECLINE_MASS_LIMIT + 0.2, fb_conf=0.4,
        fb_stage="gated:low_conf")
    assert cause == str(Cause.CARD_EXPIRED)
    assert stage == "llm_triage"


def test_tier3_never_lowers_the_floor():
    """Whatever the LLM does, the result is never worse than plain abstention."""
    from eval.classifier import CauseClassifier
    for payload in ["garbage", '{"cause":"NOPE","confidence":1}',
                    '{"cause":"AMBIGUOUS","confidence":0.9}']:
        fake(payload)
        cause, _c, _s = CauseClassifier._tier3(_row(), 0.0, 0.4, "gated:low_conf")
        assert cause == AMBIG


def test_unsafe_proposal_can_never_produce_a_retry_plan():
    """End to end: a hallucinated retryable cause on a high-hard-mass row must
    not yield a plan containing a retry."""
    from datetime import date
    from eval.classifier import CauseClassifier, HARD_DECLINE_MASS_LIMIT
    from services.app.domain.policy import Ctx, build_plan
    fake('{"cause": "BD_INSUFFICIENT_FUNDS", "confidence": 1.0, "rationale": "x"}')
    cause, _c, _s = CauseClassifier._tier3(
        _row(), HARD_DECLINE_MASS_LIMIT + 0.3, 0.3, "gated:low_conf")
    plan = build_plan(Cause(cause), Ctx(49900, "card", date(2026, 9, 3),
                                        frozenset({"upi_intent", "card"}), True))
    assert plan.retry_count == 0
