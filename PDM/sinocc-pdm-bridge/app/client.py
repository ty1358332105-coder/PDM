"""SinoCC (Chigo) PDM SOAP / HTTP client.

The PDM server exposes a small, undocumented set of JSP endpoints with bespoke
SOAP bodies. There is no WSDL, so we hand-build envelopes using string templates
that mirror the patterns in `CCPM.bpl` / `CCSht.bpl`:

    POST {base}/jsp/{endpoint}.jsp?loginguid=<UID>
    Content-Type: application/soap+xml; charset=utf-8
    Cookie: JSESSIONID=...
    Body: <soapenv:Envelope>...<multiSoReq>...</multiSoReq>...</soapenv:Envelope>
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx
from lxml import etree

from .config import Settings

log = logging.getLogger("sinocc.client")

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
NSMAP = {"soapenv": SOAP_NS}


@dataclass
class Session:
    """In-process session cache.

    A real deployment with multiple uvicorn workers should swap this for Redis.
    """

    user: str = ""
    login_uid: str = ""
    jsessionid: str = ""
    entcode: str = ""
    invorgid: str = ""
    created_at: float = field(default_factory=time.time)


# Simple in-memory session store (per-process). In multi-worker / multi-pod
# environments, replace with the Redis-backed implementation in `session_store.py`.
_session_store: Dict[str, Session] = {}


def get_session(key: str = "default") -> Session:
    return _session_store.setdefault(key, Session())


SOAP_ENVELOPE_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soapenv:Header>
    <pCPCReq>
      <UserID>{user}</UserID>
      <OrgCode>{entcode}</OrgCode>
      <LoginUID>{login_uid}</LoginUID>
      <InvOrgID>{invorgid}</InvOrgID>
    </pCPCReq>
  </soapenv:Header>
  <soapenv:Body>
    <multiSoReq>
      <formname>{formname}</formname>
      <method>{method}</method>
      <oStr><![CDATA[{payload}]]></oStr>
    </multiSoReq>
  </soapenv:Body>
</soapenv:Envelope>
"""


def _serialize_tpost_type(items: Dict[str, Any]) -> str:
    """Render a TPostType as XML fragment.

    TPostType is a generic key/value bag the server uses to dispatch. We don't
    have the exact XML schema; the practice is to emit one <P> per field. The
    server is forgiving about attributes vs elements.
    """
    chunks = ["<root>"]
    for k, v in items.items():
        if v is None:
            continue
        if isinstance(v, bool):
            val = "1" if v else "0"
        else:
            val = str(v)
        chunks.append(f'<{k}>{val}</{k}>')
    chunks.append("</root>")
    return "".join(chunks)


def build_soap(method: str, *, formname: str, payload: Dict[str, Any], session: Session) -> str:
    return SOAP_ENVELOPE_TMPL.format(
        user=session.user,
        entcode=session.entcode,
        login_uid=session.login_uid,
        invorgid=session.invorgid,
        formname=formname,
        method=method,
        payload=_serialize_tpost_type(payload),
    )


@dataclass
class SoapResult:
    status_code: int
    text: str
    parsed: Optional[Any] = None
    raw_envelope: str = ""


