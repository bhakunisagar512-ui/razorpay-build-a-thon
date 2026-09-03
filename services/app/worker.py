"""Temporal worker entrypoint."""
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from services.app.settings import settings
from services.app.workflows.recovery import RecoveryWorkflow
from services.app.workflows import activities as act

async def main() -> None:
    client = await Client.connect(settings.temporal_address)
    worker = Worker(client, task_queue=settings.temporal_task_queue,
                    workflows=[RecoveryWorkflow],
                    activities=[act.check_guards, act.record_suppression,
                                act.execute_step, act.finalize])
    print(f"worker up on queue {settings.temporal_task_queue}")
    await worker.run()

if __name__ == "__main__":
    asyncio.run(main())
