"""Inventory of known Nemoclaw data stores (catalog for dashboard)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nemoclaw_health.health_coach_store import sqlite_tables_with_counts
from nemoclaw_health.settings import Settings


def _mtime_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _stat_file(path: Path) -> tuple[bool, int | None, str | None]:
    try:
        st = path.stat()
        return True, int(st.st_size), _mtime_iso(st.st_mtime)
    except OSError:
        return False, None, None


def _dir_byte_estimate(path: Path, *, max_entries: int = 500) -> tuple[bool, int | None, str | None]:
    if not path.is_dir():
        return False, None, None
    total = 0
    count = 0
    try:
        mtime = path.stat().st_mtime
        for dirpath, _dirnames, filenames in os.walk(path):
            for fn in filenames:
                if count >= max_entries:
                    return True, None, _mtime_iso(mtime)
                fp = Path(dirpath) / fn
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
                count += 1
        return True, total, _mtime_iso(mtime)
    except OSError:
        return True, None, None


@dataclass
class CatalogStore:
    id: str
    title: str
    kind: str
    path: str
    purpose: str
    consumers: str
    exists: bool
    size_bytes: int | None
    mtime_iso: str | None
    wal_exists: bool | None = None
    shm_exists: bool | None = None
    tables: list[dict[str, Any]] = field(default_factory=list)


def build_storage_catalog(settings: Settings, *, include_tables: bool = False) -> dict[str, Any]:
    stores: list[CatalogStore] = []

    nemo = settings.resolved_sqlite()
    ex, sz, mt = _stat_file(nemo)
    wal = nemo.parent / f"{nemo.name}-wal"
    shm = nemo.parent / f"{nemo.name}-shm"
    nem_rows: list[dict[str, Any]] = []
    if include_tables and ex:
        nem_rows = sqlite_tables_with_counts(nemo)
    stores.append(
        CatalogStore(
            id="nemoclaw_sqlite",
            title="Nemoclaw primary SQLite",
            kind="sqlite",
            path=str(nemo.resolve()),
            purpose="Profiles, raw_events, WHOOP mirror tables, delegation, data-entry dynamics",
            consumers="Popeye, connectors, data-entry",
            exists=ex,
            size_bytes=sz,
            mtime_iso=mt,
            wal_exists=wal.is_file(),
            shm_exists=shm.is_file(),
            tables=nem_rows,
        ),
    )

    art = settings.resolved_artifact_log()
    ex_a, sz_a, mt_a = _stat_file(art)
    stores.append(
        CatalogStore(
            id="orchestration_jsonl",
            title="Orchestration artifact log",
            kind="jsonl",
            path=str(art.resolve()),
            purpose="Append-only orchestration / agent artifact stream",
            consumers="HealthOrchestrator",
            exists=ex_a,
            size_bytes=sz_a,
            mtime_iso=mt_a,
        ),
    )

    apple = settings.resolved_apple_imports_dir()
    ex_d, sz_d, mt_d = _dir_byte_estimate(apple)
    stores.append(
        CatalogStore(
            id="apple_imports_dir",
            title="Apple Health import staging",
            kind="directory",
            path=str(apple.resolve()),
            purpose="Temporary uploads for Apple Health export ZIP processing",
            consumers="Apple connector",
            exists=ex_d,
            size_bytes=sz_d,
            mtime_iso=mt_d,
        ),
    )

    health_p = settings.resolved_health_db()
    ex_h, sz_h, mt_h = _stat_file(health_p)
    h_tables: list[dict[str, Any]] = []
    if include_tables and ex_h:
        h_tables = sqlite_tables_with_counts(health_p)
    wal_h = health_p.parent / f"{health_p.name}-wal"
    shm_h = health_p.parent / f"{health_p.name}-shm"
    stores.append(
        CatalogStore(
            id="health_coach_sqlite",
            title="OpenClaw health-coach SQLite (health.db)",
            kind="sqlite",
            path=str(health_p.resolve()),
            purpose="Legacy health-coach biometric samples, meals, sleep, tasks",
            consumers="data-entry (Nemoclaw mirror), vendored CLI workers",
            exists=ex_h,
            size_bytes=sz_h,
            mtime_iso=mt_h,
            wal_exists=wal_h.is_file(),
            shm_exists=shm_h.is_file(),
            tables=h_tables,
        ),
    )

    def _dump(st: CatalogStore) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": st.id,
            "title": st.title,
            "kind": st.kind,
            "path": st.path,
            "purpose": st.purpose,
            "consumers": st.consumers,
            "exists": st.exists,
            "size_bytes": st.size_bytes,
            "mtime_iso": st.mtime_iso,
        }
        if st.kind == "sqlite":
            d["wal_exists"] = st.wal_exists
            d["shm_exists"] = st.shm_exists
            if include_tables:
                d["tables"] = st.tables
        return d

    return {
        "managed_by_agent": "data-entry",
        "stores": [_dump(s) for s in stores],
    }
