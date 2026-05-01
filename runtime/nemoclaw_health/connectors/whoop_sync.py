from __future__ import annotations

import datetime as pydt
import hashlib
import json
from typing import Any

from nemoclaw_health.connectors import whoop_oauth
from nemoclaw_health.connectors.whoop_client import WhoopAPIClient, default_window_iso
from nemoclaw_health.data_entry import DataEntryService, utc_now_iso
from nemoclaw_health.db import (
    Database,
    fetch_connector_state,
    idempotency_seen,
    put_connector_state,
    record_idempotency,
    upsert_whoop_body_measurement_snapshot,
    upsert_whoop_cycle,
    upsert_whoop_profile,
    upsert_whoop_recovery,
    upsert_whoop_sleep,
    upsert_whoop_workout,
)
from nemoclaw_health.settings import Settings

WHOOP_ROWS: list[dict[str, Any]] = [
    {
        "key": "workout",
        "domain": "whoop_workout",
        "hints": ["whoop_id", "sport_id", "start"],
        "event_type": "whoop_workout",
        "fetch_attr": "get_workouts",
    },
    {
        "key": "sleep",
        "domain": "whoop_sleep",
        "hints": ["whoop_id", "start"],
        "event_type": "whoop_sleep",
        "fetch_attr": "get_sleep",
    },
    {
        "key": "recovery",
        "domain": "whoop_recovery",
        "hints": ["whoop_id", "cycle_id"],
        "event_type": "whoop_recovery",
        "fetch_attr": "get_recovery",
    },
    {
        "key": "cycle",
        "domain": "whoop_cycle",
        "hints": ["whoop_id", "cycle_start_time"],
        "event_type": "whoop_cycle",
        "fetch_attr": "get_cycles",
    },
]


def register_who_domains(svc: DataEntryService) -> None:
    for row in WHOOP_ROWS:
        svc.register_domain(row["domain"], row["hints"])
    svc.register_domain("whoop_body_measurement", ["snapshot_hash"])
    svc.register_domain("whoop_profile", ["user_id", "email"])


def normalize_whoop_occurred(keys: tuple[str, ...], record: dict[str, Any]) -> str | None:
    from datetime import timezone

    for k in keys:
        v = record.get(k)
        if not isinstance(v, str):
            continue
        s = v.strip()
        if len(s) < 10:
            continue
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt_obj = pydt.datetime.fromisoformat(s)
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            return dt_obj.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return None


def whoop_stable_id(kind: str, record: dict[str, Any]) -> str | None:
    sid = record.get("id")
    if sid is None:
        sid = record.get("sleep_id")
    if isinstance(sid, str) and sid:
        return f"{kind}:{sid}"
    if sid is not None and not isinstance(sid, dict):
        return f"{kind}:{sid}"
    return None


def _body_measurement_dedupe_key(snapshot: dict[str, Any]) -> str:
    trimmed = dict(sorted(snapshot.items()))
    blob = json.dumps(trimmed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:48]
    return f"whoop_body_measurement:{h}"


