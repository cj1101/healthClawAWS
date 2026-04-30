from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SKILL_DIR.parents[1]
RESULTS_DIR = WORKSPACE_ROOT / "workspace" / "agent-network" / "results"


def ensure_results_dir() -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    return RESULTS_DIR


def _result_filename(agent_id: str, ts: int) -> str:
    return f"{agent_id}_{ts}.json"


def write_agent_result(agent_id: str, payload: Dict[str, Any], *, ts: Optional[int] = None) -> Path:
    out_dir = ensure_results_dir()
    unix_ts = int(ts if ts is not None else time.time())
    target = out_dir / _result_filename(agent_id, unix_ts)
    suffix = 1
    while target.exists():
        target = out_dir / f"{agent_id}_{unix_ts}_{suffix}.json"
        suffix += 1

    envelope = {
        "agent_id": agent_id,
        "timestamp": unix_ts,
        "payload": payload,
    }
    target.write_text(json.dumps(envelope, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def list_agent_results(
    *,
    agent_id: Optional[str] = None,
    since_ts: Optional[int] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    out_dir = ensure_results_dir()
    entries: List[Dict[str, Any]] = []
    for path in sorted(out_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if limit and len(entries) >= limit:
            break
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record_agent = str(payload.get("agent_id", ""))
            record_ts = int(payload.get("timestamp", 0))
        except Exception:
            continue
        if agent_id and record_agent != agent_id:
            continue
        if since_ts is not None and record_ts < since_ts:
            continue
        entries.append(
            {
                "path": str(path),
                "agent_id": record_agent,
                "timestamp": record_ts,
                "payload": payload.get("payload", {}),
            }
        )
    return entries
