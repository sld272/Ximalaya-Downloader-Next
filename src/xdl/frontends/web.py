# -*- coding: utf-8 -*-
"""FastAPI WebUI 入口与 JSON API。"""
from __future__ import annotations

import argparse
import threading
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from ..errors import XdlError
from .web_runtime import (OperationBusyError, OperationNotCancellableError,
                          WebRuntime)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DownloadRequest(StrictModel):
    mode: Literal["track", "album"]
    target: str = Field(min_length=1)
    quality: Literal["high", "standard", "low"] | None = None
    range: str | None = None


class TargetRequest(StrictModel):
    target: str = Field(min_length=1)


class GenSignRequest(StrictModel):
    device_info_path: str | None = None
    repeat: int = Field(default=1, ge=1, le=20)


class ExtractDeviceRequest(StrictModel):
    output: str | None = None
    profile: str | None = None
    headless: bool = True
    refresh: bool = False
    fresh_profile: bool = False


class RefreshCookiesRequest(StrictModel):
    headless: bool = True


class OpenDownloadsRequest(StrictModel):
    task_id: int | None = None


class SettingsUpdate(StrictModel):
    download_dir: str | None = None
    default_quality: Literal["high", "standard", "low"] | None = None
    source_backend: Literal["http", "chrome"] | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=16)
    resolve_timeout: int | None = Field(default=None, ge=1, le=300)
    http_timeout: int | None = Field(default=None, ge=1, le=1800)
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    retry_backoff_base: float | None = Field(default=None, ge=0, le=300)
    cooldown: float | None = Field(default=None, ge=0, le=3600)
    global_retry_rounds: int | None = Field(default=None, ge=0, le=20)
    chrome_path: str | None = None
    chrome_profile_dir: str | None = None
    cdp_port: int | None = Field(default=None, ge=1, le=65535)
    task_db_path: str | None = None
    risk_log_path: str | None = None
    device_info_path: str | None = None
    cookies_cache_path: str | None = None
    source_impersonate: str | None = None
    chrome_headless: bool | None = None
    risk_fallback_headful: bool | None = None
    reset_device_fingerprint: bool | None = None
    experiment_rotate_device_on_risk: bool | None = None
    experiment_browser_clear_state: bool | None = None
    experiment_browser_fresh_profile: bool | None = None
    experiment_rotate_headless: bool | None = None
    experiment_persist_device_info: bool | None = None
    experiment_strip_device_cookies: bool | None = None
    experiment_max_device_rotations: int | None = Field(default=None, ge=0, le=100)
    experiment_risk_cooldown_seconds: float | None = Field(
        default=None, ge=0, le=3600,
    )


def create_app(runtime: WebRuntime | None = None) -> FastAPI:
    service = runtime or WebRuntime()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        shutdown = getattr(service, "shutdown", None)
        if shutdown is not None:
            shutdown()

    app = FastAPI(
        title="XDL WebUI API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.runtime = service

    @app.exception_handler(OperationBusyError)
    async def busy_handler(_request: Request, exc: OperationBusyError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(OperationNotCancellableError)
    async def stop_handler(_request: Request,
                           exc: OperationNotCancellableError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(XdlError)
    async def xdl_error_handler(_request: Request, exc: XdlError):
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "category": exc.category},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/bootstrap")
    def bootstrap():
        return service.bootstrap()

    @app.get("/api/operation")
    def operation():
        return {"operation": service.operation_snapshot()}

    @app.get("/api/tasks")
    def tasks():
        return service.tasks_snapshot()

    @app.get("/api/risk-report")
    def risk_report():
        return service.risk_report()

    @app.post("/api/operations/login", status_code=202)
    def login():
        return service.start_login()

    @app.post("/api/operations/download", status_code=202)
    def download(body: DownloadRequest):
        return service.start_download(
            mode=body.mode, target=body.target,
            quality=body.quality, range_=body.range,
        )

    @app.post("/api/operations/resume", status_code=202)
    def resume():
        return service.start_resume()

    @app.post("/api/operations/formats", status_code=202)
    def formats(body: TargetRequest):
        return service.start_formats(body.target)

    @app.post("/api/operations/inspect-storage", status_code=202)
    def inspect_storage():
        return service.start_inspect_storage()

    @app.post("/api/operations/gen-sign", status_code=202)
    def gen_sign(body: GenSignRequest):
        return service.start_gen_sign(
            device_info_path=body.device_info_path, repeat=body.repeat,
        )

    @app.post("/api/operations/extract-device", status_code=202)
    def extract_device(body: ExtractDeviceRequest):
        return service.start_extract_device(
            output=body.output, profile=body.profile,
            headless=body.headless, refresh=body.refresh,
            fresh_profile=body.fresh_profile,
        )

    @app.post("/api/operations/refresh-cookies", status_code=202)
    def refresh_cookies(body: RefreshCookiesRequest):
        return service.start_refresh_cookies(headless=body.headless)

    @app.post("/api/operations/stop")
    def stop():
        return service.request_stop()

    @app.put("/api/settings")
    def update_settings(body: SettingsUpdate):
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        return {"settings": service.update_settings(changes)}

    @app.post("/api/open-downloads")
    def open_downloads(body: OpenDownloadsRequest):
        return service.open_downloads(body.task_id)

    static_dir = Path(__file__).with_name("web_static")
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="webui")
    else:
        @app.get("/")
        def root():
            return {"name": "XDL WebUI", "api": "/api/docs"}
    return app


def serve(*, host: str = "127.0.0.1", port: int = 8787,
          open_browser: bool = True) -> int:
    import uvicorn

    url = f"http://{host}:{port}"
    if open_browser:
        timer = threading.Timer(0.8, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("[警告] WebUI 没有远程访问认证；请只在可信网络监听非本机地址。")
    print(f"XDL WebUI: {url}")
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 XDL 本地 WebUI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8787, help="监听端口（默认 8787）")
    parser.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    return serve(host=args.host, port=args.port, open_browser=not args.no_open)
