"""
Tier 3 — LLM triage for error tuples the classical stages cannot resolve.

WHY THIS EXISTS
    Rules and the GBM only know error tuples that appeared in training. Real
    gateways emit a long tail: new codes after issuer integrations, bank-specific
    strings, unmapped `reason` fields. Today those land in AMBIGUOUS forever.
    Mapping an unseen code string onto a known taxonomy is semantic work, which
    is the one thing a language model does better than one-hot gradient boosting.

WHY IT IS SAFE
    1. It is reached ONLY when rules and the model have both abstained. It can
       never override a confident classical prediction.
    2. Its output is a PROPOSAL. The same hard-decline gate that polices every
       other stage polices this one — see classifier.predict_row.
    3. Every failure mode (no key, timeout, bad JSON, unknown enum, low
       confidence) returns AMBIGUOUS, which is exactly the current behaviour.
       The floor cannot move down.

    So the tier is monotonic: it can only improve on the tier below it.

NO KEY REQUIRED. Without LLM_API_KEY the system behaves exactly as before.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache

from services.app.domain.taxonomy import Cause

# --------------------------------------------------------------------- config
PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
API_KEY = os.getenv("LLM_API_KEY", "")
MODEL = os.getenv("LLM_MODEL", "gemini-2.0-flash")
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "6"))
ENABLED = os.getenv("LLM_TRIAGE_ENABLED", "true").lower() == "true"

# An LLM proposal below this is not worth acting on — take AMBIGUOUS instead.
TRIAGE_CONF_FLOOR = 0.70

VALID = {str(c) for c in Cause}
AMBIG = str(Cause.AMBIGUOUS)

_ENDPOINTS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models/"
               "{model}:generateContent?key={key}"),
    "groq":   "https://api.groq.com/openai/v1/chat/completions",
}

# ---------------------------------------------------------------------- stats
STATS = {"calls": 0, "resolved": 0, "abstained": 0, "errors": 0, "cache_hits": 0}


@dataclass(frozen=True)
class TriageResult:
    cause: str
    confidence: float
    rationale: str
    ok: bool                       # False => the caller must treat as AMBIGUOUS


ABSTAIN = TriageResult(AMBIG, 0.0, "triage unavailable", False)


# --------------------------------------------------------------------- prompt
SYSTEM = """You map payment-gateway error signals onto a fixed taxonomy of \
failure causes. You are the last resort: deterministic rules and a trained \
classifier have both already declined to decide.

TAXONOMY — choose exactly one:
  BD_INSUFFICIENT_FUNDS  Account lacks balance. The instrument is fine; the money is not there yet.
  BD_USER_ABANDONED_OTP  Customer never completed authentication. Instrument fine, human left.
  TD_ISSUER_DOWN         The issuing bank's systems failed or were unreachable. Transient.
  TD_PSP_TIMEOUT         Gateway/PSP timeout or internal error. Transient, may have silently succeeded.
  CARD_EXPIRED           The instrument is dead and will never authorise again.
  RISK_BLOCKED           Deliberately blocked by fraud/risk/compliance controls.
  AMBIGUOUS              You cannot tell. USE THIS FREELY — it is the safe answer.

DECISION RULES
- Choosing CARD_EXPIRED or RISK_BLOCKED means "never retry". Choosing anything
  else may cause a retry. A wrong retry against a dead or blocked instrument is
  the most expensive mistake you can make here.
- If the signal is generic ("payment failed", "error", "declined") with no
  further detail, answer AMBIGUOUS. Do not guess from the payment method alone.
- Confidence must reflect the evidence in the error strings, not your general
  knowledge of payments.

