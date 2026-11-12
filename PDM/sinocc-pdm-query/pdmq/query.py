"""两个查询（与桥接口径完全一致）+ 分段进度。

一、流程查询（kind=rels）——对应桥接 ``/wf/materials/byid/{id}/bomrpt-active-rels``：
  1. cpcbomrpt.getbom 取整棵 BOM
  2. 逐物料 cpcitemrelobj.selectrel（relid 3/1/5）取关联的样品确认单/更改通知单
  3. 逐对象 publicman cpcobjinfo.getobjwf 取流程状态，只保留 stat ∈ {2,3,4,5,6}（未结束）
  结果列与桥接「流程查询」一致：22 列（含「流程中样品确认单」「流程中变更流程单」）
  时间窗口径：物料 ctrltime + 关联对象 objdef2（编制时间），months=0 不限

二、确认单查询（kind=samconfirms）——对应桥接 ``/wf/materials/byid/{id}/bomrpt-samconfirms``：
  1. 同上取 BOM
  2. 逐物料 selectrel(relid=3) 取样品确认单（objtypeid=101，TC/SC 混列）
  3. 逐张 cpcecnsamconfirm.select 取 vendor（供应商「全称 简称」）+ samcon_type + 编制时间
     —— 关联对象行本身没有供应商字段，必须逐张取（2026-11-09 实证）
  不按流程状态过滤：已归档的确认单也列

协议全部来自抓包实证，无猜测字段。
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import date as _date, datetime as _dt
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import jobs
from .vendor.client import get_session
from .vendor.client_zipxml import SinoCCZipClient, _extract_data_inner

router = APIRouter(prefix="/api", tags=["query"])

INVORGID = "104"

# 临时排障：指定物料编码时把 selectrel 原始结果打进 job 日志（PDMQ_DEBUG_CODE=编码）
_DBG_CODE = os.environ.get("PDMQ_DEBUG_CODE", "").strip()
ACTIVE_STATS = {"2", "3", "4", "5", "6"}

# ---- 缓存（同一产品反复查询时复用，避免重复轰服务端） ----
_SELECTREL_CACHE: dict = {}
_SELECTREL_TTL = 300.0
_SELECTREL_EMPTY_TTL = 45.0   # 空结果只缓存 45s：空壳可能是服务端偶发，不能钉死 5 分钟
_OBJWF_CACHE: dict = {}
_OBJWF_TTL = 300.0
_SAMCONF_CACHE: dict = {}
_SAMCONF_TTL = 900.0


class _Cancelled(Exception):
    """用户点了「停止」。"""


class QueryIn(BaseModel):
    itemcode: str = Field(..., description="整机 / 物料编码")
    maxlayer: str = Field("5", description="最大层数，0=全部")
    months: int = Field(6, description="时间窗（月），0=不限")
    concurrency: int = Field(10, description="并发度")
    purchflags: str = Field("P,OP", description="确认单查询：只查这些采购标识的物料（逗号分隔；空=不筛）")


# =============================== 工具 ===============================
def _months_ago(months: int) -> _date:
    today = _date.today()
    y, m = today.year, today.month - months
    while m <= 0:
        y -= 1
        m += 12
    return _date(y, m, min(today.day, 28))


def _parse_date(s) -> Optional[_date]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return _dt.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _zc(app) -> SinoCCZipClient:
    """按当前会话建客户端。入参既可以是 FastAPI app，也可以是 Request（自动取 .app）。"""
    app = getattr(app, "app", app)      # 兼容误传 Request：Request.app 才是 FastAPI 实例
    sess = get_session("default")
    if not sess.login_uid:
        raise HTTPException(status_code=401, detail="未登录：请先完成自动登录")
    zc = SinoCCZipClient(app.state.settings).set_login_uid(sess.login_uid)
    if sess.user:
        zc.set_user(sess.user)
    return zc


async def _resolve_item(zc: SinoCCZipClient, itemcode: str) -> tuple:
    """编码 → (itemid, itemname)。cpcitem.search 实证口径（mdmanager.jsp）。"""
    xml = await zc.call("mdmanager.jsp", "cpcitem", "search",
                        f"<itemcode>{itemcode}</itemcode><invorgid>{INVORGID}</invorgid><getcount>0</getcount>")
    d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", _extract_data_inner(xml)))
    return d.get("itemid", "0"), d.get("itemname", "")


def _check_cancel(job: jobs.Job) -> None:
    if job.cancel_requested:
        raise _Cancelled()


# =============================== 任务入口 ===============================
@router.post("/query/rels", summary="发起流程查询任务")
async def start_rels(body: QueryIn, request: Request) -> dict:
    job = jobs.create("rels", body.model_dump())
    job.task = asyncio.create_task(_run_rels(request.app, job, body))
    return {"ok": True, "job_id": job.id}


@router.post("/query/samconfirms", summary="发起确认单查询任务")
async def start_samconfirms(body: QueryIn, request: Request) -> dict:
    job = jobs.create("samconfirms", body.model_dump())
    job.task = asyncio.create_task(_run_samconfirms(request.app, job, body))
    return {"ok": True, "job_id": job.id}


@router.get("/jobs/{job_id}", summary="查询任务进度")
async def job_status(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {"ok": True, **job.snap()}


@router.get("/jobs/{job_id}/result", summary="取查询结果（任务完成后）")
async def job_result(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    if job.status == "running":
        raise HTTPException(status_code=409, detail="任务仍在执行")
    if job.status != "done":
        raise HTTPException(status_code=400, detail=job.error or "任务失败")
    return {"ok": True, **job.result}


@router.post("/jobs/{job_id}/cancel", summary="停止任务")
async def job_cancel(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    job.cancel_requested = True
    # 协作式检查只在「任务开始前/两次 await 之间」生效，已并发出去的上百个请求不会立刻停；
    # 直接 cancel 掉 worker task（其内部 gather 会连带取消子任务）才能真正秒停。
    if job.task is not None and not job.task.done():
        job.task.cancel()
    return {"ok": True}


# =============================== 公共：取 BOM + 物料时间窗 ===============================
async def _load_bom(zc: SinoCCZipClient, job: jobs.Job, body: QueryIn,
                    fill_root_ctrltime: bool = False) -> dict:
    job.set_stage(1, "读取 BOM 多层明细", total=1)
    itemid, itemname = await _resolve_item(zc, body.itemcode)
    if not itemid or itemid == "0":
        raise HTTPException(status_code=404, detail=f"未找到物料编码 {body.itemcode}")
    # 根物料 rev 必须先取（getbom 需要 rev，缺省会展开失败）
    inner_item = _extract_data_inner(await zc.item_select(itemid))
    rev_m = re.search(r"<rev>([^<]*)</rev>", inner_item)
    rev = rev_m.group(1).strip() if rev_m else ""
    if not itemname:
        nm = re.search(r"<itemname>([^<]*)</itemname>", inner_item)
        itemname = nm.group(1).strip() if nm else ""
    ct_m = re.search(r"<ctrltime>([^<]*)</ctrltime>", inner_item)
    root_ctrltime = ct_m.group(1).strip() if ct_m else ""
    _check_cancel(job)

    bom_inner = _extract_data_inner(await zc.bomrpt_getbom(itemid, body.itemcode, rev, body.maxlayer))
    rows = []
    for b in re.findall(r"<bomd>([\s\S]*?)</bomd>", bom_inner):
        rows.append({k: v for k, v in re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b)})
    # 根行（wbs=0）的 <bomd> 里没有 ctrltime（实测：整机/单物料做根时该字段恒空）→ 用
    # cpcitem.select 拿到的受控时间回填。不回填的后果：直接查一个受控时间很早的物料时，
    # 它会被当成「受控时间未知」而逃过时间窗过滤（用户实测 801242001527 就是这么冒出来的）。
    # ⚠ 只在确认单查询里回填：流程查询要与桥接逐条一致（桥接那边根行的空 ctrltime 是保留的）。
    if fill_root_ctrltime and root_ctrltime:
        for r in rows:
            if (r.get("itemcode") or "").strip() == body.itemcode and not (r.get("ctrltime") or "").strip():
                r["ctrltime"] = root_ctrltime
    maxlayer_m = re.search(r"<maxlayer>([^<]*)</maxlayer>", bom_inner)
    job.tick(1, f"BOM {len(rows)} 行")
    return {
        "itemid": itemid, "itemname": itemname, "itemrev": rev,
        "maxlayer": (maxlayer_m.group(1) if maxlayer_m else body.maxlayer),
        "rows": rows,
        "bom_ms": 0,
    }


def _material_targets(rows: list, months: int,
                      purchflags: set | None = None,
                      filter_ctrltime: bool = True,
                      exclude_code_prefixes: set | None = None) -> tuple:
    """去重物料 + 采购标识过滤 +（可选）时间窗过滤。返回 (targets, stats)。

    流程查询：时间窗口径与桥接一致 —— 物料 ctrltime < cutoff 的跳过（空 ctrltime 保留，只计数）。

    确认单查询（2026-11-09 起）：
      · `purchflags={"P","OP"}` → 只查采购件；其余物料整行不参与（前端也就不会显示）
      · `filter_ctrltime=False` → **不按物料受控时间过滤**。原因：本功能要回答「这期内有没有确认单」，
        老物料（受控时间久）照样可能刚做过确认；按 ctrltime 剪掉会让这些采购件凭空消失、
        还会被误判成「期内无确认」。时间窗只作用于确认单自身的编制时间。
    """
    cutoff = _months_ago(months) if months and months > 0 else None
    seen, metas = set(), []
    for r in rows:
        c = (r.get("itemcode") or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        metas.append((c, (r.get("itemid") or "0").strip(), r.get("ctrltime", ""),
                      (r.get("purchflag") or "").strip()))
    targets: dict[str, str] = {}
    skipped_ctrltime = empty_ctrltime = skipped_purchflag = skipped_prefix = 0
    prefixes = tuple((exclude_code_prefixes or set()))
    for c, i, ct, pf in metas:
        if prefixes and c.upper().startswith(prefixes):
            skipped_prefix += 1
            continue
        if purchflags and pf not in purchflags:
            skipped_purchflag += 1
            continue
        d = _parse_date(ct)
        if not filter_ctrltime:
            if d is None:
                empty_ctrltime += 1
            targets[c] = i
            continue
        if d is None:
            empty_ctrltime += 1
            targets[c] = i
        elif cutoff is None or d >= cutoff:
            targets[c] = i
        else:
            skipped_ctrltime += 1
    return targets, {
        "cutoff": cutoff.isoformat() if cutoff else None,
        "months": months or 0,
        "skipped_by_ctrltime": skipped_ctrltime,
        "empty_ctrltime": empty_ctrltime,
        "skipped_by_purchflag": skipped_purchflag,
        "skipped_by_prefix": skipped_prefix,
        "exclude_prefixes": sorted(exclude_code_prefixes) if exclude_code_prefixes else [],
        "purchflags": sorted(purchflags) if purchflags else [],
        "filter_ctrltime": bool(filter_ctrltime),
    }


def _parse_rel_rows(xml: str) -> list:
    out = []
    for mm in re.finditer(r"<itemrelobjofitemrelobj>(.*?)</itemrelobjofitemrelobj>", xml, re.S):
        out.append(dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", mm.group(1))))
    return out


async def _selectrel(zc: SinoCCZipClient, code: str, itemid: str, relid: str,
                     sem: asyncio.Semaphore, job: "jobs.Job | None" = None) -> list:
    """取某物料在指定 relid 下的关联对象行（带缓存 + 空结果重试）。

    2026-11-09 用户实测事故：整机查询里 801142000120 明明有 SC2411110016，却显示「无确认单」；
    单独查该物料又是对的。同一 itemid 单独/并发直调都稳定返回 1 行 —— 说明是**批量跑时偶发空壳**
    （服务端在几十路并发下偶发返回空列表），而旧代码把这个空壳当「真的没有」：
      ① 直接判定「无确认单」（假阴性，最伤）
      ② 空结果还被缓存 5 分钟 → 后续重跑也一直错
    对策：
      · 空结果**先重试一次**再定论；
      · 空结果缓存只留 45 秒（好结果 300 秒），避免把偶发空壳长期钉死；
      · 真取数失败（异常）不缓存，由调用方标记成「取数失败」，绝不谎报「无确认单」。
    """
    key = f"{relid}:{itemid}"
    hit = _SELECTREL_CACHE.get(key)
    if hit is not None and time.time() - hit["ts"] < hit.get("ttl", _SELECTREL_TTL):
        return hit["rows"]

    async def _once() -> str:
        async with sem:
            return await zc.call("bomman.jsp", "cpcitemrelobj", "selectrel",
                                 f"<relid>{relid}</relid><itemid>{itemid}</itemid>"
                                 f"<itemcode>{code}</itemcode><invorgid>{INVORGID}</invorgid><reftypeid>0</reftypeid>")

    rows = _parse_rel_rows(await _once())
    if not rows:
        if job is not None:
            job.note(f"{code} 关联对象返回空，重试一次")
        await asyncio.sleep(0.12)
        retry = _parse_rel_rows(await _once())
        if retry:
            rows = retry
    _SELECTREL_CACHE[key] = {"rows": rows, "ts": time.time(),
                             "ttl": _SELECTREL_TTL if rows else _SELECTREL_EMPTY_TTL}
    return rows


# =============================== 流程查询 ===============================
async def _run_rels(app, job: jobs.Job, body: QueryIn) -> None:
    zc = None
    t0 = time.time()
    try:
        zc = _zc(app)
        t_bom0 = time.time()
        base = await _load_bom(zc, job, body)
        t_bom = time.time() - t_bom0

        rows = base["rows"]
        targets, tstats = _material_targets(rows, body.months)
        if not targets:
            job.finish({**base, "count": len(rows), "active_rels": {},
                        "skipped": {**tstats, "no_active_wf": 0, "wf_error": 0, "no_wf": 0,
                                    "rel_types": 0, "unique_objids": 0, "skipped_by_objtime": 0},
                        "perf_ms": {"bom_ms": int(t_bom * 1000), "rels_ms": 0, "wf_ms": 0,
                                    "total_ms": int((time.time() - t0) * 1000)}})
            return

        # 关联类型定义（一次）：只保留 relid 3=样品确认单 / 1=更改通知单 / 5=更改通知单-生产类型转换
        xml_defs = await zc.call("bomman.jsp", "cpcitemrelobj", "selectitemobjrel",
                                 f"<itemid>{base['itemid']}</itemid><itemcode>{body.itemcode}</itemcode>"
                                 f"<invorgid>{INVORGID}</invorgid><reftypeid>0</reftypeid>")
        rel_types = []
        for m in re.finditer(r"<itemrelobjofitemrelobj>(.*?)</itemrelobjofitemrelobj>", xml_defs, re.S):
            d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", m.group(1)))
            if d.get("relid") in ("3", "1", "5"):
                rel_types.append({"relid": d["relid"], "reftype": d.get("reftypename", "")})

        job.set_stage(2, "扫描各物料的关联对象（确认单 / 变更通知单）", total=len(targets))
        sem = asyncio.Semaphore(max(1, body.concurrency))
        cutoff = _parse_date(tstats["cutoff"]) if tstats.get("cutoff") else None
        skipped_objtime = 0
        all_objs: list[tuple[str, dict]] = []

        async def one_item(code_one: str, itemid_one: str):
            nonlocal skipped_objtime
            _check_cancel(job)
            objs = []
            try:
                for rt in rel_types:
                    try:
                        for f in await _selectrel(zc, code_one, itemid_one, rt["relid"], sem, job):
                            oid, t = f.get("objid", ""), f.get("objtypeid", "")
                            if t not in ("101", "91046") or not oid:
                                continue
                            if cutoff is not None:
                                od = _parse_date(f.get("objdef2", ""))
                                if od is not None and od < cutoff:
                                    skipped_objtime += 1
                                    continue
                            objs.append({
                                "objid": oid, "objcode": f.get("objcode", ""),
                                "objname": f.get("objname", ""), "objsubject": f.get("objsubject", ""),
                                "objdef1": f.get("objdef1", ""), "objdef2": f.get("objdef2", ""),
                                "objtypeid": t,
                                "objtypename": "样品确认单" if t == "101" else "变更流程单",
                                "reftypename": rt["reftype"],
                            })
                    except _Cancelled:
                        raise
                    except Exception:
                        pass
            finally:
                # 进度必须在这里 tick（不能等 gather 回来统一算）：否则 47 个物料
                # 并发跑完后才一次性 +47，进度条会「卡住然后跳一大格」，进度就失去意义。
                job.tick(1, f"{code_one}：{len(objs)} 个关联对象")
            return code_one, objs

        results = await asyncio.gather(*[one_item(c, i) for c, i in targets.items()])
        for c, objs in results:
            for o in objs:
                all_objs.append((c, o))
        _check_cancel(job)

        unique_objids = list({o["objid"] for _, o in all_objs})
        objtype_of = {o["objid"]: o["objtypeid"] for _, o in all_objs}

        # 第 3 段：逐个对象取流程状态（同一 objid 多处引用只查一次）
        job.set_stage(3, "读取对象的流程状态", total=len(unique_objids))

        async def _wf_inner(oid: str, objtype: str) -> tuple:
            _check_cancel(job)
            key = f"{objtype}:{oid}"
            hit = _OBJWF_CACHE.get(key)
            if hit is not None and time.time() - hit["ts"] < _OBJWF_TTL:
                return oid, hit["wf"], hit["err"]
            async with sem:
                try:
                    xml_wf = await zc.call("publicman.jsp", "cpcobjinfo", "getobjwf",
                                           f"<objid>{oid}</objid><objtype>{objtype}</objtype>"
                                           f"<wftempid>0</wftempid><codeid>0</codeid><dsid>0</dsid>")
                    wf_rows = []
                    for b in re.finditer(r"<wfofobj[ >](.*?)</wfofobj>", xml_wf, re.S):
                        f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b.group(1)))
                        wf_rows.append({k: f.get(k, "") for k in
                                        ("wfid", "wfname", "stat", "startor", "statime")})
                except _Cancelled:
                    raise
                except Exception:
                    _OBJWF_CACHE[key] = {"wf": None, "err": "wf_error", "ts": time.time()}
                    return oid, None, "wf_error"

            # 一个对象可能挂多个流程（含下游归档流程）→ 用它自己的源 wfid 判状态
            src = None
            if str(objtype) in ("101", "91046") and len(wf_rows) > 1:
                try:
                    if str(objtype) == "101":
                        xml_src = await zc.samconfirm_select(oid)
                        tags = ("objattachs", "cpcecnsamconfirmitemsofecnsamconfirms",
                                "ecnsamconfirmlineofecnsamconfirms")
                    else:
                        xml_src = await zc.ecn_select(oid)
                        tags = ("objattachs", "cpcecnitemlineofcpcecns",
                                "cpcecnbomlineofcpcecns", "cpcecndrawlineofcpcecns")
                    cleaned = xml_src
                    for t in tags:
                        cleaned = re.sub(rf"<{t}>.*?</{t}>", "", cleaned, flags=re.S)
                    m = re.search(r"<wfid>(\d+)</wfid>", cleaned)
                    if m and m.group(1).strip() not in ("", "0"):
                        src = m.group(1).strip()
                except Exception:
                    src = None
            wf, err = None, None
            if src:
                wf = next((w for w in wf_rows if w.get("wfid") == src), None)
            if wf is None:
                act = [w for w in wf_rows if w.get("stat") in ACTIVE_STATS]
                wf = act[0] if act else (wf_rows[0] if wf_rows else None)
                err = None if act else ("no_active_wf" if wf else "no_wf")
            _OBJWF_CACHE[key] = {"wf": wf, "err": err, "ts": time.time()}
            return oid, wf, err

        async def wf_check(oid: str, objtype: str) -> tuple:
            # 进度在这里统一 tick（缓存命中/异常路径也计数），保证进度条连续
            try:
                return await _wf_inner(oid, objtype)
            finally:
                job.tick(1)

        wf_results = await asyncio.gather(*[wf_check(o, objtype_of[o]) for o in unique_objids])

        wf_by_objid = {oid: (wf, err) for oid, wf, err in wf_results}
        active_rels: dict = {c: [] for c in targets}
        skipped = {**tstats, "no_active_wf": 0, "wf_error": 0, "no_wf": 0,
                   "rel_types": len(rel_types), "unique_objids": len(unique_objids),
                   "skipped_by_objtime": skipped_objtime,
                   "no_active_wf_or_no_wf": 0}
        for c, obj in all_objs:
            wf, err = wf_by_objid.get(obj["objid"], (None, "wf_error"))
            if err == "wf_error":
                skipped["wf_error"] += 1
                continue
            if not wf:
                skipped["no_wf"] += 1
                continue
            if wf.get("stat") not in ACTIVE_STATS:
                skipped["no_active_wf"] += 1
                continue
            obj.update({"wfid": wf.get("wfid", ""), "wfname": wf.get("wfname", ""),
                        "wfstat": wf.get("stat", ""), "wfstartor": wf.get("startor", ""),
                        "wfstatime": wf.get("statime", "")})
            active_rels[c].append(obj)

        job.finish({
            **base, "count": len(rows), "active_rels": active_rels, "skipped": skipped,
            "perf_ms": {"bom_ms": int(t_bom * 1000), "wf_ms": 0,
                        "total_ms": int((time.time() - t0) * 1000)},
        })
    except (asyncio.CancelledError, _Cancelled):
        job.cancelled()
    except HTTPException as e:
        job.fail(str(e.detail))
    except Exception as e:  # noqa: BLE001
        job.fail(f"{type(e).__name__}: {e}")
    finally:
        if zc is not None:
            try:
                await zc.close()
            except Exception:
                pass


# =============================== 确认单查询 ===============================
def _parse_samconfirm_head(xml: str) -> dict:
    """确认单主表字段（剔除子表块，防同名标签覆盖表头）。"""
    inner = _extract_data_inner(xml)
    head = inner
    for tag in ("cpcecnsamconfirmitemsofecnsamconfirms", "ecnsamconfirmlineofecnsamconfirms", "objattachs"):
        head = re.sub(rf"<{tag}>.*?</{tag}>", "", head, flags=re.S)
    f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", head))
    return {
        "samcon_code": (f.get("samcon_code") or "").strip(),
        "samcon_type": (f.get("samcon_type") or "").strip(),
        "vendor": (f.get("vendor") or f.get("vendorname") or "").strip(),
        "vendor_id": (f.get("vendor_id") or "").strip(),
        "creator": (f.get("creator") or "").strip(),
        "create_time": (f.get("create_time") or "").strip(),
        "stat": (f.get("stat") or "").strip(),
        "wfid": (f.get("wfid") or "").strip(),
    }


async def _run_samconfirms(app, job: jobs.Job, body: QueryIn) -> None:
    zc = None
    t0 = time.time()
    try:
        zc = _zc(app)
        t_bom0 = time.time()
        base = await _load_bom(zc, job, body, fill_root_ctrltime=True)
        t_bom = time.time() - t_bom0
        rows = base["rows"]
        # 口径（2026-11-09 用户两次实测后定案）：
        #   · 只列采购件（purchflag ∈ {P, OP}）
        #   · **时间窗只作用于「物料受控时间 ctrltime」**：受控时间在窗外的物料整行不出现、不计入缺单统计
        #     （实测 801242001527 受控 2019 年、单号也是 2019/2022 → 属于窗外，不该出现在这份清单里）
        #   · 窗内物料列出**全部**确认单，不再按确认单编制时间剪
        #     （实测 802103100075 受控 2025-05、确认单 2025-05~07 → 按编制时间剪会被误判成「无确认」）
        pf = {s.strip().upper() for s in (body.purchflags or "").split(",") if s.strip()}
        # 2026-11-09：编码以 P 开头的物料（P 前缀类）不查确认单、不参与缺单判定，整行排除
        targets, tstats = _material_targets(rows, body.months, purchflags=pf,
                                            filter_ctrltime=True, exclude_code_prefixes={"P"})
        rows_out = [r for r in rows if (r.get("itemcode") or "").strip() in targets]

        job.set_stage(2, "扫描各物料的样品确认单", total=max(1, len(targets)),
                      message=(f"窗内采购件 {len(targets)} 个（排除非 P/OP {tstats['skipped_by_purchflag']}，"
                               f"P 前缀 {tstats['skipped_by_prefix']}，受控超窗 {tstats['skipped_by_ctrltime']}）"))
        sem = asyncio.Semaphore(max(1, body.concurrency))
        skipped_objtime = 0

        async def one_item(code_one: str, itemid_one: str):
            nonlocal skipped_objtime
            _check_cancel(job)
            out = []
            failed = False
            try:
                rel_rows = await _selectrel(zc, code_one, itemid_one, "3", sem, job)
                if _DBG_CODE and code_one == _DBG_CODE:
                    job.note(f"DEBUG {code_one} itemid={itemid_one} rows={len(rel_rows)} "
                             f"types={[x.get('objtypeid') for x in rel_rows]} "
                             f"objcodes={[x.get('objcode') for x in rel_rows]}")
                for f in rel_rows:
                    if f.get("objtypeid") != "101" or not f.get("objid"):
                        continue
                    out.append({"objid": f.get("objid", ""),
                                "objcode": (f.get("objcode") or "").strip(),
                                "objdef1": f.get("objdef1", ""),
                                "objdef2": f.get("objdef2", "")})
            except _Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                # 取数异常 ≠ 没有确认单。标记成「取数失败」，前端用灰标提示，绝不标红谎报缺单
                failed = True
                job.note(f"{code_one} 取数失败：{type(e).__name__}: {str(e)[:60]}")
            finally:
                job.tick(1, f"{code_one}：{('取数失败' if failed else str(len(out)) + ' 张确认单')}")
            return code_one, out, failed

        rel_results = await asyncio.gather(*[one_item(c, i) for c, i in targets.items()])
        by_code: dict = {c: [] for c in targets}
        owner: dict[str, str] = {}
        fetch_failed: list[str] = []
        for code_one, objs, bad in rel_results:
            if bad:
                fetch_failed.append(code_one)
                continue
            for o in objs:
                # ⚠ 2026-11-09 生产事故修复：一张确认单的明细里可以有多个物料，所以同一个 objid
                #   会被多个物料的关联对象列表同时引用。旧代码 `if objid in owner: continue`
                #   把对象**归属**给了第一个物料，后面那些物料就凭空「无确认单」了
                #   （实测 801142000120 的 SC2411110016 被记到了先出现的 801107100003 名下）。
                #   正确做法：归属照记（每个引用它的物料都要有），只在取详情时按 objid 去重。
                by_code.setdefault(code_one, []).append(o)
                if o["objid"] not in owner:
                    owner[o["objid"]] = code_one
        _check_cancel(job)

        all_objids = list(owner.keys())
        job.set_stage(3, "读取确认单明细（供应商）", total=max(1, len(all_objids)))
        details: dict = {}

        async def _detail_inner(oid: str):
            _check_cancel(job)
            hit = _SAMCONF_CACHE.get(oid)
            if hit is not None and time.time() - hit["ts"] < _SAMCONF_TTL:
                return oid, hit["data"]
            async with sem:
                try:
                    d = _parse_samconfirm_head(await zc.samconfirm_select(oid))
                except _Cancelled:
                    raise
                except Exception:
                    return oid, None
            _SAMCONF_CACHE[oid] = {"data": d, "ts": time.time()}
            return oid, d

        async def one_detail(oid: str):
            try:
                return await _detail_inner(oid)
            finally:
                job.tick(1)

        if all_objids:
            for oid, d in await asyncio.gather(*[one_detail(o) for o in all_objids]):
                details[oid] = d

        sam_map: dict = {}
        for code_one, objs in by_code.items():
            lst = []
            for o in objs:
                if o["objid"] not in details:
                    continue
                d = details[o["objid"]] or {}
                code = (d.get("samcon_code") or o["objcode"] or "").strip()
                typ = d.get("samcon_type") or ""
                up = code[:2].upper()
                kind = ("formal" if up == "SC" else "temp" if up == "TC"
                        else ("temp" if "临时" in typ else "formal"))
                lst.append({
                    "objid": o["objid"], "code": code, "kind": kind, "type": typ,
                    "vendor": d.get("vendor") or "", "vendor_id": d.get("vendor_id") or "",
                    "creator": d.get("creator") or o.get("objdef1") or "",
                    "createtime": d.get("create_time") or o.get("objdef2") or "",
                    "stat": d.get("stat") or "", "wfid": d.get("wfid") or "",
                })
            lst.sort(key=lambda x: (x["kind"], x["createtime"]))
            if lst:
                sam_map[code_one] = lst

        # 缓存清理
        now = time.time()
        for k in [k for k, v in _SAMCONF_CACHE.items() if now - v["ts"] > _SAMCONF_TTL]:
            _SAMCONF_CACHE.pop(k, None)

        job.finish({
            **base, "count": len(rows_out), "rows": rows_out,
            "samconfirms": sam_map,
            "fetch_failed": fetch_failed,
            "skipped": {**tstats, "skipped_by_objtime": skipped_objtime,
                        "unique_objs": len(owner),
                        "fetch_failed": len(fetch_failed),
                        "no_confirm": sum(1 for c in targets if c not in sam_map and c not in fetch_failed)},
            "perf_ms": {"bom_ms": int(t_bom * 1000), "objs_ms": 0,
                        "total_ms": int((time.time() - t0) * 1000)},
        })
    except (asyncio.CancelledError, _Cancelled):
        job.cancelled()
    except HTTPException as e:
        job.fail(str(e.detail))
    except Exception as e:  # noqa: BLE001
        job.fail(f"{type(e).__name__}: {e}")
    finally:
        if zc is not None:
            try:
                await zc.close()
            except Exception:
                pass
