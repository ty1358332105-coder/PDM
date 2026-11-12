"""对象只读详情（点确认单号 / 变更单号时弹窗用）。

只做**读**：ecmman cpcecnsamconfirm.select / cpcecn.select，字段与子表口径同桥接实证结果。
后端直接把结果整理成「字段行 + 表格」结构，前端只负责渲染，不再各自解析 XML。
"""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request

from .query import _zc
from .vendor.client_zipxml import _extract_data_inner

router = APIRouter(prefix="/api", tags=["objects"])

# ---- 附件（objattachs 子表，确认单/ECN select 响应直接带） ----
def _parse_attachments(inner: str) -> list:
    out = []
    block = re.search(r"<objattachs>(.*?)</objattachs>", inner, re.S)
    if not block:
        return out
    for chunk in re.finditer(r"<objattach>(.*?)</objattach>", block.group(1), re.S):
        a = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1)))
        if a.get("docname"):
            out.append({"docid": a.get("docid", ""), "docname": a.get("docname", ""),
                        "reftype": a.get("reftype", "")})
    return out


def _strip_subtables(inner: str, tags: tuple) -> str:
    for t in tags:
        inner = re.sub(rf"<{t}>.*?</{t}>", "", inner, flags=re.S)
    inner = re.sub(r"<objattachs>.*?</objattachs>", "", inner, flags=re.S)
    return inner


def _rows(inner: str, tag: str) -> list:
    out = []
    for chunk in re.finditer(rf"<{tag}>(.*?)</{tag}>", inner, re.S):
        out.append(dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1))))
    return out


def _pair(label: str, value: str) -> list:
    return [label, (value or "").strip()]


@router.get("/objects/{objid}/samconfirm", summary="样品确认单详情（只读）")
async def samconfirm_detail(objid: str, request: Request) -> dict:
    zc = _zc(request.app)
    try:
        xml = await zc.samconfirm_select(objid)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"确认单详情查询失败：{type(e).__name__}: {e}")
    finally:
        try:
            await zc.close()
        except Exception:
            pass

    inner = _extract_data_inner(xml)
    head = _strip_subtables(inner, ("cpcecnsamconfirmitemsofecnsamconfirms",
                                    "ecnsamconfirmlineofecnsamconfirms"))
    f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", head))
    code = (f.get("samcon_code") or "").strip()
    typ = (f.get("samcon_type") or "").strip()
    # 类型守卫（2026-11-09 用户实测 91869 打开空白）：objid 是「更改通知单」时，
    # cpcecnsamconfirm.select 也会回 200，但内容是个空壳（只有 stat=0/wfid=0），
    # 前端"先试确认单"就会弹出一个空面板。这里直接判否，让上层按 objtype 或回退去取 ECN。
    if not code and not (f.get("vendor") or f.get("vendorname")):
        raise HTTPException(status_code=404,
                            detail=f"objid={objid} 不是样品确认单（可能是更改通知单/其它对象，请按 objtype 取对应详情）")
    kind = "formal" if code[:2].upper() == "SC" else ("temp" if code[:2].upper() == "TC" else "")

    fields = [
        _pair("确认单号", code),
        _pair("确认类型", typ or ("临时确认" if kind == "temp" else "正式确认" if kind == "formal" else "")),
        _pair("供应商", f.get("vendor") or f.get("vendorname")),
        _pair("供应商ID", f.get("vendor_id")),
        _pair("编制人", f.get("creator")),
        _pair("编制时间", f.get("create_time")),
        _pair("状态", f.get("stat")),
        _pair("流程号", f.get("wfid")),
        _pair("结论", f.get("con_conclusion")),
        _pair("品牌", f.get("brand")),
        _pair("生产厂家", f.get("sourcemanuf")),
        _pair("代理商", f.get("agent")),
        _pair("规格", f.get("spec")),
        _pair("材质", f.get("material")),
        _pair("库存组织", f.get("invorgname")),
        _pair("备注", f.get("note")),
    ]
    items = _rows(inner, "cpcecnsamconfirmitemsofecnsamconfirm")
    return {
        "ok": True, "kind": "samconfirm", "objid": objid, "code": code,
        "kind_of_code": kind,
        "fields": [p for p in fields if p[1]],
        "tables": [{
            "title": f"确认物料项（{len(items)} 行）",
            "columns": ["序号", "物料编码", "中文名称", "代号", "规格", "材质"],
            "rows": [[r.get("seq", ""), r.get("itemcode", ""), r.get("itemname", ""),
                      r.get("drawid", ""), r.get("spec", ""), r.get("material", "")] for r in items],
        }] if items else [],
        "attachments": _parse_attachments(inner),
    }


