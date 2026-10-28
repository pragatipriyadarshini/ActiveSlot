"""Send SMS messages through the configured notification provider."""
import os

import httpx

from .common import outbound_headers

PROVIDER_URL = os.environ.get("PROVIDER_URL", "http://localhost:8005")
TIMEOUT = float(os.environ.get("PROVIDER_TIMEOUT_SECONDS", "2"))


def send_sms(to: str, text: str) -> bool:
    """True if the (mock) provider accepted the message. Never raises: failure is a normal outcome."""
    try:
        r = httpx.post(f"{PROVIDER_URL}/sms/send", json={"to": to, "text": text},
                       headers=outbound_headers(), timeout=TIMEOUT)
        return r.status_code == 200
    except httpx.HTTPError:
        return False
