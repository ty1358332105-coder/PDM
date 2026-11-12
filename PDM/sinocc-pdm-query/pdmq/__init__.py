"""SinoCC PDM 查询工具（独立小程序）：流程查询 + 确认单查询。

设计取舍：
  · 只读。不含检出/检入/审批/写流程等任何写操作 —— 那些留在原桥接里。
  · 协议层复用已抓包实证的客户端模块（pdmq/vendor/，从 sinocc-pdm-bridge 同步）。
  · 长查询走后台 job + 进度条（pdmq/jobs.py），前端轮询确定性进度，不转圈。
  · 前端资源双通道：源码态读 static/ 目录；打包态用 tools/embed_static.py 内嵌的结果，
    不依赖 %TEMP%\\_MEI*（该解包目录会被终端 agent 清理，曾导致 onefile 版 UI 白屏）。
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import router as auth_router
from .config import load_settings
from .objects import router as objects_router
from .query import router as query_router

APP_NAME = "SinoCC PDM 查询工具"
APP_VERSION = "1.0.0"
BUILD = "2026-11-09"

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 内嵌资源（tools/embed_static.py 生成）；可能不存在（未生成过），那就走磁盘
try:
    from .assets_gen import ASSETS_B64  # type: ignore
except Exception:  # noqa: BLE001
    ASSETS_B64 = {}

FROZEN = bool(getattr(sys, "frozen", False))


def _assets_mode() -> str:
    """disk | embed —— 打包态优先内嵌，源码态优先磁盘（改完 Ctrl+F5 即生效）。"""
    if not ASSETS_B64:
        return "disk"
    if FROZEN or os.environ.get("PDMQ_EMBED") == "1":
        return "embed"
    return "disk"


def create_app() -> FastAPI:
    app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.state.settings = load_settings()
    app.state.login_source = ""
    app.state.assets_mode = _assets_mode()

    app.include_router(auth_router)
    app.include_router(query_router)
    app.include_router(objects_router)

    @app.get("/api/health", tags=["meta"])
    async def health() -> dict:
        return {"ok": True, "app": "sinocc-pdm-query", "version": APP_VERSION, "build": BUILD,
                "base_url": app.state.settings.base_url,
                "assets": app.state.assets_mode, "frozen": FROZEN}

    @app.get("/api/meta", tags=["meta"])
    async def meta() -> dict:
        return {"ok": True, "app": APP_NAME, "version": APP_VERSION, "build": BUILD,
                "base_url": app.state.settings.base_url,
                "entcode": app.state.settings.default_entcode,
                "pid": os.getpid(), "assets": app.state.assets_mode, "frozen": FROZEN}

    @app.post("/api/shutdown", tags=["meta"], summary="退出程序（仅本机；UI 顶栏「退出」用）")
    async def shutdown(request: Request) -> dict:
        host = (request.client.host if request.client else "") or ""
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(status_code=403, detail="只允许本机调用")
        import asyncio

        # 先把响应发出去，再硬退（uvicorn 跑在子线程，没有优雅 stop 的入口）
        asyncio.get_running_loop().call_later(0.4, os._exit, 0)
        return {"ok": True}

    if app.state.assets_mode == "embed":
        def _blob(name: str):
            item = ASSETS_B64.get(name)
            if not item:
                return None
            mime, b64 = item
            return Response(base64.b64decode(b64), media_type=mime,
                            headers={"Cache-Control": "no-store"})

        @app.get("/", include_in_schema=False)
        async def index_embed():
            r = _blob("index.html")
            return r or Response("index.html missing", status_code=500)

        @app.get("/static/{name}", include_in_schema=False)
        async def static_embed(name: str):
            # 只允许扁平文件名（本项目前端资源都在 static 根下），防目录穿越
            safe = os.path.basename(name)
            r = _blob(safe)
            return r or Response("not found", status_code=404)
    else:
        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    return app


__all__ = ["create_app", "APP_NAME", "APP_VERSION", "BUILD"]
