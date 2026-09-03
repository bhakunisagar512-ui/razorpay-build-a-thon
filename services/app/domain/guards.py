"""Stopping rules. Evaluated at EXECUTION time, never plan time — the world
moves during a 72-hour timer. Every suppression is logged with the guard name;
the suppression log is evidence, not a gap."""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from sqlalchemy import text

IST = timezone(timedelta(hours=5, minutes=30))
MAX_CONTACTS_PER_PAYMENT = 4
MAX_RETRIES_PER_PAYMENT = 6
MAX_CUSTOMER_CONTACTS_PER_WEEK = 6
QUIET_START, QUIET_END = 21, 9   # IST, outbound messages only

@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    guard: str | None = None

def check(conn, payment_id: str, step: dict, now: datetime | None = None) -> GuardResult:
    now = now or datetime.now(tz=IST)
    is_contact = step.get("channel") is not None
    is_retry = step.get("action") in ("silent_retry", "rail_switch")

    row = conn.execute(text("SELECT customer_id, order_id FROM failed_payment WHERE id=:p"),
                       {"p": payment_id}).first()
    if row is None:
        return GuardResult(False, "unknown_payment")
    customer_id, order_id = row

    # the one everybody forgets: order already paid through some other path
    paid = conn.execute(text("""SELECT 1 FROM recovery_outcome o
        JOIN failed_payment f ON f.id = o.payment_id
        WHERE f.order_id = :o AND o.recovered LIMIT 1"""), {"o": order_id}).first()
    if paid:
        return GuardResult(False, "order_paid_elsewhere")

    counts = conn.execute(text("""SELECT
          COUNT(*) FILTER (WHERE a.channel IS NOT NULL AND a.outcome <> 'suppressed'),
          COUNT(*) FILTER (WHERE a.action IN ('silent_retry','rail_switch') AND a.outcome <> 'suppressed')
        FROM recovery_attempt a JOIN recovery_plan p ON p.id = a.plan_id
        WHERE p.payment_id = :p"""), {"p": payment_id}).first()
    contacts, retries = counts or (0, 0)
    if is_contact and contacts >= MAX_CONTACTS_PER_PAYMENT:
        return GuardResult(False, "max_contact_attempts")
    if is_retry and retries >= MAX_RETRIES_PER_PAYMENT:
        return GuardResult(False, "max_retries")

    if is_contact:
        wk = conn.execute(text("""SELECT COUNT(*) FROM recovery_attempt a
            JOIN recovery_plan p ON p.id = a.plan_id
            JOIN failed_payment f ON f.id = p.payment_id
            WHERE f.customer_id = :c AND a.channel IS NOT NULL
              AND a.outcome <> 'suppressed'
              AND a.executed_at > now() - interval '7 days'"""), {"c": customer_id}).scalar()
        if (wk or 0) >= MAX_CUSTOMER_CONTACTS_PER_WEEK:
            return GuardResult(False, "max_customer_contacts_per_week")
        h = now.astimezone(IST).hour
        if h >= QUIET_START or h < QUIET_END:
            return GuardResult(False, "quiet_hours")
    return GuardResult(True)
