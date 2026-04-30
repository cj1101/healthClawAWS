from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from nemoclaw_health.auth_http import install_dashboard_auth
from nemoclaw_health.connectors.apple_health import (
    apple_health_connector_status,
    ingest_apple_health_export_from_zip,
)
from nemoclaw_health.connectors.whoop_oauth import (
    WhoopConfigError,
    WhoopOAuthError,
    WhoopStateError,
    build_authorization_url,
    disconnect_whoop,
    exchange_callback_code,
    oauth_status_from_state,
)
from nemoclaw_health.connectors.whoop_sync import sync_whoop
from nemoclaw_health.data_entry import DataEntryService
from nemoclaw_health.debug_service import (
    analyze_environment,
    analyze_task_trace,
    recent_sessions,
    session_trace,
)
from nemoclaw_health.db import fetch_connector_state, fetch_profile, get_db, new_id
from nemoclaw_health.events import (
    EventValidationError,
    UserVisibilityInvariantError,
    validate_orchestration_event,
)
from nemoclaw_health.orchestrator import HealthOrchestrator
from nemoclaw_health.export_backup import export_raw_events_jsonl
from nemoclaw_health.retention import run_delegation_metadata_prune, run_raw_event_prune
from nemoclaw_health.settings import Settings


class ChatReq(BaseModel):
    message: str = Field(..., min_length=1)


class DomainReq(BaseModel):
    display_name: str = Field(..., min_length=1)
    schema_hint: list[str] | None = None


