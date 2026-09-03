"""Outbound channel adapters. MockChannel records to DB — it's what lets the
eval run thousands of sims and what the demo uses by default."""
from typing import Protocol
from sqlalchemy import text

class Channel(Protocol):
    async def send(self, to: str, template: str, vars: dict) -> dict: ...

class MockChannel:
    name = "mock"
    def __init__(self, engine): self.engine = engine
    async def send(self, to: str, template: str, vars: dict) -> dict:
        with self.engine.begin() as conn:
            conn.execute(text("""INSERT INTO webhook_event (event_id, raw)
                VALUES (:i, CAST(:r AS jsonb)) ON CONFLICT DO NOTHING"""),
                {"i": f"mocksend_{to}_{template}_{vars.get('step_idx','x')}",
                 "r": '{"mock_send": true}'})
        return {"status": "sent", "cost_minor": 0}

def get_channel(mode: str, engine):
    # 'live' would return Twilio/WhatsApp Cloud API impls; mock is the default
    return MockChannel(engine)
