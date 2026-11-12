"""Material / BOM router (物料查询).

Exposes:
- GET  /items                    按关键字查物料
- GET  /items/{code}             物料详情
- GET  /items/{code}/bom         该物料的 BOM
- GET  /items/{code}/where-used  被哪里用
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from lxml import etree
from pydantic import BaseModel
from typing import List

from ..client import SinoCCClient, get_session
from ..client_zipxml import SinoCCZipClient, _extract_data_inner
from ..deps import get_client

router = APIRouter()


def _parse_bom_xml(xml_text: str):
    """解析客户端通道 BOM 响应 XML → (根信息, 子件列表)。"""
    root = etree.fromstring(xml_text.encode("utf-8", errors="ignore"))
    data = root.find("data")
    if data is None:
        return {}, []
    def _kids(el):
        return {etree.QName(c).localname: (c.text or "") for c in el}
    root_info = {etree.QName(c).localname: (c.text or "") for c in data
                 if etree.QName(c).localname != "bomofboms"}
    children = []
    bb = data.find("bomofboms")
    if bb is not None:
        for el in bb.iterfind("bomofbom"):
            children.append(_kids(el))
    return root_info, children


def _zipxml_client(request: Request) -> SinoCCZipClient:
    settings = request.app.state.settings
    sess = get_session("default")
    if not sess.login_uid:
        raise HTTPException(status_code=401, detail="未登录：请先调用 POST /auth/session 注入登录态")
    return SinoCCZipClient(settings).set_login_uid(sess.login_uid).set_user(sess.user)


def _settings(request: Request):
    return request.app.state.settings


async def _client(request: Request, client: SinoCCClient = Depends(get_client)) -> SinoCCClient:
    return client


class ItemHit(BaseModel):
    item_id: str = ""
    item_code: str
    item_drawid: str = ""
    item_name: str = ""
    spec: str = ""
    material: str = ""
    category: str = ""
    unit: str = ""
    product_model: str = ""
    market_model: str = ""
    purch_flag: str = ""
    actived: str = ""  # 2026-08-25：2=活动, 1=未激活/禁用（前端禁用标识用）
    raw: dict = {}


class ItemList(BaseModel):
    rows: List[ItemHit]
    total: int


class BomNode(BaseModel):
    item_code: str
    item_name: str = ""
    qty: float = 0.0
    level: int = 0
    parent: str = ""
    raw: dict = {}


class BomTree(BaseModel):
    root: str
    nodes: List[BomNode]


def _rows_from_response(res) -> list[dict]:
    parsed = res.parsed
    if parsed is None and res.text:
        try:
            parsed = etree.fromstring(res.text.encode("utf-8", errors="ignore"))
        except etree.XMLSyntaxError:
            return []
    if parsed is None:
        return []
    out = []
    for el in parsed.iter("Row"):
        d = {}
        for child in el:
            tag = etree.QName(child).localname
            d[tag] = child.text or ""
        if d:
            out.append(d)
    return out


def _hit_from_row(d: dict) -> ItemHit:
    return ItemHit(
        item_code=str(d.get("itemcode") or d.get("bcitemcode") or d.get("code") or ""),
        item_drawid=str(d.get("itemdrawid") or d.get("bcdrawid") or ""),
        item_name=str(d.get("itemname") or d.get("bcitemname") or ""),
        spec=str(d.get("spec") or d.get("bcspec") or ""),
        material=str(d.get("material") or d.get("bcmaterial") or ""),
        category=str(d.get("catname") or d.get("bccatname") or ""),
        unit=str(d.get("itemunit") or d.get("bcitemunit") or ""),
        product_model=str(d.get("productmodel") or d.get("bcproductmodel") or ""),
        market_model=str(d.get("marketmodel") or d.get("bcmarketmodel") or ""),
        purch_flag=str(d.get("purchflag") or d.get("bcpurchflag") or ""),
        raw=d,
    )


def _parse_zip_search(xml: str) -> list[ItemHit]:
    """解析 bomman cpcitem.search 响应。

    2026-08-25 实测（物料编码查询 814042400004）：
      响应 = <response><data>…条件回显…<items><item>…记录…</item></items>…</data></response>
      <item> 内字段顺序不定（itemname/itemcode 在 itemid 之前），必须按 <item> 块切分，
      不能按 <itemid> split（会丢失前置字段）。
    兼容旧格式：无 <items> 时退回按 <itemid> split。
    """
    m = re.search(r"<data[^>]*>([\s\S]*?)</data>", xml, re.S)
    inner = m.group(1) if m else xml

    def _g(p, t):
        mm = re.search(rf"<{t}>([^<]*)</{t}>", p)
        return mm.group(1) if mm else ""

    def _hit(p: str, iid: str) -> ItemHit | None:
        code = _g(p, "itemcode") or _g(p, "bcitemcode")
        if not code:
            return None
        raw_dict = {k: v for k, v in re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", p)}
        return ItemHit(
            item_id=iid or _g(p, "itemid"),
            item_code=code,
            item_drawid=_g(p, "drawid"),
            item_name=_g(p, "itemname") or _g(p, "itemnewname"),
            spec=_g(p, "itemdesc") or _g(p, "spec"),
            material=_g(p, "material"),
            category=_g(p, "codecatname") or _g(p, "catname") or _g(p, "materialcatname"),
            unit=_g(p, "stockunit") or _g(p, "itemunit"),
            product_model=_g(p, "productmodel"),
            market_model=_g(p, "marketmodel"),
            purch_flag=_g(p, "purchflag"),
            actived=raw_dict.get("actived", ""),  # 2026-08-25：顶置 actived 以便前端禁用标识
            raw=raw_dict,
        )

    rows: list[ItemHit] = []
    items_m = re.search(r"<items[^>]*>([\s\S]*?)</items>", inner, re.S)
    if items_m:
        for it in re.findall(r"<item[^>]*>([\s\S]*?)</item>", items_m.group(1), re.S):
            h = _hit(it, _g(it, "itemid"))
            if h:
                rows.append(h)
        return rows
    # 旧格式：字段平铺，按 <itemid> 切分
    parts = re.split(r"<itemid>", inner)
    for p in parts:
        if not p.strip():
            continue
        iid = p.split("</itemid>")[0].strip() if "</itemid>" in p else ""
        if not iid or iid == "0":
            continue
        h = _hit(p, iid)
        if h:
            rows.append(h)
    return rows


@router.get("", response_model=ItemList, summary="按属性查物料（编码/名称/代号/规格/材质/型号）")
async def search_items(
    request: Request,
    q: str = Query(..., min_length=1, description="搜索关键字"),
    column: str = Query("itemcode", description="搜索属性：itemcode/itemname/drawid/spec/material/productmodel/custommodel"),
    limit: int = Query(30, le=300),
    filter_disabled: bool = Query(False, description="过滤掉禁用物料（sqlwhere 追加 CpcItem.Actived = 2）；默认 false 显示全部含禁用"),
    client: SinoCCClient = Depends(_client),
):
    """2026-08-24/25 实测（prod_search_field2 / material_search_capture / material_name_search_capture / material_drawid_search_capture 抓包）：
    cpcitem.search 的字段参数过滤不生效，必须用 <sqlwhere> 服务端 SQL；但**字段类搜索例外**：
    - 中文名称（itemname）：客户端放 <itemname> 字段（2026-08-25 抓包实证），不走 sqlwhere
    - 代号（drawid）：客户端放 <drawid> 字段（2026-08-25 抓包实证），不走 sqlwhere
      以上两类 sqlwhere 只放 ` 1=1  and (CpcItem.Actived = 2) `（活动过滤，"过滤禁用"关则空）
    - 物料编码（itemcode）：客户端走 sqlwhere 前缀匹配 ( lower(CpcItem.ItemCode) like 'xxx%' )
    - 产品型号（productmodel）：客户端放 <productmodel> 字段（2026-08-25 抓包实证），不走 sqlwhere
    - 客户型号（custommodel）：客户端放 <custommodel> 字段（2026-08-25 抓包实证），不走 sqlwhere
    - 市场型号（marketmodel）：客户端放 <marketmodel> 字段（2026-08-25 抓包实证），不走 sqlwhere
    - 其他列（Spec/Material）：like '%q%' 模糊（未抓包实证，沿用）
    - filter_disabled=false 时（客户端"过滤禁用"关）：sqlwhere 不追加 actived 条件，结果含禁用物料
    注入防护：值禁止 ' ; -- /* */ 等字符。"""
    zc = _zipxml_client(request)
    try:
        col_map = {
            "itemcode": "CpcItem.ItemCode",
            "itemname": "CpcItem.ItemName",
            "drawid": "CpcItem.DrawID",
            "spec": "CpcItem.Spec",
            "material": "CpcItem.Material",
            "productmodel": "CpcItem.ProductModel",
            "custommodel": "CpcItem.CustomModel",
            "marketmodel": "CpcItem.MarketModel",
        }
        col = col_map.get(column, "CpcItem.ItemCode")
        if re.search(r"['\";]|--|/\*|\*/", q):
            return ItemList(rows=[], total=0)
        actived_sql = " 1=1  and (CpcItem.Actived = 2) " if filter_disabled else " 1=1 "
        actived_sql = " 1=1  and (CpcItem.Actived = 2) " if filter_disabled else " 1=1 "
        if column == "itemname":
            # 对齐真实客户端（2026-08-25 抓包）：名称条件放 <itemname> 字段，sqlwhere 只留活动过滤
            sqlwhere = actived_sql
            body = (f"<itemname>{q}</itemname><sqlwhere>{sqlwhere}</sqlwhere>"
                    f"<maxsearchrltcmt>{min(limit, 300)}</maxsearchrltcmt>"
                    f"<invorgid>104</invorgid>")
        elif column == "drawid":
            # 对齐真实客户端（2026-08-25 抓包）：代号条件放 <drawid> 字段（如 CR-VD400SR1M-T-DK82.1），
            # sqlwhere 只放活动过滤；不走 sqlwhere like
            sqlwhere = actived_sql
            body = (f"<drawid>{q}</drawid><sqlwhere>{sqlwhere}</sqlwhere>"
                    f"<maxsearchrltcmt>{min(limit, 300)}</maxsearchrltcmt>"
                    f"<invorgid>104</invorgid>")
        elif column == "productmodel":
            # 对齐真实客户端（2026-08-25 抓包）：产品型号条件放 <productmodel> 字段
            # （GCHV-V160WA-B→2条 / TS-D28F1-HF1A14→2条），sqlwhere 只放活动过滤
            sqlwhere = actived_sql
            body = (f"<productmodel>{q}</productmodel><sqlwhere>{sqlwhere}</sqlwhere>"
                    f"<maxsearchrltcmt>{min(limit, 300)}</maxsearchrltcmt>"
                    f"<invorgid>104</invorgid>")
        elif column == "custommodel":
            # 对齐真实客户端（2026-08-25 抓包）：客户型号条件放 <custommodel> 字段
            # （MMD-GP0093MPHN-C），sqlwhere 只放活动过滤
            sqlwhere = actived_sql
            body = (f"<custommodel>{q}</custommodel><sqlwhere>{sqlwhere}</sqlwhere>"
                    f"<maxsearchrltcmt>{min(limit, 300)}</maxsearchrltcmt>"
                    f"<invorgid>104</invorgid>")
        elif column == "marketmodel":
            # 对齐真实客户端（2026-08-25 抓包）：市场型号条件放 <marketmodel> 字段
            # （MCY-MGP1411HTN-CF），sqlwhere 只放活动过滤
            sqlwhere = actived_sql
            body = (f"<marketmodel>{q}</marketmodel><sqlwhere>{sqlwhere}</sqlwhere>"
                    f"<maxsearchrltcmt>{min(limit, 300)}</maxsearchrltcmt>"
                    f"<invorgid>104</invorgid>")
        elif column == "itemcode":
            # 对齐真实客户端（2026-08-25 抓包）：前缀匹配 + lower + 括号；"过滤禁用"关时追加 actived
            # 注意：lower() 只作用于列，查询值必须一并转小写（否则大写输入不匹配，2026-08-25 实证）
            sqlwhere = f"( lower({col}) like '{q.lower()}%' )"
            if filter_disabled:
                sqlwhere += " and (CpcItem.Actived = 2)"
            body = (f"<sqlwhere>{sqlwhere}</sqlwhere><maxsearchrltcmt>{min(limit, 300)}</maxsearchrltcmt>"
                    f"<invorgid>104</invorgid><actived>1</actived>")
        else:
            sqlwhere = f"1=1 and {col} like '%{q}%'"
            if filter_disabled:
                sqlwhere += " and (CpcItem.Actived = 2)"
            body = (f"<sqlwhere>{sqlwhere}</sqlwhere><maxsearchrltcmt>{min(limit, 300)}</maxsearchrltcmt>"
                    f"<invorgid>104</invorgid><actived>1</actived>")
        try:
            xml = await asyncio.wait_for(zc.call("bomman.jsp", "cpcitem", "search", body), timeout=15)
        except asyncio.TimeoutError:
            return ItemList(rows=[], total=0)
        rows = _parse_zip_search(xml)
        return ItemList(rows=rows, total=len(rows))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"物料搜索失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.get("/orgs", summary="组织/分类树（cpcorg.search）")
