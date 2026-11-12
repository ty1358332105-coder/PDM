"""Auth: POST /auth/login, POST /auth/session (login-UID injection)."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..client import SinoCCClient

_LOG = logging.getLogger("sinocc.bridge.auth")

router = APIRouter()


class LoginIn(BaseModel):
    account: str
    password: str
    session_key: str = "default"


class SessionIn(BaseModel):
    """Login-UID injection: reuse an existing PDM session WITHOUT password."""
    login_uid: str = Field(..., description="从 web 版登录后取到的 loginguid（见 README 4.x）")
    session_key: str = "default"
    userid: str = Field("", description="可选：前端记住的账号（localStorage），用于反查服务端权威用户名")


class LoginOut(BaseModel):
    ok: bool
    user: str
    login_uid_mask: str
    entcode: str
    session_key: str
    login_uid: str | None = None


def _settings(request: Request):
    return request.app.state.settings


async def _client(request: Request, account: str = "", password: str = "", session_key: str = "default") -> SinoCCClient:
    return SinoCCClient(_settings(request), account=account, password=password, session_key=session_key)


# 网络/传输类异常：脚本可重试，且与「token 判死」是不同的处置路径
NET_EXC = ()

try:  # httpx 可能未安装（理论上不会），退化为按名字判定
    import httpx as _httpx

    NET_EXC = (
        _httpx.ConnectError, _httpx.ConnectTimeout, _httpx.ReadTimeout,
        _httpx.WriteTimeout, _httpx.PoolTimeout, _httpx.RemoteProtocolError,
        _httpx.ProxyError, _httpx.NetworkError, _httpx.TransportError,
    )
except Exception:  # pragma: no cover
    NET_EXC = (OSError,)


def _net_hint() -> str:
    """给用户/排障用的网络出口提示。"""
    try:
        from ..config import load_settings
        st = load_settings()
        return (f"base_url={st.base_url} trust_env={st.trust_env} "
                f"proxy={st.proxy or '(系统/环境变量代理)'}")
    except Exception:
        return ""


async def _probe_login_uid(settings, login_uid: str, attempts: int = 2) -> tuple:
    """探测 loginUID 是否被 PDM 服务器接受。用一次性 scratch 会话，绝不污染正式会话。

    返回 ``(ok, err_text, kind)``：
      - ``kind="dead"``    ：服务器明确拒绝（20011 / 非法的登录用户 / 空闲超时）
      - ``kind="network"`` ：请求没到服务器（连接/代理/DNS/超时），**不代表 token 失效**

    2026-09-07：网络类异常重试 ``attempts`` 次（每次间隔 1.5s），并把异常类型/repr
    记全——此前只记 ``str(e)``，出现过空消息导致无法定位「打包后连不上」的根因。
    """
    import asyncio

    from ..client import _session_store, get_session

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
            # 判定放宽：HTTP 2xx 一律视为活；只有响应正文带明确「登录用户/超时」特征才判死。
            # （cpcitem.search 用不存在 itemcode 可能返回 200+空集 / 或带业务错误体，统一按活处理。
            #  真正的 20011 由响应体特征识别，避免被「空结果」误杀。）
            body = (res.text or "")[:300]
            looks_dead = (
                res.status_code >= 400
                or '"id":20011' in body
                or '"id": 20011' in body
                or "非法的登录用户" in body
                or "用户空闲已经超时" in body
                or "请重新登录" in body
            )
            _LOG.info("auth probe uid=%s... http=%s dead=%s body=%r",
                      (login_uid or "")[:24], res.status_code, looks_dead, body[:120])
            if looks_dead:
                return False, (body or f"http {res.status_code}"), "dead"
            return True, "", ""
        except Exception as e:  # noqa: BLE001 — 网络/解析异常一律视为不可用
            last_repr = f"{type(e).__name__}: {e!r}"
            is_net = isinstance(e, NET_EXC) or isinstance(e, (TimeoutError, asyncio.TimeoutError))
            _LOG.warning("auth probe exception uid=%s... type=%s repr=%r net=%s (attempt %d/%d) %s",
                         (login_uid or "")[:24], type(e).__name__, e, is_net,
                         i + 1, max(1, attempts), _net_hint(), exc_info=True)
            if is_net and i + 1 < max(1, attempts):
                await asyncio.sleep(1.5)
                continue
            return False, (last_repr or type(e).__name__), "network"
        finally:
            await client.close()
            _session_store.pop(scratch, None)
    return False, last_repr, "network"


async def _resolve_username(settings, login_uid: str, hint_userid: str = "") -> str:
    """用 loginUID 反查服务端权威用户名（免账号密码注入场景）。

    2026-08-26：登录响应 get.xml 已含 <username>，但 loginUID 注入（/session、/auto）
    没有重新登录。服务端「查自己」接口只回显请求体 userid（不校验归属），故需
    hint_userid（前端 localStorage 记住的账号）来验证并取回真名。
    失败返回空串（调用方回退 account / "session-only"）。
    """
    try:
        from ..client_zipxml import SinoCCZipClient
        zc = SinoCCZipClient(settings).set_login_uid(login_uid)
        try:
            return await zc.whoami(hint_userid=hint_userid)
        finally:
            await zc.session.aclose()
    except Exception:
        return ""


async def _verify_username_remote(settings, login_uid: str, candidate: str) -> bool:
    """服务端真伪验证：loginUID 会话下，candidate 是否是本人。

    2026-09-02 两次登录态复现实证：``vaultman cpcworkspace selectall``(wstype=4,modid=12,userid=candidate)
    本人返回 ``<wsname>{candidate}的工作区</wsname>``，他人/不存在只返回「回收站」，
    且与 loginUID 会话绑定（唐阳 token 查曾祥志也只有回收站），可当真伪判别。
    """
    if not login_uid or not candidate:
        return False
    try:
        from ..client_zipxml import SinoCCZipClient
        zc = SinoCCZipClient(settings).set_login_uid(login_uid)
        try:
            xml = await zc.workspace_selectall(wstype="4", modid="12", userid=candidate)
            return f"<wsname>{candidate}的工作区</wsname>" in xml
        finally:
            await zc.session.aclose()
    except Exception:
        return False


def _local_candidate(settings) -> str:
    """本地内存探测候选用户名（不校验）。失败返回空串。

    2026-09-02：内存会话串 ``|{ent};{name};{n};104|`` 稳定命中，作为服务端验证的 hint。
    """
    try:
        from ..auto_login import detect_identity
        r = detect_identity(entcode=settings.default_entcode)
        return r.get("userid", "") if r.get("ok") else ""
    except Exception:
        return ""


@router.post("/login", response_model=LoginOut, summary="登录 PDM 拿 loginUID/JSESSIONID")
async def login(body: LoginIn, request: Request) -> LoginOut:
    client = await _client(request, body.account, body.password, body.session_key)
    sess = await client.login(body.account, body.password)
    await client.close()
    masked = (sess.login_uid[:6] + "***") if sess.login_uid else ""
    return LoginOut(ok=True, user=sess.user, login_uid_mask=masked, entcode=sess.entcode,
                    session_key=body.session_key, login_uid=sess.login_uid)


@router.post("/session", response_model=LoginOut, summary="注入已有登录态（无需密码）")
async def use_session(body: SessionIn, request: Request) -> LoginOut:
    """复用已有的 PDM 会话：只收 loginguid，不存/不用密码。

    loginguid 来源（二选一，见 README 4.3）：
      1) web 版 http://newpdm.carcgl.com/portal/ 登录后，地址栏或 localStorage 里的 loginguid
      2) 之前调用 /auth/login 返回的值
    """
    from ..client import get_session

    settings = _settings(request)
    # ★ 先验证、后落库：死 token 绝不能污染正式会话（2026-09-03 修复）
    ok, err, kind = await _probe_login_uid(settings, body.login_uid)
    if not ok:
        if kind == "network":
            # 请求没到服务器（代理/DNS/连接）→ 不是 token 的锅，别让前端清缓存重试
            raise HTTPException(status_code=503,
                                detail=f"PDM 服务器不可达（网络/代理/DNS）：{err}；{_net_hint()}")
        raise HTTPException(status_code=400,
                            detail=f"loginguid 无效或已过期: {err}")
    sess = get_session(body.session_key)
    sess.login_uid = body.login_uid
    sess.entcode = settings.default_entcode
    # 解析用户名：优先 body.userid 验证（前端 localStorage 提示），否则用本地内存探测候选
    settings = _settings(request)
    hint = body.userid or ""
    name = ""
    confidence = "low"
    if hint and hint != "session-only":
        if await _verify_username_remote(settings, sess.login_uid, hint):
            name, confidence = hint, "high"
        else:
            # 不可信，回退到本地探测
            cand = _local_candidate(settings)
            if cand and await _verify_username_remote(settings, sess.login_uid, cand):
                name, confidence = cand, "high"
    else:
        # 无 hint：直接走本地探测
        cand = _local_candidate(settings)
        if cand and await _verify_username_remote(settings, sess.login_uid, cand):
            name, confidence = cand, "high"

    if not name and hint and hint != "session-only":
        # 全部验证失败，仍把 hint 回填（最低置信，便于用户看到）
        name, confidence = hint, "low"

    if name:
        sess.user = name
    masked = (sess.login_uid[:6] + "***") if sess.login_uid else ""
    return LoginOut(ok=True, user=sess.user or "session-only", login_uid_mask=masked,
                    entcode=sess.entcode, session_key=body.session_key,
                    login_uid=sess.login_uid)


@router.post("/zip-login", response_model=LoginOut,
             summary="客户端 ZIP 通道直登（loginman.jsp 同构协议，无需 sinocc.exe）")
async def zip_login(body: LoginIn, request: Request) -> LoginOut:
    """打包版在原版客户端未运行时的自助登录通道。

    走抓包实证的客户端登录协议（2026-08-18）：客户端自生成 GUID + loginman.jsp
    （Content-Type image/jpeg，body=0x02+ZIP UTF-16 XML，clientversion=3,7,1,0），
    响应返回 CHIGOPDM.a.{GUID}.uacpc 形态的 loginguid（web JSON 登录拿不到，
    且 web 通道密码直登会被 20011 拒绝）。
    """
    from ..client import get_session
    from ..client_zipxml import SinoCCZipClient

    settings = _settings(request)
    zc = SinoCCZipClient(settings, account=body.account, password=body.password)
    try:
        uid = await zc.ensure_login()
        if not uid:
            raise HTTPException(status_code=400, detail="client_login 未返回 loginguid")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"ZIP 直登失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()

    sess = get_session(body.session_key)
    sess.login_uid = uid
    sess.entcode = settings.default_entcode
    sess.user = body.account
    # 用户名权威化：服务端反查（低置信也回填，与 /session 行为一致）
    try:
        if await _verify_username_remote(settings, uid, body.account):
            sess.user = body.account
    except Exception:
        pass
    masked = uid[:6] + "***"
    return LoginOut(ok=True, user=sess.user, login_uid_mask=masked,
                    entcode=sess.entcode, session_key=body.session_key, login_uid=uid)


@router.post("/logout", summary="丢弃当前会话")
async def logout(session_key: str = "default") -> dict:
    from ..client import _session_store
    _session_store.pop(session_key, None)
    return {"ok": True}


@router.get("/auto", summary="自动从本机 PDM 客户端进程检测登录态")
async def auto_detect(request: Request, session_key: str = "default", userid: str = ""):
    """一次扫描拿 loginUID + 用户名（2026-09-02 实证 2/2），无需账号密码 / 前端 hint。

    流程：
      1. 内存扫描拿 loginUID + 候选用户名
      2. probe loginUID 有效性（cpcitem.search）
      3. 用 ``vaultman cpcworkspace selectall`` 服务端验证候选
         → 验证通过：高置信；失败：回退到查询参数 userid（前端 localStorage 提示）；再失败：低置信
    """
    import asyncio
    from ..auto_login import detect_identity

    settings = _settings(request)
    try:
        # 2026-09-07：扫描已加早停+自身进程排除（实测 ~1s），超时仅作极端情况兜底
        ident = await asyncio.wait_for(
            asyncio.to_thread(lambda: detect_identity(entcode=settings.default_entcode)),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "reason": "扫描超时（>45 秒），请以管理员权限运行 bridge 或手动粘贴 loginUID",
                "hint": "若 SinoCC 客户端已登录仍超时，可直接粘贴 loginUID 或改用密码登录"}

    if not ident.get("ok"):
        return {"ok": False,
                "reason": ident.get("reason", "未识别"),
                "scanned": ident.get("scanned")}

    login_uid = ident.get("login_uid", "")
    candidate = ident.get("userid", "")
    if not login_uid:
        return {"ok": False, "reason": "扫到会话身份串但未拿到 loginUID（极罕见）", "detected": ident}

    # ★ 先验证、后落库：内存扫到的 token 可能已被服务器判死（空闲超时/被顶），
    #   不能让它覆盖当前好会话（2026-09-03 修复：20011 反复出现的根因）
    settings = _settings(request)
    ok, err, kind = await _probe_login_uid(settings, login_uid)
    if not ok:
        if kind == "network":
            raise HTTPException(status_code=503,
                                detail=f"PDM 服务器不可达（网络/代理/DNS）：{err}；{_net_hint()}")
        raise HTTPException(status_code=400,
                            detail=f"loginguid 无效或已过期: {err}")

    from ..client import get_session
    sess = get_session(session_key)
    sess.login_uid = login_uid
    sess.entcode = settings.default_entcode

    # 2) 用户名解析：本地候选 → 服务端验证 → 高置信
    user = ""
    confidence = "low"
    if candidate:
        if await _verify_username_remote(settings, login_uid, candidate):
            user, confidence = candidate, "high"
    if not user and userid and userid != "session-only":
        # 回退：前端 localStorage 提示
        resolved = await _resolve_username(settings, login_uid, hint_userid=userid)
        if resolved:
            user, confidence = resolved, "medium"
        else:
            user, confidence = userid, "low"
    if user and user != "session-only":
        sess.user = user

    masked = (login_uid[:10] + "***") if login_uid else ""
    return {
        "ok": True,
        "user": user or "session-only",
        "login_uid_mask": masked,
        "login_uid": login_uid,
        "entcode": settings.default_entcode,
        "session_key": session_key,
        "confidence": confidence,
        "detected": ident,
    }


# Helper that all routers can use to make a logged-in client
async def logged_in_client(request: Request, account: str, password: str, session_key: str = "default") -> SinoCCClient:
    client = SinoCCClient(_settings(request), account=account, password=password, session_key=session_key)
    return client
