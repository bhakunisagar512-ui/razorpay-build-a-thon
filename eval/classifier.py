"""
Three-stage cause classifier.

Stage 1 — RULES: deterministic mapping for observable tuples that are
   near-unambiguous. Versioned, reviewable, no training required.
Stage 2 — MODEL: probabilistic fallback for the collision zone (the tuples
   several causes emit). Gradient boosting over one-hot features, calibrated.

Stage 3 — LLM TRIAGE: last resort for tuples stages 1 and 2 both declined.
   Rules and the GBM only know codes seen in training; real gateways emit a long
   tail. The LLM maps an unseen code string onto the taxonomy — semantic work a
   one-hot GBM cannot do. It PROPOSES; the safety gate below disposes. Disabled
   without an API key, in which case behaviour is identical to two stages.

SAFETY GATE (the part that matters more than accuracy):
   Misclassifications are not symmetric. Predicting BD_INSUFFICIENT_FUNDS when
   the truth is CARD_EXPIRED makes the engine hammer a dead card — the exact
   behaviour we exist to prevent. So: if the model's confidence is below
   THRESHOLD, or if any hard-decline cause has non-trivial probability mass,
   we return AMBIGUOUS and the policy engine takes the conservative path.
   We deliberately trade recall on retryable causes for a low unsafe-retry rate;
   the achieved rate and its frontier against uplift are reported by step4.
   The gate applies identically to all three stages — an LLM proposal gets no
   more trust than a rule hit.
"""
from __future__ import annotations
import hashlib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import OneHotEncoder
from sklearn.metrics import f1_score, recall_score, precision_score
from services.app.domain.taxonomy import Cause, RETRYABLE

RULES_VERSION = "rules@v1"
CONF_THRESHOLD = 0.60
HARD_DECLINE_MASS_LIMIT = 0.25          # if P(expired ∪ risk) > this, go conservative
HARD_CAUSES = (str(Cause.CARD_EXPIRED), str(Cause.RISK_BLOCKED))

# Tuples where one cause dominates emission strongly enough to hard-code.
RULES: dict[tuple[str, str, str], Cause] = {
    ("BAD_REQUEST_ERROR", "bank", "authorization"): Cause.BD_INSUFFICIENT_FUNDS,
    ("BAD_REQUEST_ERROR", "customer", "authentication"): Cause.BD_USER_ABANDONED_OTP,
    ("GATEWAY_ERROR", "issuer", "authorization"): Cause.TD_ISSUER_DOWN,
    ("GATEWAY_ERROR", "gateway", "payment"): Cause.TD_PSP_TIMEOUT,
    ("SERVER_ERROR", "internal", "payment"): Cause.TD_PSP_TIMEOUT,
    ("BAD_REQUEST_ERROR", "customer", "payment"): Cause.CARD_EXPIRED,
    ("BAD_REQUEST_ERROR", "razorpay", "payment"): Cause.RISK_BLOCKED,
    # deliberately ABSENT: ("GATEWAY_ERROR","gateway","authorization") — the
    # collision zone shared by 5 causes. That is the model's job.
}

FEATURES = ["gw_error_code", "gw_error_source", "gw_error_step", "method", "instrument_hint"]


def train_test_split_by_hash(df: pd.DataFrame, test_pct: int = 30):
    """Deterministic split on payment_id. Same discipline as the arm split:
    commit the hash, never peek at test."""
    def bucket(pid: str) -> str:
        return "test" if int(hashlib.sha256(pid.encode()).hexdigest()[:8], 16) % 100 < test_pct \
               else "train"
    b = df["payment_id"].map(bucket)
    return df[b == "train"].copy(), df[b == "test"].copy()


