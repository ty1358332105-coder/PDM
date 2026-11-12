"""Files router (downloading CAD/docs from fileman.jsp)."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import FileResponse, StreamingResponse

from ..client import SinoCCClient
from ..deps import get_client

router = APIRouter()


async def _client(request: Request, client: SinoCCClient = Depends(get_client)) -> SinoCCClient:
    return client


@router.get("/{objname:path}", summary="下载/预览文件（fileman.jsp）")
async def download(
    objname: str,
    request: Request,
    ver: str = "",
    client: SinoCCClient = Depends(_client),
):
    sess = client.session
    if not sess.login_uid:
        raise HTTPException(status_code=401, detail="not logged in")
    # Stream the file via fileman.jsp
    url = f"{client.settings.base_url}/jsp/fileman.jsp"
    params = {"loginguid": sess.login_uid, "objname": objname, "ver": ver}
    cookies = {"JSESSIONID": sess.jsessionid} if sess.jsessionid else None

    import httpx
    async with httpx.AsyncClient(timeout=client.settings.timeout) as h:
        # Stream the response back to the client
        req = h.build_request("GET", url, params=params, cookies=cookies)
        r = await h.send(req, stream=True)
        if r.status_code >= 400:
            text = (await r.aread()).decode(errors="ignore")[:500]
            await r.aclose()
            raise HTTPException(status_code=r.status_code, detail=text)

        async def gen():
            async for chunk in r.aiter_bytes():
                yield chunk
            await r.aclose()

        # best-effort filename
        fname = os.path.basename(objname.split("@")[-1] if "@" in objname else objname)
        headers = {"Content-Disposition": f'inline; filename="{fname}"'}
        return StreamingResponse(gen(), headers=headers, media_type=r.headers.get("Content-Type", "application/octet-stream"))
