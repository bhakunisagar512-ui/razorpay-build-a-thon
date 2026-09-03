"""
FastAPI surface.

  POST /webhooks/razorpay   signed webhook ingest (payment.failed and settlements)
  POST /simulate/failure    demo injector — drives the pitch video (DEMO_MODE only)
  GET  /payments/{id}/timeline
  GET  /metrics/uplift
  GET  /metrics/by-cause
  GET  /audit/verify
  GET  /health
"""
from __future__ import annotations
import hashlib, hmac, json, uuid
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from temporalio.client import Client

from services.app.settings import settings
from services.app.domain.taxonomy import Cause
from services.app.domain.policy import Ctx, build_plan, baseline_plan
from services.app.domain import ledger
from services.app.workflows.recovery import RecoveryWorkflow
from services.app.adapters.diagnosis import diagnose

app = FastAPI(title="rzp-recovery", version="0.1.0")

_engine = create_engine(
    settings.database_url.replace("postgresql://", "postgresql+psycopg://"),
    pool_pre_ping=True)
_temporal: Client | None = None


async def temporal() -> Client:
    global _temporal
    if _temporal is None:
        _temporal = await Client.connect(settings.temporal_address)
    return _temporal


def assign_arm(payment_id: str) -> str:
    h = int(hashlib.sha256(payment_id.encode()).hexdigest()[:8], 16)
    return "holdout" if h % 100 < settings.holdout_pct else "treatment"