class CauseClassifier:
    def __init__(self):
        self.enc = OneHotEncoder(handle_unknown="ignore")
        self.model = HistGradientBoostingClassifier(max_iter=200, random_state=0)
        self.classes_: list[str] = []

    # ---- stage 1 ----
    @staticmethod
    def rule_hit(row) -> Cause | None:
        return RULES.get((row.gw_error_code, row.gw_error_source, row.gw_error_step))

    # ---- stage 2 ----
    def fit(self, train: pd.DataFrame) -> "CauseClassifier":
        X = self.enc.fit_transform(train[FEATURES])
        self.model.fit(X.toarray(), train["true_cause"])
        self.classes_ = list(self.model.classes_)
        return self

    # ---- stage 3 ----
    @staticmethod
    def _tier3(row, hard_mass: float, fb_conf: float, fb_stage: str
               ) -> tuple[str, float, str]:
        """Last resort, reached only when stages 1 and 2 have both abstained.

        The LLM PROPOSES; this function disposes. Its output passes through the
        same hard-decline mass gate as every other stage, so a hallucinated
        retryable cause on a payment the model thinks is a hard decline is
        rejected exactly as a bad rule hit would be. Every failure path returns
        the caller's original abstention, so the floor cannot move down.
        """
        from services.app.adapters import llm_triage

        if not llm_triage.available():
            return str(Cause.AMBIGUOUS), fb_conf, fb_stage
        res = llm_triage.triage(
            gw_error_code=row.gw_error_code, gw_error_source=row.gw_error_source,
            gw_error_step=row.gw_error_step, method=row.method,
            instrument_hint=getattr(row, "instrument_hint", "na"))
        if not res.ok:
            return str(Cause.AMBIGUOUS), fb_conf, f"{fb_stage}+llm_abstain"
        # THE gate: a retryable proposal cannot survive high hard-decline mass.
        if res.cause not in HARD_CAUSES and hard_mass > HARD_DECLINE_MASS_LIMIT:
            return str(Cause.AMBIGUOUS), fb_conf, f"{fb_stage}+llm_gated"
        return res.cause, res.confidence, "llm_triage"

    def predict_row(self, row) -> tuple[str, float, str]:
        """Returns (cause, confidence, stage)."""
        hit = self.rule_hit(row)
        # Rule hits mapping to HARD causes are safe to trust: the error direction
        # is conservative (worst case we under-retry). Retryable rule hits are NOT
        # safe to trust blindly — hard declines leak into those tuples — so they
        # still pass through the hard-decline mass gate below. Found by eval:
        # trusting rules unconditionally produced a 20.5% unsafe retry rate.
        if hit is not None and str(hit) in HARD_CAUSES:
            return str(hit), 1.0, "rules"
        X = self.enc.transform(pd.DataFrame([{f: getattr(row, f) for f in FEATURES}]))
        proba = self.model.predict_proba(X.toarray())[0]
        hard_mass = sum(float(proba[i]) for i, c in enumerate(self.classes_) if c in HARD_CAUSES)
        if hit is not None:
            if hard_mass > HARD_DECLINE_MASS_LIMIT:
                return str(Cause.AMBIGUOUS), 1.0 - hard_mass, "gated:rules_hard_risk"
            return str(hit), 1.0 - hard_mass, "rules"
        top = int(np.argmax(proba))
        cause, conf = self.classes_[top], float(proba[top])
        if conf < CONF_THRESHOLD:
            return self._tier3(row, hard_mass, conf, "gated:low_conf")
        if cause not in HARD_CAUSES and hard_mass > HARD_DECLINE_MASS_LIMIT:
            return self._tier3(row, hard_mass, conf, "gated:hard_decline_risk")
        return cause, conf, "model"

    def predict_df(self, df: pd.DataFrame, use_llm: bool = False) -> pd.DataFrame:
        """Vectorised: one batched predict_proba for the whole frame.

        use_llm routes stage-2 abstentions through tier 3. Off by default so the
        headline eval stays offline and deterministic; the triage eval turns it
        on. Triage is cached on the observable tuple, so even 40,000 rows cost
        only as many calls as there are distinct tuples."""
        out = df.copy()
        rows = list(df.itertuples(index=False)) if use_llm else None
        proba = self.model.predict_proba(self.enc.transform(df[FEATURES]).toarray())
        hard_idx = [i for i, c in enumerate(self.classes_) if c in HARD_CAUSES]
        hard_mass = proba[:, hard_idx].sum(axis=1)
        top = proba.argmax(axis=1)
        top_cause = np.array(self.classes_)[top]
        top_conf = proba[np.arange(len(df)), top]
        rule = [RULES.get((c, s, st)) for c, s, st in
                zip(df["gw_error_code"], df["gw_error_source"], df["gw_error_step"])]
        rule = [str(c) if c is not None else None for c in rule]

        causes, confs, stages = [], [], []
        for i in range(len(df)):
            if rule[i] is not None and rule[i] in HARD_CAUSES:
                causes.append(rule[i]); confs.append(1.0); stages.append("rules")
            elif rule[i] is not None:
                if hard_mass[i] > HARD_DECLINE_MASS_LIMIT:
                    causes.append(str(Cause.AMBIGUOUS)); confs.append(1 - hard_mass[i])
                    stages.append("gated:rules_hard_risk")
                else:
                    causes.append(rule[i]); confs.append(1 - hard_mass[i]); stages.append("rules")
            elif top_conf[i] < CONF_THRESHOLD:
                c, cf, st = (self._tier3(rows[i], hard_mass[i], top_conf[i], "gated:low_conf")
                             if use_llm else
                             (str(Cause.AMBIGUOUS), top_conf[i], "gated:low_conf"))
                causes.append(c); confs.append(cf); stages.append(st)
            elif top_cause[i] not in HARD_CAUSES and hard_mass[i] > HARD_DECLINE_MASS_LIMIT:
                c, cf, st = (self._tier3(rows[i], hard_mass[i], top_conf[i], "gated:hard_decline_risk")
                             if use_llm else
                             (str(Cause.AMBIGUOUS), top_conf[i], "gated:hard_decline_risk"))
                causes.append(c); confs.append(cf); stages.append(st)
            else:
                causes.append(top_cause[i]); confs.append(top_conf[i]); stages.append("model")
        out["pred_cause"], out["pred_conf"], out["pred_stage"] = causes, confs, stages
        return out


def evaluate(test_pred: pd.DataFrame) -> dict:
    y, p = test_pred["true_cause"], test_pred["pred_cause"]
    labels = sorted(set(y))
    decided = test_pred[test_pred.pred_cause != str(Cause.AMBIGUOUS)]

    # THE safety metric: how often would we have retried a true hard decline?
    unsafe = decided[
        decided.true_cause.isin(HARD_CAUSES)
        & decided.pred_cause.map(lambda c: RETRYABLE.get(Cause(c), True))
    ]
    hard_total = int(test_pred.true_cause.isin(HARD_CAUSES).sum())

    return {
        "n_test": len(test_pred),
        "abstain_rate": float((p == str(Cause.AMBIGUOUS)).mean()),
        "stage_mix": test_pred["pred_stage"].value_counts(normalize=True).to_dict(),
        "macro_f1_decided": float(f1_score(decided.true_cause, decided.pred_cause,
                                           labels=labels, average="macro", zero_division=0)),
        "per_class_recall": {
            l: float(recall_score(y == l, p == l, zero_division=0)) for l in labels},
        "per_class_precision": {
            l: float(precision_score(y == l, p == l, zero_division=0)) for l in labels},
        "unsafe_retry_rate": float(len(unsafe) / hard_total) if hard_total else 0.0,
        "unsafe_retry_count": int(len(unsafe)),
        "hard_decline_count": hard_total,
    }