async def list_orgs(
    request: Request,
    orgname: str = "",
    limit: int = 300,
    client: SinoCCClient = Depends(_client),
):
    """拉所有组织/分类。client 端再组装成树形结构（按 parentid 嵌套）。

    验证 2026-08-06: cpcorg.search 可用，按 orgname 模糊匹配（org 数量 216）。
    """
    payload = {"searchflag": 1, "pagination": limit}
    if orgname:
        payload["orgname"] = orgname
    res = await client.call("cpcorg", "search", formname="TCPCSoapDefini",
                            payload=payload)
    if res.status_code >= 400:
        return {"ok": False, "error": res.text[:300]}
    import json as _json
    obj = _json.loads(res.text)
    orgs = obj.get("orgs") or obj.get("rows") or []
    keep = []
    for o in orgs:
        keep.append({k: o.get(k) for k in
                    ["orgid", "orgname", "orgcode", "orgdesc", "parentid",
                     "parentorgid", "level", "orgtype", "enable"] if k in o})
    return {"ok": True, "total": len(keep), "orgs": keep}


@router.get("/{code}", summary="物料详情")
async def item_detail(
    code: str,
    request: Request,
    client: SinoCCClient = Depends(_client),
):
    res = await client.call(
        endpoint="soapmain",
        method="GetFormData",
        formname="TCpcItemRelObjDetailForm",
        payload={"ItemCode": code, "OpUser": client.session.user},
    )
    return {"code": code, "raw_text": res.text[:8000]}