@router.get("/objects/{objid}/ecn", summary="更改通知单详情（只读）")
async def ecn_detail(objid: str, request: Request) -> dict:
    zc = _zc(request.app)
    try:
        xml = await zc.ecn_select(objid)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"变更单详情查询失败：{type(e).__name__}: {e}")
    finally:
        try:
            await zc.close()
        except Exception:
            pass

    inner = _extract_data_inner(xml)
    head = _strip_subtables(inner, ("cpcecnitemlineofcpcecns", "cpcecnbomlineofcpcecns",
                                    "cpcecndrawlineofcpcecns"))
    f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", head))
    code = (f.get("ntcode") or f.get("ecncode") or "").strip()
    # 同 samconfirm 的类型守卫：拿不到变更单号说明这个 objid 不是变更通知单
    if not code:
        raise HTTPException(status_code=404,
                            detail=f"objid={objid} 不是更改通知单（可能是样品确认单/其它对象）")

    fields = [
        _pair("变更单号", code),
        _pair("变更原因", f.get("creason")),
        _pair("编制人", f.get("creator")),
        _pair("编制时间", f.get("createtime")),
        _pair("状态", f.get("stat")),
        _pair("流程号", f.get("wfid")),
        _pair("库存组织", f.get("invorgname")),
        _pair("生效时间", f.get("efftime")),
        _pair("备注", f.get("note")),
    ]

    def _has(v):
        return v and str(v).strip() not in ("", "0", "-1", "False")

    # 物料变更行：两种结构并存（属性变更对 / BOM 行变更），统一成 5 列
    item_rows = []
    for r in _rows(inner, "cpcecnitemlineofcpcecn"):
        if not any(_has(r.get(k)) for k in ("mpropname", "oldvalue", "newvalue",
                                            "bcitemcode", "acitemcode", "itemoration")):
            continue
        item_rows.append([
            r.get("itemcode") or r.get("bcitemcode") or r.get("acitemcode") or "",
            r.get("bcdrawid") or r.get("acdrawid") or r.get("drawid") or "",
            r.get("mpropname") or r.get("itemoration") or ("BOM 行变更" if r.get("bcitemcode") else ""),
            r.get("oldvalue") or r.get("bcname") or "",
            r.get("newvalue") or r.get("acname") or "",
        ])
    # BOM 行变更
    bom_rows = []
    for r in _rows(inner, "cpcecnbomlineofcpcecn"):
        if not (r.get("bcitemcode") or r.get("acitemcode") or r.get("supercode")):
            continue
        bom_rows.append([
            r.get("supercode", ""), r.get("supername", ""),
            r.get("bcitemcode", ""), r.get("bcqty", ""), r.get("bcunit", ""),
            r.get("acitemcode", ""), r.get("acqty", ""), r.get("acunit", ""),
            r.get("bomoperation", ""),
        ])
    # 图纸变更行
    draw_rows = []
    for r in _rows(inner, "cpcecndrawlineofcpcecn"):
        if not any(_has(r.get(k)) for k in ("olddrawname", "newdrawname", "olddrawid", "newdrawid")):
            continue
        draw_rows.append([
            r.get("drawname") or r.get("olddrawname2") or "",
            r.get("olddrawid", ""), r.get("oldrevlabel") or r.get("oldrev", ""),
            r.get("newdrawid", ""), r.get("newrevlabel") or r.get("newrev", ""),
            r.get("olddrawdescription", ""), r.get("newdrawdescription", ""),
            r.get("drawoperation", ""),
        ])

    tables = []
    if item_rows:
        tables.append({"title": f"物料变更行（{len(item_rows)} 行）",
                       "columns": ["物料编码", "代号", "变更项", "变更前", "变更后"],
                       "rows": item_rows})
    if bom_rows:
        tables.append({"title": f"BOM 行变更（{len(bom_rows)} 行）",
                       "columns": ["上层编码", "上层名称", "变更前编码", "前数量", "前单位",
                                   "变更后编码", "后数量", "后单位", "操作"],
                       "rows": bom_rows})
    if draw_rows:
        tables.append({"title": f"图纸变更（{len(draw_rows)} 行）",
                       "columns": ["图纸名称", "旧图号", "旧版本", "新图号", "新版本",
                                   "旧说明", "新说明", "操作"],
                       "rows": draw_rows})
    return {
        "ok": True, "kind": "ecn", "objid": objid, "code": code,
        "fields": [p for p in fields if p[1]],
        "tables": tables,
        "attachments": _parse_attachments(inner),
    }


@router.get("/items/{itemcode}", summary="物料属性（只读，行菜单用）")
async def item_detail(itemcode: str, request: Request) -> dict:
    zc = _zc(request.app)
    try:
        from .query import _resolve_item
        itemid, name = await _resolve_item(zc, itemcode)
        if not itemid or itemid == "0":
            raise HTTPException(status_code=404, detail=f"未找到物料 {itemcode}")
        inner = _extract_data_inner(await zc.item_select(itemid))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"物料属性查询失败：{type(e).__name__}: {e}")
    finally:
        try:
            await zc.close()
        except Exception:
            pass
    f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", inner))
    fields = [
        _pair("物料编码", f.get("itemcode")), _pair("中文名称", f.get("itemname")),
        _pair("中文描述", f.get("itemdesc")), _pair("代号", f.get("drawid")),
        _pair("版本", f.get("rev")), _pair("计量单位", f.get("itemunit")),
        _pair("规格", f.get("spec")), _pair("材质", f.get("material")),
        _pair("采购标识", f.get("purchflag")), _pair("研发分类", f.get("catname")),
        _pair("产品型号", f.get("productmodel")), _pair("重量", f.get("weight")),
        _pair("受控时间", f.get("ctrltime")), _pair("基础时间", f.get("basetime")),
        _pair("创建", (f.get("creator") or "") + " " + (f.get("createtime") or "")),
        _pair("更新", (f.get("updator") or "") + " " + (f.get("updatetime") or "")),
        _pair("负责人", f.get("owneruser")), _pair("部门", f.get("dept")),
        _pair("备注", f.get("note")),
    ]
    return {"ok": True, "itemid": itemid, "code": itemcode, "name": name,
            "fields": [p for p in fields if p[1].strip()]}