Reply with ONLY a JSON object, no markdown fence:
{"cause": "<TAXONOMY_KEY>", "confidence": <0.0-1.0>, "rationale": "<max 15 words>"}"""


def _user_prompt(code, source, step, method, hint) -> str:
    return (f"error_code: {code}\nerror_source: {source}\nerror_step: {step}\n"
            f"payment_method: {method}\ninstrument_hint: {hint}")


# ------------------------------------------------------------------- provider
def _call_provider(user: str) -> str:
    """Raw text back from the model. Raises on any transport problem."""
    import httpx

    if PROVIDER == "gemini":
        url = _ENDPOINTS["gemini"].format(model=MODEL, key=API_KEY)
        body = {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 200,
                                 "responseMimeType": "application/json"},
        }
        r = httpx.post(url, json=body, timeout=TIMEOUT_S)
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    if PROVIDER == "groq":
        r = httpx.post(_ENDPOINTS["groq"], timeout=TIMEOUT_S,
                       headers={"Authorization": f"Bearer {API_KEY}"},
                       json={"model": MODEL, "temperature": 0.0,
                             "response_format": {"type": "json_object"},
                             "messages": [{"role": "system", "content": SYSTEM},
                                          {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    raise RuntimeError(f"unknown LLM_PROVIDER: {PROVIDER}")


# --------------------------------------------------------------------- parse
def parse_response(text: str) -> TriageResult:
    """Strict schema validation. Exposed separately so the adversarial eval can
    attack it with zero API calls."""
    try:
        cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(),
                         flags=re.MULTILINE).strip()
        obj = json.loads(cleaned)
    except Exception:
        return TriageResult(AMBIG, 0.0, "unparseable response", False)

    if not isinstance(obj, dict):
        return TriageResult(AMBIG, 0.0, "response was not an object", False)

    cause = obj.get("cause")
    if not isinstance(cause, str) or cause not in VALID:
        return TriageResult(AMBIG, 0.0, f"cause not in taxonomy: {cause!r}", False)

    try:
        conf = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        return TriageResult(AMBIG, 0.0, "confidence not numeric", False)
    if not 0.0 <= conf <= 1.0:
        return TriageResult(AMBIG, 0.0, "confidence out of range", False)

    rationale = str(obj.get("rationale", ""))[:120]

    if cause == AMBIG:
        return TriageResult(AMBIG, conf, rationale or "model abstained", False)
    if conf < TRIAGE_CONF_FLOOR:
        return TriageResult(AMBIG, conf, f"below floor: {rationale}", False)

    return TriageResult(cause, conf, rationale, True)


# ---------------------------------------------------------------------- entry
_injected = None          # tests / offline eval install a fake here


def set_provider(fn):
    """Inject a callable(user_prompt) -> raw text. Pass None to restore."""
    global _injected
    _injected = fn
    _triage_cached.cache_clear()


def available() -> bool:
    return ENABLED and (bool(API_KEY) or _injected is not None)


@lru_cache(maxsize=4096)
def _triage_cached(code, source, step, method, hint) -> TriageResult:
    """Cached on the observable tuple. Distinct tuples are few even across
    40,000 rows, so an eval run costs tens of calls, not thousands."""
    STATS["calls"] += 1
    try:
        caller = _injected or _call_provider
        raw = caller(_user_prompt(code, source, step, method, hint))
    except Exception as exc:                                   # noqa: BLE001
        STATS["errors"] += 1
        return TriageResult(AMBIG, 0.0, f"transport: {type(exc).__name__}", False)

    res = parse_response(raw)
    STATS["resolved" if res.ok else "abstained"] += 1
    return res


def triage(*, gw_error_code: str, gw_error_source: str, gw_error_step: str,
           method: str, instrument_hint: str = "na") -> TriageResult:
    """Never raises. Returns ABSTAIN when unavailable or unusable."""
    if not available():
        return ABSTAIN
    before = STATS["calls"]
    res = _triage_cached(gw_error_code, gw_error_source, gw_error_step,
                         method, instrument_hint)
    if STATS["calls"] == before:
        STATS["cache_hits"] += 1
    return res


def reset_stats():
    for k in STATS:
        STATS[k] = 0
    _triage_cached.cache_clear()
