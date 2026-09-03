"""Thin Razorpay test-mode client. Only the endpoints the engine needs."""
import httpx
from services.app.settings import settings

BASE = "https://api.razorpay.com/v1"

class RazorpayClient:
    def __init__(self):
        self.auth = (settings.razorpay_key_id, settings.razorpay_key_secret)

    async def create_payment_link(self, amount_minor: int, ref_order: str,
                                  method_hint: str | None = None) -> dict:
        payload = {"amount": amount_minor, "currency": "INR",
                   "reference_id": f"rec_{ref_order}",
                   "notes": {"recovery": "true", "order_id": ref_order}}
        async with httpx.AsyncClient(auth=self.auth, timeout=15) as c:
            r = await c.post(f"{BASE}/payment_links", json=payload)
            r.raise_for_status()
            return r.json()

    async def fetch_payment(self, payment_id: str) -> dict:
        async with httpx.AsyncClient(auth=self.auth, timeout=15) as c:
            r = await c.get(f"{BASE}/payments/{payment_id}")
            r.raise_for_status()
            return r.json()
