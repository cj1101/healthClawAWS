from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from models import DailySnapshot

SKILL_DIR = Path(__file__).resolve().parent
DATA_DIR = SKILL_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
STORE_FILE = DATA_DIR / "health_store.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _blank_store() -> Dict[str, Any]:
    return {
        "version": 1,
        "updated_at": _now_iso(),
        "snapshots": {},
        "raw": {},
        "push_state": {},
    }


def ensure_store() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if not STORE_FILE.exists():
        STORE_FILE.write_text(json.dumps(_blank_store(), indent=2, ensure_ascii=False), encoding="utf-8")


def load_store() -> Dict[str, Any]:
    ensure_store()
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _blank_store()


def save_store(store: Dict[str, Any]) -> None:
    ensure_store()
    store["updated_at"] = _now_iso()
    STORE_FILE.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")


def upsert_snapshot(snapshot: DailySnapshot) -> None:
    store = load_store()
    store.setdefault("snapshots", {})
    store["snapshots"][snapshot.date] = snapshot.to_dict()
    save_store(store)


def get_snapshot(date_str: str) -> Optional[DailySnapshot]:
    store = load_store()
    payload = store.get("snapshots", {}).get(date_str)
    if not payload:
        return None
    return DailySnapshot.from_dict(payload)


def list_snapshots(limit: int = 14) -> List[DailySnapshot]:
    store = load_store()
    items = list(store.get("snapshots", {}).items())
    items.sort(key=lambda x: x[0], reverse=True)
    return [DailySnapshot.from_dict(p) for _, p in items[:limit]]


def write_raw_payload(key: str, payload: Dict[str, Any]) -> Path:
    ensure_store()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = key.replace("/", "_")
    path = RAW_DIR / f"{ts}_{safe}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    store = load_store()
    store.setdefault("raw", {})
    store["raw"][key] = str(path)
    save_store(store)
    return path


def get_push_state() -> Dict[str, Any]:
    return load_store().get("push_state", {}) or {}


def set_push_marker(marker: str, value: str) -> None:
    store = load_store()
    store.setdefault("push_state", {})
    store["push_state"][marker] = value
    save_store(store)
