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
                supplied=None,
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

        return {
            "status": "committed",
            "domain_slug": slug,
            "confidence": conf,
            "inference_reason": conf_reason,
            "dynamic_row_id": pending_row_id,
            "raw_event_id": raw_id,
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
        return {
            "status": "committed",
            "domain_slug": slug,
            "confidence": conf,
            "inference_reason": conf_reason,
            "dynamic_row_id": dyn_id,
            "raw_event_id": raw_id,
        }