def _enrich_activity_record(
    settings: Settings,
    client: WhoopAPIClient,
    kind: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    if not settings.whoop_fetch_activity_detail:
        return record
    if kind == "workout":
        wid = record.get("id")
        if isinstance(wid, str) and wid.strip():
            detail = client.get_workout_by_id(wid)
            if detail:
                return detail
    if kind == "sleep":
        sid = record.get("id")
        if isinstance(sid, str) and sid.strip():
            detail = client.get_sleep_by_id(sid)
            if detail:
                return detail
    return record


def _upsert_normalized_row(
    cur,
    *,
    kind: str,
    payload: dict[str, Any],
    fetched_at: str,
) -> None:
    if kind == "workout":
        upsert_whoop_workout(cur, payload, fetched_at=fetched_at)
    elif kind == "sleep":
        upsert_whoop_sleep(cur, payload, fetched_at=fetched_at)
    elif kind == "recovery":
        upsert_whoop_recovery(cur, payload, fetched_at=fetched_at)
    elif kind == "cycle":
        upsert_whoop_cycle(cur, payload, fetched_at=fetched_at)


def _mark_whoop_sync_attempt(db: Database) -> None:
    at = utc_now_iso()
    with db.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")
        st.setdefault("sync", {})
        st["sync"]["last_attempt_at"] = at
        put_connector_state(cur, "whoop", st)


def _mark_whoop_sync_success(
    db: Database,
    *,
    start_iso: str,
    end_iso: str,
    totals: dict[str, Any],
) -> None:
    with db.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")
        st.setdefault("sync", {})
        st["sync"]["last_success_at"] = utc_now_iso()
        st["sync"]["last_window_start"] = start_iso
        st["sync"]["last_window_end"] = end_iso
        st["sync"]["last_sync_ok"] = True
        st["sync"]["last_error"] = None
        st["sync"]["last_totals"] = totals
        put_connector_state(cur, "whoop", st)


def _mark_whoop_sync_failure(db: Database, message: str) -> None:
    msg = (message or "sync_failed").strip()
    if len(msg) > 800:
        msg = msg[:797] + "..."
    with db.transaction() as cur:
        st = fetch_connector_state(cur, "whoop")
        st.setdefault("sync", {})
        st["sync"]["last_sync_ok"] = False
        st["sync"]["last_error"] = msg
        put_connector_state(cur, "whoop", st)


def sync_whoop(
    db: Database,
    settings: Settings,
    *,
    days: int | None = None,
) -> dict[str, Any]:
    """Pull WHOOP data for sliding window into DataEntry domains + connector idempotency."""
    svc = DataEntryService(settings)
    register_who_domains(svc)

    window_days = days if days is not None else settings.whoop_default_sync_days
    start_iso, end_iso = default_window_iso(window_days)

    _mark_whoop_sync_attempt(db)

    try:
        whoop_oauth.ensure_whoop_access_token(db, settings)
    except Exception as e:
        _mark_whoop_sync_failure(db, str(e))
        raise

    def _token_provider() -> str:
        return whoop_oauth.ensure_whoop_access_token(db, settings)

    try:
        client = WhoopAPIClient(settings, _token_provider)
    except Exception as e:
        _mark_whoop_sync_failure(db, str(e))
        raise

    totals: dict[str, Any] = {
        row["key"]: {"fetched": 0, "ingested": 0, "skipped_duplicate": 0, "skipped_no_id": 0}
        for row in WHOOP_ROWS
    }
    totals["body_measurement"] = {"fetched": 0, "ingested": 0, "skipped_duplicate": 0}
    totals["profile"] = {"fetched": 0, "ingested": 0, "skipped_duplicate": 0}

    fallback_at = utc_now_iso()
    connector = "whoop"
    fetched_at_batch = utc_now_iso()

    try:
        for meta in WHOOP_ROWS:
            fetch = getattr(client, meta["fetch_attr"])
            records = fetch(start=start_iso, end=end_iso)
            totals[meta["key"]]["fetched"] = len(records)

            for record in records:
                if not isinstance(record, dict):
                    continue
                rec = _enrich_activity_record(settings, client, meta["key"], record)
                sid_key = whoop_stable_id(meta["key"], rec)
                if not sid_key:
                    totals[meta["key"]]["skipped_no_id"] += 1
                    continue

                with db.transaction() as cur_n:
                    _upsert_normalized_row(cur_n, kind=meta["key"], payload=rec, fetched_at=fetched_at_batch)

                with db.transaction() as cur_chk:
                    if idempotency_seen(cur_chk, connector, sid_key):
                        totals[meta["key"]]["skipped_duplicate"] += 1
                        continue

                occurred = normalize_whoop_occurred(
                    ("start", "cycle_start_time", "sleep_start_time", "created_at", "end"),
                    rec,
                )
                at_iso = occurred or fallback_at

                payload = dict(rec)
                rid = payload.get("id")
                payload.setdefault("whoop_id", rid if rid is not None and not isinstance(rid, dict) else sid_key)
                provenance = {"connector": "whoop", "whoop_stable_id": sid_key}
                out = svc.ingest(
                    domain=meta["domain"],
                    payload=payload,
                    source="whoop",
                    provenance=provenance,
                    occurred_at=at_iso,
                    committed_raw_event_type=meta["event_type"],
                )
                if out.get("status") == "committed" and isinstance(out.get("raw_event_id"), str):
                    raw_id_val = str(out["raw_event_id"])
                    totals[meta["key"]]["ingested"] += 1
                    with db.transaction() as cur2:
                        record_idempotency(cur2, connector, sid_key, raw_id_val)

        prof = client.get_profile_basic()
        if isinstance(prof, dict) and prof.get("user_id") is not None:
            totals["profile"]["fetched"] = 1
            prof_fetched_at = utc_now_iso()
            with db.transaction() as cur_pf:
                upsert_whoop_profile(cur_pf, prof, fetched_at=prof_fetched_at)
            prof_key = f"whoop_profile:{prof.get('user_id')}"
            with db.transaction() as cur_chk:
                if idempotency_seen(cur_chk, connector, prof_key):
                    totals["profile"]["skipped_duplicate"] += 1
                else:
                    occurred_p = normalize_whoop_occurred(("created_at",), prof) or fallback_at
                    out_p = svc.ingest(
                        domain="whoop_profile",
                        payload=dict(prof),
                        source="whoop",
                        provenance={"connector": "whoop", "dedupe_key": prof_key},
                        occurred_at=occurred_p,
                        committed_raw_event_type="whoop_profile",
                    )
                    if out_p.get("status") == "committed" and isinstance(out_p.get("raw_event_id"), str):
                        totals["profile"]["ingested"] += 1
                        with db.transaction() as cur_pi:
                            record_idempotency(cur_pi, connector, prof_key, str(out_p["raw_event_id"]))

        bm = client.get_body_measurement()
        totals["body_measurement"]["fetched"] = int(bool(bm))
        if bm:
            bm_key = _body_measurement_dedupe_key(bm)
            hash_only = bm_key.split(":", 1)[-1]
            with db.transaction() as cur_snap:
                upsert_whoop_body_measurement_snapshot(
                    cur_snap,
                    snapshot_hash=hash_only,
                    payload=bm,
                    fetched_at=utc_now_iso(),
                )
            with db.transaction() as cur_bm:
                if idempotency_seen(cur_bm, connector, bm_key):
                    totals["body_measurement"]["skipped_duplicate"] += 1
                else:
                    measured_at = normalize_whoop_occurred(("measurement_timestamp",), bm) or fallback_at
                    out_bm = svc.ingest(
                        domain="whoop_body_measurement",
                        payload={"snapshot_hash": hash_only, **bm},
                        source="whoop",
                        provenance={"connector": "whoop", "dedupe_key": bm_key},
                        occurred_at=measured_at,
                        committed_raw_event_type="whoop_body_measurement",
                    )
                    if out_bm.get("status") == "committed" and isinstance(out_bm.get("raw_event_id"), str):
                        totals["body_measurement"]["ingested"] += 1
                        with db.transaction() as cur3:
                            record_idempotency(cur3, connector, bm_key, str(out_bm["raw_event_id"]))
    except Exception as e:
        _mark_whoop_sync_failure(db, str(e))
        raise

    totals["window"] = {"start": start_iso, "end": end_iso}
    _mark_whoop_sync_success(db, start_iso=start_iso, end_iso=end_iso, totals=totals)
    return {"ok": True, "totals": totals}
