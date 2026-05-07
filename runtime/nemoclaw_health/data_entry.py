from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from nemoclaw_health.db import _DYN_SLUG_OK, fetch_profile, get_db, insert_raw_event, new_id
from nemoclaw_health.health_coach_store import (
    health_db_biometrics_window,
    health_db_meals_window,
    health_store_bootstrap,
    load_latest_stan_snapshot,
    mirror_ingest_payload_to_biometric,
    sqlite_tables_with_counts,
    upsert_biometric_sample,
)
from nemoclaw_health.settings import Settings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_slug_re = re.compile(r"[^a-z0-9]+")

MAX_QUERY_LIMIT = 200

FOOD_LOG_SEED_SCHEMA_HINT = (
    "meal_ts",
    "meal_date",
    "description",
    "protein_g",
    "carbs_g",
    "fats_g",
    "fiber_g",
    "calories",
)

# Dynamic ingest domains treated as food for merge UI + pending-row backfill.
FOOD_DOMAIN_SLUGS = ("food_log", "food", "meal", "nutrition", "diet")


def clamp_query_limit(limit: int | None, *, default: int = 50, max_lim: int = MAX_QUERY_LIMIT) -> int:
    if limit is None:
        return default
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(max_lim, n))


def clamp_insight_days(days: int | None) -> int:
    if days is None:
        return 14
    try:
        d = int(days)
    except (TypeError, ValueError):
        return 14
    return max(1, min(90, d))