class SinoCCClient:
    """Async HTTP wrapper around the JSP endpoints.

    Auto re-login when the server returns 401/440 (login expired).
    """

    def __init__(self, settings: Settings, *, account: Optional[str] = None,
                 password: Optional[str] = None,
                 session_key: str = "default"):
        self.settings = settings
        self._account = account
        self._password = password
        self._session_key = session_key
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.timeout,
            follow_redirects=False,
            trust_env=settings.trust_env,          # 2026-09-07：可关系统代理 / 可显式指定
            proxy=settings.proxy or None,
            headers={
                "User-Agent": "sinocc-pdm-bridge/0.1",
                "Accept": "application/xml, text/xml, */*",
            },
        )

    async def __aenter__(self) -> "SinoCCClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def session(self) -> Session:
        return get_session(self._session_key)

    async def login(self, account: Optional[str] = None, password: Optional[str] = None) -> Session:
        """Full login with account+password. Use only when no existing loginUID is available."""
        account = account or self._account
        password = password or self._password
        if not account or not password:
            raise ValueError("login requires account & password")

        # Verified protocol (2026-08-05):
        # POST /jsp/loginman.jsp?loginguid=&format=mjson&classid=loginuser&action=login
        # {"userid", "md5sn": md5hex(pwd), "entcode", "userpass": "PORTAL"+b64(utf8(pwd+rand)), "isportal": 2}
        #
        # 2026-08-18 实测修正：登录请求的 User-Agent 决定服务端是否下发 .uacpc 写权限签名
        #   UA=sinocc-pdm-bridge/0.1（bridge 默认） → 无签名 → 文件服务器拒写(50003)
        #   UA=python-httpx/Chrome 等通用 UA      → 带 .uacpc 签名 ✓
        #   UA=Mozilla/3.0 (compatible;Super Indy Library) → 触发「版本已过期」校验(400)，
        #      因我们无法提供真实客户端版本号，不可用。
        # 结论：登录必须用通用浏览器 UA（不能用 bridge 自定义 UA，也不能用真客户端 UA）。
        rand_digits = "".join(str(random.randint(0, 9)) for _ in range(len(password)))
        userpass = "PORTAL" + base64.b64encode((password + rand_digits).encode("utf-8")).decode("ascii")
        body = {
            "userid": account,
            "md5sn": hashlib.md5(password.encode("utf-8")).hexdigest(),
            "entcode": self.settings.default_entcode,
            "userpass": userpass,
            "isportal": 2,
        }
        r = await self._http.post(
            "/jsp/loginman.jsp",
            params={"loginguid": "", "format": "mjson", "classid": "loginuser", "action": "login"},
            json=body,
            headers={
                "Content-Type": "application/json",
                # 必须用通用浏览器 UA 才能拿到 .uacpc 写权限签名（见上注释）
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            },
        )
        if r.status_code >= 400:
            raise RuntimeError(f"login failed: {r.status_code} {r.text[:200]!r}")

        data = r.json()
        lg = data.get("loginguid", "")
        if not lg:
            raise RuntimeError(f"login response missing loginguid: {r.text[:200]!r}")

        sess = self.session
        sess.user = account
        sess.login_uid = lg
        sess.jsessionid = r.cookies.get("JSESSIONID", sess.jsessionid)
        sess.entcode = data.get("entcode") or self.settings.default_entcode
        sess.invorgid = str(data.get("currmrporgid") or self.settings.default_invorgid or "")
        log.info("login OK user=%s loginUID=%s...", account, lg[:6])
        return sess

    @staticmethod
    def _extract_login_uid(html_or_xml: str) -> str:
        """Server returns loginUID embedded in the body or session."""
        m = re.search(r"loginguid=([A-Za-z0-9_\-]+)", html_or_xml)
        if m:
            return m.group(1)
        # XML/CDATA fallback
        m = re.search(r"<LoginUID>([^<]+)</LoginUID>", html_or_xml)
        return m.group(1) if m else ""

    async def call(self, endpoint: str, method: str, *, formname: str,
                   payload: Dict[str, Any], retry: bool = True) -> SoapResult:
        """POST to loginman.jsp (web-API JSON mode).

        Verified 2026-08-05: business calls are
        POST /jsp/loginman.jsp?loginguid=<LG>&format=mjson&classid=<cls>&action=<act>
        with a JSON body of parameters (no SOAP envelope needed).
        `endpoint` here is treated as the classid; `method` as the action.
        """
        sess = self.session
        if not sess.login_uid:
            if not self._account or not self._password:
                raise RuntimeError("not logged in: call login() or inject a loginUID via /auth/session")
            await self.login()

        params = {
            "loginguid": sess.login_uid,
            "format": "mjson",
            "classid": endpoint,
            "action": method,
        }
        cookies = {"JSESSIONID": sess.jsessionid} if sess.jsessionid else None
        if self.settings.dump_soap:
            log.debug("JSON OUT -> /jsp/loginman.jsp %s %s", params, payload)

        r = await self._http.post(
            "/jsp/loginman.jsp",
            params=params,
            json=payload,
            cookies=cookies,
            headers={"Content-Type": "application/json"},
        )
        text = r.text
        parsed = None
        try:
            parsed = etree.fromstring(text.encode("utf-8", errors="ignore"))
        except etree.XMLSyntaxError:
            pass

        # Auto re-login on session expiry
        if retry and (r.status_code in (401, 403) or (parsed is not None and _looks_like_session_expired(parsed))):
            log.warning("session expired (status=%s), retrying after re-login", r.status_code)
            if not self._account or not self._password:
                raise RuntimeError("session expired and no credentials cached")
            await self.login()
            return await self.call(endpoint, method, formname=formname, payload=payload, retry=False)

        return SoapResult(status_code=r.status_code, text=text, parsed=parsed, raw_envelope=str(payload))


def _looks_like_session_expired(root: etree._Element) -> bool:
    txt = etree.tostring(root, encoding="unicode").lower()
    return any(s in txt for s in ("sessionexpired", "logintimeout", "please login", "needlogin"))


__all__ = [
    "SinoCCClient",
    "Session",
    "build_soap",
    "SoapResult",
]