def verify_sig(raw: bytes, sig: str | None) -> bool:
    if not sig:
        return False
    expected = hmac.new(settings.razorpay_webhook_secret.encode(), raw,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


async def ingest_failure(p: dict, event_id: str,
                         forced_cause: Cause | None = None,
                         forced_arm: str | None = None) -> dict:
    """Shared path for real webhooks and simulated failures.

    forced_cause is set ONLY by /simulate/failure, so the demo can drive a
    specific cause deterministically. Real webhooks always go through the
    classifier."""
    payment_id = p["payment_id"]
    with _engine.begin() as conn:
        dup = conn.execute(text(
            "INSERT INTO webhook_event (event_id, raw) VALUES (:i, CAST(:r AS jsonb)) "
            "ON CONFLICT DO NOTHING RETURNING event_id"),
            {"i": event_id, "r": json.dumps(p)}).first()
        if dup is None:
            return {"status": "duplicate_ignored", "payment_id": payment_id}

        if forced_cause is not None:
            cause, conf, stage = forced_cause, 1.0, "forced:simulate"
        else:
            d = diagnose(gw_error_code=p.get("gw_error_code"),
                         gw_error_source=p.get("gw_error_source"),
                         gw_error_step=p.get("gw_error_step"),
                         method=p["method"],
                         instrument_hint=p.get("instrument_hint", "na"))
            cause, conf, stage = Cause(d.cause), d.confidence, d.stage
        arm = forced_arm or assign_arm(payment_id)
        conn.execute(text("""INSERT INTO failed_payment
            (id, order_id, customer_id, amount_minor, method,
             gw_error_code, gw_error_source, gw_error_step, instrument_hint,
             cause, cause_conf, cause_stage, arm, failed_at)
            VALUES (:id,:o,:c,:a,:m,:ec,:es,:st,:ih,:cause,:conf,:stage,:arm,:t)
            ON CONFLICT (id) DO NOTHING"""),
            {"id": payment_id, "o": p["order_id"], "c": p["customer_id"],
             "a": p["amount_minor"], "m": p["method"],
             "ec": p.get("gw_error_code"), "es": p.get("gw_error_source"),
             "st": p.get("gw_error_step"), "ih": p.get("instrument_hint", "na"),
             "cause": str(cause), "conf": round(conf, 3), "stage": stage, "arm": arm,
             "t": datetime.now(tz=timezone.utc)})

        ctx = Ctx(amount_minor=p["amount_minor"], method=p["method"],
                  today=datetime.now(tz=timezone.utc).date(),
                  available_rails=frozenset(p.get("available_rails",
                                                  "upi_intent,card").split(",")),
                  consent_whatsapp=p.get("consent_whatsapp", True))
        plan = build_plan(cause, ctx) if arm == "treatment" else baseline_plan(cause, ctx)
        steps = [asdict(s) for s in plan.steps]
        plan_id = conn.execute(text("""INSERT INTO recovery_plan
            (payment_id, policy_ver, steps) VALUES (:p,:v,CAST(:s AS jsonb)) RETURNING id"""),
            {"p": payment_id, "v": plan.policy_ver, "s": json.dumps(steps)}).scalar()
        ledger.append(conn, entity_type="payment", entity_id=payment_id,
                      actor=plan.policy_ver, action="plan_created",
                      payload={"arm": arm, "cause": str(cause), "cause_conf": round(conf, 3),
                               "cause_stage": stage, "steps": steps},
                      policy_ver=plan.policy_ver)

    client = await temporal()
    await client.start_workflow(
        RecoveryWorkflow.run,
        args=[payment_id, plan_id, steps, settings.demo_mode],
        id=f"recovery-{payment_id}",
        task_queue=settings.temporal_task_queue)
    return {"status": "accepted", "payment_id": payment_id, "arm": arm,
            "cause": str(cause), "cause_conf": round(conf, 3), "cause_stage": stage,
            "plan_id": plan_id, "steps": steps}


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request,
                           x_razorpay_signature: str | None = Header(None)):
    raw = await request.body()
    if not verify_sig(raw, x_razorpay_signature):
        raise HTTPException(401, "bad signature")
    body = json.loads(raw)
    event_id = body.get("event_id") or f"evt_{uuid.uuid4().hex[:12]}"
    if body.get("event") == "payment.failed":
        e = body["payload"]["payment"]["entity"]
        return await ingest_failure({
            "payment_id": e["id"], "order_id": e.get("order_id") or "unknown",
            "customer_id": e.get("customer_id") or e.get("contact") or "unknown",
            "amount_minor": e["amount"], "method": e.get("method", "card"),
            "gw_error_code": e.get("error_code"),
            "gw_error_source": e.get("error_source"),
            "gw_error_step": e.get("error_step"),
            "instrument_hint": e.get("instrument_hint", "na"),
        }, event_id)   # no forced_cause -> the classifier decides
    if body.get("event") == "payment.captured":
        e = body["payload"]["payment"]["entity"]
        with _engine.begin() as conn:
            conn.execute(text("""UPDATE recovery_outcome o SET recovered=true,
                recovered_at=now(), recovered_minor=:a
                FROM failed_payment f
                WHERE f.id=o.payment_id AND f.order_id=:ord"""),
                {"a": e["amount"], "ord": e.get("order_id")})
        try:
            client = await temporal()
            with _engine.begin() as conn:
                rows = conn.execute(text(
                    "SELECT id FROM failed_payment WHERE order_id=:o"),
                    {"o": e.get("order_id")}).scalars().all()
            for pid in rows:
                try:
                    h = client.get_workflow_handle(f"recovery-{pid}")
                    await h.signal(RecoveryWorkflow.order_settled)
                except Exception:
                    pass
        except Exception:
            pass
        return {"status": "settlement_recorded"}
    return {"status": "ignored", "event": body.get("event")}


class SimulatedFailure(BaseModel):
    cause: Cause
    amount_minor: int = 49_900
    method: str = "card"
    available_rails: str = "upi_intent,card,netbanking"
    consent_whatsapp: bool = True
    arm: str | None = None      # demo only: force "treatment" or "holdout".
                                # Real traffic is always assigned by hash.