def _iso_to_dt(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    if not t:
        return None
    try:
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        return datetime.fromisoformat(t)
    except Exception:
        return None


def _meal_sort_key(m: dict[str, Any]) -> datetime:
    for k in ("meal_ts", "recorded_at"):
        dt = _iso_to_dt(str(m.get(k) or ""))
        if dt:
            return dt
    return datetime.min.replace(tzinfo=timezone.utc)


def _collect_dyn_food_meals(de: Any, *, days: int, row_limit: int) -> list[dict[str, Any]]:
    """Committed dynamic-domain rows for food slugs in the same calendar window as health.db meals."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    start_iso = start_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    out: list[dict[str, Any]] = []
    with de.db.transaction() as cur:
        for slug in FOOD_DOMAIN_SLUGS:
            if not _DYN_SLUG_OK.match(slug):
                continue
            if not cur.execute(
                "SELECT 1 FROM tracking_registry WHERE slug = ?",
                (slug,),
            ).fetchone():
                continue
            de.db.ensure_dynamic_table(slug)
            table = f"evt_dyn_{slug}"
            rows = cur.execute(
                f"""
                SELECT id, recorded_at, payload_json, confidence, source, clarification_pending
                FROM {table}
                WHERE clarification_pending = 0 AND recorded_at >= ?
                ORDER BY recorded_at DESC
                LIMIT ?
                """,
                (start_iso, row_limit * 3),
            ).fetchall()
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                ra = str(r["recorded_at"] or "")
                meal_ts = payload.get("meal_ts") or ra
                meal_date = payload.get("meal_date")
                if meal_date is None and meal_ts:
                    dt = _iso_to_dt(str(meal_ts))
                    if dt:
                        meal_date = dt.date().isoformat()
                out.append(
                    {
                        "id": r["id"],
                        "meal_ts": meal_ts,
                        "meal_date": meal_date,
                        "description": payload.get("description"),
                        "protein_g": payload.get("protein_g"),
                        "carbs_g": payload.get("carbs_g"),
                        "fats_g": payload.get("fats_g"),
                        "fiber_g": payload.get("fiber_g"),
                        "calories": payload.get("calories"),
                        "input_type": "nemoclaw_chat",
                        "source_ref": slug,
                        "nemoclaw_source": r["source"],
                        "nemoclaw_confidence": r["confidence"],
                        "nemoclaw_domain": slug,
                        "recorded_at": ra,
                    },
                )
    return out


def _merge_meal_lists(
    hb_recent: list[dict[str, Any]],
    dyn_meals: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def consider(item: dict[str, Any]) -> None:
        desc = str(item.get("description") or "").strip().lower()[:200]
        ts = str(item.get("meal_ts") or item.get("recorded_at") or "")
        cid = str(item.get("id") or "")
        key = (cid, ts[:19], desc)
        if key in seen:
            return
        seen.add(key)
        merged.append(item)

    for r in hb_recent:
        consider({**r, "_origin": "health_db"})
    for r in dyn_meals:
        consider({**r, "_origin": "nemoclaw"})
    merged.sort(key=_meal_sort_key, reverse=True)
    return merged[:limit]


ALLOWED_INGEST_SOURCES = frozenset(
    {
        "manual",
        "whoop",
        "healthkit_export",
        "wearable_auto",
        "image_derived_local",
        "barcode_local",
    },
)


def normalize_ingest_source(source: str) -> str:
    s = str(source).strip().lower()
    if s not in ALLOWED_INGEST_SOURCES:
        raise ValueError(
            f"unsupported source {source!r}; allowed: {sorted(ALLOWED_INGEST_SOURCES)}",
        )
    return s


def safe_slug(name: str, *, max_len: int = 48) -> str:
    s = name.lower().strip().replace("-", "_")
    s = _slug_re.sub("_", s).strip("_")
    if not s:
        s = "domain"
    if s[0].isdigit():
        s = "d_" + s
    return s[:max_len]


def infer_confidence(
    *,
    supplied: float | None,
    payload: dict[str, Any],
    schema_hint: list[str] | None,
    source: str,
) -> tuple[float, str]:
    """Inference-first heuristic; returns (confidence, reason)."""
    if supplied is not None:
        return max(0.0, min(1.0, supplied)), "client_supplied"

    if source in ("whoop", "healthkit_export", "wearable_auto"):
        return 0.92, "device_pipeline_default"

    if source in ("image_derived_local", "barcode_local"):
        key_count = len([k for k, v in payload.items() if v not in (None, "", [])])
        if key_count >= 2:
            return 0.55, "local_capture_partial_heuristic"
        return 0.38, "local_capture_sparse_heuristic"

    if schema_hint:
        hits = sum(1 for k in schema_hint if k in payload and payload.get(k) not in (None, "", []))
        ratio = hits / len(schema_hint)
        score = 0.45 + 0.5 * ratio
        return score, "schema_hint_coverage"

    key_count = len([k for k, v in payload.items() if v not in (None, "", [])])
    if key_count >= 3:
        return 0.78, "rich_payload_heuristic"
    if key_count == 2:
        return 0.58, "partial_payload_heuristic"
    return 0.42, "sparse_payload_heuristic"


def clarification_questions(
    domain_slug: str,
    schema_hint: list[str] | None,
    payload: dict[str, Any],
) -> list[str]:
    out: list[str] = []
    if schema_hint:
        for k in schema_hint:
            if k not in payload or payload[k] in (None, "", []):
                out.append(f"What is your {k} for this {domain_slug} entry?")
    if not out:
        out.append(
            f"Please add a bit more detail for this {domain_slug} log "
            "(e.g. quantity, time, or context) so I can record it accurately.",
        )
    return out[:3]


def _delete_clarification_raws(cur, *, slug: str, pending_row_id: str) -> None:
    cur.execute(
        """
        DELETE FROM raw_events
        WHERE event_type = ?
          AND domain_slug = ?
          AND json_extract(provenance_json, '$.dyn_row') = ?
        """,
        ("data_entry_clarification_pending", slug, pending_row_id),
    )


class DataEntryService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = get_db(settings)

    def health_store_bootstrap(self) -> dict[str, Any]:
        """Create or migrate OpenClaw ``health.db`` at the configured path."""
        return health_store_bootstrap()

    def health_store_upsert_biometric(
        self,
        *,
        sample_date: str,
        source: str,
        hrv_rmssd_milli: float | None = None,
        resting_hr: float | None = None,
        sleep_hours: float | None = None,
        sleep_performance_pct: float | None = None,
        recovery_score: float | None = None,
        avg_strain: float | None = None,
        workout_kcal: float | None = None,
        workout_count: int | None = None,
        body_weight_kg: float | None = None,
    ) -> dict[str, Any]:
        normalize_ingest_source(source)
        upsert_biometric_sample(
            sample_date=sample_date,
            source=source,
            hrv_rmssd_milli=hrv_rmssd_milli,
            resting_hr=resting_hr,
            sleep_hours=sleep_hours,
            sleep_performance_pct=sleep_performance_pct,
            recovery_score=recovery_score,
            avg_strain=avg_strain,
            workout_kcal=workout_kcal,
            workout_count=workout_count,
            body_weight_kg=body_weight_kg,
        )
        return {"ok": True, "sample_date": sample_date, "source": source}

    def _register_domain_in_tx(
        self,
        cur,
        display_name: str,
        schema_hint: list[str] | None,
    ) -> dict[str, Any]:
        slug = safe_slug(display_name)
        self.db.ensure_dynamic_table(slug)
        hint_json = json.dumps(schema_hint or [])
        row = cur.execute(
            "SELECT id, slug, display_name, schema_hint_json, created_at FROM tracking_registry WHERE slug = ?",
            (slug,),
        ).fetchone()
        if not row:
            rid = new_id("tr_")
            cur.execute(
                """
                INSERT INTO tracking_registry (id, slug, display_name, schema_hint_json)
                VALUES (?, ?, ?, ?)
                """,
                (rid, slug, display_name, hint_json),
            )
            row = cur.execute(
                "SELECT id, slug, display_name, schema_hint_json, created_at FROM tracking_registry WHERE slug = ?",
                (slug,),
            ).fetchone()
        return {
            "id": row["id"],
            "slug": row["slug"],
            "display_name": row["display_name"],
            "schema_hint": json.loads(row["schema_hint_json"] or "[]"),
            "created_at": row["created_at"],
        }

    def register_domain(
        self,
        display_name: str,
        schema_hint: list[str] | None = None,
    ) -> dict[str, Any]:
        with self.db.transaction() as cur:
            return self._register_domain_in_tx(cur, display_name, schema_hint)

    def resolve_domain_row(self, cur, domain: str) -> Any:
        row = cur.execute(
            "SELECT * FROM tracking_registry WHERE slug = ? OR display_name = ? COLLATE NOCASE LIMIT 1",
            (domain, domain),
        ).fetchone()
        return row

    def update_schema_hints(self, *, domain: str, schema_hint: list[str]) -> dict[str, Any]:
        hint_json = json.dumps(list(schema_hint))
        with self.db.transaction() as cur:
            row = self.resolve_domain_row(cur, domain)
            if not row:
                reg = self._register_domain_in_tx(cur, domain, schema_hint)
                return {"ok": True, **reg}
            slug = row["slug"]
            cur.execute(
                "UPDATE tracking_registry SET schema_hint_json = ? WHERE slug = ?",
                (hint_json, slug),
            )
            row2 = cur.execute(
                "SELECT id, slug, display_name, schema_hint_json, created_at FROM tracking_registry WHERE slug = ?",
                (slug,),
            ).fetchone()
        return {
            "ok": True,
            "id": row2["id"],
            "slug": row2["slug"],
            "display_name": row2["display_name"],
            "schema_hint": json.loads(row2["schema_hint_json"] or "[]"),
            "created_at": row2["created_at"],
        }

    def _resolve_table_slug(self, cur, domain_slug: str) -> str | None:
        row = self.resolve_domain_row(cur, domain_slug)
        if row:
            return str(row["slug"])
        row2 = cur.execute(
            "SELECT slug FROM tracking_registry WHERE slug = ? LIMIT 1",
            (domain_slug,),
        ).fetchone()
        if row2:
            return str(row2["slug"])
        return None

    def cancel_clarification(self, *, pending_row_id: str, domain_slug: str) -> dict[str, Any]:
        with self.db.transaction() as cur:
            slug = self._resolve_table_slug(cur, domain_slug)
            if not slug:
                return {"status": "domain_not_found"}
            table = f"evt_dyn_{slug}"
            self.db.ensure_dynamic_table(slug)
            ex = cur.execute(
                f"SELECT id FROM {table} WHERE id = ? AND clarification_pending = 1",
                (pending_row_id,),
            ).fetchone()
            if not ex:
                return {"status": "not_found"}
            cur.execute(f"DELETE FROM {table} WHERE id = ?", (pending_row_id,))
            _delete_clarification_raws(cur, slug=slug, pending_row_id=pending_row_id)
        return {"status": "cancelled", "domain_slug": slug}

    def commit_clarification(
        self,
        *,
        pending_row_id: str,
        domain_slug: str,
        payload_patch: dict[str, Any],
        committed_raw_event_type: str = "data_entry_committed",
        force: bool = False,
    ) -> dict[str, Any]:
        with self.db.transaction() as cur:
            slug = self._resolve_table_slug(cur, domain_slug)
            if not slug:
                return {"status": "domain_not_found"}
            table = f"evt_dyn_{slug}"
            self.db.ensure_dynamic_table(slug)
            dyn = cur.execute(
                f"""
                SELECT payload_json, source, recorded_at, confidence
                FROM {table}
                WHERE id = ? AND clarification_pending = 1
                """,
                (pending_row_id,),
            ).fetchone()
            if not dyn:
                return {"status": "not_found"}

            base_payload = dict(json.loads(dyn["payload_json"] or "{}"))
            merged = {**base_payload, **payload_patch}
            schema_row = cur.execute(
                "SELECT schema_hint_json FROM tracking_registry WHERE slug = ?",
                (slug,),
            ).fetchone()
            schema_hint = json.loads(schema_row["schema_hint_json"] or "[]") if schema_row else []

            src = normalize_ingest_source(str(dyn["source"]))
            conf, conf_reason = infer_confidence(
                supplied=1.0 if force else None,
                payload=merged,
                schema_hint=schema_hint or None,
                source=src,
            )
            threshold = self.settings.confidence_commit_threshold
            recorded_at = str(dyn["recorded_at"])

            if conf < threshold:
                questions = clarification_questions(slug, schema_hint or None, merged)
                prov = {
                    "inference": conf_reason,
                    "needs_clarification": True,
                    "questions": questions,
                }
                cur.execute(
                    f"""
                    UPDATE {table}
                    SET payload_json = ?, confidence = ?, provenance_json = ?, clarification_pending = 1
                    WHERE id = ?
                    """,
                    (
                        json.dumps(merged, ensure_ascii=False),
                        conf,
                        json.dumps(prov, ensure_ascii=False),
                        pending_row_id,
                    ),
                )
                _delete_clarification_raws(cur, slug=slug, pending_row_id=pending_row_id)
                insert_raw_event(
                    cur,
                    occurred_at=recorded_at,
                    source=src,
                    event_type="data_entry_clarification_pending",
                    domain_slug=slug,
                    payload=merged,
                    confidence=conf,
                    provenance={
                        "reason": conf_reason,
                        "questions": questions,
                        "dyn_row": pending_row_id,
                    },
                )
                return {
                    "status": "clarification_required",
                    "domain_slug": slug,
                    "confidence": conf,
                    "inference_reason": conf_reason,
                    "questions": questions,
                    "pending_row_id": pending_row_id,
                }

            cur.execute(
                f"""
                UPDATE {table}
                SET payload_json = ?, confidence = ?, provenance_json = ?, clarification_pending = 0
                WHERE id = ?
                """,
                (
                    json.dumps(merged, ensure_ascii=False),
                    conf,
                    json.dumps({"inference": conf_reason}, ensure_ascii=False),
                    pending_row_id,
                ),
            )
            _delete_clarification_raws(cur, slug=slug, pending_row_id=pending_row_id)
            raw_id = insert_raw_event(
                cur,
                occurred_at=recorded_at,
                source=src,
                event_type=committed_raw_event_type,
                domain_slug=slug,
                payload=merged,
                confidence=conf,
                provenance={"reason": conf_reason, "dyn_row": pending_row_id},
            )

        result = {
            "status": "committed",
            "domain_slug": slug,
            "confidence": conf,
            "inference_reason": conf_reason,
            "dynamic_row_id": pending_row_id,
            "raw_event_id": raw_id,
        }
        result["health_db_mirror"] = mirror_ingest_payload_to_biometric(
            payload=merged,
            source=src,
            occurred_at=recorded_at,
        )
        return result

    def commit_all_pending_food_rows(self) -> dict[str, Any]:
        """Force-commit all clarification-pending rows in food-like domains (startup backfill)."""
        self.ensure_optional_seed_domains()
        committed_ids: list[str] = []
        errors: list[dict[str, Any]] = []
        for slug in FOOD_DOMAIN_SLUGS:
            if not _DYN_SLUG_OK.match(slug):
                continue
            with self.db.transaction() as cur:
                if not cur.execute(
                    "SELECT 1 FROM tracking_registry WHERE slug = ?",
                    (slug,),
                ).fetchone():
                    continue
            self.db.ensure_dynamic_table(slug)
            table = f"evt_dyn_{slug}"
            with self.db.transaction() as cur:
                rows = cur.execute(
                    f"SELECT id FROM {table} WHERE clarification_pending = 1",
                ).fetchall()
            pending = [str(r["id"]) for r in rows]
            for pid in pending:
                try:
                    r = self.commit_clarification(
                        pending_row_id=pid,
                        domain_slug=slug,
                        payload_patch={},
                        force=True,
                    )
                    if r.get("status") == "committed":
                        committed_ids.append(pid)
                    else:
                        errors.append({"id": pid, "slug": slug, "result": r})
                except Exception as e:
                    errors.append({"id": pid, "slug": slug, "error": str(e)})
        return {
            "committed_count": len(committed_ids),
            "committed_ids": committed_ids,
            "errors": errors,
        }

    def ingest(
        self,
        *,
        domain: str,
        payload: dict[str, Any],
        source: str,
        client_confidence: float | None = None,
        provenance: dict[str, Any] | None = None,
        occurred_at: str | None = None,
        committed_raw_event_type: str = "data_entry_committed",
    ) -> dict[str, Any]:
        now = utc_now_iso()
        at = occurred_at if occurred_at else now
        source_n = normalize_ingest_source(source)
        with self.db.transaction() as cur:
            row = self.resolve_domain_row(cur, domain)
            if not row:
                reg = self._register_domain_in_tx(cur, domain, None)
                slug = reg["slug"]
                schema_hint = []
            else:
                slug = row["slug"]
                schema_hint = json.loads(row["schema_hint_json"] or "[]")

            self.db.ensure_dynamic_table(slug)
            table = f"evt_dyn_{slug}"

            conf, conf_reason = infer_confidence(
                supplied=client_confidence,
                payload=payload,
                schema_hint=schema_hint or None,
                source=source_n,
            )
            threshold = self.settings.confidence_commit_threshold

            if conf < threshold:
                questions = clarification_questions(slug, schema_hint or None, payload)
                pending_id = new_id("pe_")
                cur.execute(
                    f"""
                    INSERT INTO {table}
                      (id, recorded_at, payload_json, confidence, source, provenance_json, clarification_pending)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        pending_id,
                        at,
                        json.dumps(payload, ensure_ascii=False),
                        conf,
                        source_n,
                        json.dumps(
                            {
                                **(provenance or {}),
                                "inference": conf_reason,
                                "needs_clarification": True,
                                "questions": questions,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
                insert_raw_event(
                    cur,
                    occurred_at=at,
                    source=source_n,
                    event_type="data_entry_clarification_pending",
                    domain_slug=slug,
                    payload=payload,
                    confidence=conf,
                    provenance={
                        "reason": conf_reason,
                        "questions": questions,
                        "dyn_row": pending_id,
                    },
                )
                return {
                    "status": "clarification_required",
                    "domain_slug": slug,
                    "confidence": conf,
                    "inference_reason": conf_reason,
                    "questions": questions,
                    "pending_row_id": pending_id,
                }

            dyn_id = new_id("dv_")
            cur.execute(
                f"""
                INSERT INTO {table}
                  (id, recorded_at, payload_json, confidence, source, provenance_json, clarification_pending)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    dyn_id,
                    at,
                    json.dumps(payload, ensure_ascii=False),
                    conf,
                    source_n,
                    json.dumps({**(provenance or {}), "inference": conf_reason}, ensure_ascii=False),
                ),
            )
            raw_id = insert_raw_event(
                cur,
                occurred_at=at,
                source=source_n,
                event_type=committed_raw_event_type,
                domain_slug=slug,
                payload=payload,
                confidence=conf,
                provenance={"reason": conf_reason, "dyn_row": dyn_id},
            )
        result = {
            "status": "committed",
            "domain_slug": slug,
            "confidence": conf,
            "inference_reason": conf_reason,
            "dynamic_row_id": dyn_id,
            "raw_event_id": raw_id,
        }
        result["health_db_mirror"] = mirror_ingest_payload_to_biometric(
            payload=payload,
            source=source_n,
            occurred_at=at,
        )
        return result

    def ensure_optional_seed_domains(self) -> dict[str, Any]:
        """Idempotent: register ``food_log`` with meal-oriented schema hints if absent."""
        with self.db.transaction() as cur:
            if cur.execute(
                "SELECT 1 FROM tracking_registry WHERE slug = ?",
                ("food_log",),
            ).fetchone():
                return {"food_log_seeded": False}
            self._register_domain_in_tx(cur, "Food log", list(FOOD_LOG_SEED_SCHEMA_HINT))
        return {"food_log_seeded": True}

    def _canonical_domain_slug(self, cur, slug_or_name: str) -> str | None:
        row = self.resolve_domain_row(cur, slug_or_name.strip())
        if not row:
            return None
        slug = str(row["slug"])
        if not _DYN_SLUG_OK.match(slug):
            return None
        return slug

    def build_data_entry_catalog(self) -> dict[str, Any]:
        hp = self.settings.resolved_health_db()
        hp_ex = hp.is_file()
        h_tables = sqlite_tables_with_counts(hp) if hp_ex else []
        meals_tbl = next((t for t in h_tables if t["name"] == "meals"), None)
        bio_tbl = next((t for t in h_tables if t["name"] == "biometric_samples"), None)

        domains: list[dict[str, Any]] = []
        with self.db.transaction() as cur:
            regs = cur.execute(
                """
                SELECT id, slug, display_name, schema_hint_json, created_at
                FROM tracking_registry
                ORDER BY slug
                """,
            ).fetchall()
            for reg in regs:
                slug = str(reg["slug"])
                if not _DYN_SLUG_OK.match(slug):
                    continue
                self.db.ensure_dynamic_table(slug)
                table = f"evt_dyn_{slug}"
                cnt = int(cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                mm = cur.execute(
                    f"SELECT MIN(recorded_at), MAX(recorded_at) FROM {table}",
                ).fetchone()
                src_rows = cur.execute(
                    f"SELECT source, COUNT(*) AS n FROM {table} GROUP BY source ORDER BY n DESC",
                ).fetchall()
                sources = [{"source": str(r["source"]), "count": int(r["n"])} for r in src_rows]
                raw_c = int(
                    cur.execute(
                        "SELECT COUNT(*) FROM raw_events WHERE domain_slug = ?",
                        (slug,),
                    ).fetchone()[0],
                )
                schema_hint = json.loads(reg["schema_hint_json"] or "[]")
                domains.append(
                    {
                        "slug": slug,
                        "display_name": reg["display_name"],
                        "schema_hint": schema_hint,
                        "created_at": reg["created_at"],
                        "dynamic_table": table,
                        "row_count": cnt,
                        "earliest_recorded_at": mm[0],
                        "latest_recorded_at": mm[1],
                        "sources": sources,
                        "raw_events_count": raw_c,
                    },
                )

        return {
            "managed_by_agent": "data-entry",
            "domains": domains,
            "health_store": {
                "path": str(hp.resolve()),
                "exists": hp_ex,
                "tables": h_tables,
                "meals_table_rows": int(meals_tbl["row_count"]) if meals_tbl else None,
                "biometric_samples_rows": int(bio_tbl["row_count"]) if bio_tbl else None,
            },
        }

    def _fetch_primary_raw_for_dyn_row(self, cur, *, domain_slug: str, dyn_row_id: str) -> dict[str, Any] | None:
        row = cur.execute(
            """
            SELECT id, occurred_at, source, event_type, payload_json, confidence, provenance_json
            FROM raw_events
            WHERE domain_slug = ?
              AND json_extract(provenance_json, '$.dyn_row') = ?
            ORDER BY datetime(occurred_at) DESC
            LIMIT 1
            """,
            (domain_slug, dyn_row_id),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        pj = d.pop("payload_json", None)
        pr = d.pop("provenance_json", None)
        payload: dict[str, Any] = {}
        if isinstance(pj, str):
            try:
                payload = dict(json.loads(pj))
            except json.JSONDecodeError:
                payload = {}
        prov: dict[str, Any] = {}
        if isinstance(pr, str):
            try:
                prov = dict(json.loads(pr))
            except json.JSONDecodeError:
                prov = {}
        d["payload"] = payload
        d["provenance"] = prov
        return d

    def list_domain_rows(
        self,
        *,
        slug: str,
        limit: int | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        cap = clamp_query_limit(limit)
        since_s = since.strip() if isinstance(since, str) and since.strip() else None
        out_rows: list[dict[str, Any]] = []
        with self.db.transaction() as cur:
            canon = self._canonical_domain_slug(cur, slug)
            if not canon:
                raise ValueError("unknown_domain")
            self.db.ensure_dynamic_table(canon)
            table = f"evt_dyn_{canon}"
            if since_s:
                dyn = cur.execute(
                    f"""
                    SELECT id, recorded_at, payload_json, confidence, source, provenance_json, clarification_pending
                    FROM {table}
                    WHERE recorded_at >= ?
                    ORDER BY datetime(recorded_at) DESC
                    LIMIT ?
                    """,
                    (since_s, cap),
                ).fetchall()
            else:
                dyn = cur.execute(
                    f"""
                    SELECT id, recorded_at, payload_json, confidence, source, provenance_json, clarification_pending
                    FROM {table}
                    ORDER BY datetime(recorded_at) DESC
                    LIMIT ?
                    """,
                    (cap,),
                ).fetchall()
            for r in dyn:
                payload: dict[str, Any] = {}
                try:
                    payload = dict(json.loads(r["payload_json"] or "{}"))
                except json.JSONDecodeError:
                    payload = {}
                prov_obj: dict[str, Any] = {}
                try:
                    prov_obj = dict(json.loads(r["provenance_json"] or "{}"))
                except json.JSONDecodeError:
                    prov_obj = {}
                raw_link = self._fetch_primary_raw_for_dyn_row(
                    cur,
                    domain_slug=canon,
                    dyn_row_id=str(r["id"]),
                )
                out_rows.append(
                    {
                        "id": r["id"],
                        "recorded_at": r["recorded_at"],
                        "payload": payload,
                        "confidence": r["confidence"],
                        "source": r["source"],
                        "provenance": prov_obj,
                        "clarification_pending": bool(r["clarification_pending"]),
                        "raw_event": raw_link,
                    },
                )
        return {
            "domain_slug": canon,
            "limit": cap,
            "since": since_s,
            "rows": out_rows,
        }

    def list_raw_events_filtered(
        self,
        *,
        domain: str | None,
        source: str | None,
        since: str | None,
        limit: int | None,
    ) -> dict[str, Any]:
        cap = clamp_query_limit(limit)
        dom_slug: str | None = None
        if domain and str(domain).strip():
            with self.db.transaction() as cur:
                dom_slug = self._canonical_domain_slug(cur, str(domain).strip())
                if not dom_slug:
                    raise ValueError("unknown_domain")
        src_f = str(source).strip() if source and str(source).strip() else None
        since_s = since.strip() if isinstance(since, str) and since.strip() else None

        where: list[str] = []
        params: list[Any] = []
        if dom_slug:
            where.append("domain_slug = ?")
            params.append(dom_slug)
        if src_f:
            where.append("source = ?")
            params.append(src_f)
        if since_s:
            where.append("occurred_at >= ?")
            params.append(since_s)
        wh = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"""
            SELECT id, occurred_at, source, event_type, domain_slug, payload_json, confidence, provenance_json
            FROM raw_events
            {wh}
            ORDER BY datetime(occurred_at) DESC
            LIMIT ?
        """
        params.append(cap)
        items: list[dict[str, Any]] = []
        with self.db.transaction() as cur:
            rows = cur.execute(sql, tuple(params)).fetchall()
        for r in rows:
            d = dict(r)
            pj = d.pop("payload_json", None)
            pr = d.pop("provenance_json", None)
            payload: dict[str, Any] = {}
            if isinstance(pj, str):
                try:
                    payload = dict(json.loads(pj))
                except json.JSONDecodeError:
                    payload = {}
            prov: dict[str, Any] = {}
            if isinstance(pr, str):
                try:
                    prov = dict(json.loads(pr))
                except json.JSONDecodeError:
                    prov = {}
            d["payload"] = payload
            d["provenance"] = prov
            items.append(d)
        return {"items": items, "limit": cap, "domain": dom_slug, "source": src_f, "since": since_s}

    def build_insight_context(
        self,
        *,
        days: int | None = None,
        recent_events_limit: int | None = None,
        meals_row_limit: int | None = None,
    ) -> dict[str, Any]:
        d_days = clamp_insight_days(days)
        ev_lim = clamp_query_limit(recent_events_limit, default=50)
        meal_lim = clamp_query_limit(meals_row_limit, default=50, max_lim=MAX_QUERY_LIMIT)
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=d_days)
        cutoff_iso = cutoff_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        profile: dict[str, Any] = {}
        goals_slim: list[dict[str, Any]] = []
        with self.db.transaction() as cur:
            profile = fetch_profile(cur)
            g_rows = cur.execute(
                """
                SELECT id, title, created_at
                FROM goals
                WHERE deleted_at IS NULL
                ORDER BY datetime(created_at) DESC
                LIMIT 50
                """,
            ).fetchall()
            goals_slim = [{"id": r["id"], "title": r["title"], "created_at": r["created_at"]} for r in g_rows]

            domain_summaries: list[dict[str, Any]] = []
            regs = cur.execute(
                "SELECT slug, display_name FROM tracking_registry ORDER BY slug",
            ).fetchall()
            for reg in regs:
                slug = str(reg["slug"])
                if not _DYN_SLUG_OK.match(slug):
                    continue
                self.db.ensure_dynamic_table(slug)
                table = f"evt_dyn_{slug}"
                dyn_in = cur.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE recorded_at >= ?",
                    (cutoff_iso,),
                ).fetchone()[0]
                raw_in = cur.execute(
                    """
                    SELECT COUNT(*) FROM raw_events
                    WHERE domain_slug = ? AND occurred_at >= ?
                    """,
                    (slug, cutoff_iso),
                ).fetchone()[0]
                domain_summaries.append(
                    {
                        "slug": slug,
                        "display_name": reg["display_name"],
                        "dynamic_rows_in_window": int(dyn_in),
                        "raw_events_in_window": int(raw_in),
                    },
                )

            recent = cur.execute(
                """
                SELECT id, occurred_at, source, event_type, domain_slug, payload_json, confidence, provenance_json
                FROM raw_events
                WHERE occurred_at >= ?
                ORDER BY datetime(occurred_at) DESC
                LIMIT ?
                """,
                (cutoff_iso, ev_lim),
            ).fetchall()
            recent_items: list[dict[str, Any]] = []
            for r in recent:
                d = dict(r)
                pj = d.pop("payload_json", None)
                pr = d.pop("provenance_json", None)
                payload: dict[str, Any] = {}
                if isinstance(pj, str):
                    try:
                        payload = dict(json.loads(pj))
                    except json.JSONDecodeError:
                        payload = {}
                prov: dict[str, Any] = {}
                if isinstance(pr, str):
                    try:
                        prov = dict(json.loads(pr))
                    except json.JSONDecodeError:
                        prov = {}
                d["payload"] = payload
                d["provenance"] = prov
                recent_items.append(d)

            src_tot = cur.execute(
                """
                SELECT source, COUNT(*) AS n
                FROM raw_events
                WHERE occurred_at >= ?
                GROUP BY source
                ORDER BY n DESC
                """,
                (cutoff_iso,),
            ).fetchall()
            by_source = {str(r["source"]): int(r["n"]) for r in src_tot}

            dom_tot = cur.execute(
                """
                SELECT domain_slug, COUNT(*) AS n
                FROM raw_events
                WHERE occurred_at >= ? AND domain_slug IS NOT NULL AND domain_slug != ''
                GROUP BY domain_slug
                ORDER BY n DESC
                LIMIT 40
                """,
                (cutoff_iso,),
            ).fetchall()
            by_domain = {str(r["domain_slug"]): int(r["n"]) for r in dom_tot}

            raw_total = cur.execute(
                "SELECT COUNT(*) FROM raw_events WHERE occurred_at >= ?",
                (cutoff_iso,),
            ).fetchone()[0]

            mirror_counts: dict[str, int] = {}
            for tbl in ("whoop_sleep", "whoop_workout", "whoop_recovery", "whoop_cycle"):
                exists = cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
                    (tbl,),
                ).fetchone()
                if not exists:
                    mirror_counts[tbl] = 0
                    continue
                n = cur.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE fetched_at >= ?",
                    (cutoff_iso,),
                ).fetchone()[0]
                mirror_counts[tbl] = int(n)

        meals_ctx = health_db_meals_window(days=d_days, meals_row_limit=meal_lim)
        bio_ctx = health_db_biometrics_window(days=d_days)

        whoop_like = sum(by_source.get(s, 0) for s in ("whoop", "wearable_auto"))
        apple_like = int(by_source.get("healthkit_export", 0))

        stan_snapshot: dict[str, Any] | None = None
        try:
            stan_snapshot = load_latest_stan_snapshot(self.settings.resolved_sqlite())
        except Exception:
            stan_snapshot = None

        return {
            "managed_by_agent": "data-entry",
            "window": {
                "days": d_days,
                "nemoclaw_cutoff_occurred_at": cutoff_iso,
            },
            "profile": profile,
            "goals": goals_slim,
            "domains_summary": domain_summaries,
            "recent_events": recent_items,
            "nemoclaw_raw_events": {
                "total_in_window": int(raw_total),
                "by_source": by_source,
                "by_domain_top": by_domain,
                "whoop_related_raw_total": int(whoop_like),
                "apple_health_export_raw_total": int(apple_like),
            },
            "whoop_mirror_row_counts_in_window": mirror_counts,
            "health_db": {
                "meals": meals_ctx,
                "biometrics": bio_ctx,
            },
            "stan_latest_snapshot": stan_snapshot,
        }

    def meals_window_payload(self, *, days: int | None = None, limit: int | None = None) -> dict[str, Any]:
        d = clamp_insight_days(days)
        lim = clamp_query_limit(limit, default=50)
        self.ensure_optional_seed_domains()
        hb = health_db_meals_window(days=d, meals_row_limit=lim)
        dyn = _collect_dyn_food_meals(self, days=d, row_limit=lim)
        hb_recent = hb.get("recent") if isinstance(hb.get("recent"), list) else []
        meals = _merge_meal_lists(hb_recent, dyn, limit=lim)
        return {"managed_by_agent": "data-entry", "days": d, **hb, "meals": meals}
