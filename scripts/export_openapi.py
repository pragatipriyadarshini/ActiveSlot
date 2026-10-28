#!/usr/bin/env python3
"""Write each service's OpenAPI spec to docs/openapi/<service>.json (no database needed).
    python scripts/export_openapi.py"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = ROOT / "docs" / "openapi"
out.mkdir(parents=True, exist_ok=True)
service = sys.argv[1] if len(sys.argv) > 1 else None
names = ["schedule-service", "booking-service", "payment-service", "notification-service", "mock-gateway"]

if service is None:  # one fresh interpreter per service: each has its own top-level `app` package
    import subprocess
    for n in names:
        subprocess.run([sys.executable, __file__, n], check=True)
    sys.exit(0)

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/x.db")
os.environ["SERVICE_NAME"] = service
os.environ["ENABLE_NAIVE_DEMO"] = "false"
sys.path.insert(0, str(ROOT / "services" / service))
app = importlib.import_module("app.main").app
(out / f"{service}.json").write_text(json.dumps(app.openapi(), indent=2))
print("wrote", out / f"{service}.json")
