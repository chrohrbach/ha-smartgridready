"""Ingress web UI (FastAPI).

Provides a minimal browser interface served through the Home Assistant
ingress proxy. Lets the user inspect connected devices, the current
evaluation context, the audit trail, and the raw configuration without
SSH-ing into the container.

Note: the ingress proxy strips the ingress URL prefix before requests
reach the add-on, so paths are written as if the app were mounted at
the root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

SRC_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = SRC_DIR / "templates"
STATIC_DIR = SRC_DIR / "static"


def build_app(state) -> FastAPI:
    """Build the FastAPI app bound to a shared ``AppState`` instance."""
    app = FastAPI(
        title="SmartGridready",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    # HTML pages
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Any:
        cfg = state.user_config
        last = state.rules_engine.last_result if state.rules_engine else {}
        optimizer_result = (
            state.optimizer.get_last_result() if state.optimizer and state.optimizer.enabled else None
        )
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "version": state.version,
                "device_count": len(cfg.devices) if cfg else 0,
                "rule_count": len(cfg.rules) if cfg else 0,
                "vehicle_count": len(cfg.vehicles) if cfg else 0,
                "evaluation_interval": state.options.evaluation_interval,
                "last": last,
                "mqtt_enabled": state.mqtt_bridge.enabled if state.mqtt_bridge else False,
                "optimizer_enabled": bool(state.optimizer and state.optimizer.enabled),
                "optimizer_result": optimizer_result.to_dict() if optimizer_result else None,
            },
        )

    @app.get("/devices", response_class=HTMLResponse)
    async def devices(request: Request) -> Any:
        return templates.TemplateResponse(
            "devices.html",
            {
                "request": request,
                "devices": state.sgr_service.list_devices() if state.sgr_service else [],
                "virtual_devices": state.virtual_devices.list_devices() if state.virtual_devices else [],
            },
        )

    @app.get("/rules", response_class=HTMLResponse)
    async def rules(request: Request) -> Any:
        cfg = state.user_config
        return templates.TemplateResponse(
            "rules.html",
            {
                "request": request,
                "rules": cfg.rules if cfg else [],
                "last": state.rules_engine.last_result if state.rules_engine else {},
            },
        )

    @app.get("/audit", response_class=HTMLResponse)
    async def audit(request: Request) -> Any:
        entries = state.rules_engine.load_audit(limit=50) if state.rules_engine else []
        return templates.TemplateResponse(
            "audit.html",
            {"request": request, "entries": entries},
        )

    @app.get("/config", response_class=HTMLResponse)
    async def config_view(request: Request) -> Any:
        cfg_path = state.options.config_path
        body = ""
        if cfg_path.exists():
            body = cfg_path.read_text(encoding="utf-8")
        return templates.TemplateResponse(
            "config.html",
            {"request": request, "config_path": str(cfg_path), "body": body},
        )

    # ------------------------------------------------------------------
    # JSON API (small surface — useful for diagnostics)
    # ------------------------------------------------------------------

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        last = state.rules_engine.last_result if state.rules_engine else {}
        optimizer_enabled = bool(state.optimizer and state.optimizer.enabled)
        return JSONResponse({
            "version": state.version,
            "devices": state.sgr_service.list_devices() if state.sgr_service else [],
            "virtual_devices": state.virtual_devices.list_devices() if state.virtual_devices else [],
            "mqtt_enabled": state.mqtt_bridge.enabled if state.mqtt_bridge else False,
            "optimizer_enabled": optimizer_enabled,
            "last_evaluation": last,
        })

    @app.get("/api/optimizer")
    async def api_optimizer() -> JSONResponse:
        """Last computed predictive-dispatch schedule, or a disabled marker."""
        if not state.optimizer or not state.optimizer.enabled:
            return JSONResponse({"enabled": False})
        result = state.optimizer.get_last_result()
        if result is None:
            return JSONResponse({"enabled": True, "schedule": None})
        payload = result.to_dict()
        payload["enabled"] = True
        return JSONResponse(payload)

    @app.get("/api/audit")
    async def api_audit(limit: int = 50) -> JSONResponse:
        entries = state.rules_engine.load_audit(limit=limit) if state.rules_engine else []
        return JSONResponse(entries)

    @app.get("/api/config.yaml", response_class=PlainTextResponse)
    async def api_config_yaml() -> PlainTextResponse:
        cfg_path = state.options.config_path
        if not cfg_path.exists():
            return PlainTextResponse("# (no configuration file yet)", media_type="text/yaml")
        return PlainTextResponse(cfg_path.read_text(encoding="utf-8"), media_type="text/yaml")

    @app.post("/api/evaluate")
    async def api_evaluate() -> JSONResponse:
        """Trigger an evaluation cycle manually (returns the result)."""
        if not state.rules_engine or not state.user_config:
            return JSONResponse({"error": "engine_not_ready"}, status_code=503)
        result = await state.rules_engine.evaluate(state.user_config)
        return JSONResponse(result)

    @app.get("/logo.svg", response_class=FileResponse)
    async def logo() -> FileResponse:
        return FileResponse(str(SRC_DIR.parent / "logo.svg"), media_type="image/svg+xml")

    return app
