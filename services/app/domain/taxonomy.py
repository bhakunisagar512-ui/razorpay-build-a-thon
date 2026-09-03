"""Canonical failure taxonomy. Single source of truth for generator, policy and simulator."""
from __future__ import annotations
import sys
if sys.version_info < (3, 11):
    raise RuntimeError(
        "Python 3.11+ required (enum.StrEnum). Everything runs in the "
        "container: ./setup.sh, then `make eval-policy`.")
from enum import StrEnum


class Cause(StrEnum):
    BD_INSUFFICIENT_FUNDS = "BD_INSUFFICIENT_FUNDS"
    BD_USER_ABANDONED_OTP = "BD_USER_ABANDONED_OTP"
    TD_ISSUER_DOWN = "TD_ISSUER_DOWN"
    TD_PSP_TIMEOUT = "TD_PSP_TIMEOUT"
    CARD_EXPIRED = "CARD_EXPIRED"
    RISK_BLOCKED = "RISK_BLOCKED"
    AMBIGUOUS = "AMBIGUOUS"


RETRYABLE = {
    Cause.BD_INSUFFICIENT_FUNDS: True,
    Cause.BD_USER_ABANDONED_OTP: False,   # instrument was the friction, not the balance
    Cause.TD_ISSUER_DOWN: True,
    Cause.TD_PSP_TIMEOUT: True,
    Cause.CARD_EXPIRED: False,            # retrying a dead card degrades issuer trust
    Cause.RISK_BLOCKED: False,            # never auto-retry a risk decision
}

DAY = 86_400
