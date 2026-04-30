from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from nemoclaw_health.data_entry import DataEntryService
from nemoclaw_health.db import get_db
from nemoclaw_health.events import (
    EventValidationError,
    UserVisibilityInvariantError,
    validate_orchestration_event,
)
from nemoclaw_health.orchestrator import HealthOrchestrator
from nemoclaw_health.retention import run_raw_event_prune
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


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = get_db(settings)
        db.init_schema()
        settings.resolved_artifact_log().parent.mkdir(parents=True, exist_ok=True)
        yield

    app = FastAPI(title="Nemoclaw Health Phase 1", lifespan=lifespan)
    app.state.settings = settings

    def svc_settings() -> Settings:
        return app.state.settings

    @app.get("/healthz")
    def healthz(s: Settings = Depends(svc_settings)):
        db = get_db(s)
        db.init_schema()
        return {"ok": True}

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
        return svc.ingest(
            domain=req.domain,
            payload=req.payload,
            source=req.source,
            client_confidence=req.confidence,
        )

    @app.post("/v1/jobs/raw-event-prune")
    def prune(req: PruneReq = Body(...), s: Settings = Depends(svc_settings)):
        db = get_db(s)
        return run_raw_event_prune(db, s.raw_event_retention_days, dry_run=req.dry_run)

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

    return app


app = create_app()
