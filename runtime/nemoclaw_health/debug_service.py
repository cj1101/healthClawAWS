"""Read-only debug / session trace helpers (no LLM)."""

from __future__ import annotations

import json
from typing import Any

from nemoclaw_health.connectors.whoop_oauth import oauth_status_from_state
from nemoclaw_health.db import Database, fetch_connector_state
from nemoclaw_health.settings import Settings


def recent_sessions(db: Database, *, limit: int = 50) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 200))
    with db.transaction() as cur:
        rows = cur.execute(
            """
            SELECT task_id, MAX(recorded_at) AS last_at, COUNT(*) AS n_ev
            FROM delegation_events
            GROUP BY task_id
            ORDER BY last_at DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "task_id": r[0],
                "last_recorded_at": r[1],
                "delegation_event_count": int(r[2]),
            },
        )
    return out


def session_trace(db: Database, task_id: str) -> dict[str, Any]:
    tid = (task_id or "").strip()
    if not tid:
        return {"task_id": "", "delegation_events": [], "agent_runs": [], "disclaimer_audit": []}
    with db.transaction() as cur:
        de_rows = cur.execute(
            """
            SELECT id, task_id, recorded_at, event_json
            FROM delegation_events
            WHERE task_id = ?
            ORDER BY recorded_at ASC, id ASC
            """,
            (tid,),
        ).fetchall()
        ar_rows = cur.execute(
            """
            SELECT id, task_id, source_agent, started_at, payload_json
            FROM agent_runs
            WHERE task_id = ?
            ORDER BY started_at ASC, id ASC
            """,
            (tid,),
        ).fetchall()
        da_rows = cur.execute(
            """
            SELECT id, task_id, tier, templates_applied_json, recorded_at
            FROM disclaimer_audit
            WHERE task_id = ?
            ORDER BY recorded_at ASC, id ASC
            """,
            (tid,),
        ).fetchall()

    de: list[dict[str, Any]] = []
    for r in de_rows:
        d = dict(r)
        ej = d.get("event_json")
        ev = None
        if isinstance(ej, str):
            try:
                ev = json.loads(ej)
            except json.JSONDecodeError:
                ev = {"_parse_error": True}
        de.append(
            {
                "id": d.get("id"),
                "task_id": d.get("task_id"),
                "recorded_at": d.get("recorded_at"),
                "event": ev,
            },
        )

    ar: list[dict[str, Any]] = []
    for r in ar_rows:
        d = dict(r)
        pj = d.get("payload_json")
        pl = None
        if isinstance(pj, str):
            try:
                pl = json.loads(pj)
            except json.JSONDecodeError:
                pl = {"_parse_error": True}
        ar.append(
            {
                "id": d.get("id"),
                "task_id": d.get("task_id"),
                "source_agent": d.get("source_agent"),
                "started_at": d.get("started_at"),
                "payload": pl,
            },
        )

    da: list[dict[str, Any]] = []
    for r in da_rows:
        d = dict(r)
        tj = d.get("templates_applied_json")
        ta = None
        if isinstance(tj, str):
            try:
                ta = json.loads(tj)
            except json.JSONDecodeError:
                ta = None
        da.append(
            {
                "id": d.get("id"),
                "task_id": d.get("task_id"),
                "tier": d.get("tier"),
                "templates_applied": ta,
                "recorded_at": d.get("recorded_at"),
            },
        )

    return {
        "task_id": tid,
        "delegation_events": de,
        "agent_runs": ar,
        "disclaimer_audit": da,
    }


def analyze_environment(db: Database, settings: Settings) -> list[dict[str, Any]]:
    """Deterministic checks only (no writes)."""
    findings: list[dict[str, Any]] = []

    if not settings.openrouter_api_key:
        findings.append(
            {
                "severity": "warning",
                "code": "OPENROUTER_KEY_MISSING",
                "message": "OpenRouter API key unset — LLM routing/workers fall back to stubs.",
                "hint": "Set NEMOWLAW_OPENROUTER_API_KEY for full agent quality.",
            },
        )

    with db.transaction() as cur:
        clar = cur.execute(
            "SELECT COUNT(*) FROM raw_events WHERE event_type = ?",
            ("data_entry_clarification_pending",),
        ).fetchone()
        n_clar = int(clar[0]) if clar else 0
        w_blob = fetch_connector_state(cur, "whoop")

    if n_clar >= 5:
        findings.append(
            {
                "severity": "info",
                "code": "CLARIFICATION_BACKLOG",
                "message": f"{n_clar} clarification-pending data-entry events in raw_events.",
                "hint": "Review /v1/data/clarifications or complete pending logs in the dashboard.",
            },
        )

    st_whoop = oauth_status_from_state(w_blob)
    if st_whoop.get("connected") and st_whoop.get("expired"):
        findings.append(
            {
                "severity": "warning",
                "code": "WHOOP_TOKEN_EXPIRED",
                "message": "WHOOP access token expired — sync may fail until refresh succeeds.",
                "hint": "Trigger sync or reconnect WHOOP from integrations.",
            },
        )
    if st_whoop.get("last_sync_ok") is False and st_whoop.get("last_error"):
        findings.append(
            {
                "severity": "warning",
                "code": "WHOOP_LAST_SYNC_FAILED",
                "message": st_whoop.get("last_error") or "Last WHOOP sync failed.",
                "hint": "Check API status and WHOOP developer app configuration.",
            },
        )

    return findings


def analyze_task_trace(db: Database, settings: Settings, task_id: str) -> dict[str, Any]:
    env = analyze_environment(db, settings)
    trace = session_trace(db, task_id)
    findings = list(env)

    if not trace["delegation_events"]:
        findings.append(
            {
                "severity": "error",
                "code": "NO_DELEGATION_EVENTS",
                "message": f"No delegation_events rows for task_id {task_id!r}.",
                "hint": "Task id may be wrong or pruning removed history.",
            },
        )
    else:
        presented = False
        for row in trace["delegation_events"]:
            ev = row.get("event") or {}
            acts = ev.get("actions") or []
            if isinstance(acts, list) and any(
                isinstance(a, dict) and a.get("type") == "present_to_user" for a in acts
            ):
                presented = True
                break
        if not presented:
            findings.append(
                {
                    "severity": "warning",
                    "code": "NO_PRESENT_TO_USER",
                    "message": "Trace has no present_to_user action — response may be incomplete.",
                    "hint": "Inspect orchestration logs and worker failures.",
                },
            )

        if len(trace["delegation_events"]) > 30:
            findings.append(
                {
                    "severity": "info",
                    "code": "LARGE_TRACE",
                    "message": f"Unusually long delegation chain ({len(trace['delegation_events'])} events).",
                    "hint": "May indicate retry loops or excessive delegation.",
                },
            )

    sev_rank = {"error": 3, "warning": 2, "info": 1}
    findings.sort(key=lambda f: sev_rank.get(str(f.get("severity")), 0), reverse=True)

    return {"task_id": task_id, "findings": findings, "trace": trace}