@router.get("/{code}/bom", response_model=BomTree, summary="该物料 BOM 展开（客户端通道）")
async def item_bom(
    code: str,
    request: Request,
    asmid: str = Query("0", description="装配件 asmid（有则更准）"),
    invorgid: str = Query("104", description="组织 ID"),
    rev: str = Query("", description="版本号"),
):
    """2026-10-10 起仅走复刻的客户端通道（bomman.jsp cpcbom.selectref，完整抓包模板）。
    旧 web 通道回退（cpcbom.getchildren，需 asmid 上下文否则全空）已按用户要求移除。"""
    zc = _zipxml_client(request)
    try:
        xml = await asyncio.wait_for(
            zc.bom_selectref(asmid=asmid, invorgid=invorgid, itemcode=code, itemname="", rev=rev),
            timeout=60)
        if not xml or not xml.lstrip().startswith("<"):
            # 空体/非 XML（编码不存在或服务端拒绝时的常见形态）——给明确口径而不是裸 XMLSyntaxError
            raise HTTPException(status_code=404,
                                detail=f"PDM 服务端未返回 BOM 数据（itemcode={code} 可能不存在或非装配件）；"
                                       f"先用 /items?q=<编码前缀>&column=itemcode 确认编码")
        root_info, children = _parse_bom_xml(xml)
        if not root_info and not children:
            raise HTTPException(status_code=404,
                                detail=f"客户端通道未返回 BOM（itemcode={code}, asmid={asmid}, rev={rev or '最新'}）；"
                                       f"请确认编码正确或传 asmid/itemid 走 /wf/materials/byid/{{itemid}}/bomdetail")
        nodes = [
            BomNode(
                item_code=str(d.get("itemcode") or d.get("drawid") or d.get("asmid") or ""),
                item_name=str(d.get("itemname") or ""),
                qty=float(d.get("qty") or 0),
                level=1,
                parent=code,
                raw=d,
            )
            for d in children
        ]
        return BomTree(root=str(root_info.get("itemname") or code), nodes=nodes)
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="BOM 展开超时（上游 >60s）")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"BOM 展开失败（客户端通道）: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.get("/{code}/where-used", summary="反查：哪里在用该物料")
async def where_used(
    code: str,
    request: Request,
    client: SinoCCClient = Depends(_client),
):
    res = await client.call(
        endpoint="soapmain",
        method="WhereUsed",
        formname="TBomOppSearchForm",
        payload={"ItemCode": code, "OpUser": client.session.user},
    )
    return {"code": code, "raw_text": res.text[:8000]}


