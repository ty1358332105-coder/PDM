"""Dependency helpers for getting a configured client from request state.

Support two auth modes:
  A) Session-injection (recommended): /auth/session already stored a loginUID in
     the in-process session store → business calls need NO credentials headers.
  B) Per-request credentials: X-PDM-Account + X-PDM-Password headers.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException, Request

from .client import SinoCCClient, get_session
from .config import Settings


def _settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_client(
    request: Request,
    x_pdm_account: Optional[str] = Header(default=None, description="PDM 工号（方式 B 用）"),
    x_pdm_password: Optional[str] = Header(default=None, description="PDM 密码（方式 B 用）"),
    x_pdm_session: str = Header("default", alias="X-PDM-Session", description="会话标识（方式 A 用）"),
) -> SinoCCClient:
    """Return a client whose session store already has credentials or loginUID.

    Precedence: if the session store for x_pdm_session already has a loginUID,
    use it (session mode). Otherwise require account+password headers.
    """
    sess = get_session(x_pdm_session)

    # Mode A: existing loginUID in session store
    if sess.login_uid:
        return SinoCCClient(_settings(request), session_key=x_pdm_session)

    # Mode B: per-request credentials
    if x_pdm_account and x_pdm_password:
        return SinoCCClient(_settings(request), account=x_pdm_account,
                            password=x_pdm_password, session_key=x_pdm_session)

    raise HTTPException(
        status_code=401,
        detail="未登录：请先调用 POST /auth/session 注入登录态，或传 X-PDM-Account/X-PDM-Password 头",
    )