@app.post("/simulate/failure")
async def simulate_failure(sim: SimulatedFailure):
    """Demo injector: fire any cause on demand. This endpoint drives the video."""
    if not settings.demo_mode:
        raise HTTPException(403, "DEMO_MODE is off")
    pid = f"pay_sim_{uuid.uuid4().hex[:10]}"
    return await ingest_failure({
        "payment_id": pid, "order_id": f"ord_sim_{uuid.uuid4().hex[:8]}",
        "customer_id": f"cust_sim_{uuid.uuid4().hex[:6]}",
        "amount_minor": sim.amount_minor, "method": sim.method,
        "available_rails": sim.available_rails,
        "consent_whatsapp": sim.consent_whatsapp,
    }, f"evt_sim_{pid}", forced_cause=sim.cause, forced_arm=sim.arm)


class DiagnoseRequest(BaseModel):
    gw_error_code: str
    gw_error_source: str = "gateway"
    gw_error_step: str = "authorization"
    method: str = "card"
    instrument_hint: str = "na"


@app.post("/diagnose")
def diagnose_only(req: DiagnoseRequest):
    """Classification with no side effects: shows the diagnosed cause, the
    confidence, which stage decided it, and the plan that cause would produce.
    Use it to watch the safety gate abstain on ambiguous error tuples."""
    d = diagnose(**req.model_dump())
    ctx = Ctx(amount_minor=49_900, method=req.method,
              today=datetime.now(tz=timezone.utc).date(),
              available_rails=frozenset({"upi_intent", "card", "netbanking"}),
              consent_whatsapp=True)
    plan = build_plan(Cause(d.cause), ctx)
    return {"cause": d.cause, "confidence": round(d.confidence, 3),
            "stage": d.stage, "retry_count": plan.retry_count,
            "plan": [asdict(s) for s in plan.steps]}


@app.get("/payments/{payment_id}/timeline")
def timeline(payment_id: str):
    with _engine.begin() as conn:
        fp = conn.execute(text("SELECT * FROM failed_payment WHERE id=:p"),
                          {"p": payment_id}).mappings().first()
        if not fp:
            raise HTTPException(404, "unknown payment")
        attempts = [dict(r) for r in conn.execute(text(
            """SELECT a.* FROM recovery_attempt a
               JOIN recovery_plan p ON p.id=a.plan_id
               WHERE p.payment_id=:p ORDER BY a.step_idx"""),
            {"p": payment_id}).mappings()]
        audit = [dict(r) for r in conn.execute(text(
            """SELECT action, actor, payload, created_at FROM audit_log
               WHERE entity_type='payment' AND entity_id=:p ORDER BY seq"""),
            {"p": payment_id}).mappings()]
    return {"payment": dict(fp), "attempts": attempts, "audit": audit}


@app.get("/metrics/uplift")
def uplift():
    with _engine.begin() as conn:
        rows = conn.execute(text("""SELECT f.arm,
                COUNT(*) n,
                AVG(CASE WHEN o.recovered THEN 1.0 ELSE 0.0 END) rate,
                COALESCE(SUM(o.recovered_minor - o.cost_minor),0) net_minor
            FROM failed_payment f
            LEFT JOIN recovery_outcome o ON o.payment_id=f.id
            GROUP BY f.arm""")).mappings()
        return {r["arm"]: {"n": r["n"], "recovery_rate": float(r["rate"] or 0),
                           "net_inr": float(r["net_minor"] or 0) / 100} for r in rows}


@app.get("/metrics/by-cause")
def by_cause():
    with _engine.begin() as conn:
        rows = conn.execute(text("""SELECT f.cause, f.arm, COUNT(*) n,
                AVG(CASE WHEN o.recovered THEN 1.0 ELSE 0.0 END) rate
            FROM failed_payment f
            LEFT JOIN recovery_outcome o ON o.payment_id=f.id
            GROUP BY f.cause, f.arm ORDER BY f.cause""")).mappings()
        return [dict(r) for r in rows]


@app.get("/audit/verify")
def audit_verify():
    with _engine.begin() as conn:
        return ledger.verify_chain(conn)


@app.get("/health")
def health():
    with _engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True, "demo_mode": settings.demo_mode}
