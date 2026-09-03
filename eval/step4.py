"""
Step 4 evaluation: what does using a REAL classifier (instead of oracle labels)
cost us, and is the safety gate holding?

Three numbers come out:
  1. classifier quality on held-out test (macro-F1, per-class, abstain rate)
  2. unsafe retry rate — retries fired at true hard declines (must be ~0)
  3. end-to-end uplift: oracle vs predicted, same seed, same holdout
"""
from __future__ import annotations
import numpy as np
import pandas as pd

from services.app.domain.taxonomy import Cause
from .generate import generate, assign_arm
from services.app.domain.policy import (Ctx, build_plan, baseline_plan,
                                        strong_baseline_plan)
from .simulate import simulate_plan
from .classifier import CauseClassifier, train_test_split_by_hash, evaluate


def run_arm(df: pd.DataFrame, cause_col: str, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rows = []
    for r in df.itertuples(index=False):
        plan_cause = Cause(getattr(r, cause_col))
        true_cause = Cause(r.true_cause)          # world reacts to the TRUTH
        ctx = Ctx(int(r.amount_minor), r.method, pd.Timestamp(r.failed_at).date(),
                  frozenset(r.available_rails.split(",")), bool(r.consent_whatsapp))
        plan = build_plan(plan_cause, ctx) if r.arm == "treatment" \
            else baseline_plan(true_cause, ctx)
        res = simulate_plan(plan, true_cause, int(r.amount_minor), rng)
        rows.append({"arm": r.arm, "net_minor": res["recovered_minor"] - res["cost_minor"],
                     "recovered": res["recovered"]})
    return pd.DataFrame(rows)


def uplift(res: pd.DataFrame, seed: int) -> dict:
    t = res[res.arm == "treatment"]["net_minor"].to_numpy() / 100
    h = res[res.arm == "holdout"]["net_minor"].to_numpy() / 100
    rng = np.random.default_rng(seed)
    boot = np.array([rng.choice(t, t.size, True).mean() - rng.choice(h, h.size, True).mean()
                     for _ in range(5000)])
    lo, hi = np.percentile(boot, [2.5, 97.5])
    return {"t_mean": t.mean(), "h_mean": h.mean(),
            "diff": t.mean() - h.mean(),
            "pct": (t.mean() - h.mean()) / h.mean() * 100,
            "ci": (float(lo), float(hi))}


def main() -> None:
    N, SEED = 40_000, 42
    df = generate(n=N, seed=SEED)
    df = df.drop_duplicates(subset=["payment_id"]).reset_index(drop=True)
    df["arm"] = df["payment_id"].map(assign_arm)

    train, test = train_test_split_by_hash(df, test_pct=30)
    clf = CauseClassifier().fit(train)
    test_pred = clf.predict_df(test)
    m = evaluate(test_pred)

    print(f"\n{'='*68}\n  CLASSIFIER — held-out test (n={m['n_test']}, train n={len(train)})\n{'='*68}")
    print(f"  macro-F1 (decided rows) : {m['macro_f1_decided']:.3f}")
    print(f"  abstain -> AMBIGUOUS    : {m['abstain_rate']*100:.1f}%")
    print(f"  stage mix               : " + ", ".join(
        f"{k} {v*100:.0f}%" for k, v in sorted(m["stage_mix"].items())))
    print(f"\n  {'cause':<26}{'recall':>8}{'precision':>11}")
    for c in sorted(m["per_class_recall"]):
        print(f"  {c:<26}{m['per_class_recall'][c]:>8.2f}{m['per_class_precision'][c]:>11.2f}")
    print(f"\n  UNSAFE RETRIES (retry fired at a true hard decline):")
    print(f"  {m['unsafe_retry_count']} of {m['hard_decline_count']} hard declines "
          f"({m['unsafe_retry_rate']*100:.2f}%)  <- the metric that must be ~0")

    # ---- end-to-end: oracle vs predicted, on the SAME test rows ----
    full_pred = clf.predict_df(df)
    oracle = uplift(run_arm(full_pred, "true_cause", SEED + 7), SEED)
    pred = uplift(run_arm(full_pred, "pred_cause", SEED + 7), SEED)

    print(f"\n{'='*68}\n  END-TO-END UPLIFT vs naive baseline (n={len(df)})\n{'='*68}")
    for name, u in (("oracle causes", oracle), ("predicted causes", pred)):
        sig = u["ci"][0] > 0
        print(f"  {name:<18} +₹{u['diff']:.0f}/failure  ({u['pct']:+.1f}%)  "
              f"CI [₹{u['ci'][0]:+.0f}, ₹{u['ci'][1]:+.0f}]  significant={sig}")
    kept = pred["pct"] / oracle["pct"] * 100 if oracle["pct"] else 0
    print(f"\n  classification cost: {oracle['pct']:.1f}% -> {pred['pct']:.1f}% "
          f"uplift ({kept:.0f}% of oracle value retained)\n")


if __name__ == "__main__":
    main()
