"""SinoCC PDM **SOAP channel** client (复刻 Delphi WebBroker SOAP 私有协议).

This is the protocol the real SinoCC.exe desktop client uses — NOT the web JSON
channel in `client.py`. Verified facts (2026-08-05/13):

- Endpoint:  POST /jsp/loginman.jsp?loginguid=<UID>   (no format/classid/action)
- Header:    Content-Type: application/soap+xml; charset=utf-8
- Body:      <soapenv:Envelope><soapenv:Header><pCPCReq>…</pCPCReq></soapenv:Header>
               <soapenv:Body><multiSoReq><formname>…</formname>
                 <method>…</method><oStr><![CDATA[…]]></oStr></multiSoReq>
               </soapenv:Body></soapenv:Envelope>
- Login:     reuse the web JSON login (client.SinoCCClient.login) to obtain a
             loginguid + JSESSIONID; the same loginguid is valid as the SOAP
             <LoginUID>. (Probed: cpcwfobj.gettaskstartor / cpcwfproc.getsubmitnodeinfo
             return HTTP 200 over this channel with a web-login loginguid.)

The exact `oStr` XML for each (formname, method) is sourced from the Fiddler
capture and lives in `soap_templates.py`. This module owns only the *transport*
(envelope build + send + response parse), which is fully determined without the
capture. Per-action body builders are filled in once the capture is parsed.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx
from lxml import etree

from .client import Session, get_session, SinoCCClient
from .config import Settings

log = logging.getLogger("sinocc.client_proto")

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"

# Envelope template. NOTE: once the Fiddler capture arrives, verify the namespace
# prefix / root element EXACTLY match the client bytes (Resin's SOAP reader can be
# strict). If the capture uses a bare <soap:Envelope> or no envelope at all, swap it here.
ENVELOPE_TMPL = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{ns}">
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
      <oStr><![CDATA[{ostr}]]></oStr>
    </multiSoReq>
  </soapenv:Body>
</soapenv:Envelope>
"""


@dataclass
class SoapResp:
    ok: bool
    status_code: int
    fault: Optional[str] = None
    raw: str = ""
    # inner content of <multiSoReq> (the business payload), as pretty XML/text
    body_xml: str = ""
    # convenience: parsed lxml root of body_xml (None if not XML)
    _root: Optional[etree._Element] = None

    def xpath_text(self, expr: str) -> str:
        if self._root is None:
            return ""
        els = self._root.xpath(expr)
        if not els:
            return ""
        el = els[0]
        return (el.text or "") if isinstance(el, etree._Element) else str(el)

    def find_first(self, local_tag: str) -> str:
        return self.xpath_text(f"//*[local-name()='{local_tag}']")


def build_envelope(session: Session, *, formname: str, method: str, ostr: str) -> str:
    return ENVELOPE_TMPL.format(
        ns=SOAP_NS,
        user=session.user,
        entcode=session.entcode,
        login_uid=session.login_uid,
        invorgid=session.invorgid,
        formname=formname,
        method=method,
        ostr=ostr,
    )


class SinoCCSoapClient:
    """Transport for the SOAP channel. Reuses the shared web session store, so a
    prior login (web JSON) is visible here."""

    def __init__(self, settings: Settings, *, session_key: str = "default",
                 account: Optional[str] = None, password: Optional[str] = None):
        self.settings = settings
        self._session_key = session_key
        self._account = account
        self._password = password
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.timeout,
            follow_redirects=False,
            headers={
                "User-Agent": "sinocc-pdm-bridge/soap",
                "Accept": "application/xml, text/xml, */*",
                "Content-Type": "application/soap+xml; charset=utf-8",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def session(self) -> Session:
        return get_session(self._session_key)

    async def ensure_login(self) -> Session:
        sess = self.session
        if not sess.login_uid:
            helper = SinoCCClient(self.settings, account=self._account,
                                  password=self._password, session_key=self._session_key)
            sess = await helper.login()
            await helper.close()
        return sess

    async def call_soap(self, formname: str, method: str, ostr: str,
                        *, retry: bool = True) -> SoapResp:
        sess = await self.ensure_login()
        envelope = build_envelope(sess, formname=formname, method=method, ostr=ostr)
        cookies = {"JSESSIONID": sess.jsessionid} if sess.jsessionid else None
        if self.settings.dump_soap:
            log.debug("SOAP OUT %s.%s\n%s", formname, method, envelope)
        r = await self._http.post(
            "/jsp/loginman.jsp",
            params={"loginguid": sess.login_uid},
            content=envelope.encode("utf-8", errors="replace"),
            cookies=cookies,
        )
        text = r.text
        if self.settings.dump_soap:
            log.debug("SOAP IN %s\n%s", r.status_code, text[:2000])

        # session expiry -> re-login once and retry
        if retry and (r.status_code in (401, 403) or _looks_expired(text)):
            if not self._account or not self._password:
                return SoapResp(ok=False, status_code=r.status_code, fault="session expired, no creds", raw=text)
            await self.ensure_login()
            return await self.call_soap(formname, method, ostr, retry=False)

        return _parse_soap_response(r.status_code, text)

    # convenience wrappers once soap_templates.py is populated
    async def call_template(self, formname: str, method: str,
                            fields: Optional[dict] = None, *, ostr: Optional[str] = None) -> SoapResp:
        from . import soap_templates
        if ostr is None:
            ostr = soap_templates.render(formname, method, fields or {})
        return await self.call_soap(formname, method, ostr)


def _looks_expired(text: str) -> bool:
    low = text.lower()
    return any(s in low for s in ("sessionexpired", "logintimeout", "please login", "needlogin", "未登录"))


def _parse_soap_response(status_code: int, text: str) -> SoapResp:
    if status_code >= 400:
        return SoapResp(ok=False, status_code=status_code, fault=f"HTTP {status_code}", raw=text)
    try:
        root = etree.fromstring(text.encode("utf-8", errors="replace"))
    except etree.XMLSyntaxError:
        # not XML — return raw
        return SoapResp(ok=True, status_code=status_code, raw=text, body_xml=text)

    # SOAP Fault?
    faults = root.xpath("//*[local-name()='Fault']")
    if faults:
        return SoapResp(ok=False, status_code=status_code, fault=etree.tostring(faults[0], encoding="unicode"), raw=text)

    # inner <multiSoReq> payload
    msr = root.xpath("//*[local-name()='multiSoReq']")
    if msr:
        inner = msr[0]
        # drop the oStr echo if present, keep the result node
        body = etree.tostring(inner, encoding="unicode")
        return SoapResp(ok=True, status_code=status_code, raw=text, body_xml=body, _root=inner)

    body = etree.tostring(root, encoding="unicode")
    return SoapResp(ok=True, status_code=status_code, raw=text, body_xml=body, _root=root)


__all__ = ["SinoCCSoapClient", "SoapResp", "build_envelope"]