class IngestReq(BaseModel):
    domain: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(..., min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class PruneReq(BaseModel):
    dry_run: bool = False


class ValidateEventReq(BaseModel):
    event: dict[str, Any]
    enforce_user_visibility_invariant: bool = True


class StorageSummary(BaseModel):
    raw_events: int
    tracking_domains: int
    sqlite_path: str


class ExportRawReq(BaseModel):
    dest_relative: str = Field(default="artifacts/raw_events_backup.jsonl")


class ClarificationCommitReq(BaseModel):
    domain_slug: str = Field(..., min_length=1)
    payload_patch: dict[str, Any] = Field(default_factory=dict)


class ClarificationCancelReq(BaseModel):
    domain_slug: str = Field(..., min_length=1)


class SchemaHintsPatchReq(BaseModel):
    schema_hint: list[str] = Field(default_factory=list)


class LoginReq(BaseModel):
    password: str = Field(..., min_length=1)


class GoalCreate(BaseModel):
    title: str = Field(..., min_length=1)
    body_json: dict[str, Any] = Field(default_factory=dict)


class DebugAnalyzeBody(BaseModel):
    task_id: str | None = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = get_db(settings)
        db.init_schema()
        settings.resolved_artifact_log().parent.mkdir(parents=True, exist_ok=True)
        settings.resolved_apple_imports_dir().mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(title="Nemoclaw Health", lifespan=lifespan)
    app.state.settings = settings

    def svc_settings() -> Settings:
        return app.state.settings

    @app.get("/healthz")
    def healthz(s: Settings = Depends(svc_settings)):
        db = get_db(s)
        db.init_schema()
        return {"ok": True}

    @app.post("/v1/auth/login")
    async def auth_login(
        request: Request,
        req: LoginReq = Body(...),
        s: Settings = Depends(svc_settings),
    ):
        if not s.dashboard_password:
            request.session["authenticated"] = True
            return {"ok": True, "auth": "disabled"}
        if req.password != s.dashboard_password:
            raise HTTPException(status_code=401, detail="Invalid password")
        request.session["authenticated"] = True
        return {"ok": True}

    @app.post("/v1/auth/logout")
    async def auth_logout(request: Request, s: Settings = Depends(svc_settings)):
        if s.dashboard_password:
            request.session.clear()
        return {"ok": True}

    @app.get("/v1/profile")
    def profile_get(s: Settings = Depends(svc_settings)):
        db = get_db(s)
        with db.transaction() as cur:
            body = fetch_profile(cur)
        return {"profile": body}

    @app.put("/v1/profile")
    def profile_put(body: dict[str, Any] = Body(...), s: Settings = Depends(svc_settings)):
        db = get_db(s)
        with db.transaction() as cur:
            fetch_profile(cur)
            cur.execute(
                """
                UPDATE user_profile
                SET body_json = ?, updated_at = datetime('now')
                WHERE id = 1
                """,
                (json.dumps(body, ensure_ascii=False),),
            )
        return {"ok": True}

    @app.get("/v1/goals")
    def goals_list(s: Settings = Depends(svc_settings)):
        db = get_db(s)
        with db.transaction() as cur:
            rows = cur.execute(
                """
                SELECT id, title, body_json, created_at
                FROM goals
                WHERE deleted_at IS NULL
                ORDER BY created_at DESC
                """,
            ).fetchall()
        goals: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            if "body_json" in d and isinstance(d["body_json"], str):
                try:
                    d["body"] = json.loads(d["body_json"])
                except json.JSONDecodeError:
                    d["body"] = {}
                del d["body_json"]
            goals.append(d)
        return {"goals": goals}

    @app.post("/v1/goals")
    def goals_create(req: GoalCreate = Body(...), s: Settings = Depends(svc_settings)):
        db = get_db(s)
        gid = new_id("g_")
        with db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO goals (id, title, body_json)
                VALUES (?, ?, ?)
                """,
                (gid, req.title, json.dumps(req.body_json, ensure_ascii=False)),
            )
        return {"ok": True, "id": gid}

    @app.get("/v1/timeline")
    def timeline(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        source: Annotated[str | None, Query()] = None,
        s: Settings = Depends(svc_settings),
    ):
        db = get_db(s)
        with db.transaction() as cur:
            if source:
                rows = cur.execute(
                    """
                    SELECT id, occurred_at, source, event_type, domain_slug, payload_json, confidence
                    FROM raw_events
                    WHERE source = ?
                    ORDER BY occurred_at DESC
                    LIMIT ?
                    """,
                    (source, limit),
                ).fetchall()
            else:
                rows = cur.execute(
                    """
                    SELECT id, occurred_at, source, event_type, domain_slug, payload_json, confidence
                    FROM raw_events
                    ORDER BY occurred_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            pj = d.pop("payload_json", None)
            if isinstance(pj, str):
                try:
                    d["payload"] = json.loads(pj)
                except json.JSONDecodeError:
                    d["payload"] = {}
            else:
                d["payload"] = {}
            items.append(d)
        return {"items": items, "limit": limit, "source": source}

    @app.get("/v1/debug/sessions")
    def debug_sessions_list(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        s: Settings = Depends(svc_settings),
    ):
        return {"sessions": recent_sessions(get_db(s), limit=limit)}

    @app.get("/v1/debug/session/{task_id}")
    def debug_session_detail(task_id: str, s: Settings = Depends(svc_settings)):
        return session_trace(get_db(s), task_id)

    @app.post("/v1/debug/analyze")
    def debug_analyze(
        body: DebugAnalyzeBody | None = Body(default=None),
        s: Settings = Depends(svc_settings),
    ):
        db = get_db(s)
        b = body or DebugAnalyzeBody()
        if b.task_id:
            return analyze_task_trace(db, s, b.task_id.strip())
        return {"task_id": None, "findings": analyze_environment(db, s)}

    @app.post("/v1/chat")
    def chat(req: ChatReq = Body(...), s: Settings = Depends(svc_settings)):
        orch = HealthOrchestrator(s)
        return orch.run_chat_turn(req.message)

    @app.post("/v1/data/domain")
    def register_domain(req: DomainReq = Body(...), s: Settings = Depends(svc_settings)):
        svc = DataEntryService(s)
        return svc.register_domain(req.display_name, req.schema_hint)

    @app.post("/v1/data/ingest")
    def ingest(req: IngestReq = Body(...), s: Settings = Depends(svc_settings)):
        svc = DataEntryService(s)
        try:
            return svc.ingest(
                domain=req.domain,
                payload=req.payload,
                source=req.source,
                client_confidence=req.confidence,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/v1/data/clarifications/{pending_row_id}/commit")
    def clarification_commit(
        pending_row_id: str,
        req: ClarificationCommitReq = Body(...),
        s: Settings = Depends(svc_settings),
    ):
        svc = DataEntryService(s)
        return svc.commit_clarification(
            pending_row_id=pending_row_id,
            domain_slug=req.domain_slug,
            payload_patch=req.payload_patch,
        )

    @app.post("/v1/data/clarifications/{pending_row_id}/cancel")
    def clarification_cancel(
        pending_row_id: str,
        req: ClarificationCancelReq = Body(...),
        s: Settings = Depends(svc_settings),
    ):
        svc = DataEntryService(s)
        return svc.cancel_clarification(pending_row_id=pending_row_id, domain_slug=req.domain_slug)

    @app.patch("/v1/data/domain/{slug}/schema-hints")
    def patch_schema_hints(
        slug: str,
        req: SchemaHintsPatchReq = Body(...),
        s: Settings = Depends(svc_settings),
    ):
        svc = DataEntryService(s)
        return svc.update_schema_hints(domain=slug, schema_hint=req.schema_hint)

    @app.post("/v1/jobs/raw-event-prune")
    def prune(req: PruneReq = Body(...), s: Settings = Depends(svc_settings)):
        db = get_db(s)
        return run_raw_event_prune(db, s.raw_event_retention_days, dry_run=req.dry_run)

    @app.post("/v1/jobs/delegation-prune")
    def delegation_prune(req: PruneReq = Body(...), s: Settings = Depends(svc_settings)):
        db = get_db(s)
        return run_delegation_metadata_prune(
            db,
            s.delegation_metadata_retention_days,
            dry_run=req.dry_run,
        )

    @app.post("/v1/jobs/whoop-sync")
    def job_whoop_sync(
        s: Settings = Depends(svc_settings),
        days: Annotated[int | None, Query()] = None,
    ):
        """Cron-friendly alias for POST /v1/connectors/whoop/sync."""
        db = get_db(s)
        try:
            return sync_whoop(db, s, days=days)
        except WhoopConfigError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except WhoopOAuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.post("/v1/storage/export-raw-jsonl")
    def export_raw_jsonl(req: ExportRawReq = Body(...), s: Settings = Depends(svc_settings)):
        db = get_db(s)
        dest = (s.data_dir / req.dest_relative).resolve()
        try:
            dest.relative_to(s.data_dir.resolve())
        except ValueError as e:
            raise HTTPException(status_code=400, detail="dest_relative must stay under data_dir") from e
        return export_raw_events_jsonl(db, dest)

    @app.post("/v1/contracts/validate-event")
    def validate_evt(req: ValidateEventReq = Body(...)):
        try:
            validate_orchestration_event(
                req.event,
                enforce_invariant=req.enforce_user_visibility_invariant,
            )
        except UserVisibilityInvariantError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        except EventValidationError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True}

    @app.get("/v1/storage/summary")
    def storage_summary(s: Settings = Depends(svc_settings)):
        db = get_db(s)
        with db.transaction() as cur:
            re_c = cur.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
            tr_c = cur.execute("SELECT COUNT(*) FROM tracking_registry").fetchone()[0]
        return StorageSummary(raw_events=re_c, tracking_domains=tr_c, sqlite_path=str(s.resolved_sqlite()))

    # --- Phase 2: WHOOP + Apple Health connectors ---

    @app.get("/v1/connectors/whoop/status")
    def whoop_status(s: Settings = Depends(svc_settings)):
        db = get_db(s)
        with db.transaction() as cur:
            blob = fetch_connector_state(cur, "whoop")
        return oauth_status_from_state(blob)

    @app.get("/v1/connectors/whoop/authorize-url")
    def whoop_authorize_url(s: Settings = Depends(svc_settings)):
        db = get_db(s)
        try:
            url = build_authorization_url(db, s)
            return {"authorization_url": url}
        except WhoopConfigError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @app.get("/v1/connectors/whoop/callback")
    def whoop_callback(
        s: Settings = Depends(svc_settings),
        code: Annotated[str | None, Query()] = None,
        state: Annotated[str | None, Query()] = None,
    ):
        db = get_db(s)
        try:
            return exchange_callback_code(db, s, code=code, state=state)
        except WhoopStateError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except WhoopConfigError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except WhoopOAuthError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.post("/v1/connectors/whoop/disconnect")
    def whoop_disconnect(s: Settings = Depends(svc_settings)):
        db = get_db(s)
        return disconnect_whoop(db)

    @app.post("/v1/connectors/whoop/sync")
    def whoop_sync(
        s: Settings = Depends(svc_settings),
        days: Annotated[int | None, Query()] = None,
    ):
        db = get_db(s)
        try:
            return sync_whoop(db, s, days=days)
        except WhoopConfigError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        except WhoopOAuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @app.get("/v1/connectors/apple-health/status")
    def apple_status(s: Settings = Depends(svc_settings)):
        db = get_db(s)
        return apple_health_connector_status(db)

    @app.post("/v1/connectors/apple-health/import")
    async def apple_import(
        file: Annotated[UploadFile, File(description="Apple Health export.zip")],
        s: Settings = Depends(svc_settings),
    ):
        db = get_db(s)
        dest_dir = s.resolved_apple_imports_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "export.zip").suffix or ".zip"
        name = f"import_{uuid.uuid4().hex}{suffix}"
        path = dest_dir / name
        contents = await file.read()
        path.write_bytes(contents)
        try:
            result = await asyncio.to_thread(ingest_apple_health_export_from_zip, db, path)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return result

    static_dash = Path(__file__).resolve().parent / "static" / "dashboard"
    assets_dir = static_dash / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="dash_assets")

    @app.get("/", include_in_schema=False)
    def dash_index():
        index = static_dash / "index.html"
        if not index.is_file():
            return {"service": "nemoclaw-health", "hint": "Install static/dashboard files or use /docs"}
        return FileResponse(index)

    install_dashboard_auth(app, settings)
    return app


app = create_app()
