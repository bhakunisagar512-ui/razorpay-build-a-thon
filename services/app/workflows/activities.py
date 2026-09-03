"""All I/O lives here. The workflow itself must stay deterministic."""
import json
from dataclasses import dataclass
from temporalio import activity
from sqlalchemy import create_engine, text
from services.app.settings import settings
from services.app.domain import guards, ledger
from services.app.adapters.channels import get_channel

_engine = None
def engine():
    global _engine
    if _engine is None:
        url = settings.database_url.replace("postgresql://", "postgresql+psycopg://")
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine

@dataclass
class StepInput:
    payment_id: str
    plan_id: int
    step_idx: int
    step: dict

@activity.defn
async def check_guards(inp: StepInput) -> dict:
    with engine().begin() as conn:
        res = guards.check(conn, inp.payment_id, inp.step)
        return {"allowed": res.allowed, "guard": res.guard}

@activity.defn
async def record_suppression(inp: StepInput, guard: str) -> None:
    with engine().begin() as conn:
        conn.execute(text("""INSERT INTO recovery_attempt
            (plan_id, step_idx, action, channel, rail, outcome, suppressed_by)
            VALUES (:p,:i,:a,:c,:r,'suppressed',:g) ON CONFLICT DO NOTHING"""),
            {"p": inp.plan_id, "i": inp.step_idx, "a": inp.step["action"],
             "c": inp.step.get("channel"), "r": inp.step.get("rail"), "g": guard})
        ledger.append(conn, entity_type="payment", entity_id=inp.payment_id,
                      actor="guardrails@v1", action="step_suppressed",
                      payload={"step_idx": inp.step_idx, "guard": guard})

@activity.defn
async def execute_step(inp: StepInput) -> dict:
    """Demo/mock execution: records the attempt; live mode would call Razorpay."""
    ch = get_channel(settings.channel_mode, engine())
    cost = 0
    if inp.step.get("channel"):
        await ch.send("customer", inp.step["action"], {"step_idx": inp.step_idx})
    with engine().begin() as conn:
        conn.execute(text("""INSERT INTO recovery_attempt
            (plan_id, step_idx, action, channel, rail, outcome, cost_minor)
            VALUES (:p,:i,:a,:c,:r,'sent',:m) ON CONFLICT DO NOTHING"""),
            {"p": inp.plan_id, "i": inp.step_idx, "a": inp.step["action"],
             "c": inp.step.get("channel"), "r": inp.step.get("rail"), "m": cost})
        ledger.append(conn, entity_type="payment", entity_id=inp.payment_id,
                      actor="executor@v1", action="step_executed",
                      payload={"step_idx": inp.step_idx, **inp.step})
    return {"outcome": "sent", "cost_minor": cost}

@activity.defn
async def finalize(payment_id: str) -> None:
    with engine().begin() as conn:
        conn.execute(text("""INSERT INTO recovery_outcome (payment_id, recovered, attempts_used)
            SELECT :p, false, COALESCE((SELECT COUNT(*) FROM recovery_attempt a
                JOIN recovery_plan pl ON pl.id=a.plan_id WHERE pl.payment_id=:p
                AND a.outcome <> 'suppressed'),0)
            ON CONFLICT (payment_id) DO NOTHING"""), {"p": payment_id})
        conn.execute(text("UPDATE recovery_plan SET status='exhausted' WHERE payment_id=:p AND status='active'"),
                     {"p": payment_id})
        ledger.append(conn, entity_type="payment", entity_id=payment_id,
                      actor="workflow@v1", action="plan_finalized", payload={})
