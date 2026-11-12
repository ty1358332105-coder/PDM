"""Product workspace router (产品工作区 / 物料分类树).

Exposes:
- GET  /product/workspace           产品工作区根（直接子文件夹）
- GET  /product/folder/{fdrid}      展开文件夹（子文件夹 + 物料列表）
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Query, Request, UploadFile
from pydantic import BaseModel

from ..client_zipxml import SinoCCZipClient
from ..deps import get_client
from ..client import get_session

router = APIRouter()


def _zipxml_client(request: Request) -> SinoCCZipClient:
    settings = request.app.state.settings
    sess = get_session("default")
    if not sess.login_uid:
        raise HTTPException(status_code=401, detail="未登录：请先调用 POST /auth/session 注入登录态")
    return SinoCCZipClient(settings).set_login_uid(sess.login_uid).set_user(sess.user)


def _detect_userid() -> str:
    """安全过滤的当前用户名（2026-09-02 修）。

    历史实现硬编码 ``C:\\sinocchome\\CHIGOCAC\\<用户名>\\``，但实际目录已变更为
    ``vault_1\\<用户名>`` 且会被重新登录清空 → 不可靠。
    现策略：优先取当前 session 已通过服务端验证的 user；无会话时回退到本地内存探测。
    """
    sess = get_session("default")
    if sess.user and sess.user != "session-only":
        return sess.user
    # 兜底：本地内存探测（客户端运行中但未注入 session）
    try:
        from ..auto_login import detect_session_tag
        t = detect_session_tag()
        if t:
            return t["userid"]
    except Exception:
        pass
    return ""


def _current_userid(userid_param: str) -> str:
    """解析并校验当前登录用户（2026-08-26 安全修复）。

    PDM 服务端 cpcworkspace.selectall 的 userid 参数不校验 loginUID 归属（越权实证：
    唐阳 token + userid=康力 可返回康力的工作区 8732）。为避免 bridge 放大越权，
    userid 只允许 = 本机探测用户 或 会话用户；传他人名字一律拒绝。
    """
    sess = get_session("default")
    local = _detect_userid()
    uid = userid_param or sess.user
    if uid and uid != "session-only" and (uid == local or uid == sess.user):
        return uid
    # 无参数/会话无用户时，回退本机探测用户（仅查"自己"）
    if local:
        return local
    if sess.user and sess.user != "session-only":
        return sess.user
    return ""


# ---------- DTOs ----------
class FolderNode(BaseModel):
    fdrid: str = ""
    fdrname: str = ""
    attribute: str = ""
    flag: str = ""
    typepath: str = ""
    idpath: str = ""
    objaccess: str = ""
    objaccesstype: str = ""


class ItemHit(BaseModel):
    item_id: str = ""
    asmid: str = ""
    item_code: str = ""
    item_name: str = ""
    product_model: str = ""
    custom_model: str = ""
    drawid: str = ""
    spec: str = ""
    item_unit: str = ""
    rev: str = ""
    purch_flag: str = ""
    raw: dict = {}


class FolderContent(BaseModel):
    fdrid: str = ""
    fdrname: str = ""
    subfolders: List[FolderNode] = []
    items: List[ItemHit] = []
    has_items: bool = False


# ---------- 文档管理（vaultman.jsp，2026-08-25 抓包 doc_mgmt_capture1 实证） ----------
# 树加载：cpcworkspace.selectall（工作区）→ cpcworkspace.selectref(wsid)（顶层文件夹）
#        → cpcfdr.selectref(fdrid)（子文件夹 <fdr> + 文档 <doc> 混合返回）
# 文档工作区 wsid=-4 / 产品工作区 wsid=-14。idpath 形如 -4\115179\397449\5916764\
class DocNode(BaseModel):
    docid: str = ""
    docname: str = ""
    rev: str = ""
    revlabel: str = ""
    stat: str = ""
    plmstate: str = ""
    creator: str = ""
    updator: str = ""
    createtime: str = ""
    updatetime: str = ""
    filetime: str = ""
    oldsize: str = ""
    zipsize: str = ""
    crc32: str = ""
    locked: str = ""
    actived: str = ""
    flag: str = ""
    doctype: str = ""
    isfamily: str = ""
    isvirtual: str = ""
    locid: str = ""
    appid: str = ""
    objaccess: str = ""
    idpath: str = ""
    typepath: str = ""
    raw: dict = {}


class DocFolderContent(BaseModel):
    fdrid: str = ""
    fdrname: str = ""
    subfolders: List[FolderNode] = []
    docs: List[DocNode] = []
    has_docs: bool = False


class DocSearchResult(BaseModel):
    q: str = ""
    total: int = 0
    docs: List[DocNode] = []


# ---------- 我的文件（vaultman.jsp，2026-08-25 抓包 myfiles_capture 实证） ----------
# 我的文件 = 个人工作区（modid=12, wstype=4）：workspace.selectall → 工作区列表（个人+回收站-99）
# → workspace.selectref(wsid) → 顶层文件夹 <fdr> + 共享快捷方式 <shortcut>（reftype=-2）
# → cpcfdr.selectref(fdrid)（复用 /docs/folder）
class WsNode(BaseModel):
    wsid: str = ""
    wsname: str = ""
    wstype: str = ""


class ShortcutNode(BaseModel):
    """共享快捷方式行 = workspace selectref 根响应 <shortcut> 整行 10 字段原样存储
    （2026-09-02 shared_with_me_v2 实证：sourcetypepath/objaccesspass 为点开时
    cpcshortcut selectref 请求体所需，必须原样回传，缺则无法展开）。"""
    refid: str = ""
    scname: str = ""
    idpath: str = ""
    sourceidpath: str = ""
    reftype: str = ""
    objaccess: str = ""
    objaccesstype: str = ""
    typepath: str = ""
    sourcetypepath: str = ""
    objaccesspass: str = ""


class FileWsContent(BaseModel):
    wsid: str = ""
    wsname: str = ""
    folders: List[FolderNode] = []
    shortcuts: List[ShortcutNode] = []
    wsright: str = ""  # data 层 <wsright>，点开共享目录时原样回传（2026-09-02 实证逐字节一致）


class ShortcutContent(BaseModel):
    """共享目录展开结果（cpcshortcut selectref 响应解析）。"""
    refid: str = ""
    scname: str = ""
    fdrid: str = ""
    fdrname: str = ""
    subfolders: List[FolderNode] = []
    docs: List[DocNode] = []
    has_docs: bool = False


@router.get("/files/workspaces", summary="我的文件：工作区列表（个人工作区 + 回收站；仅限当前登录用户）")
async def files_workspaces(request: Request, userid: str = Query("", description="当前登录用户名（如 唐阳）；仅允许本人，传他人名字将被拒绝（2026-08-26 安全修复）")):
    zc = _zipxml_client(request)
    uid = _current_userid(userid)
    if not uid:
        return {"ok": True, "workspaces": [], "need_userid": True,
                "error": "无法确定当前用户名（loginUID 注入无 user 信息，客户端目录未探测到），请用账号密码登录"}
    if userid and userid != uid:
        return {"ok": False, "workspaces": [], "error": f"无权查询用户 {userid} 的工作区（仅限当前登录用户）"}
    try:
        xml = await zc.workspace_selectall(wstype="4", modid="12", userid=uid)
    except HTTPException:
        raise
    except Exception:
        return {"ok": True, "workspaces": []}
    wss = []
    for w in re.findall(r"<workspace>(.*?)</workspace>", xml, re.S):
        wss.append(WsNode(wsid=_g("wsid", w), wsname=_g("wsname", w), wstype=_g("wstype", w)))
    return {"ok": True, "workspaces": wss, "userid": uid}


@router.get("/files/workspace", response_model=FileWsContent,
            summary="我的文件：个人工作区根（文件夹 + 共享快捷方式；仅限本人 wsid）")
async def files_workspace(request: Request, wsid: str = Query("7203", description="个人工作区 wsid（仅限当前登录用户自己的，否则 403）"),
                          wsname: str = Query("", description="工作区名，如 唐阳的工作区")):
    zc = _zipxml_client(request)
    uid = _current_userid("")
    if not uid:
        raise HTTPException(status_code=403, detail="无法确定当前登录用户")
    # 工作区归属校验：wsid 必须在当前用户自己的工作区列表内（2026-08-26 安全修复）
    try:
        xml_ws = await zc.workspace_selectall(wstype="4", modid="12", userid=uid)
    except Exception:
        xml_ws = ""
    valid_ids = {_g("wsid", w) for w in re.findall(r"<workspace>(.*?)</workspace>", xml_ws, re.S)}
    if wsid not in valid_ids:
        raise HTTPException(status_code=403, detail=f"无权访问工作区 {wsid}（仅限当前登录用户 {uid} 自己的工作区）")
    try:
        xml = await zc.workspace_selectref(wsid=wsid, wsname=wsname, wstype="4")
    except HTTPException:
        raise
    except Exception:
        return FileWsContent(wsid=wsid, wsname=wsname, folders=[], shortcuts=[])
    out = FileWsContent(wsid=wsid, wsname=wsname)
    # data 层 <wsright>（selectref 响应中唯一非空的 wsright 标签，shortcut/fdr 行内不含）
    m_ws = re.search(r"<wsright>([^<]*)</wsright>", xml)
    out.wsright = m_ws.group(1) if m_ws else ""
    for f in re.findall(r"<fdr>(.*?)</fdr>", xml, re.S):
        out.folders.append(FolderNode(
            fdrid=_g("fdrid", f), fdrname=_g("fdrname", f),
            attribute=_g("attribute", f), flag=_g("flag", f),
            typepath=_g("typepath", f), idpath=_g("idpath", f),
            objaccess=_g("objaccess", f), objaccesstype=_g("objaccesstype", f),
        ))
    for s in re.findall(r"<shortcut>(.*?)</shortcut>", xml, re.S):
        out.shortcuts.append(ShortcutNode(
            refid=_g("refid", s), scname=_g("scname", s),
            idpath=_g("idpath", s), sourceidpath=_g("sourceidpath", s),
            reftype=_g("reftype", s), objaccess=_g("objaccess", s),
            objaccesstype=_g("objaccesstype", s), typepath=_g("typepath", s),
            sourcetypepath=_g("sourcetypepath", s), objaccesspass=_g("objaccesspass", s),
        ))
    return out


@router.get("/files/shortcut/{refid}", response_model=ShortcutContent,
            summary="我的文件：展开共享快捷方式目录（cpcshortcut selectref；返回该共享目录内文档）")
async def files_shortcut(
    request: Request,
    refid: str = Path(..., description="共享目录 id（= workspace 根响应 shortcut.refid）"),
    scname: str = Query("", description="共享目录名（根响应 shortcut.scname 原样回传）"),
    idpath: str = Query("", description="本机侧 idpath（根响应原样回传，如 7203\\597400\\）"),
    typepath: str = Query("", description="本机侧 typepath（根响应原样回传，如 1\\2\\）"),
    sourceidpath: str = Query("", description="源侧完整路径（根响应原样回传，如 6486\\597400\\）"),
    sourcetypepath: str = Query("", description="源侧类型路径（根响应原样回传，如 1\\2\\）"),
    reftype: str = Query("-2", description="引用类型（根响应 reftype，固定 -2）"),
    objaccess: str = Query("", description="根响应 shortcut.objaccess 原样回传"),
    objaccesstype: str = Query("", description="根响应 shortcut.objaccesstype 原样回传"),
    objaccesspass: str = Query("", description="根响应 shortcut.objaccesspass 原样回传"),
    wsright: str = Query("", description="workspace 根响应 data 层 wsright（files/workspace 返回，原样回传）"),
):
    # parentid = idpath 第一段（父 wsid，REQ3 实证 7203）；parenttype 固定 1（父为 workspace）
    parentid = idpath.rstrip("\\").split("\\")[0] if idpath.rstrip("\\") else ""
    zc = _zipxml_client(request)
    try:
        xml = await zc.shortcut_selectref(
            refid=refid, scname=scname, idpath=idpath, typepath=typepath,
            sourceidpath=sourceidpath, sourcetypepath=sourcetypepath, reftype=reftype,
            objaccess=objaccess, objaccesstype=objaccesstype, objaccesspass=objaccesspass,
            wsright=wsright, parentid=parentid,
        )
    except HTTPException:
        raise
    except Exception:
        return ShortcutContent(refid=refid, scname=scname, subfolders=[], docs=[], has_docs=False)
    out = _parse_doc_folder_xml(xml)
    return ShortcutContent(refid=refid, scname=scname,
                           fdrid=out.fdrid, fdrname=out.fdrname,
                           subfolders=out.subfolders, docs=out.docs, has_docs=out.has_docs)


def _parse_doc_folder_xml(xml: str) -> DocFolderContent:
    """解析文档管理 cpcworkspace/cpcfdr selectref 响应（子文件夹 + 文档）。"""
    out = DocFolderContent()
    m = re.search(r"<response>\s*<data>\s*<fdrid>([^<]*)</fdrid>\s*<fdrname>([^<]*)</fdrname>", xml, re.S)
    if m:
        out.fdrid = m.group(1)
        out.fdrname = m.group(2)
    for f in re.findall(r"<fdr>(.*?)</fdr>", xml, re.S):
        out.subfolders.append(FolderNode(
            fdrid=_g("fdrid", f), fdrname=_g("fdrname", f),
            attribute=_g("attribute", f), flag=_g("flag", f),
            typepath=_g("typepath", f), idpath=_g("idpath", f),
            objaccess=_g("objaccess", f), objaccesstype=_g("objaccesstype", f),
        ))
    for d in re.findall(r"<doc>(.*?)</doc>", xml, re.S):
        docid = _g("docid", d)
        if not docid:
            continue
        raw = dict(re.findall(r"<([a-zA-Z]\w*)>([^<]*)</\1>", d))
        out.docs.append(DocNode(
            docid=docid, docname=_g("docname", d), rev=_g("rev", d),
            revlabel=_g("revlabel", d), stat=_g("stat", d), plmstate=_g("plmstate", d),
            creator=_g("creator", d), updator=_g("updator", d),
            createtime=_g("createtime", d), updatetime=_g("updatetime", d),
            filetime=_g("filetime", d), oldsize=_g("oldsize", d), zipsize=_g("zipsize", d),
            crc32=_g("crc32", d), locked=_g("locked", d), actived=_g("actived", d),
            flag=_g("flag", d), doctype=_g("doctype", d), isfamily=_g("isfamily", d),
            isvirtual=_g("isvirtual", d), locid=_g("locid", d), appid=_g("appid", d),
            objaccess=_g("objaccess", d), idpath=_g("idpath", d), typepath=_g("typepath", d),
            raw=raw,
        ))
    out.has_docs = len(out.docs) > 0
    return out


@router.get("/docs/workspace", response_model=DocFolderContent,
            summary="文档工作区根（wsid=-4，直接子文件夹）")
async def docs_workspace(request: Request):
    zc = _zipxml_client(request)
    try:
        xml = await zc.workspace_selectref(wsid="-4", wsname="文档工作区")
    except HTTPException:
        raise
    except Exception:
        return DocFolderContent(fdrid="-4", fdrname="文档工作区", subfolders=[], docs=[], has_docs=False)
    return _parse_doc_folder_xml(xml)


@router.get("/docs/folder/{fdrid}", response_model=DocFolderContent,
            summary="展开文档文件夹（子文件夹 + 文档列表）")
async def docs_folder(
    request: Request,
    fdrid: str = Path(..., description="文件夹 fdrid"),
    fdrname: str = Query("", description="文件夹名（回显用，可选）"),
    idpath: str = Query("", description="节点自身 idpath（来自父响应，必带）"),
    typepath: str = Query("", description="节点自身 typepath（来自父响应，必带）"),
    objaccess: str = Query("", description="节点自身 objaccess（来自父响应，必带）"),
    objaccesstype: str = Query("", description="节点自身 objaccesstype（来自父响应，必带）"),
):
    # 从 idpath/typepath 派生 parentid/parenttype（完整父路径，服务端据此定位）
    parentid, parenttype = "", ""
    if idpath:
        parentid = idpath.rstrip("\\").rsplit("\\", 1)[0] if "\\" in idpath.rstrip("\\") else "-4"
    if typepath:
        parenttype = typepath.rstrip("\\").rsplit("\\", 1)[0] if "\\" in typepath.rstrip("\\") else "1"
    zc = _zipxml_client(request)
    try:
        xml = await zc.fdr_selectref(fdrid=fdrid, fdrname=fdrname,
                                     idpath=idpath, typepath=typepath,
                                     parentid=parentid, parenttype=parenttype,
                                     objaccess=objaccess, objaccesstype=objaccesstype)
    except HTTPException:
        raise
    except Exception:
        return DocFolderContent(fdrid=fdrid, fdrname=fdrname, subfolders=[], docs=[], has_docs=False)
    return _parse_doc_folder_xml(xml)


# =========================== 我的文件：写操作（2026-09-08 抓包实证） ===========================
# 协议来源 docs/captured/myfiles_ops_v1/PROTOCOL.md：
#   导入    = getupstatus + file upload(8011) + cpcdoc insert
#   重命名  = cpcdoc select 回显整行 + 改 docname → cpcdoc rename
#   复制粘贴 = 轻量行 + 目标 parentid/parenttype → cpcdoc paste（服务端给新 docid）
#   剪切移动 = 同构轻量行 → cpcdoc cut（docid 不变，响应 act=6）
#   删除    = 列表 <doc> 行 + parentid/parenttype/wsright → cpcdoc delete


@router.post("/files/import", summary="我的文件：导入本地文件到文件夹")
async def files_import(
    request: Request,
    file: UploadFile = File(..., description="本地文件（multipart）"),
    wsid: str = Form(..., description="所在工作区 wsid（如 7203）"),
    parentid: str = Form(..., description="目标文件夹完整 idpath（如 7203\\511269）"),
    parenttype: str = Form(..., description="目标文件夹完整 typepath（如 1\\2）"),
    clientPath: str = Form("", description="本地原始路径（仅记录到 clientfilename，可空）"),
):
    zc = _zipxml_client(request)
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件内容为空")
    try:
        res = await zc.doc_import(wsid=wsid, parentid=parentid, parenttype=parenttype,
                                  filename=file.filename or "unnamed", raw=raw,
                                  client_path=clientPath or (file.filename or ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"导入失败：{e}")
    return {"ok": True, "docid": res.get("docid", ""), "docname": res.get("docname", ""),
            "revlabel": res.get("revlabel", ""), "idpath": res.get("idpath", "")}


@router.post("/files/doc/rename", summary="我的文件：重命名文档（cpcdoc rename）")
async def files_doc_rename(request: Request, body: dict):
    zc = _zipxml_client(request)
    docid = str(body.get("docid") or "").strip()
    name = str(body.get("name") or "").strip()
    if not docid or not name:
        raise HTTPException(status_code=400, detail="docid 与 name 必填")
    try:
        res = await zc.doc_rename(docid, name, str(body.get("rev") or "1"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"重命名失败：{e}")
    return {"ok": True, "docid": res.get("docid", ""), "docname": res.get("docname", ""),
            "olddocname": res.get("olddocname", "")}


@router.post("/files/doc/copy", summary="我的文件：复制到目标文件夹（cpcdoc paste，生成新 docid）")
async def files_doc_copy(request: Request, body: dict):
    return await _files_doc_paste(request, body, move=False)


@router.post("/files/doc/move", summary="我的文件：剪切移动到目标文件夹（cpcdoc cut，docid 不变）")
async def files_doc_move(request: Request, body: dict):
    return await _files_doc_paste(request, body, move=True)


async def _files_doc_paste(request: Request, body: dict, move: bool) -> dict:
    zc = _zipxml_client(request)
    docid = str(body.get("docid") or "").strip()
    dest_parentid = str(body.get("parentid") or "").strip()
    dest_parenttype = str(body.get("parenttype") or "").strip()
    if not docid or not dest_parentid:
        raise HTTPException(status_code=400, detail="docid 与目标 parentid 必填")
    try:
        res = await zc.doc_paste(docid, dest_parentid, dest_parenttype,
                                 rev=str(body.get("rev") or "1"),
                                 move=move, docname=str(body.get("docname") or ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{'移动' if move else '复制'}失败：{e}")
    return {"ok": True, "docid": res.get("docid", ""), "docname": res.get("docname", ""),
            "act": res.get("act", ""), "moved": res.get("moved", False)}


@router.post("/files/doc/delete", summary="我的文件：删除文档（cpcdoc delete，行数据服务端自取）")
async def files_doc_delete(request: Request, body: dict):
    """删除文档。行数据不信任前端，服务端按 fdrid 重新展开文件夹取 <doc> 子表行
    （REQ15 实证：请求体必须是列表 doc 行 + parentid/parenttype + wsright）。"""
    zc = _zipxml_client(request)
    docid = str(body.get("docid") or "").strip()
    fdrid = str(body.get("fdrid") or "").strip()
    idpath = str(body.get("idpath") or "").strip()
    typepath = str(body.get("typepath") or "").strip()
    objaccess = str(body.get("objaccess") or "").strip()
    objaccesstype = str(body.get("objaccesstype") or "").strip()
    wsright = str(body.get("wsright") or "").strip()
    if not docid or not fdrid:
        raise HTTPException(status_code=400, detail="docid 与所属文件夹 fdrid 必填")
    # 两种 parentid 口径别搞混（2026-09-08 回归踩坑）：
    #   - 展开文件夹 fdr_selectref 要的是「文件夹的父路径」（去掉最后一段）
    #   - delete 请求行里的 parentid 是「**文档**的父路径」＝文件夹自己的 idpath（抓包 REQ15 实证）
    parentid = idpath.rstrip("\\").rsplit("\\", 1)[0] if idpath and "\\" in idpath.rstrip("\\") else ""
    parenttype = typepath.rstrip("\\").rsplit("\\", 1)[0] if typepath and "\\" in typepath.rstrip("\\") else ""
    doc_parentid = idpath.rstrip("\\")
    doc_parenttype = typepath.rstrip("\\")
    try:
        xml = await zc.fdr_selectref(fdrid=fdrid, idpath=idpath, typepath=typepath,
                                     parentid=parentid, parenttype=parenttype,
                                     objaccess=objaccess, objaccesstype=objaccesstype)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"读取文件夹失败：{e}")
    row = None
    for d in re.findall(r"<doc>(.*?)</doc>", xml, re.S):
        fields = dict(re.findall(r"<([a-zA-Z]\w*)>([^<]*)</\1>", d))
        if (fields.get("docid") or "").strip() == docid:
            row = fields
            break
    if row is None:
        raise HTTPException(status_code=404, detail=f"文件夹 {fdrid} 内未找到文档 {docid}（可能已被移动/删除）")
    try:
        await zc.doc_delete(row, parentid=doc_parentid, parenttype=doc_parenttype, wsright=wsright)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"删除失败：{e}")
    # 复核：服务端 parentid 口径不对时会返回 200 但**没真删**（2026-09-08 回归踩坑），
    # 故重新展开一次文件夹确认文档确实消失，避免前端误报成功。
    try:
        xml2 = await zc.fdr_selectref(fdrid=fdrid, idpath=idpath, typepath=typepath,
                                      parentid=parentid, parenttype=parenttype,
                                      objaccess=objaccess, objaccesstype=objaccesstype)
        still = any((re.search(r"<docid>(\d+)</docid>", d) or [None, ""])[1] == docid
                    for d in re.findall(r"<doc>(.*?)</doc>", xml2, re.S))
    except Exception:
        still = None  # 复核失败不阻塞，仅不置 verified
    if still:
        raise HTTPException(status_code=502, detail=f"删除请求已受理但文档仍存在（docid={docid}），请重试或检查权限")
    return {"ok": True, "docid": docid, "verified": (still is False)}


# ---------------- 文件夹（cpcfdr）写操作（2026-09-08 folder_ops_v1 抓包实证） ----------------

def _fdr_parent_paths(idpath: str, typepath: str) -> tuple:
    """由文件夹自身 idpath/typepath 推其父路径（最后一段去掉）。"""
    pid = idpath.rstrip("\\").rsplit("\\", 1)[0] if idpath.rstrip("\\") and "\\" in idpath.rstrip("\\") else ""
    ptype = typepath.rstrip("\\").rsplit("\\", 1)[0] if typepath.rstrip("\\") and "\\" in typepath.rstrip("\\") else ""
    return pid, ptype


def _find_fdr_row(xml: str, fdrid: str):
    """在文件夹列表（cpcfdr selectref 响应）里找指定 <fdr> 行。"""
    for d in re.findall(r"<fdr>(.*?)</fdr>", xml, re.S):
        f = dict(re.findall(r"<([a-zA-Z]\w*)>([^<]*)</\1>", d))
        if (f.get("fdrid") or "").strip() == str(fdrid):
            return f
    return None


async def _fetch_fdr_row(zc, body: dict):
    """公共前置：按文件夹自身 idpath/typepath 展开其父文件夹，取该文件夹的列表行。"""
    fdrid = str(body.get("fdrid") or "").strip()
    idpath = str(body.get("idpath") or "").strip()
    typepath = str(body.get("typepath") or "").strip()
    objaccess = str(body.get("objaccess") or "").strip()
    objaccesstype = str(body.get("objaccesstype") or "").strip()
    if not fdrid or not idpath:
        raise HTTPException(status_code=400, detail="fdrid 与 idpath 必填")
    parentid, parenttype = _fdr_parent_paths(idpath, typepath)
    if not parentid:
        raise HTTPException(status_code=400, detail=f"无法从 idpath 推出父路径：{idpath}")
    # 展开父文件夹：fdrid/idpath/typepath = 父文件夹；selectref 的 parentid/parenttype
    # = **父文件夹的父路径**（再往上退一段，511269 的父是工作区 7203）——与文档删除端点的口径一致
    gp_id, gp_type = _fdr_parent_paths(parentid, parenttype)
    parent_fdrid = parentid.rstrip("\\").rsplit("\\", 1)[1] if "\\" in parentid.rstrip("\\") else parentid

    async def _list_parent():
        return await zc.fdr_selectref(fdrid=parent_fdrid, idpath=parentid + "\\",
                                      typepath=parenttype + "\\", parentid=gp_id,
                                      parenttype=gp_type, objaccess=objaccess or "127",
                                      objaccesstype=objaccesstype or "2")

    # 写操作（move/delete）后服务端索引有秒级延迟，行暂时找不到时重试两次
    row = None
    for attempt in range(3):
        try:
            xml = await _list_parent()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"读取父文件夹失败：{e}")
        row = _find_fdr_row(xml, fdrid)
        if row is not None:
            break
        if attempt < 2:
            await asyncio.sleep(1.5)
    if row is None:
        raise HTTPException(status_code=404, detail=f"父文件夹中未找到文件夹 {fdrid}（可能已被移动/删除）")
    # 行内补上下文（重命名 select / 删除 delete 请求行需要）。
    # ⚠ 强制覆写而非 setdefault：parentid/parenttype 由 idpath/typepath 推导，是权威值——
    #   前端 typepath 传空时（create 响应曾缺 typepath）空串会污染 select/rename 链（41124 父对象路径错误）
    row["parentid"] = parentid
    row["parenttype"] = parenttype
    if body.get("wsright"):
        row["wsright"] = str(body["wsright"])
    return row, parentid, parenttype


@router.post("/files/fdr/create", summary="我的文件：新建文件夹（cpcfdr insert，REQ2 实证）")
async def files_fdr_create(request: Request, body: dict):
    zc = _zipxml_client(request)
    name = str(body.get("name") or "").strip()
    parentid = str(body.get("parentid") or "").strip()   # 父文件夹 idpath（如 7203\511269）
    parenttype = str(body.get("parenttype") or "").strip()
    wsright = str(body.get("wsright") or "").strip()
    if not name or not parentid:
        raise HTTPException(status_code=400, detail="name 与 parentid（父文件夹路径）必填")
    try:
        r = await zc.fdr_create(name, parentid, parenttype, wsright=wsright,
                                objaccess=str(body.get("objaccess") or "127"))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"新建文件夹失败：{e}")
    if not r.get("fdrid"):
        raise HTTPException(status_code=502, detail="新建请求已受理但服务端未返回 fdrid")
    return {"ok": True, "fdrid": r["fdrid"], "fdrname": r.get("fdrname", ""),
            "idpath": r.get("idpath", ""), "typepath": r.get("typepath", "")}


@router.post("/files/fdr/rename", summary="我的文件：重命名文件夹（cpcfdr select→rename，REQ3 实证）")
async def files_fdr_rename(request: Request, body: dict):
    zc = _zipxml_client(request)
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 必填")
    row, _, _ = await _fetch_fdr_row(zc, body)
    try:
        r = await zc.fdr_rename(row, name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"重命名文件夹失败：{e}")
    if (r.get("fdrname") or "") != name:
        raise HTTPException(status_code=502, detail=f"重命名未生效（服务端回显 {r.get('fdrname')!r}）")
    return {"ok": True, "fdrid": r.get("fdrid", ""), "fdrname": name, "verified": True}


@router.post("/files/fdr/copy", summary="我的文件：复制文件夹（cpcfdr paste，REQ5 实证）")
async def files_fdr_copy(request: Request, body: dict):
    zc = _zipxml_client(request)
    destparentid = str(body.get("destparentid") or "").strip()
    destparenttype = str(body.get("destparenttype") or "").strip()
    if not destparentid:
        raise HTTPException(status_code=400, detail="destparentid（目标文件夹路径）必填")
    row, parentid, _ = await _fetch_fdr_row(zc, body)
    # 命名口径（原版客户端 REQ5 实证）：粘贴回同一父文件夹时用「复件N 名」前缀；
    # 跨文件夹粘贴保留原名（此分支抓包未覆盖，实现注明）。
    src_name = row.get("fdrname") or ""
    new_name = ("复件1 " + src_name) if destparentid == parentid else src_name
    try:
        r = await zc.fdr_copy(row.get("fdrid", ""), new_name, destparentid, destparenttype,
                              wsright=str(body.get("wsright") or ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"复制文件夹失败：{e}")
    if not r.get("fdrid") or r.get("fdrid") == row.get("fdrid"):
        raise HTTPException(status_code=502, detail="复制未生效（服务端未返回新 fdrid）")
    return {"ok": True, "fdrid": r["fdrid"], "fdrname": r.get("fdrname", new_name)}


@router.post("/files/fdr/move", summary="我的文件：剪切/移动文件夹（cpcfdr cut 单发直达，REQ13 实证）")
async def files_fdr_move(request: Request, body: dict):
    zc = _zipxml_client(request)
    destparentid = str(body.get("destparentid") or "").strip()
    destparenttype = str(body.get("destparenttype") or "").strip()
    if not destparentid:
        raise HTTPException(status_code=400, detail="destparentid（目标文件夹路径）必填")
    row, parentid, parenttype = await _fetch_fdr_row(zc, body)
    if destparentid == parentid:
        raise HTTPException(status_code=400, detail="目标与当前所在文件夹相同，无需移动")
    if destparentid.startswith((row.get("idpath") or "").rstrip("\\") + "\\"):
        raise HTTPException(status_code=400, detail="不能把文件夹移动到它自己内部")
    objaccess_hint = str(body.get("objaccess") or "127")
    objaccesstype_hint = str(body.get("objaccesstype") or "2")
    try:
        r = await zc.fdr_move(row.get("fdrid", ""), row.get("fdrname", ""),
                              destparentid, destparenttype, wsright=str(body.get("wsright") or ""))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"移动文件夹失败：{e}")
    # cut 响应只回显轻量行（fdrid 可能为空），且服务端索引同步实测可达 ~15s——
    # 验证双条件：① 目标文件夹列表出现该 fdrid，或 ② 源文件夹列表已消失（cut 无 error 即受理）
    _err = re.search(r"<error>.*?</error>", r.get("xml") or "", re.S)
    if _err:
        raise HTTPException(status_code=502, detail=f"移动失败：{re.sub(r'<[^>]+>', ' ', _err.group(0)).strip()}")
    _dest_pid, _dest_ptype = _fdr_parent_paths(destparentid, destparenttype)
    _dest_fdrid = destparentid.rsplit("\\", 1)[1] if "\\" in destparentid.rstrip("\\") else destparentid
    _src_pid, _src_ptype = parentid, parenttype
    _src_gp_id, _src_gp_type = _fdr_parent_paths(parentid, parenttype)
    moved = False
    for attempt in range(6):
        try:
            if not moved:
                xml2 = await zc.fdr_selectref(fdrid=_dest_fdrid, idpath=destparentid + "\\",
                                              typepath=destparenttype + "\\", parentid=_dest_pid,
                                              parenttype=_dest_ptype, objaccess=objaccess_hint,
                                              objaccesstype=objaccesstype_hint)
                if _find_fdr_row(xml2, row.get("fdrid", "")) is not None:
                    moved = True
                    break
        except Exception:
            pass
        try:
            xml3 = await zc.fdr_selectref(fdrid=_src_pid.rsplit("\\", 1)[1] if "\\" in _src_pid.rstrip("\\") else _src_pid,
                                          idpath=_src_pid + "\\", typepath=_src_ptype + "\\",
                                          parentid=_src_gp_id, parenttype=_src_gp_type,
                                          objaccess=objaccess_hint, objaccesstype=objaccesstype_hint)
            if _find_fdr_row(xml3, row.get("fdrid", "")) is None:
                moved = True  # 源已消失（索引同步中，目标列表可能稍后才可见）
                break
        except Exception:
            pass
        if attempt < 5:
            await asyncio.sleep(2)
    if not moved:
        raise HTTPException(status_code=502, detail=f"移动请求已受理但在目标文件夹未发现（fdrid={row.get('fdrid')}），请刷新后确认")
    return {"ok": True, "fdrid": row.get("fdrid", ""), "fdrname": row.get("fdrname", ""), "moved": True, "verified": True}


@router.post("/files/fdr/delete", summary="我的文件：删除文件夹（cpcfdr delete，REQ16 实证，删后复核）")
async def files_fdr_delete(request: Request, body: dict):
    zc = _zipxml_client(request)
    row, parentid, parenttype = await _fetch_fdr_row(zc, body)
    wsright = str(body.get("wsright") or "").strip()
    try:
        await zc.fdr_delete(row, parentid=parentid, parenttype=parenttype, wsright=wsright)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"删除文件夹失败：{e}")
    # 失败不报错的服务器不少见——重新展开父文件夹确认真删（同文档删除的复核逻辑）。
    # 服务端索引有秒级延迟（move/delete 实测可达 ~15s），复核带 4×2s 重试
    try:
        idpath = str(body.get("idpath") or "").strip()
        typepath = str(body.get("typepath") or "").strip()
        objaccess = str(body.get("objaccess") or "").strip()
        objaccesstype = str(body.get("objaccesstype") or "").strip()
        gp_id, gp_type = _fdr_parent_paths(parentid, parenttype)
        parent_fdrid = parentid.rstrip("\\").rsplit("\\", 1)[1] if "\\" in parentid.rstrip("\\") else parentid

        def _verify_gone():
            return zc.fdr_selectref(fdrid=parent_fdrid, idpath=parentid + "\\",
                                    typepath=parenttype + "\\", parentid=gp_id,
                                    parenttype=gp_type, objaccess=objaccess or "127",
                                    objaccesstype=objaccesstype or "2")

        still = None
        for attempt in range(4):
            try:
                xml2 = await _verify_gone()
                if _find_fdr_row(xml2, row.get("fdrid", "")) is None:
                    still = False
                    break
                still = True
            except Exception:
                still = None
            if attempt < 3:
                await asyncio.sleep(2)
    except Exception:
        still = None
    if still:
        raise HTTPException(status_code=502, detail=f"删除请求已受理但文件夹仍存在（fdrid={row.get('fdrid')}），请重试或检查权限")
    return {"ok": True, "fdrid": row.get("fdrid", ""), "verified": (still is False)}


@router.get("/docs/search", response_model=DocSearchResult,
            summary="文档搜索（cpcexplorer.search，文件夹右键搜索，含子文件夹）")
async def docs_search(
    request: Request,
    q: str = Query(..., description="搜索关键字（objname 前缀模糊，如 多联机）"),
    parentid: str = Query("", description="当前文件夹完整 idpath（如 -4\\115179\\406246，来自树节点）"),
    parenttype: str = Query("", description="当前文件夹完整 typepath（来自树节点）"),
    datebegin: str = Query("", description="时间范围起 YYYY-MM-DD（默认近一月）"),
    dateend: str = Query("", description="时间范围止 YYYY-MM-DD"),
    limit: int = Query(500, description="结果上限 maxsearchrltcmt"),
):
    zc = _zipxml_client(request)
    try:
        xml = await zc.explorer_search(keyword=q, parentid=parentid, parenttype=parenttype,
                                       datebegin=datebegin, dateend=dateend,
                                       maxsearchrltcmt=limit)
    except HTTPException:
        raise
    except Exception:
        return DocSearchResult(q=q, total=0, docs=[])
    docs = []
    for d in re.findall(r"<docofexplore>(.*?)</docofexplore>", xml, re.S):
        docid = _g("docid", d)
        if not docid:
            continue
        raw = dict(re.findall(r"<([a-zA-Z]\w*)>([^<]*)</\1>", d))
        docs.append(DocNode(
            docid=docid, docname=_g("docname", d), rev=_g("rev", d),
            revlabel=_g("revlabel", d), stat=_g("stat", d), plmstate=_g("plmstate", d),
            creator=_g("creator", d), updator=_g("updator", d),
            createtime=_g("createtime", d), updatetime=_g("updatetime", d),
            filetime=_g("filetime", d), oldsize=_g("oldsize", d), zipsize=_g("zipsize", d),
            crc32=_g("crc32", d), locked=_g("locked", d), actived=_g("actived", d),
            flag=_g("flag", d), doctype=_g("doctype", d), isfamily=_g("isfamily", d),
            isvirtual=_g("isvirtual", d), locid=_g("locid", d), appid=_g("appid", d),
            objaccess=_g("objaccess", d), idpath=_g("idpath", d), typepath=_g("typepath", d),
            raw=raw,
        ))
    return DocSearchResult(q=q, total=len(docs), docs=docs)


# ---------- helpers ----------
def _g(tag, chunk):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", chunk, re.S)
    return m.group(1) if m else ""


def _parse_folder_xml(xml: str) -> FolderContent:
    """解析 cpcworkspace/cpcfdr selectref 响应。"""
    out = FolderContent()
    # 当前文件夹对象（最外层 <fdrid>/<fdrname>）
    m = re.search(r"<response>\s*<data>\s*<fdrid>([^<]*)</fdrid>\s*<fdrname>([^<]*)</fdrname>", xml, re.S)
    if m:
        out.fdrid = m.group(1)
        out.fdrname = m.group(2)
    # 子文件夹
    for f in re.findall(r"<fdr>(.*?)</fdr>", xml, re.S):
        out.subfolders.append(FolderNode(
            fdrid=_g("fdrid", f), fdrname=_g("fdrname", f),
            attribute=_g("attribute", f), flag=_g("flag", f),
            typepath=_g("typepath", f), idpath=_g("idpath", f),
            objaccess=_g("objaccess", f), objaccesstype=_g("objaccesstype", f),
        ))
    # 物料（<boms><bom>）
    item_blocks = re.findall(r"<bom>(.*?)</bom>", xml, re.S)
    for b in item_blocks:
        code = _g("itemcode", b)
        if not code and not _g("itemid", b):
            continue  # 跳过空块
        raw = dict(re.findall(r"<([a-zA-Z]\w*)>([^<]*)</\1>", b))
        out.items.append(ItemHit(
            item_id=_g("itemid", b), asmid=_g("asmid", b),
            item_code=code, item_name=_g("itemname", b),
            product_model=_g("productmodel", b), custom_model=_g("custommodel", b),
            drawid=_g("drawid", b), spec=_g("spec", b),
            item_unit=_g("itemunit", b), rev=_g("rev", b),
            purch_flag=_g("purchflag", b), raw=raw,
        ))
    out.has_items = len(out.items) > 0
    return out


# ---------- endpoints ----------
@router.get("/workspace", response_model=FolderContent, summary="产品工作区根（直接子文件夹）")
async def product_workspace(request: Request):
    zc = _zipxml_client(request)
    try:
        xml = await zc.workspace_selectref()
    except HTTPException:
        raise
    except Exception as e:
        return FolderContent(fdrid="-14", fdrname="产品工作区",
                              subfolders=[], items=[], has_items=False)
    return _parse_folder_xml(xml)


@router.get("/folder/{fdrid}", response_model=FolderContent,
            summary="展开文件夹（子文件夹 + 物料列表）")
async def product_folder(
    request: Request,
    fdrid: str = Path(..., description="文件夹 fdrid"),
    fdrname: str = Query("", description="文件夹名（回显用，可选）"),
    idpath: str = Query("", description="节点自身 idpath（来自父响应，必带）"),
    typepath: str = Query("", description="节点自身 typepath（来自父响应，必带）"),
    objaccess: str = Query("", description="节点自身 objaccess（来自父响应，必带）"),
    objaccesstype: str = Query("", description="节点自身 objaccesstype（来自父响应，必带）"),
):
    # 从 idpath/typepath 派生 parentid/parenttype（完整父路径，服务端据此定位）
    parentid, parenttype = "", ""
    if idpath:
        parentid = idpath.rstrip("\\").rsplit("\\", 1)[0] if "\\" in idpath.rstrip("\\") else "-14"
    if typepath:
        parenttype = typepath.rstrip("\\").rsplit("\\", 1)[0] if "\\" in typepath.rstrip("\\") else "1"
    zc = _zipxml_client(request)
    try:
        xml = await zc.fdr_selectref(fdrid=fdrid, fdrname=fdrname,
                                     idpath=idpath, typepath=typepath,
                                     parentid=parentid, parenttype=parenttype,
                                     objaccess=objaccess, objaccesstype=objaccesstype)
    except HTTPException:
        raise
    except Exception as e:
        return FolderContent(fdrid=fdrid, fdrname=fdrname,
                              subfolders=[], items=[], has_items=False)
    return _parse_folder_xml(xml)
