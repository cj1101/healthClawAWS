"""Session NDJSON logger (debug-acd858). Never log secrets — lengths/bools only."""

from __future__ import annotations

import json
import time
from pathlib import Path


def acd858_log(location: str, message: str, hypothesis_id: str, **data: object) -> None:
    try:
        root = Path(__file__).resolve().parent.parent.parent
        log_path = root / "debug-acd858.log"
        payload = {
            "sessionId": "acd858",
            "timestamp": int(time.time() * 1000),
            "location": location,
            "message": message,
            "hypothesisId": hypothesis_id,
            "data": data,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except OSError:
        pass
