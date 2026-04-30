from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from nemoclaw_health.db import Database, get_db, insert_raw_event, new_id
from nemoclaw_health.settings import Settings


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_slug_re = re.compile(r"[^a-z0-9]+")


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

    if schema_hint:
        hits = sum(1 for k in schema_hint if k in payload and payload.get(k) not in (None, "", []))
        ratio = hits / len(schema_hint)
        score = 0.45 + 0.5 * ratio
        return score, "schema_hint_coverage"

    # Freeform payloads: richness heuristic
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


class DataEntryService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = get_db(settings)

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

    def ingest(
        self,
        *,
        domain: str,
        payload: dict[str, Any],
        source: str,
        client_confidence: float | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
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
                source=source,
            )
            threshold = self.settings.confidence_commit_threshold

            if conf < threshold:
                questions = clarification_questions(slug, schema_hint or None, payload)
                # Store a pending clarification row (non-destructive trace)
                pending_id = new_id("pe_")
                cur.execute(
                    f"""
                    INSERT INTO {table}
                      (id, recorded_at, payload_json, confidence, source, provenance_json, clarification_pending)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        pending_id,
                        now,
                        json.dumps(payload, ensure_ascii=False),
                        conf,
                        source,
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
                    occurred_at=now,
                    source=source,
                    event_type="data_entry_clarification_pending",
                    domain_slug=slug,
                    payload=payload,
                    confidence=conf,
                    provenance={"reason": conf_reason, "questions": questions},
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
                    now,
                    json.dumps(payload, ensure_ascii=False),
                    conf,
                    source,
                    json.dumps({**(provenance or {}), "inference": conf_reason}, ensure_ascii=False),
                ),
            )
            raw_id = insert_raw_event(
                cur,
                occurred_at=now,
                source=source,
                event_type="data_entry_committed",
                domain_slug=slug,
                payload=payload,
                confidence=conf,
                provenance={"reason": conf_reason, "dyn_row": dyn_id},
            )
        return {
            "status": "committed",
            "domain_slug": slug,
            "confidence": conf,
            "inference_reason": conf_reason,
            "dynamic_row_id": dyn_id,
            "raw_event_id": raw_id,
        }
