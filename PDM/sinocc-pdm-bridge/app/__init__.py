"""SinoCC PDM 反向集成 FastAPI 桥接服务。

设计原则：
- 把 ChinoCC 的私有 SOAP / JSP 端点包装成对调用者友好的 REST。
- 会话复用：第一次登录后保留 loginUID + JSESSIONID，避免每次重复登录。
- 不修改上游系统，所有写操作都必须实际经过 loginman/portal 链路，带审计字段。
"""
from __future__ import annotations

import hashlib
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import unquote

from fastapi import Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .routers import auth, items, wf, files, product

log = logging.getLogger("sinocc-bridge")


def _mcp_transport_security():
    """构造 MCP Streamable HTTP 的 DNS 重绑定防护白名单（2026-09-23）。

    坑：mcp SDK 的 `streamable_http_app()` 在 host 为 127.0.0.1/localhost/::1（默认值）
    时会自动开启 DNS 重绑定防护，白名单**只有本机**。于是用内网穿透域名或公司域名访问
    `/mcp` 一律被挡成 `421 Invalid Host header`，表现为「企微添加插件时提示插件工具获取失败」。

    这里从 `PDM_MCP_ALLOWED_HOSTS`（逗号分隔）追加白名单，支持两种写法：
      `mcp.example.com`      精确匹配（无端口，或 http/https 默认端口）
      `mcp.example.com:*`    通配端口（Host 形如 mcp.example.com:8443）
    未设置时返回 None，交给 SDK 默认（仅本机）——保持原行为不变。
    """
    extra = [h.strip() for h in (os.environ.get("PDM_MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    if not extra:
        return None
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"] + extra
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    for h in extra:
        base = h[:-2] if h.endswith(":*") else h
        origins += [f"http://{base}", f"https://{base}"]
        if h.endswith(":*"):
            origins += [f"http://{base}:*", f"https://{base}:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    log.info("sinocc-pdm-bridge starting (base_url=%s)", app.state.settings.base_url)
    # MCP in-process 挂载时，其 Streamable HTTP session manager 的 task group
    # 不会随子应用挂载自动启动，必须在宿主 lifespan 中显式进入（2026-09-04）。
    mcp_asgi = getattr(app.state, "mcp_asgi", None)
    if mcp_asgi is not None:
        async with mcp_asgi.router.lifespan_context(mcp_asgi):
            log.info("MCP streamable-http session manager started")
            yield
    else:
        yield
    log.info("sinocc-pdm-bridge shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="SinoCC PDM Bridge",
        version="0.1.0",
        description="Reverse integration bridge for SinoCC (Chigo) PDM via SOAP/JSP.",
        lifespan=lifespan,
    )

    from .config import load_settings
    app.state.settings = load_settings()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router, prefix="/auth", tags=["auth"])
    app.include_router(wf.router, prefix="/wf", tags=["workflow"])
    app.include_router(items.router, prefix="/items", tags=["items"])
    app.include_router(files.router, prefix="/files", tags=["files"])
    app.include_router(product.router, prefix="/product", tags=["product"])

    # MCP server is served as a SEPARATE process via `mcp_standalone.py` on port
    # 9124 (uvicorn mcp_standalone:app --port 9124) to avoid route conflicts with
    # this FastAPI app. See README section 2.5. Nothing to mount here.

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        st = app.state.settings
        return {"ok": True, "base_url": st.base_url,
                "trust_env": st.trust_env, "proxy": st.proxy or "",
                "proxy_effective": st.proxy or ("(跟随系统/环境代理)" if st.trust_env else "(直连)")}

    # Static UI (single-page test console mirroring the original PDM layout)
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse("/ui/index.html", headers=NO_CACHE)

    def _index_build() -> str:
        """index.html 的 8 位 md5 指纹（前端版本自检用，2026-09-08）。

        背景：用户浏览器标签页长期不刷，页面里跑的还是旧 JS（本次「右键没菜单」
        即此因）。服务端已发 no-cache 头，但挡不住"开着不关"的旧标签页 →
        页面加载时拿注入的 UI_BUILD 与 /ui-version 实时值对比，不一致则弹强刷提示。
        """
        return hashlib.md5((static_dir / "index.html").read_bytes()).hexdigest()[:8]

    def _serve_index() -> Response:
        """直读 index.html 并注入 window.UI_BUILD 指纹（每请求读盘，与原行为一致）。"""
        raw = (static_dir / "index.html").read_bytes()
        build = hashlib.md5(raw).hexdigest()[:8]
        marker = b"<head>"
        inject = b'<script>window.UI_BUILD="%s";</script>' % build.encode()
        # 注：不能拿 "window.UI_BUILD" 是否已存在做防重——页尾版本自检脚本本身就引用
        # window.UI_BUILD（读取方），会误判成"已注入"导致指纹缺失（2026-09-08 实测踩坑）。
        # 这里只替换第一个 <head>，天然幂等，无需额外守卫。
        if marker in raw:
            raw = raw.replace(marker, marker + inject, 1)
        return Response(content=raw, media_type="text/html", headers=NO_CACHE)

    @app.get("/ui-version", include_in_schema=False)
    async def ui_version():
        return {"ok": True, "build": _index_build()}

    @app.get("/ui/{name:path}", include_in_schema=False)
    async def ui(name: str):
        # serve files under app/static/; force no-cache so updates are picked up immediately
        target = static_dir / name
        if name in ("index.html", "") or not target.is_file():
            return _serve_index()
        return FileResponse(target, headers=NO_CACHE)

    # ---------- MCP in-process 挂载（2026-09-04 开关版） ----------
    # 端点 http://127.0.0.1:9123/mcp（Streamable HTTP），由 web UI 顶栏「MCP」开关控制。
    # 独立进程入口 mcp_standalone.py(9124) 保持不变，两条接入路径二选一即可。
    # 注意：控制端点必须先注册；根挂载放最后（按注册顺序兜底匹配，只接未被显式路由命中的请求）。
    # 2026-10-19：默认改为开启——此前内存态默认关，重启后必须手动重开（用户高频踩坑）。
    # UI 顶栏开关仍可随时关闭；PDM_MCP_TOKEN / PDM_MCP_ALLOWED_HOSTS 防护不受影响。
    app.state.mcp_enabled = True
    # 公网接入鉴权（2026-09-23，为企业微信「MCP 插件」等公网调用方而加）。
    # 企业微信云端会主动拉取 tool list，插件 URL 必须公网可达 → 端点从此可能被公网访问。
    # 未设置 PDM_MCP_TOKEN 时行为不变（本机/内网直连免鉴权）；设置后强制校验。
    # 企微「授权方式」对应填法：Service token / API key → 位置 Header → 参数名 Authorization
    # → 参数值 Bearer <token>（同时兼容 X-PDM-MCP-Token 头与 ?token= 查询参数）。
    MCP_TOKEN = (os.environ.get("PDM_MCP_TOKEN") or "").strip()

    @app.get("/mcp-status", tags=["meta"], summary="MCP 服务开关状态")
    async def mcp_status() -> dict:
        return {
            "ok": True,
            "enabled": app.state.mcp_enabled,
            "endpoint": "/mcp",
            "auth_required": bool(MCP_TOKEN),
            "auth_hint": ("Header: Authorization: Bearer <PDM_MCP_TOKEN>" if MCP_TOKEN
                          else "未设鉴权（仅建议本机/内网使用，勿直接暴露公网）"),
            "identity_header": "X-WeCom-User-Id",
            "public_base": (os.environ.get("PDM_PUBLIC_BASE") or "").strip(),
            # 公网接入自检用：非本机 Host 不在白名单里会被 MCP SDK 挡成 421
            "mcp_allowed_hosts": [h.strip() for h in
                                  (os.environ.get("PDM_MCP_ALLOWED_HOSTS") or "").split(",")
                                  if h.strip()],
        }

    @app.post("/mcp-toggle", tags=["meta"], summary="开/关 MCP 服务（web UI 顶栏开关用）")
    async def mcp_toggle(enabled: bool = Body(..., embed=True)) -> dict:
        app.state.mcp_enabled = bool(enabled)
        log.info("MCP toggled -> %s", app.state.mcp_enabled)
        return {"ok": True, "enabled": app.state.mcp_enabled, "endpoint": "/mcp"}

    try:
        from starlette.responses import JSONResponse as _JSONResp

        from .mcp_server import create_mcp_server

        _srv = create_mcp_server()
        if _srv is not None:
            # 内部路径保持 /mcp（默认），以「/」根挂载兜底——FastAPI 显式路由先匹配，
            # /mcp 直接命中无 307 重定向；其余未匹配路径落入 MCP app 返回普通 404。
            _mcp_asgi = _srv.streamable_http_app(transport_security=_mcp_transport_security())
            app.state.mcp_asgi = _mcp_asgi   # 供 lifespan 启动 session manager

            class _MCPGate:
                """开关关闭时端点返回 404；开启后校验 token 并透传给 MCP Streamable HTTP app。

                鉴权（P，见上）：PDM_MCP_TOKEN 非空时，请求必须携带匹配 token，
                否则 401。顺带把企业微信注入的 X-WeCom-User-Id 落到日志，
                便于把「企微成员」与「PDM 账号」对齐（当前 MCP 调用共用 default 会话）。
                """

                def __init__(self, asgi):
                    self._asgi = asgi
                    self._noauth_warned = False

                @staticmethod
                def _headers(scope) -> dict:
                    return {
                        k.decode("latin-1").lower(): v.decode("latin-1")
                        for k, v in (scope.get("headers") or [])
                    }

                @classmethod
                def _token(cls, scope) -> str:
                    h = cls._headers(scope)
                    auth = h.get("authorization", "").strip()
                    if auth.lower().startswith("bearer "):
                        return auth[7:].strip()
                    for name in ("x-pdm-mcp-token", "x-api-key", "service-token"):
                        if h.get(name):
                            return h[name].strip()
                    for pair in (scope.get("query_string", b"").decode("latin-1")).split("&"):
                        if pair.startswith("token="):
                            return unquote(pair[6:]).strip()
                    return ""

                async def __call__(self, scope, receive, send):
                    if scope.get("type") not in ("http", "https") or not app.state.mcp_enabled:
                        resp = _JSONResp(
                            {"ok": False, "error": "MCP 服务未开启：请在 web UI 顶栏打开「MCP」开关"},
                            status_code=404,
                        )
                        await resp(scope, receive, send)
                        return
                    if MCP_TOKEN:
                        if self._token(scope) != MCP_TOKEN:
                            resp = _JSONResp(
                                {"ok": False, "error": "401 unauthorized: MCP token 缺失或不匹配"},
                                status_code=401,
                                headers={"WWW-Authenticate": "Bearer"},
                            )
                            await resp(scope, receive, send)
                            return
                    elif not self._noauth_warned:
                        self._noauth_warned = True
                        log.warning("MCP 端点未设鉴权（PDM_MCP_TOKEN 为空）：请勿直接暴露到公网")
                    wcid = self._headers(scope).get("x-wecom-user-id", "")
                    if wcid:
                        log.info("MCP 请求来自企业微信成员 %s（path=%s）", wcid, scope.get("path", ""))
                    await self._asgi(scope, receive, send)

            app.mount("/", _MCPGate(_mcp_asgi))
    except Exception as e:  # pragma: no cover — mcp 包缺失/版本不符时桥接主功能不受影响
        log.warning("MCP in-process mount failed: %s", e)

    return app


app = create_app()