# -------------------------- 零部件引用查询（2026-09-09 抓包实证） --------------------------
# bomman.jsp?classid=cpcitemsearch&action=getreference
# searchflag: 1=直接上层  2=引用产品；actived: -1=过滤禁用（仅有效）  1=全部（含禁用）
# 行级 actived: 2=有效引用  1=禁用引用（87⊂258 / 13⊂47 严格子集实证）

def _zipxml_client_of(request: Request) -> SinoCCZipClient:
    sess = get_session("default")
    if not sess.login_uid:
        raise HTTPException(status_code=401, detail="未登录：请先注入登录态")
    return (SinoCCZipClient(request.app.state.settings)
            .set_login_uid(sess.login_uid)
            .set_user(sess.user))


@router.get("/{code}/reference", summary="零部件引用查询（客户端同款：直接上层/引用产品 + 过滤禁用）")
async def item_reference(
    code: str,
    request: Request,
    searchflag: str = Query("1", pattern="^[12]$", description="1=直接上层 2=引用产品"),
    filter_disabled: bool = Query(True, description="True=过滤禁用引用(原版 actived=-1)"),
    drawid: str = Query("", description="图号（可选，原版客户端原样携带）"),
    invorgid: str = Query("104"),
):
    zc = _zipxml_client_of(request)
    try:
        xml = await asyncio.wait_for(
            zc.item_getreference(code, drawid=drawid, searchflag=searchflag,
                                 filter_disabled=filter_disabled, invorgid=invorgid),
            timeout=90)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="引用查询超时（上游 >90s）")
    inner = _extract_data_inner(xml)
    rows = [dict(re.findall(r"<([a-zA-Z]\w*)>([^<]*)</\1>", r))
            for r in re.findall(r"<itemsearchofitemsearch>([\s\S]*?)</itemsearchofitemsearch>", inner)]
    return {"ok": True, "code": code, "searchflag": searchflag,
            "filter_disabled": filter_disabled, "total": len(rows), "rows": rows}
