#!/usr/bin/env python3
"""
Goal Manager — Manager-driven goal proposal lifecycle.

Role contract (Manager only):
  - No goal is activated without EXPLICIT user approval.
  - "Approve", "Reject", "Modify" are the only valid transitions from 'pending'.
  - The Manager calls propose/list/status. Users drive approve/reject/modify.

CLI commands:
    python goal_manager.py propose --title "..." --description "..." --rationale "..."
    python goal_manager.py list [--status pending|approved|rejected|modified|all]
    python goal_manager.py approve <id> [--response "notes"]
    python goal_manager.py reject  <id> [--response "notes"]
    python goal_manager.py modify  <id> --modification "new plan text" [--response "notes"]
    python goal_manager.py status  <id>
    python goal_manager.py morning_synthesis [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_DIR = Path(__file__).resolve().parent


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_thresholds() -> Dict[str, Any]:
    p = SKILL_DIR / "thresholds.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Proposal lifecycle
# ---------------------------------------------------------------------------

def cmd_propose(args: argparse.Namespace) -> int:
    from health_db import insert_goal_proposal
    pid = insert_goal_proposal(args.title, args.description, args.rationale)
    result = {
        "ok": True,
        "proposal_id": pid,
        "status": "pending",
        "message": (
            f"Goal proposal #{pid} created: \"{args.title}\"\n"
            "Reply with: Approve {id} | Reject {id} | Modify {id} <new plan>"
        ),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from health_db import get_all_proposals, get_pending_proposals
    status_filter = args.status if args.status != "all" else None
    if status_filter == "pending":
        proposals = get_pending_proposals()
    else:
        proposals = get_all_proposals(limit=args.limit)
        if status_filter:
            proposals = [p for p in proposals if p["status"] == status_filter]
    print(json.dumps({"proposals": proposals, "count": len(proposals)}, ensure_ascii=False))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    from health_db import get_proposal, update_goal_proposal_status
    prop = get_proposal(args.id)
    if not prop:
        print(json.dumps({"error": f"Proposal #{args.id} not found"}))
        return 1
    if prop["status"] != "pending":
        print(json.dumps({"error": f"Proposal #{args.id} is not pending (current: {prop['status']})"}))
        return 1
    update_goal_proposal_status(args.id, "approved", user_response=args.response)
    print(json.dumps({
        "ok": True,
        "proposal_id": args.id,
        "status": "approved",
        "title": prop["title"],
        "message": f"Goal approved and activated: \"{prop['title']}\"",
    }, ensure_ascii=False))
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    from health_db import get_proposal, update_goal_proposal_status
    prop = get_proposal(args.id)
    if not prop:
        print(json.dumps({"error": f"Proposal #{args.id} not found"}))
        return 1
    if prop["status"] != "pending":
        print(json.dumps({"error": f"Proposal #{args.id} is not pending (current: {prop['status']})"}))
        return 1
    update_goal_proposal_status(args.id, "rejected", user_response=args.response)
    print(json.dumps({
        "ok": True,
        "proposal_id": args.id,
        "status": "rejected",
        "title": prop["title"],
        "message": f"Goal rejected: \"{prop['title']}\"",
    }, ensure_ascii=False))
    return 0


def cmd_modify(args: argparse.Namespace) -> int:
    from health_db import get_proposal, update_goal_proposal_status
    prop = get_proposal(args.id)
    if not prop:
        print(json.dumps({"error": f"Proposal #{args.id} not found"}))
        return 1
    update_goal_proposal_status(
        args.id,
        "modified",
        user_response=args.response,
        modification_text=args.modification,
    )
    print(json.dumps({
        "ok": True,
        "proposal_id": args.id,
        "status": "modified",
        "title": prop["title"],
        "modification": args.modification,
        "message": (
            f"Goal #{args.id} modified. The Manager will revise the plan based on: \"{args.modification}\""
        ),
    }, ensure_ascii=False))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from health_db import get_proposal
    prop = get_proposal(args.id)
    if not prop:
        print(json.dumps({"error": f"Proposal #{args.id} not found"}))
        return 1
    print(json.dumps(prop, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# Morning synthesis (Manager's pre-check-in report)
# ---------------------------------------------------------------------------

def morning_synthesis(date_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Synthesize all health streams for the Manager's morning check-in.

    Runs:
      - WHOOP biometrics read
      - Nurse risk assessment
      - Nutritionist analysis
      - Pending goal proposals
      - Active qualitative modifiers
      - Latest weather + AQI

    Returns a structured payload for the Manager to interpret and act on.
    """
    date = date_str or _today()
    result: Dict[str, Any] = {"date": date}

    # Biometrics
    try:
        from health_db import get_biometrics
        bio = get_biometrics(date)
        result["biometrics"] = bio
    except Exception as exc:
        result["biometrics_error"] = str(exc)

    # Nurse risk
    try:
        from nurse_engine import calculate_injury_risk
        risk = calculate_injury_risk(date)
        result["injury_risk"] = risk
    except Exception as exc:
        result["injury_risk_error"] = str(exc)

    # Nutritionist (yesterday's analysis — today's meals not yet logged)
    from datetime import timedelta
    yesterday = (datetime.fromisoformat(date) - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        from nutritionist_engine import analyze_nutrient_timing
        nutrition = analyze_nutrient_timing(yesterday)
        result["nutrition_yesterday"] = nutrition
    except Exception as exc:
        result["nutrition_error"] = str(exc)

    # Pending proposals
    try:
        from health_db import get_pending_proposals
        pending = get_pending_proposals()
        result["pending_proposals"] = pending
    except Exception as exc:
        result["pending_proposals_error"] = str(exc)

    # Active qualitative modifiers
    try:
        from health_db import get_active_modifiers
        modifiers = get_active_modifiers(date, days_back=3)
        result["active_modifiers"] = modifiers
    except Exception as exc:
        result["active_modifiers_error"] = str(exc)

    # Weather + AQI
    try:
        from health_db import get_latest_aqi, get_latest_weather
        result["weather"] = get_latest_weather(date)
        result["aqi"] = get_latest_aqi(date)
    except Exception as exc:
        result["weather_error"] = str(exc)

    # Allostatic load
    try:
        from health_db import get_allostatic_score
        result["allostatic_load"] = get_allostatic_score(date)
    except Exception as exc:
        result["allostatic_error"] = str(exc)

    # Manager action flags
    flags: List[str] = []
    risk_data = result.get("injury_risk", {})
    if risk_data.get("category") in ("high",):
        flags.append("FORCE_RECOVERY_PROPOSAL: injury risk is high — propose recovery goal now")
    elif risk_data.get("category") == "moderate":
        flags.append("SUGGEST_DELOAD: moderate risk detected — flag at next check-in")

    nutrition_data = result.get("nutrition_yesterday", {})
    if nutrition_data.get("flags_count", 0) > 0:
        flags.append(f"NUTRITION_ALERTS: {nutrition_data['flags_count']} nutrition threshold breach(es) from yesterday")

    if result.get("pending_proposals"):
        flags.append(f"PENDING_PROPOSALS: {len(result['pending_proposals'])} goal proposal(s) awaiting user decision")

    thr = _load_thresholds().get("manager", {})
    green_streak = int(thr.get("green_streak_notify_days", 3))
    try:
        from health_db import get_biometrics_range
        from datetime import timedelta as td
        start = (datetime.fromisoformat(date) - td(days=green_streak)).strftime("%Y-%m-%d")
        recent_bio = get_biometrics_range(start, date)
        green_days = sum(
            1 for b in recent_bio
            if b.get("recovery_score") and float(b["recovery_score"]) >= 67
        )
        if green_days >= green_streak:
            flags.append(
                f"GREEN_STREAK: {green_days} consecutive green recovery days — "
                "good time for a qualitative check-in on training feel"
            )
    except Exception:
        pass

    result["manager_flags"] = flags
    return result


def cmd_morning_synthesis(args: argparse.Namespace) -> int:
    result = morning_synthesis(getattr(args, "date", None))
    print(json.dumps(result, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# Qualitative note ingestion
# ---------------------------------------------------------------------------

def cmd_note(args: argparse.Namespace) -> int:
    """Ingest a qualitative physical condition note."""
    from db_ingest import ingest_qualitative_note
    result = ingest_qualitative_note(args.text)
    result["ok"] = True
    result["text"] = args.text
    print(json.dumps(result, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Goal Manager — goal lifecycle + morning synthesis")
    sub = parser.add_subparsers(dest="command", required=True)

    p_propose = sub.add_parser("propose", help="Create a new goal proposal")
    p_propose.add_argument("--title", required=True)
    p_propose.add_argument("--description", default="")
    p_propose.add_argument("--rationale", default="")
    p_propose.set_defaults(func=cmd_propose)

    p_list = sub.add_parser("list", help="List goal proposals")
    p_list.add_argument("--status", default="all", choices=["pending", "approved", "rejected", "modified", "all"])
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_approve = sub.add_parser("approve", help="Approve a pending proposal")
    p_approve.add_argument("id", type=int)
    p_approve.add_argument("--response", default=None)
    p_approve.set_defaults(func=cmd_approve)

    p_reject = sub.add_parser("reject", help="Reject a pending proposal")
    p_reject.add_argument("id", type=int)
    p_reject.add_argument("--response", default=None)
    p_reject.set_defaults(func=cmd_reject)

    p_modify = sub.add_parser("modify", help="Modify a proposal (keep pending, add direction)")
    p_modify.add_argument("id", type=int)
    p_modify.add_argument("--modification", required=True, help="New direction / changes requested")
    p_modify.add_argument("--response", default=None)
    p_modify.set_defaults(func=cmd_modify)

    p_status = sub.add_parser("status", help="Show a single proposal")
    p_status.add_argument("id", type=int)
    p_status.set_defaults(func=cmd_status)

    p_synth = sub.add_parser("morning_synthesis", help="Run morning synthesis for the Manager")
    p_synth.add_argument("--date", default=None)
    p_synth.set_defaults(func=cmd_morning_synthesis)

    p_note = sub.add_parser("note", help="Ingest a qualitative physical condition note")
    p_note.add_argument("text", help="Free-form note e.g. \"fingers feel tweaky\"")
    p_note.set_defaults(func=cmd_note)

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
