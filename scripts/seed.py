#!/usr/bin/env python3
"""Create a few demo classes so you can click around in the Swagger UI.   python scripts/seed.py"""
from datetime import timedelta
import http
from _lib import SCHEDULE, http, now_utc

for i, (title, trainer, cap, price) in enumerate([
        ("HIIT 6 AM", "Coach Arjun", 20, 49900), 
        ("Yoga Flow 7 AM", "Mira", 15, 39900),
        ("Zumba 6 PM", "Rohan", 25, 29900), 
        ("CrossFit 9AM", "Mira", 30, 600000),
        ("MMA 7PM", "Pulkit", 36, 99900),
        ("Strength 8 PM", "Kabir", 10, 59900)]):
    r = http.post(f"{SCHEDULE}/api/v1/sessions", json={"title": title, "trainer": trainer, "capacity": cap, "price_paise": price,"starts_at": (now_utc() + timedelta(days=1, hours=i)).isoformat()})
    print(r.status_code, r.json(), title)
