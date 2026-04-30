#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import threading
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from coaching_engine import build_coaching_output
from health_store import (
    get_push_state,
    get_snapshot,
    list_snapshots,
    set_push_marker,
    upsert_snapshot,
    write_raw_payload,
)
from macro_adapter import get_macro_day
from models import DailySnapshot, ScheduleDay, WhoopDay
from nurse_engine import calculate_injury_risk
from nutritionist_engine import analyze_nutrient_timing
from result_store import list_agent_results, write_agent_result
from schedule_adapter import load_schedule_day
from trainer_engine import build_training_plan
from whoop_client import WhoopClient, default_window
from whoop_oauth import (
    auth_status,
    build_auth_start_url,
    exchange_code_for_token,
    refresh_access_token,
)
from db_ingest import ingest_schedule_day, ingest_whoop_day, ingest_whoop_sleep_records

SKILL_DIR = Path(__file__).resolve().parent
SKILLS_ROOT_DIR = SKILL_DIR.parent
load_dotenv(dotenv_path=SKILL_DIR / ".env")
load_dotenv(dotenv_path=SKILLS_ROOT_DIR / ".env")
load_dotenv()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _date_only(value: str) -> str:
    if not value:
        return ""
    return value[:10]


def _avg(values: List[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _sum_workout_metrics(records: List[Dict]) -> Dict[str, float]:
    kjs: List[float] = []
    strains: List[float] = []
    for rec in records:
        score = rec.get("score") or {}
        if rec.get("score_state") != "SCORED":
            continue
        kj = score.get("kilojoule")
        if isinstance(kj, (int, float)):
            kjs.append(float(kj))
        strain = score.get("strain")
        if isinstance(strain, (int, float)):
            strains.append(float(strain))
    total_kj = sum(kjs)
    return {
        "workout_kj": round(total_kj, 2),
        "workout_kcal": round(total_kj / 4.184, 2),
        "workout_count": len(kjs),
        "avg_strain": round(_avg(strains), 2),
    }


def _aggregate_whoop_day(client: WhoopClient, date_str: str) -> WhoopDay:
    day_start = datetime.fromisoformat(f"{date_str}T00:00:00+00:00")
    day_end = day_start + timedelta(days=1)
    start = day_start.isoformat().replace("+00:00", "Z")
    end = day_end.isoformat().replace("+00:00", "Z")

    workouts = client.get_workouts(start=start, end=end)
    sleep = client.get_sleep(start=start, end=end)
    recovery = client.get_recovery(start=start, end=end)
    body = client.get_body_measurement()

    write_raw_payload(
        f"whoop_day_{date_str}",
        {"workouts": workouts, "sleep": sleep, "recovery": recovery, "body": body},
    )

    workout_stats = _sum_workout_metrics(workouts)
    whoop_day = WhoopDay(date=date_str, **workout_stats)

    # Ingest detailed sleep records for sleep-cycle tracking
    if sleep:
        try:
            ingest_whoop_sleep_records(sleep)
        except Exception:
            pass

    if recovery:
        rec = recovery[0]
        score = rec.get("score") or {}
        if isinstance(score.get("recovery_score"), (float, int)):
            whoop_day.recovery_score = float(score["recovery_score"])
        if isinstance(score.get("resting_heart_rate"), (float, int)):
            whoop_day.resting_hr = float(score["resting_heart_rate"])
        if isinstance(score.get("hrv_rmssd_milli"), (float, int)):
            whoop_day.hrv_rmssd_milli = float(score["hrv_rmssd_milli"])

    if sleep:
        sl = sleep[0]
        score = sl.get("score") or {}
        stage = score.get("stage_summary") or {}
        in_bed = stage.get("total_in_bed_time_milli")
        if isinstance(in_bed, (int, float)):
            whoop_day.sleep_hours = round(float(in_bed) / 3600000.0, 2)
        perf = score.get("sleep_performance_percentage")
        if isinstance(perf, (int, float)):
            whoop_day.sleep_performance_pct = float(perf)

    if body:
        if isinstance(body.get("weight_kilogram"), (int, float)):
            whoop_day.body_weight_kg = float(body["weight_kilogram"])
        if isinstance(body.get("height_meter"), (int, float)):
            whoop_day.body_height_m = float(body["height_meter"])
        if isinstance(body.get("max_heart_rate"), (int, float)):
            whoop_day.max_heart_rate = int(body["max_heart_rate"])
    return whoop_day


def build_snapshot(date_str: str, skip_whoop: bool = False) -> DailySnapshot:
    macros = get_macro_day(date_str)
    schedule = load_schedule_day(date_str)
    whoop = WhoopDay(date=date_str)
    if not skip_whoop:
        whoop = _aggregate_whoop_day(WhoopClient(), date_str)
    snapshot = DailySnapshot(date=date_str, macros=macros, whoop=whoop, schedule=schedule)
    upsert_snapshot(snapshot)

    # Dual-write into SQLite for cross-stream analytics
    try:
        ingest_whoop_day(whoop)
        ingest_schedule_day(schedule)
    except Exception:
        pass

    return snapshot


def sync_all(days: int = 7, skip_whoop: bool = False) -> Dict[str, object]:
    if skip_whoop:
        snapshot = build_snapshot(_today(), skip_whoop=True)
        return {"synced_dates": [snapshot.date], "whoop_skipped": True}

    window = default_window(days)
    client = WhoopClient()
    workouts = client.get_workouts(start=window["start"], end=window["end"])
    write_raw_payload("whoop_sync_workouts", {"records": workouts, "window": window})

    # Build today's full snapshot after broad sync pull.
    snapshot = build_snapshot(_today(), skip_whoop=False)
    return {"synced_dates": [snapshot.date], "window": window, "workout_records": len(workouts)}


def _format_coaching(snapshot: DailySnapshot) -> Dict[str, object]:
    output = build_coaching_output(snapshot).to_dict()
    return {
        "date": output["date"],
        "status": {
            "readiness_score": output["readiness_score"],
            "readiness_band": output["readiness_band"],
            "training_intent": output["training_intent"],
            "confidence": output["confidence"],
        },
        "recommendation": {
            "modality": output["recommended_modality"],
            "nutrition": output["nutrition_adjustment"],
            "schedule": output["schedule_advice"],
            "fallback": output["fallback_option"],
        },
        "rationale": output["rationale"],
    }


def _load_or_sync_today(skip_whoop: bool = False) -> DailySnapshot:
    snap = get_snapshot(_today())
    if snap is not None:
        return snap
    return build_snapshot(_today(), skip_whoop=skip_whoop)


def _dedupe_window(name: str, marker_value: str) -> bool:
    current = get_push_state().get(name)
    if current == marker_value:
        return False
    set_push_marker(name, marker_value)
    return True


def cmd_morning_brief(args: argparse.Namespace) -> int:
    if args.proactive and not _dedupe_window("morning_brief", _today()):
        print("NOOP: morning brief already sent today")
        return 0
    snapshot = _load_or_sync_today(skip_whoop=args.skip_whoop)
    print(json.dumps(_format_coaching(snapshot), ensure_ascii=False))
    return 0


def cmd_midday_adjust(args: argparse.Namespace) -> int:
    marker = f"{_today()}-midday"
    if args.proactive and not _dedupe_window("midday_adjust", marker):
        print("NOOP: midday adjust already sent for this window")
        return 0
    snapshot = _load_or_sync_today(skip_whoop=args.skip_whoop)
    payload = _format_coaching(snapshot)
    payload["mode"] = "midday_adjust"
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_evening_review(args: argparse.Namespace) -> int:
    marker = f"{_today()}-evening"
    if args.proactive and not _dedupe_window("evening_review", marker):
        print("NOOP: evening review already sent for this window")
        return 0
    snapshot = _load_or_sync_today(skip_whoop=args.skip_whoop)
    payload = _format_coaching(snapshot)
    payload["mode"] = "evening_review"
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_weekly_review(_: argparse.Namespace) -> int:
    snaps = list_snapshots(limit=7)
    if not snaps:
        print(json.dumps({"error": "No snapshots available; run sync_all first."}))
        return 1
    avg_readiness = _avg([build_coaching_output(s).readiness_score for s in snaps])
    total_kcal_burn = sum(s.whoop.workout_kcal for s in snaps)
    avg_calories = _avg([s.macros.calories for s in snaps])
    payload = {
        "range_days": len(snaps),
        "avg_readiness": round(avg_readiness, 1),
        "avg_intake_calories": round(avg_calories, 1),
        "total_workout_kcal_burn": round(total_kcal_burn, 1),
        "note": "Use 2-4 week trends before major target adjustments.",
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    question = args.question.lower()
    snapshot = _load_or_sync_today(skip_whoop=args.skip_whoop)
    coaching = _format_coaching(snapshot)
    if "train" in question or "workout" in question:
        print(json.dumps({"answer": coaching["recommendation"]["modality"], "context": coaching["status"]}, ensure_ascii=False))
        return 0
    if "eat" in question or "macro" in question or "calorie" in question:
        print(
            json.dumps(
                {
                    "answer": coaching["recommendation"]["nutrition"],
                    "intake_today": {
                        "calories": snapshot.macros.calories,
                        "protein_g": snapshot.macros.protein_g,
                        "carbs_g": snapshot.macros.carbs_g,
                        "fats_g": snapshot.macros.fats_g,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(json.dumps({"answer": coaching, "note": "General health-coach context returned."}, ensure_ascii=False))
    return 0


def cmd_sync_all(args: argparse.Namespace) -> int:
    result = sync_all(days=args.days, skip_whoop=args.skip_whoop)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _human_activity_payload(snapshot: DailySnapshot) -> Dict[str, object]:
    coaching = _format_coaching(snapshot)
    return {
        "date": snapshot.date,
        "source": "popeye",
        "readiness_score": coaching["status"]["readiness_score"],
        "readiness_band": coaching["status"]["readiness_band"],
        "training_intent": coaching["status"]["training_intent"],
        "confidence": coaching["status"]["confidence"],
        "whoop": snapshot.whoop.__dict__,
        "schedule": snapshot.schedule.__dict__,
        "macros": snapshot.macros.__dict__,
        "recommendation": coaching["recommendation"],
    }


def _classify_targets(message: str) -> List[str]:
    m = (message or "").lower()
    targets: List[str] = []
    nutrition_kw = ["nutrition", "food", "meal", "macro", "protein", "carb", "calorie", "diet"]
    training_kw = ["workout", "training", "bike", "cycling", "yoga", "climb", "boulder", "sport", "exercise"]
    unwell_kw = ["unwell", "sick", "ill", "pain", "hurt", "injury", "nausea", "dizzy", "fever", "tired"]

    if any(k in m for k in nutrition_kw):
        targets.append("stan")
    if any(k in m for k in training_kw):
        targets.append("dick")
    if any(k in m for k in unwell_kw):
        targets.append("joy")
    return targets


def cmd_popeye_summarize_activity(args: argparse.Namespace) -> int:
    date = args.date or _today()
    if date == _today():
        snapshot = _load_or_sync_today(skip_whoop=args.skip_whoop)
    else:
        snapshot = get_snapshot(date) or build_snapshot(date, skip_whoop=args.skip_whoop)
    payload = {
        "agent_id": "popeye",
        "role": "health_manager",
        "type": "human_activity_summary",
        "activity": _human_activity_payload(snapshot),
    }
    if args.emit_result:
        path = write_agent_result("popeye", payload)
        payload["result_path"] = str(path)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_popeye_delegate(args: argparse.Namespace) -> int:
    snapshot = _load_or_sync_today(skip_whoop=args.skip_whoop)
    activity = _human_activity_payload(snapshot)
    targets = _classify_targets(args.message)
    delegations: List[Dict[str, object]] = []

    if "stan" in targets:
        stan_payload = analyze_nutrient_timing(activity["date"])
        stan_payload["delegated_by"] = "popeye"
        stan_payload["trigger_message"] = args.message
        stan_path = write_agent_result("stan", stan_payload)
        delegations.append({"target": "stan", "result_path": str(stan_path), "alerts": stan_payload.get("alerts", [])})

        if bool(stan_payload.get("notify_joy")) and "joy" not in targets:
            targets.append("joy")

    if "dick" in targets:
        plan = build_training_plan(args.message, activity=activity, date_str=activity["date"])
        dick_payload = {
            "agent_id": "dick",
            "role": "trainer",
            "delegated_by": "popeye",
            "trigger_message": args.message,
            "activity": activity,
            "plan": plan,
        }
        dick_path = write_agent_result("dick", dick_payload)
        delegations.append({"target": "dick", "result_path": str(dick_path), "intensity": plan.get("intensity")})

    if "joy" in targets:
        joy_payload = calculate_injury_risk(activity["date"])
        joy_payload["delegated_by"] = "popeye"
        joy_payload["trigger_message"] = args.message
        joy_path = write_agent_result("joy", joy_payload)
        delegations.append({"target": "joy", "result_path": str(joy_path), "category": joy_payload.get("category")})

    output = {
        "agent_id": "popeye",
        "role": "health_manager",
        "message": args.message,
        "delegations": delegations,
        "targets": targets,
        "activity_date": activity["date"],
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0


def cmd_popeye_synthesize_results(args: argparse.Namespace) -> int:
    push_state = get_push_state()
    since_ts = args.since_ts
    if args.incremental and since_ts is None:
        marker = push_state.get("popeye_results_last_ts")
        try:
            since_ts = int(marker) if marker else None
        except Exception:
            since_ts = None

    records = list_agent_results(since_ts=since_ts, limit=args.limit)
    worker_records = [r for r in records if r.get("agent_id") in {"stan", "dick", "joy"}]
    latest_by_agent: Dict[str, Dict[str, object]] = {}
    max_ts = since_ts or 0
    for record in worker_records:
        agent = str(record.get("agent_id", ""))
        ts = int(record.get("timestamp") or 0)
        if ts > max_ts:
            max_ts = ts
        if agent not in latest_by_agent or ts >= int(latest_by_agent[agent]["timestamp"]):
            latest_by_agent[agent] = record

    high_priority_alerts: List[str] = []
    for record in worker_records:
        payload = record.get("payload") or {}
        if record.get("agent_id") == "stan":
            for alert in payload.get("alerts", []):
                if any(k in str(alert).lower() for k in ["baseline deviation", "goal contradiction"]):
                    high_priority_alerts.append(f"stan: {alert}")
        if record.get("agent_id") == "joy":
            if payload.get("category") in {"high", "critical"}:
                high_priority_alerts.append(
                    f"joy: risk category {payload.get('category')} (score {payload.get('risk_score')})"
                )

    synthesis = {
        "agent_id": "popeye",
        "role": "health_manager",
        "type": "results_synthesis",
        "records_considered": len(worker_records),
        "latest_by_agent": latest_by_agent,
        "high_priority_alerts": high_priority_alerts,
        "next_actions": [
            "Escalate high-priority health alerts to Koda.",
            "Continue hierarchical routing through Popeye only.",
        ],
    }

    synthesis_path = write_agent_result("popeye", synthesis) if args.emit_result else None
    if synthesis_path:
        synthesis["result_path"] = str(synthesis_path)
    if args.incremental and max_ts:
        set_push_marker("popeye_results_last_ts", str(max_ts))
    print(json.dumps(synthesis, ensure_ascii=False))
    return 0


def cmd_auth_start(_: argparse.Namespace) -> int:
    print(json.dumps({"auth_url": build_auth_start_url()}, ensure_ascii=False))
    return 0


def cmd_auth_finish(args: argparse.Namespace) -> int:
    token = exchange_code_for_token(args.code, args.state)
    print(json.dumps({"ok": True, "expires_at": token.get("expires_at")}, ensure_ascii=False))
    return 0


def cmd_auth_status(_: argparse.Namespace) -> int:
    print(json.dumps(auth_status(), ensure_ascii=False))
    return 0


def cmd_auth_refresh(_: argparse.Namespace) -> int:
    token = refresh_access_token()
    print(json.dumps({"ok": True, "expires_at": token.get("expires_at")}, ensure_ascii=False))
    return 0


def cmd_auth_listen(args: argparse.Namespace) -> int:
    """
    Start a minimal local HTTP server for WHOOP OAuth redirect (default http://localhost:8765/callback).
    Exchanges code for token and then exits.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a: object) -> None:
            # Silence default stdout logging
            return

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                if parsed.path != args.path:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Not Found")
                    return

                qs = parse_qs(parsed.query or "")
                code = (qs.get("code") or [""])[0]
                state = (qs.get("state") or [""])[0]
                err = (qs.get("error") or [""])[0]
                if err:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<h2>WHOOP authorization failed</h2>")
                    return
                if not code:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<h2>Missing code</h2>")
                    return

                try:
                    token = exchange_code_for_token(code, state or None)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b"<h2>WHOOP connected.</h2><p>You can close this tab and return to Mission Control.</p>"
                    )
                except Exception as exc:
                    self.send_response(500)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(b"<h2>Token exchange failed.</h2><p>Check logs.</p>")
                finally:
                    # Stop the server after handling one callback
                    threading.Thread(target=httpd.shutdown, daemon=True).start()
            except Exception as exc:
                try:
                    self.send_response(500)
                    self.end_headers()
                except Exception:
                    pass

    # Hypotheses:
    # H2: port 8765 is unavailable or bind fails
    # H3: callback never hits server
    # H5: token exchange fails after callback
    host = args.host
    port = int(args.port)
    try:
        httpd = HTTPServer((host, port), Handler)
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"bind_failed: {exc}"}))
        return 1

    deadline = time.time() + float(args.timeout_s)
    try:
        httpd.timeout = 1
        while time.time() < deadline:
            httpd.handle_request()
        print(json.dumps({"ok": False, "error": "timeout_waiting_for_callback"}))
        return 1
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Health coach skill")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync_all", help="Sync WHOOP + local context and store snapshot")
    p_sync.add_argument("--days", type=int, default=7)
    p_sync.add_argument("--skip-whoop", action="store_true")
    p_sync.set_defaults(func=cmd_sync_all)

    p_m = sub.add_parser("morning_brief")
    p_m.add_argument("--proactive", action="store_true")
    p_m.add_argument("--skip-whoop", action="store_true")
    p_m.set_defaults(func=cmd_morning_brief)

    p_mid = sub.add_parser("midday_adjust")
    p_mid.add_argument("--proactive", action="store_true")
    p_mid.add_argument("--skip-whoop", action="store_true")
    p_mid.set_defaults(func=cmd_midday_adjust)

    p_e = sub.add_parser("evening_review")
    p_e.add_argument("--proactive", action="store_true")
    p_e.add_argument("--skip-whoop", action="store_true")
    p_e.set_defaults(func=cmd_evening_review)

    p_w = sub.add_parser("weekly_review")
    p_w.set_defaults(func=cmd_weekly_review)

    p_q = sub.add_parser("query")
    p_q.add_argument("question", help="Free-form question")
    p_q.add_argument("--skip-whoop", action="store_true")
    p_q.set_defaults(func=cmd_query)

    p_ps = sub.add_parser("popeye_summarize_activity", help="Popeye: summarize human activity context")
    p_ps.add_argument("--date", default=None)
    p_ps.add_argument("--skip-whoop", action="store_true")
    p_ps.add_argument("--emit-result", action="store_true")
    p_ps.set_defaults(func=cmd_popeye_summarize_activity)

    p_pd = sub.add_parser("popeye_delegate", help="Popeye: classify request and delegate to worker agents")
    p_pd.add_argument("--message", required=True, help="Inbound message from Koda/user context")
    p_pd.add_argument("--skip-whoop", action="store_true")
    p_pd.set_defaults(func=cmd_popeye_delegate)

    p_pr = sub.add_parser("popeye_synthesize_results", help="Popeye: synthesize worker results from shared store")
    p_pr.add_argument("--since-ts", dest="since_ts", type=int, default=None, help="Only read results at or after unix ts")
    p_pr.add_argument("--limit", type=int, default=200)
    p_pr.add_argument("--incremental", action="store_true", help="Use and advance synthesis marker state")
    p_pr.add_argument("--emit-result", action="store_true")
    p_pr.set_defaults(func=cmd_popeye_synthesize_results)

    p_a1 = sub.add_parser("auth_start")
    p_a1.set_defaults(func=cmd_auth_start)

    p_a2 = sub.add_parser("auth_finish")
    p_a2.add_argument("--code", required=True)
    p_a2.add_argument("--state", required=False)
    p_a2.set_defaults(func=cmd_auth_finish)

    p_a3 = sub.add_parser("auth_status")
    p_a3.set_defaults(func=cmd_auth_status)

    p_a4 = sub.add_parser("auth_refresh")
    p_a4.set_defaults(func=cmd_auth_refresh)

    p_a5 = sub.add_parser("auth_listen", help="Start local WHOOP OAuth callback listener and exchange token")
    p_a5.add_argument("--host", default="127.0.0.1")
    p_a5.add_argument("--port", default=8765, type=int)
    p_a5.add_argument("--path", default="/callback")
    p_a5.add_argument("--timeout-s", dest="timeout_s", default=300, type=int)
    p_a5.set_defaults(func=cmd_auth_listen)

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
