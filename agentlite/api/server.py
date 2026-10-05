"""Lightweight local HTTP API.

Endpoints
---------
GET  /health                 service health (unauthenticated)
GET  /api/status             resolved configuration, provider, tools
GET  /api/tools              tool catalogue
POST /api/run                run a task (202 if it stops for confirmation)
GET  /api/runs/{id}          status of a parked run
POST /api/runs/{id}/confirm  answer a pending confirmation
GET  /api/audit              tail of the audit log

The server binds to loopback by default. Binding elsewhere requires an API
token (``AGENTLITE_API_TOKEN``) - AgentLite refuses to expose unrestricted
computer control to the network without one.
"""

from __future__ import annotations

import hmac
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..core.agent import Agent, AgentRun
from ..core.config import Config, validate_config
from ..core.models import RunStatus
from ..core.registry import ToolRegistry

START_TIME = time.time()


# --------------------------------------------------------------------------- #
# Runtime container
# --------------------------------------------------------------------------- #


class RunStore:
    """In-memory store for runs that are parked waiting for a human."""

    def __init__(self, ttl_seconds: int = 1800, max_runs: int = 50):
        self.ttl = ttl_seconds
        self.max_runs = max_runs
        self._runs: dict[str, dict[str, Any]] = {}

    def put(self, run: AgentRun) -> None:
        self._evict()
        self._runs[run.run_id] = {"run": run, "updated": time.time()}

    def get(self, run_id: str) -> Optional[AgentRun]:
        entry = self._runs.get(run_id)
        if not entry:
            return None
        if time.time() - entry["updated"] > self.ttl:
            self._runs.pop(run_id, None)
            return None
        return entry["run"]

    def drop(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    def _evict(self) -> None:
        now = time.time()
        for run_id in [k for k, v in self._runs.items() if now - v["updated"] > self.ttl]:
            self._runs.pop(run_id, None)
        while len(self._runs) >= self.max_runs:
            oldest = min(self._runs.items(), key=lambda item: item[1]["updated"])[0]
            self._runs.pop(oldest, None)


class Runtime:
    """Everything the API needs, built once per process."""

    def __init__(
        self,
        config: Config,
        agent: Optional[Agent] = None,
        registry: Optional[ToolRegistry] = None,
        confirmation_handler=None,
    ):
        self.config = config
        self.agent = agent or Agent.from_config(
            config, registry=registry, confirmation_handler=confirmation_handler
        )
        self.registry = self.agent.registry
        self.store = RunStore()
        self.warnings = validate_config(config)

    def status(self) -> dict[str, Any]:
        provider = self.agent.provider.describe()
        provider["api_key_configured"] = provider.pop("api_key_present", False)
        return {
            "service": "agentlite",
            "version": __version__,
            "status": "ok",
            "uptime_seconds": int(time.time() - START_TIME),
            "workspace": str(self.config.workspace_root),
            "provider": provider,
            "tools": {
                entry.tool.name: {
                    "enabled": entry.enabled,
                    "risk": entry.tool.risk.value,
                }
                for entry in self.registry
            },
            "security": {
                "confirmation_mode": self.config.security.confirmation_mode,
                "max_steps": self.config.security.max_steps,
                "audit_log": self.config.security.audit_log,
                "audit_path": str(self.config.log_path) if self.config.security.audit_log else None,
                "api_auth_required": bool(self.config.server.api_token),
                "host": self.config.server.host,
            },
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class RunRequest(BaseModel):
    task: str = Field(..., description="What the agent should do.")
    max_steps: Optional[int] = Field(None, ge=1, description="Override security.max_steps.")


class ConfirmRequest(BaseModel):
    decision: str = Field(..., pattern="^(allow|deny)$", description="allow or deny.")


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #


def auth_dependency(request: Request) -> None:
    runtime: Runtime = request.app.state.agentlite
    token = runtime.config.server.api_token
    if not token:
        return
    header = request.headers.get("authorization", "")
    presented = header[7:] if header.lower().startswith("bearer ") else ""
    if not presented:
        presented = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(presented, token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def _lifespan(app: FastAPI):
    runtime: Runtime = app.state.agentlite
    runtime.config.ensure_directories()
    yield
    runtime.registry.close()


def create_app(
    config: Config,
    runtime: Optional[Runtime] = None,
    agent: Optional[Agent] = None,
    registry: Optional[ToolRegistry] = None,
) -> FastAPI:
    """Build the FastAPI application."""
    runtime = runtime or Runtime(config, agent=agent, registry=registry)
    app = FastAPI(
        title="AgentLite",
        version=__version__,
        description=(
            "A lightweight runtime that gives AI models controlled access to real computers."
        ),
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.agentlite = runtime

    if config.server.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key"],
        )

    auth = [Depends(auth_dependency)]

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "agentlite",
            "version": __version__,
            "uptime_seconds": int(time.time() - START_TIME),
        }

    @app.get("/api/status", tags=["system"], dependencies=auth)
    async def api_status() -> dict[str, Any]:
        return runtime.status()

    @app.get("/api/tools", tags=["system"], dependencies=auth)
    async def api_tools() -> dict[str, Any]:
        return {"tools": runtime.registry.describe()}

    @app.get("/api/audit", tags=["system"], dependencies=auth)
    async def api_audit(limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        return {"events": runtime.agent.audit.tail(limit)}

    @app.post("/api/run", tags=["agent"], dependencies=auth)
    async def api_run(body: RunRequest) -> Any:
        task = body.task.strip()
        if not task:
            raise HTTPException(status_code=400, detail="task must not be empty")
        run = runtime.agent.create_run(task, max_steps=body.max_steps)
        result = await run_in_threadpool(run.resume)
        if result.status is RunStatus.NEEDS_CONFIRMATION:
            runtime.store.put(run)
            return JSONResponse(status_code=202, content=result.to_dict())
        return result.to_dict()

    @app.get("/api/runs/{run_id}", tags=["agent"], dependencies=auth)
    async def api_get_run(run_id: str) -> Any:
        run = runtime.store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="unknown or expired run")
        return {
            "run_id": run.run_id,
            "status": RunStatus.NEEDS_CONFIRMATION.value,
            "task": run.task,
            "steps": run.step,
            "pending": {k: v for k, v in (run.pending or {}).items() if k != "call"},
        }

    @app.post("/api/runs/{run_id}/confirm", tags=["agent"], dependencies=auth)
    async def api_confirm(run_id: str, body: ConfirmRequest) -> Any:
        run = runtime.store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="unknown or expired run")
        if run.pending is None:
            raise HTTPException(status_code=409, detail="run is not waiting for confirmation")
        approved = body.decision == "allow"
        result = await run_in_threadpool(run.resume, approved)
        if result.status is RunStatus.NEEDS_CONFIRMATION:
            runtime.store.put(run)
            return JSONResponse(status_code=202, content=result.to_dict())
        runtime.store.drop(run_id)
        return result.to_dict()

    return app


def app_for_config(config: Config, **kwargs: Any) -> FastAPI:
    """Convenience wrapper used by the CLI and by ``uvicorn`` factories."""
    return create_app(config, **kwargs)
