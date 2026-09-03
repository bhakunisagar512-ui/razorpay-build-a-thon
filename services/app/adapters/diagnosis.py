"""
Live diagnosis adapter.

The classifier lives in eval/ because that is where it is trained and measured.
This wraps it for the serving path so the running service and the evaluation
use the SAME artifact — a model that scores well offline and a different code
path in production is the most common way a good eval becomes a lie.

Fitted once at startup on the causal generator, cached to disk so restarts are
cheap. In production this loads a model trained on real gateway logs instead;
the interface does not change.
"""
from __future__ import annotations

import pathlib
import threading
from dataclasses import dataclass

MODEL_PATH = pathlib.Path("/tmp/rzp_cause_clf.joblib")
TRAIN_N = 20_000
TRAIN_SEED = 42

_lock = threading.Lock()
_clf = None


@dataclass(frozen=True)
class Diagnosis:
    cause: str
    confidence: float
    stage: str


class _Row:
    """predict_row() reads attributes, not dict keys."""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _fit():
    from eval.generate import generate
    from eval.classifier import CauseClassifier, train_test_split_by_hash

    df = generate(n=TRAIN_N, seed=TRAIN_SEED).drop_duplicates(subset=["payment_id"])
    train, _ = train_test_split_by_hash(df, test_pct=30)   # never train on test
    return CauseClassifier().fit(train)


def classifier():
    """Lazy, thread-safe, cached. First call costs a few seconds."""
    global _clf
    if _clf is not None:
        return _clf
    with _lock:
        if _clf is not None:
            return _clf
        try:
            import joblib
            if MODEL_PATH.exists():
                _clf = joblib.load(MODEL_PATH)
                return _clf
        except Exception:
            pass
        _clf = _fit()
        try:
            import joblib
            joblib.dump(_clf, MODEL_PATH)
        except Exception:
            pass          # cache is an optimisation, not a requirement
        return _clf


def diagnose(*, gw_error_code: str | None, gw_error_source: str | None,
             gw_error_step: str | None, method: str,
             instrument_hint: str = "na") -> Diagnosis:
    """Classify one failure. Falls back to AMBIGUOUS on any error — the
    conservative path is instrument-safe, so failing closed is correct."""
    from services.app.domain.taxonomy import Cause

    if not gw_error_code:
        return Diagnosis(str(Cause.AMBIGUOUS), 0.0, "no_error_code")
    try:
        row = _Row(gw_error_code=gw_error_code,
                   gw_error_source=gw_error_source or "gateway",
                   gw_error_step=gw_error_step or "authorization",
                   method=method,
                   instrument_hint=instrument_hint or "na")
        cause, conf, stage = classifier().predict_row(row)
        return Diagnosis(cause, float(conf), stage)
    except Exception as exc:                       # noqa: BLE001
        return Diagnosis(str(Cause.AMBIGUOUS), 0.0, f"error:{type(exc).__name__}")
