"""登录（自动优先）：本机 SinoCC 客户端登录态 → 免密接管；密码直登 / loginguid 粘贴作兜底。

自动登录链路（与桥接实证一致，2026-09-02 复现 2/2）：
  1. ``auto_login.detect_identity()`` 扫 sinocc.exe 进程内存 → loginUID + 候选用户名
  2. ``cpcitem.search`` 探测 loginUID 是否被服务端接受（先验证、后落库：死 token 绝不覆盖好会话）
  3. ``vaultman cpcworkspace selectall`` 验证候选用户名真伪 → 高置信
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .vendor.auto_login import detect_identity
from .vendor.client import SinoCCClient, _session_store, get_session
from .vendor.client_zipxml import SinoCCZipClient

log = logging.getLogger("pdmq.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])

SESSION_KEY = "default"

NET_EXC = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
    httpx.WriteTimeout, httpx.PoolTimeout, httpx.RemoteProtocolError,
    httpx.ProxyError, httpx.NetworkError, httpx.TransportError,
)


class LoginIn(BaseModel):
    account: str
    password: str


class SessionIn(BaseModel):
    login_uid: str = Field(..., description="已有 loginguid（CHIGOPDM.a.{GUID}...uacpc）")
    userid: str = ""


def _settings(request: Request):
    return request.app.state.settings


def _status_dict(request: Request) -> dict:
    s = get_session(SESSION_KEY)
    if not s.login_uid:
        return {"ok": False, "logged_in": False, "reason": "未登录"}
    return {
        "ok": True, "logged_in": True,
        "user": s.user or "",
        "entcode": s.entcode or _settings(request).default_entcode,
        "login_uid_mask": (s.login_uid[:14] + "***") if s.login_uid else "",
        "source": request.app.state.login_source,
    }


async def _probe_login_uid(settings, login_uid: str, attempts: int = 2) -> tuple:
    """探测 loginUID 是否被服务端接受（一次性 scratch 会话，不污染正式会话）。

    与桥接实现同构（2026-09-07 实证）：走 web SOAP 通道 ``cpcitem.search``，
    只把「登录用户非法 / 空闲超时 / 20011」判死；空结果一律当活，避免误杀。
    """
    last_repr = ""
    for i in range(max(1, attempts)):
        scratch = f"__probe_{abs(hash(login_uid)) & 0xffffffff:x}__"
        s = get_session(scratch)
        s.login_uid = login_uid
        s.entcode = settings.default_entcode
        client = SinoCCClient(settings, session_key=scratch)
        try:
            res = await client.call("cpcitem", "search", formname="TCPCSoapDefini",
                                    payload={"itemcode": "__probe__", "searchflag": 1, "pagination": 1})
            body = (res.text or "")[:300]
            looks_dead = (
                res.status_code >= 400
                or '"id":20011' in body or '"id": 20011' in body
                or "非法的登录用户" in body or "用户空闲已经超时" in body
                or "请重新登录" in body
            )
            if looks_dead:
                return False, (body or f"http {res.status_code}"), "dead"
            return True, "", ""
        except Exception as e:  # noqa: BLE001
            last_repr = f"{type(e).__name__}: {e!r}"
            if isinstance(e, NET_EXC) and i + 1 < max(1, attempts):
                await asyncio.sleep(1.5)
                continue
            return False, (last_repr or type(e).__name__), "network"
        finally:
            try:
                await client.close()
            except Exception:
                pass
            _session_store.pop(scratch, None)
    return False, last_repr, "network"


async def _verify_username(settings, login_uid: str, candidate: str) -> bool:
    """服务端真伪验证：本人查询返回 ``<wsname>{candidate}的工作区</wsname>``。"""
    if not login_uid or not candidate:
        return False
    zc = SinoCCZipClient(settings).set_login_uid(login_uid)
    try:
        xml = await zc.workspace_selectall(wstype="4", modid="12", userid=candidate)
        return f"<wsname>{candidate}的工作区</wsname>" in xml
    except Exception:
        return False
    finally:
        try:
            await zc.session.aclose()
        except Exception:
            pass


@router.get("/status", summary="当前登录态")
async def status(request: Request) -> dict:
    return _status_dict(request)


@router.post("/auto", summary="自动登录：从本机 SinoCC 客户端登录态接管")
async def auto_login(request: Request, userid: str = "") -> dict:
    settings = _settings(request)
    try:
        ident = await asyncio.wait_for(
            asyncio.to_thread(lambda: detect_identity(entcode=settings.default_entcode)),
            timeout=45.0)
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "扫描超时（>45 秒）：请确认 SinoCC 客户端已登录，或改用密码登录"}
    if not ident.get("ok"):
        return {"ok": False, "reason": ident.get("reason", "未检测到客户端登录态"),
                "scanned": ident.get("scanned")}

    login_uid = ident.get("login_uid", "")
    candidate = ident.get("userid", "") or userid
    if not login_uid:
        return {"ok": False, "reason": "扫到会话身份串但未拿到 loginUID"}

    ok, err, kind = await _probe_login_uid(settings, login_uid)
    if not ok:
        if kind == "network":
            raise HTTPException(status_code=503, detail=f"PDM 服务器不可达（网络/代理/DNS）：{err}")
        raise HTTPException(status_code=400, detail=f"客户端登录态已失效，请重新登录 PDM 客户端后重试（{err}）")

    sess = get_session(SESSION_KEY)
    sess.login_uid = login_uid
    sess.entcode = settings.default_entcode

    user, conf = "", "low"
    if candidate and await _verify_username(settings, login_uid, candidate):
        user, conf = candidate, "high"
    if not user and candidate:
        user, conf = candidate, "low"
    if user:
        sess.user = user
    request.app.state.login_source = "客户端登录态（自动）"
    log.info("auto login ok user=%s conf=%s", user, conf)
    return {**_status_dict(request), "confidence": conf, "detected": ident}


@router.post("/login", summary="密码登录（客户端未运行时的兜底）")
async def password_login(body: LoginIn, request: Request) -> dict:
    settings = _settings(request)
    zc = SinoCCZipClient(settings, account=body.account, password=body.password)
    try:
        uid = await zc.ensure_login()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"登录失败：{type(e).__name__}: {e}")
    finally:
        try:
            await zc.session.aclose()
        except Exception:
            pass
    if not uid:
        raise HTTPException(status_code=400, detail="登录未返回 loginguid")
    sess = get_session(SESSION_KEY)
    sess.login_uid = uid
    sess.entcode = settings.default_entcode
    sess.user = body.account
    request.app.state.login_source = "密码登录"
    return _status_dict(request)


@router.post("/session", summary="粘贴 loginguid 注入登录态")
async def inject_session(body: SessionIn, request: Request) -> dict:
    settings = _settings(request)
    ok, err, kind = await _probe_login_uid(settings, body.login_uid)
    if not ok:
        if kind == "network":
            raise HTTPException(status_code=503, detail=f"PDM 服务器不可达：{err}")
        raise HTTPException(status_code=400, detail=f"loginguid 无效或已过期：{err}")
    sess = get_session(SESSION_KEY)
    sess.login_uid = body.login_uid
    sess.entcode = settings.default_entcode
    if body.userid:
        sess.user = body.userid
    request.app.state.login_source = "手动粘贴 loginguid"
    return _status_dict(request)


@router.post("/logout", summary="退出登录")
async def logout(request: Request) -> dict:
    _session_store.pop(SESSION_KEY, None)
    request.app.state.login_source = ""
    return {"ok": True, "logged_in": False}
