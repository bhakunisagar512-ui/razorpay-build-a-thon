"""Durable recovery workflow. Deterministic: all I/O in activities.
DEMO_TIME_SCALE compresses hours into seconds so a 5-minute video can show
a multi-day plan end to end."""
import asyncio
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
with workflow.unsafe.imports_passed_through():
    from services.app.workflows.activities import (
        StepInput, check_guards, record_suppression, execute_step, finalize)

DEMO_TIME_SCALE = 3600  # 1h of plan time -> 1s of wall time in demo mode

@workflow.defn
class RecoveryWorkflow:
    def __init__(self) -> None:
        self._settled = False

    @workflow.signal
    def order_settled(self) -> None:
        self._settled = True

    @workflow.run
    async def run(self, payment_id: str, plan_id: int, steps: list[dict],
                  demo: bool = True) -> str:
        for idx, step in enumerate(steps):
            delay = step.get("delay_s", 0)
            if demo:
                delay = max(delay // DEMO_TIME_SCALE, 1)
            await asyncio.sleep(delay)
            if self._settled:
                break
            inp = StepInput(payment_id, plan_id, idx, step)
            g = await workflow.execute_activity(
                check_guards, inp, start_to_close_timeout=timedelta(seconds=30))
            if not g["allowed"]:
                await workflow.execute_activity(
                    record_suppression, args=[inp, g["guard"]],
                    start_to_close_timeout=timedelta(seconds=30))
                continue
            await workflow.execute_activity(
                execute_step, inp, start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3))
        await workflow.execute_activity(
            finalize, payment_id, start_to_close_timeout=timedelta(seconds=30))
        return "done"
