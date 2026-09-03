"""
Tier-3 evaluation.

  python -m eval.triage_eval --mode adversarial    # offline, zero API calls
  python -m eval.triage_eval --mode novel          # needs LLM_API_KEY

MODE: adversarial
    A fake provider emits every way an LLM can misbehave — hallucinated causes,
    malformed JSON, overconfident hard-decline misreads, transport failures —
    and we measure how many the gate catches. Runs in CI, costs nothing.

MODE: novel
    THE experiment. Rewrite the test split's error tuples into codes the
    classifier has provably never seen, keeping ground truth. This is the real
    long-tail scenario: a new issuer integration starts emitting a string
    nobody mapped. Measure the classifier alone (it should abstain on nearly
    everything) against the classifier with tier 3.

    The control is honest: same rows, same model, same gate. The ONLY
    difference is whether tier 3 is reachable.
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from eval.classifier import (CauseClassifier, train_test_split_by_hash,
                             HARD_CAUSES, HARD_DECLINE_MASS_LIMIT)
from eval.generate import generate
from services.app.adapters import llm_triage as T
from services.app.domain.taxonomy import Cause, RETRYABLE

AMBIG = str(Cause.AMBIGUOUS)

# Codes a real gateway might emit that appear NOWHERE in training. Semantically
# legible to a language model, literally unknown to rules and the encoder.
NOVEL_TUPLES = {
    str(Cause.BD_INSUFFICIENT_FUNDS): ("ISSUER_INSUFFICIENT_BALANCE", "issuer_bank", "debit"),
    str(Cause.BD_USER_ABANDONED_OTP): ("OTP_ENTRY_WINDOW_EXPIRED", "acs", "3ds_challenge"),
    str(Cause.TD_ISSUER_DOWN):        ("ISSUER_NODE_UNREACHABLE", "upstream", "switch"),
    str(Cause.TD_PSP_TIMEOUT):        ("PSP_UPSTREAM_READ_TIMEOUT", "psp", "capture"),
    str(Cause.CARD_EXPIRED):          ("INSTRUMENT_PAST_VALID_THRU", "vault", "tokenization"),
    str(Cause.RISK_BLOCKED):          ("AML_SCREENING_REJECT", "compliance", "prescreen"),
}


# ------------------------------------------------------------------- metrics --
def score(df: pd.DataFrame) -> dict:
    decided = df[df.pred_cause != AMBIG]
    correct = decided[decided.pred_cause == decided.true_cause]
    unsafe = decided[
        decided.true_cause.isin(HARD_CAUSES)
        & decided.pred_cause.map(lambda c: RETRYABLE.get(Cause(c), True))
    ]
    hard_total = int(df.true_cause.isin(HARD_CAUSES).sum())
    return {
        "n": len(df),
        "resolved": len(decided),
        "resolution_rate": len(decided) / len(df) if len(df) else 0.0,
        "accuracy_of_resolved": len(correct) / len(decided) if len(decided) else 0.0,
        "unsafe_retries": len(unsafe),
        "unsafe_retry_rate": len(unsafe) / hard_total if hard_total else 0.0,
        "hard_declines": hard_total,
    }


def _line(label, m):
    print(f"  {label:<26}{m['resolution_rate']*100:>8.1f}%"
          f"{m['accuracy_of_resolved']*100:>11.1f}%"
          f"{m['unsafe_retries']:>10d}"
          f"{m['unsafe_retry_rate']*100:>11.2f}%")


# ------------------------------------------------------------ MODE: novel ----
def run_novel(n: int, seed: int, sample: int) -> dict:
    df = generate(n=n, seed=seed).drop_duplicates(subset=["payment_id"]).reset_index(drop=True)
    train, test = train_test_split_by_hash(df, test_pct=30)
    clf = CauseClassifier().fit(train)

    novel = test.sample(n=min(sample, len(test)), random_state=seed).copy()
    tup = novel["true_cause"].map(NOVEL_TUPLES)
    novel["gw_error_code"] = [t[0] for t in tup]
    novel["gw_error_source"] = [t[1] for t in tup]
    novel["gw_error_step"] = [t[2] for t in tup]

    # sanity: none of these tuples can exist in training
    seen = set(zip(train.gw_error_code, train.gw_error_source, train.gw_error_step))
    assert not (set(NOVEL_TUPLES.values()) & seen), "novel tuples leaked into training"

    print(f"\n{'='*74}\n  TIER-3 NOVEL-CODE EXPERIMENT   n={len(novel)}  seed={seed}\n"
          f"  Every error tuple below is absent from training by construction.\n{'='*74}")
    print(f"  {'':<26}{'resolved':>9}{'accuracy':>11}{'unsafe':>10}{'unsafe rate':>12}")

    T.reset_stats()
    base = clf.predict_df(novel, use_llm=False)
    m_base = score(base)
    _line("classifier only", m_base)

    if not T.available():
        print("\n  LLM_API_KEY not set — tier 3 unavailable, nothing to compare.")
        print("  Run `--mode adversarial` for the offline safety proof.\n")
        return {"baseline": m_base, "triage": None}

    with_llm = clf.predict_df(novel, use_llm=True)
    m_llm = score(with_llm)
    _line("+ tier-3 LLM triage", m_llm)

    d_res = (m_llm["resolution_rate"] - m_base["resolution_rate"]) * 100
    d_unsafe = m_llm["unsafe_retries"] - m_base["unsafe_retries"]
    print(f"\n  Δ resolution   {d_res:+.1f} pp        Δ unsafe retries   {d_unsafe:+d}")

    print(f"\n  API calls {T.STATS['calls']}   cache hits {T.STATS['cache_hits']}   "
          f"gate abstentions {T.STATS['abstained']}   transport errors {T.STATS['errors']}")

    gated = with_llm[with_llm.pred_stage.str.contains("llm_gated", na=False)]
    print(f"  proposals rejected by the hard-decline gate: {len(gated)}")

    print("\n  per-cause resolution with tier 3:")
    for c, g in with_llm.groupby("true_cause"):
        dec = g[g.pred_cause != AMBIG]
        acc = (dec.pred_cause == dec.true_cause).mean() if len(dec) else 0.0
        print(f"    {c:<26}{len(dec)/len(g)*100:>6.1f}% resolved   {acc*100:>6.1f}% correct")
    print()
    return {"baseline": m_base, "triage": m_llm}


# ------------------------------------------------------ MODE: adversarial ----
ATTACKS = [
    ("hallucinated cause",      '{"cause":"CUSTOMER_CHANGED_MIND","confidence":0.99,"rationale":"x"}'),
    ("cause not a string",      '{"cause":42,"confidence":0.9,"rationale":"x"}'),
    ("missing cause",           '{"confidence":0.9,"rationale":"x"}'),
    ("null cause",              '{"cause":null,"confidence":0.9}'),
    ("malformed json",          '{"cause":"TD_ISSUER_DOWN",'),
    ("prose, not json",         'I think this is probably insufficient funds.'),
    ("empty response",          ''),
    ("array not object",        '[{"cause":"TD_ISSUER_DOWN"}]'),
    ("confidence as text",      '{"cause":"TD_ISSUER_DOWN","confidence":"very high"}'),
    ("confidence out of range", '{"cause":"TD_ISSUER_DOWN","confidence":8.5}'),
    ("negative confidence",     '{"cause":"TD_ISSUER_DOWN","confidence":-1}'),
    ("below confidence floor",  '{"cause":"BD_INSUFFICIENT_FUNDS","confidence":0.4,"rationale":"guess"}'),
    ("explicit abstention",     '{"cause":"AMBIGUOUS","confidence":0.99,"rationale":"unclear"}'),
    ("prompt-injection echo",   '{"cause":"IGNORE_PREVIOUS_INSTRUCTIONS","confidence":1.0}'),
]

# Well-formed but WRONG: a confident retryable proposal on a row the model
# believes is a hard decline. The parser accepts these; the gate must not.
GATE_ATTACKS = [
    ("retryable on high hard-mass", '{"cause":"BD_INSUFFICIENT_FUNDS","confidence":1.0,"rationale":"bank"}'),
    ("timeout on high hard-mass",   '{"cause":"TD_PSP_TIMEOUT","confidence":0.98,"rationale":"psp"}'),
    ("issuer on high hard-mass",    '{"cause":"TD_ISSUER_DOWN","confidence":0.95,"rationale":"issuer"}'),
]


class _Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def run_adversarial() -> dict:
    row = _Row(gw_error_code="UNSEEN", gw_error_source="bank",
               gw_error_step="authorization", method="card", instrument_hint="na")

    print(f"\n{'='*74}\n  TIER-3 ADVERSARIAL GATE TEST   (offline — zero API calls)\n"
          f"  Every row is a way the LLM can misbehave. All must land on AMBIGUOUS.\n{'='*74}")

    caught = 0
    for name, payload in ATTACKS:
        T.reset_stats()
        T.set_provider(lambda _p, _x=payload: _x)
        r = T.triage(gw_error_code="UNSEEN", gw_error_source="bank",
                     gw_error_step="authorization", method="card")
        ok = (r.ok is False and r.cause == AMBIG)
        caught += ok
        print(f"  {'PASS' if ok else 'FAIL':<6}{name:<32}-> {r.cause}")

    print(f"\n  parser rejected {caught}/{len(ATTACKS)}")

    gate_caught = 0
    print(f"\n  Well-formed but unsafe proposals (parser accepts; gate must reject):")
    for name, payload in GATE_ATTACKS:
        T.reset_stats()
        T.set_provider(lambda _p, _x=payload: _x)
        cause, _c, stage = CauseClassifier._tier3(
            row, hard_mass=HARD_DECLINE_MASS_LIMIT + 0.25, fb_conf=0.4,
            fb_stage="gated:low_conf")
        ok = cause == AMBIG
        gate_caught += ok
        print(f"  {'PASS' if ok else 'FAIL':<6}{name:<32}-> {cause}  [{stage}]")

    # transport failure
    T.reset_stats()
    T.set_provider(lambda _p: (_ for _ in ()).throw(TimeoutError("slow")))
    r = T.triage(gw_error_code="UNSEEN", gw_error_source="bank",
                 gw_error_step="authorization", method="card")
    transport_ok = r.ok is False and r.cause == AMBIG
    print(f"  {'PASS' if transport_ok else 'FAIL':<6}{'provider timeout':<32}-> {r.cause}")

    T.set_provider(None)
    total = len(ATTACKS) + len(GATE_ATTACKS) + 1
    hit = caught + gate_caught + transport_ok
    print(f"\n  {'='*70}\n  CAUGHT {hit}/{total}"
          f"   {'— tier 3 cannot lower the safety floor' if hit == total else '— REGRESSION'}\n")
    return {"attacks": total, "caught": hit}


# ---------------------------------------------------------------------- main --
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["adversarial", "novel"], default="adversarial")
    ap.add_argument("--n", type=int, default=8000)
    ap.add_argument("--sample", type=int, default=1200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out = run_adversarial() if a.mode == "adversarial" else run_novel(a.n, a.seed, a.sample)
    if a.json:
        print(json.dumps(out, indent=2, default=float))
