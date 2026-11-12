"""Workflow router (流程审批中台).

Exposes:
- GET    /wf/tasks               待办（我的流程）
- GET    /wf/tasks/{id}          任务详情
- GET    /wf/templates           可发起流程模板
- POST   /wf/tasks/{id}/approve  通过
- POST   /wf/tasks/{id}/reject   驳回
- POST   /wf/tasks/{id}/transfer 转办
- POST   /wf/tasks/{id}/addsign  加签
- POST   /wf/launch              发起新流程
"""
from __future__ import annotations

import logging
import zlib

import httpx

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, File, UploadFile, Body
from fastapi.responses import Response
from pydantic import BaseModel, Field
import asyncio
import io
import mimetypes
import os
import re
import tempfile
import zlib
import uuid
import zipfile
from urllib.parse import quote

from ..client import SinoCCClient, get_session
from ..client_zipxml import (SinoCCZipClient, decode_file_body, _extract_data_inner,
                             parse_bomd_response, parse_cpc_tail, CPC_TAIL_SIZE,
                             CPC_NAME_LEGACY_CAP)
from ..deps import get_client

router = APIRouter()
log = logging.getLogger("sinocc.bridge.wf")

# 检出模板缓存：docid -> checkout 响应 XML（作 checkin 模板）
CHECKOUT_CACHE: dict = {}

# 2026-09-03 检出编辑会话监视器：docid -> {path, mtime, size, docname, readonly}
# open-edit 落地本地工作副本后注册；edit-status 轮询探测编辑程序是否关闭 + 内容是否变更；
# checkin-local 只允许检入监视器登记的本地路径（防任意文件读写）。
EDIT_WATCHERS: dict = {}

# 2026-08-27：getobjwf 结果缓存（objid → 主流程快照）。
# 同一 ECN/确认单 会被 BOM 里多个物料引用，且用户会反复查看同一产品，
# 缓存避免重复调用服务端（全树 579 行曾因重复 getobjwf 耗时 200s+）。
# 流程 stat 变化不频繁，TTL 300s 足够新鲜。
import time as _time
_OBJWF_CACHE: dict = {}       # key=f"{objtype}:{objid}" -> {"wfid","wfname","stat","startor","statime","ts"}
_OBJWF_TTL: float = 300.0
# 2026-08-27：selectrel 结果缓存（物料关联对象列表短期不变；同一产品反复查看时复用）
_SELECTREL_CACHE: dict = {}   # key=f"{relid}:{itemid}" -> [(objid, objtypeid, ...)], 简化存原始行 dict 列表
_SELECTREL_TTL: float = 300.0

# 2026-08-27：时间窗口过滤（只查询最近半年）。
# 物料受控时间 = getbom 每行 <bomd> 的 ctrltime；关联对象编制时间 = selectrel 每行 objdef2。
from datetime import datetime as _dt, date as _date, timedelta as _tdelta

def _months_ago(months: int) -> _date:
    """相对今天回退 N 个日历月的日期（月底安全兜底到 28 号）。用于时间窗过滤。"""
    today = _date.today()
    y, m = today.year, today.month - months
    while m <= 0:
        y -= 1
        m += 12
    return _date(y, m, min(today.day, 28))

def _parse_date(s) -> Optional[_date]:
    """解析 YYYY-MM-DD[ HH:MM:SS]（兼容 / 分隔）→ date；空或无法解析返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return _dt.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def _objwf_cache_get(objtype: str, objid: str):
    key = f"{objtype}:{objid}"
    hit = _OBJWF_CACHE.get(key)
    if hit is not None and _time.time() - hit["ts"] < _OBJWF_TTL:
        return hit
    return None


def _objwf_cache_set(objtype: str, objid: str, wf: dict | None):
    """wf=None 表示「该对象无流程/查不到」，缓存为空哨兵（与未缓存区分）。"""
    key = f"{objtype}:{objid}"
    if wf is None:
        _OBJWF_CACHE[key] = {"wf": None, "ts": _time.time()}
    else:
        _OBJWF_CACHE[key] = {"wf": {k: wf.get(k, "") for k in ("wfid", "wfname", "stat", "startor", "statime")},
                              "ts": _time.time()}


def _settings(request: Request):
    return request.app.state.settings


async def _client(request: Request, client: SinoCCClient = Depends(get_client)) -> SinoCCClient:
    return client


def _zipxml_client(request: Request) -> SinoCCZipClient:
    """客户端通道 client：复用 session store 的 loginUID + 当前用户名。

    2026-09-02：必须带上 sess.user，否则 client 侧 account 为空，
    流程提交/驳回的 <userid> 会写不出真实操作者（此前是硬编码「唐阳」）。
    """
    sess = get_session("default")
    if not sess.login_uid:
        raise HTTPException(status_code=401, detail="未登录：请先调用 POST /auth/session 注入登录态")
    return (SinoCCZipClient(request.app.state.settings)
            .set_login_uid(sess.login_uid)
            .set_user(sess.user))


# ---------- DTOs ----------
class ApproveIn(BaseModel):
    opinion: str
    next_user: Optional[str] = Field(None, description="指定下一步处理人（可选）")
    rejectto: Optional[str] = Field(None, description="驳回目标节点 procid（驳回专用；缺省=当前节点 preproc）")
    submitto: Optional[str] = Field(None, description="提交目标节点 procid（通过专用；缺省=按顺序前进到下一节点）")


class TransferIn(BaseModel):
    to_user: str
    reason: Optional[str] = ""


class AddSignIn(BaseModel):
    to_user: str
    before: bool = Field(False, description="True=前加签，False=后加签")


class LaunchIn(BaseModel):
    template_code: str = Field(..., description="模板编码（FreeMenuPageFormList 返回的 code）")
    subject: str
    fields: dict = Field(default_factory=dict, description="表单字段 key→value")


class WfLaunchCreateRequest(BaseModel):
    """发起流程（复刻客户端右键「工作流」→ 选模板 → 编制界面 → 提交创建，2026-09-02 抓包）"""
    wftempid: str = Field(..., description="流程模板 ID")
    objid: str = Field("", description="挂载对象 ID（主对象=入口文档；提供 objects 时可省略，取 objects[0]）")
    objtype: str = Field("6", description="挂载对象类型（文档=6）")
    objname: str = Field("", description="对象全名（用于 wfobjofwf.objname / 服务端自动生成 subject）")
    objrev: str = Field("", description="对象版本号（如 1）")
    objrevlabel: str = Field("", description="对象版本标签（如 A.1）")
    subject: Optional[str] = Field(None, description="流程主题。编制期用户手改 → 直传主记录（服务端原样保留 subjecttype=0）；空/None → 留空由服务端用对象名自动生成（subjecttype=6，2026-09-02 wf_launch_dialog_v2 实证）")
    objects: Optional[list[dict]] = Field(None,
        description="多对象挂载 [{'objid','objtype','objname','objrev','objrevlabel'},...]，seq 从 1 递增一次提交（2026-09-02 实证 5 对象进 wfobjofwfs）。提供时 objid/objtype/objname/... 作缺省回填")
    opinion: str = Field("同意", description="提交首节点时的意见")
    executors: dict = Field(default_factory=dict,
                            description="{procid: [{userid, username, ismajor}]}；ismajor=2=决策人；缺省按模板自动（流程启动者=当前用户）")
    node_flags: dict = Field(default_factory=dict,
                             description="{procid: {allperson: '1'|'2'}}；allperson 2=全签（所有执行人都通过），仅覆盖用户改过的节点；缺省按模板")
    submit: bool = Field(True, description="创建后是否自动提交首节点（True=完整三步）")


class WfObjectDeleteRequest(BaseModel):
    """从已创建流程实例删除对象（复刻 cpcwfobj.delete，2026-09-02 wf_launch_dialog_v2 实证）"""
    objid: str = Field(..., description="要删除的对象 ID")
    objtype: str = Field("6", description="对象类型（文档=6）")


class WfObjectArrangeRequest(BaseModel):
    """重排流程实例对象顺序（复刻 cpcwf.arrangeobjseq，2026-09-02 实证）"""
    order: list[dict] = Field(..., description="[{'seq': 旧序号, 'newseq': 新序号}, ...] 全量映射（原版删对象后重排为 1..N）")


class WfObjectAddItem(BaseModel):
    """待追加对象（字段来源 = cpcfdr selectref 的 doc 行：docid→objid、docname→objname、rev/revlabel）"""
    objid: str = Field(..., description="对象 ID（= 文档 docid）")
    objtype: str = Field("6", description="对象类型（附件文档=6）")
    objname: str = Field("", description="对象名（纯文件名，服务端回显补全路径）")
    objrev: str = Field("", description="版本号（如 1）")
    objrevlabel: str = Field("", description="版本标签（如 A.1）")


class WfObjectAddRequest(BaseModel):
    """向已创建流程实例追加对象（复刻 cpcwfobj.insert，2026-09-03 wf_detail_add_obj_v1/v2 实证）"""
    objects: list[WfObjectAddItem] = Field(..., min_length=1,
        description="待追加对象列表（服务端按现有 max seq+1 起逐个 insert；一次勾选 N 个的请求形态未抓包实证，桥接按 N 个独立 insert 串行）")


class OpResult(BaseModel):
    ok: bool
    message: Optional[str] = None


# ---------- helpers ----------


# ---------- endpoints ----------
@router.get("/instances", summary="工作流实例列表（客户端通道 cpcwf.selectref）")
async def wf_instances(
    request: Request,
    templid: str = Query("", description="流程模板 ID（空=对全部模板逐个查询并合并）"),
    limit: int = Query(100, description="每模板最多条数"),
):
    """复刻客户端「工作流内容(对象)」列表：
    selectref(wftempid=<模板>, flag=1) 返回该模板下的流程实例。
    不传 templid 时先 selectall 取模板列表，再逐个 selectref 合并。"""
    import re
    zc = _zipxml_client(request)
    rows = []
    try:
        if templid:
            xml = await zc.wf_selectref(wftempid=templid, flag="1")
            rows.extend(_parse_wf_instance_xml(xml))
        else:
            # 1) selectall 拿模板列表
            xml = await zc.wf_list()
            tpls = re.findall(r"<wftempid>(\d+)</wftempid>", xml)
            # 去重
            seen = []
            for t in tpls:
                if t not in seen:
                    seen.append(t)
            # 2) 每个模板 selectref
            for t in seen[:10]:
                try:
                    x = await zc.wf_selectref(wftempid=t, flag="1")
                    rows.extend(_parse_wf_instance_xml(x))
                except Exception:
                    continue
        # 默认按最后更新时间倒序
        rows.sort(key=lambda r: r.get("last_time") or "", reverse=True)
        return {"ok": True, "total": len(rows), "rows": rows[:limit]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "total": 0, "rows": []}


def _parse_wf_instance_xml(xml: str) -> list:
    """解析 selectref 响应 → 流程实例列表。"""
    import re
    out = []
    for m in re.finditer(r"<wf>(.*?)</wf>", xml, re.S):
        chunk = m.group(1)
        def g(tag):
            mm = re.search(rf"<{tag}>([^<]*)</{tag}>", chunk)
            return mm.group(1) if mm else ""
        out.append({
            "wf_id": g("wfid"),
            "wf_name": g("wfname"),
            "wftempid": g("wftempid"),
            "stat": g("stat"),  # 0/2/3/4/5/6/7 → 0=草稿 2=进行中 4=已归档 7=中断等
            "subject": g("subject"),
            "objname": g("objname"),
            "curr_proc": g("currprocname"),
            "curr_user": g("curruser"),
            "startor": g("startor"),
            "last_user": g("lastuser"),
            "start_time": g("statime"),
            "last_time": g("lasttime"),
            "end_time": g("endtime"),
            "priority": g("priority"),
            "flag": g("flag"),
            "objid": g("objid"),
            "url": g("url"),
        })
    return out






def _parse_wf_detail_xml(wf_id: str, xml: str, login_user: str = "") -> dict:
    """解析 cpcwf.select 响应 → 主信息 + 流程图节点 + 关联对象。

    login_user: 当前登录用户名（用于"本人已签"标记与 can_break 判断；注意节点
    can_add_user 权限已确认与 login_user 无关，只取节点字段 canmodify/operright）。
    """
    import re
    def g(chunk, tag):
        m = re.search(rf"<{tag}>([^<]*)</{tag}>", chunk)
        return m.group(1) if m else ""
    main = {t: g(xml, t) for t in
            ["wfid", "wfname", "wftempid", "stat", "subject", "startor", "startorname",
             "currprocid", "currprocname", "lastuser", "lasttime", "statime", "endtime",
             "priority", "objid", "objtype"]}
    nodes = []
    for m in re.finditer(r"<wfprocofwf>(.*?)</wfprocofwf>", xml, re.S):
        chunk = m.group(1)
        # 节点处理人列表（多人会签/未签也有名字）
        users = []
        for um in re.finditer(r"<procuserofwfproc>(.*?)</procuserofwfproc>", chunk, re.S):
            u = um.group(1)
            users.append({
                "userid": g(u, "userid"),
                "username": g(u, "username"),
                "stat": g(u, "stat"),
                "issigned": g(u, "issigned"),
                "ismajor": g(u, "ismajor"),
                "tempuser": g(u, "tempuser"),
                "rate": g(u, "rate"),
                "freq": g(u, "freq"),
                "agent": g(u, "agent"),
                # issigned=2 本人已签；issigned=3 已由代理人完成（代理人在 agent 字段）。
                "agentname": g(u, "agentname"),
                "orgpath": g(u, "orgpath"),
                "positionid": g(u, "positionid"),
                # 2026-08-25 抓包实证（1324062）：沟通加入 = tempuser=0 + addedbyuser(发起人) + iscommunicate=2；
                # 转办加入 = tempuser=1 + sourceuserid(来源) + note"XX转办给YY"
                "addedbyuser": g(u, "addedbyuser"),
                "sourceuserid": g(u, "sourceuserid"),
                "iscommunicate": g(u, "iscommunicate"),
                "note": g(u, "note"),
            })
        # 节点执行情况（用户意见历史）：freq + stat + opinion
        #   exec_count = max(freq)，驳回判断：opinion 文本含"驳回"
        # 2026-08-25 抓包实证（wf_node_green_capture，wfid 1330030）：
        #   procuserofwfproc 的 <stat> 恒为 0（不是已签标志！）；
        #   已签判断 = useropinionofwfproc 里 stat=7(已通过) 或 signtime 非空的 userid，
        #   或 procuserofwfproc.issigned == "2"（霍润波/高海棠 issigned=2 且 useropinion 有"同意"记录）
        executions = []
        max_freq = 0
        rejected = False
        signed_ids = set()
        # 2026-08-28 实证（1328577/10 审核）：procuser.agent=刘韬瑞，
        # useropinion.userid=刘韬瑞 且 isagent=2；即刘韬瑞代郭敏签。
        for om in re.finditer(r"<useropinionofwfproc>(.*?)</useropinionofwfproc>", chunk, re.S):
            ou = om.group(1)
            ud = {
                "userid": g(ou, "userid"),
                "username": g(ou, "username"),
                "freq": g(ou, "freq"),
                "stat": g(ou, "stat"),
                "opinion": g(ou, "opinion"),
                "isagent": g(ou, "isagent"),
                "signtime": g(ou, "signtime"),
            }
            # stat=7 不一定都是本人同意；原客户端对沟通/转办也保留一条记录。
            # 1328577/2 的意见文本能可靠区分：XX增加了沟通用户.../XX转办给XX...
            opinion_text = ud["opinion"]
            if "增加了沟通用户" in opinion_text and "沟通意见:" in opinion_text:
                ud["kind"] = "communication"
            elif "转办给" in opinion_text and "转办意见:" in opinion_text:
                ud["kind"] = "transfer"
            else:
                ud["kind"] = "opinion"
            executions.append(ud)
            f = int(ud["freq"] or 0)
            if f > max_freq: max_freq = f
            # 2026-09-09 实证（1330226/3 品质审核）：黄世康驳回的自定义意见
            # 「伟利的型式试验不合格的问题搞定了？」不含"驳回"二字，但
            # useropinionofwfproc.stat=5（09-01 已实证 5=驳回，原客户端状态列即显示"驳回"）。
            # rejected 必须同时认 stat=5，否则自定义措辞的驳回节点不标红。
            if "驳回" in ud["opinion"] or ud["stat"] == "5": rejected = True
            if ud["stat"] == "7" or ud["signtime"]:
                signed_ids.add(ud["userid"])
            if ud["isagent"] == "2":
                owner = next((u for u in users if u.get("agent") == ud["userid"]), None)
                if owner:
                    ud["actual_user"] = ud["username"] or ud["userid"]
                    ud["on_behalf_of"] = owner.get("username") or owner.get("userid") or ""
        for uu in users:
            # 注意：不能把 useropinion 里的沟通记录当成本人签字。
            # 1328577/2 品质会签：唐阳 issigned=1，只有增加沟通用户的 stat=7/freq=2 记录，
            # 原客户端也不将其算作本人完成。
            uu["signed"] = uu.get("issigned") in ("2", "3")
            if uu.get("issigned") == "3":
                agent_exec = next((e for e in executions if e.get("isagent") == "2"
                                   and e.get("on_behalf_of") == (uu.get("username") or uu.get("userid"))), None)
                if agent_exec:
                    uu["signed_by"] = agent_exec.get("username") or agent_exec.get("userid") or ""
                    uu["signed_time"] = agent_exec.get("signtime") or ""

        # 节点是否真正完成：仅当前未完成节点才允许用"全部必签人已签"补判；
        # stat=4 的历史/沟通记录不能把节点标成绿色。
        node_stat = g(chunk, "stat")
        # 2026-09-01 抓包实证（wf_nodeflag_capture / wf_nodeflag_v2，wfid=1330640/procid=2 会签）：
        #   节点级 allperson：1=不勾（任一执行人通过即算节点完成）；2=勾选（节点内所有人都通过才算完成）。
        #     实证：勾选"所有人都通过"后客户端 update 请求 allperson 由 1 变 2。
        #   用户级 ismajor：2=决策人（同一节点有且仅有一个）；1=普通执行人。
        #   用户级 isdecision 只在 select 响应里出现（恒 0），update 请求根本不发送 → 决策人靠 ismajor 写回。
        is_signed = lambda u: u.get("issigned") in ("2", "3")
        decision_users = [u for u in users if u.get("ismajor") == "2"]
        if g(chunk, "allperson") == "2":
            completed = bool(users) and all(is_signed(u) for u in users)
        elif decision_users:
            completed = all(is_signed(u) for u in decision_users)
        else:
            completed = any(is_signed(u) for u in users)
        completed = node_stat == "7" or completed

        # 2026-08-28 实证（1330211/3、4）：历史意见有驳回，但最终一条意见是
        # 同意且节点 stat=7；节点完成态应优先于历史驳回，不能标红。
        if node_stat == "7":
            rejected = False

        # 2026-09-02 用户原版客户端实测（1330699，742 模板）最终结论——推翻 09-01 版：
        #   原版节点"可编辑执行人"判据 = 我是流程启动人(startor) 且 canmodify=2 且 节点未完成(stat∈{1,4,5})。
        #   · operright 与编辑权无关：1330699 前三个业务节点 operright=3 但原版可编辑；
        #     1330640 编制节点 operright=3 置灰，真实原因是 stat=7 已走过（过了可编辑时机）。
        #   · canmodify：开始/结束=0，启动人也不能编辑 → canmodify=2 必要。
        #   · stat：4=当前处理中、5=未完成（待处理）、1=未到达（用户确认也可编辑）、7=已完成、8=终止 → 仅已完成/终止不可编辑。
        already_signed = any(u.get("userid") == login_user and u.get("issigned") in ("2", "3") for u in users)
        nodes.append({
            "procid": g(chunk, "procid"),
            "procname": g(chunk, "procname"),
            "manager": g(chunk, "managername") or g(chunk, "manager"),
            "stat": g(chunk, "stat"),
            "pstatime": g(chunk, "pstatime"),
            "pendtime": g(chunk, "pendtime"),
            "flag": g(chunk, "flag"),
            "preproc": g(chunk, "preproc"),
            "posx": g(chunk, "posx"),
            "posy": g(chunk, "posy"),
            "width": g(chunk, "width"),
            "height": g(chunk, "height"),
            "users": users,
            "exec_count": max_freq,
            "completed": completed,
            "rejected": rejected,
            "executions": executions,
            # 节点"可编辑执行人"权限：判据=启动人 + canmodify=2 + 节点未完成(stat∈{1,4,5})（2026-09-02 用户实测最终版）。
            "canmodify": g(chunk, "canmodify"),
            "operright": g(chunk, "operright"),
            "tempuser": g(chunk, "tempuser"),
            "allperson": g(chunk, "allperson"),
            "minperson": g(chunk, "minperson"),
            "sufprocuserset": g(chunk, "sufprocuserset"),
            "can_add_user": (login_user and (g(xml, "startor") == login_user)) and (g(chunk, "canmodify") == "2") and (g(chunk, "stat") in ("1", "4", "5")),
        })
    objects = []
    for m in re.finditer(r"<wfobjofwf>(.*?)</wfobjofwf>", xml, re.S):
        chunk = m.group(1)
        objects.append({
            "objid": g(chunk, "objid"),
            "objcode": g(chunk, "objcode"),
            "objname": g(chunk, "objname"),
            "objrev": g(chunk, "objrev"),
            "objtype": g(chunk, "objtype"),
            "seq": g(chunk, "seq"),
            # 补充字段：客户端用 objtempname 决定双击弹窗类型，物料类带 itemcode
            "objtempname": g(chunk, "objtempname"),
            "itemcode": g(chunk, "itemcode"),
            "itemname": g(chunk, "itemname"),
            "drawid": g(chunk, "drawid"),
        })
    # ---- 附件页签（2026-09-14 抓包实证 wf_detail_tabs_v1 / wfid 1331289）----
    # 「关联对象」与「附件」是原版客户端工作流执行窗口里两个独立页签，数据源也不同：
    #   wfobjofwfs   → 关联对象（业务对象：ECN/物料/确认单/流程单号…，字段完整）
    #   wfattachofwfs→ 附件（流程附件文档，每行**只有 docid + docname + wfid**）
    # docname 带「附件工作区\」前缀（工作区名可变），显示/落盘都要剥到末段。
    # 缺 rev/大小/检出状态 → 原版客户端切到附件页签时逐条补 cpcdoc select(rev=-1)，
    # 桥接同口径：详情只给这三字段，元数据走 /wf/doc-attrs 懒加载。
    attachments = []
    for idx, m in enumerate(re.finditer(r"<wfattachofwf>([\s\S]*?)</wfattachofwf>", xml), 1):
        chunk = m.group(1)
        raw_name = g(chunk, "docname")
        attachments.append({
            "seq": idx,
            "docid": g(chunk, "docid"),
            "docname": _basename_win(raw_name) or raw_name,
            "rawname": raw_name,
            "wfid": g(chunk, "wfid"),
        })
    # 真实流转连线：proccondofwf 路由表（fromprocid -> toprocid），比 preproc 可靠
    routes = []
    for m in re.finditer(r"<proccondofwf>(.*?)</proccondofwf>", xml, re.S):
        chunk = m.group(1)
        f = re.search(r"<fromprocid>([^<]*)</fromprocid>", chunk)
        t = re.search(r"<toprocid>([^<]*)</toprocid>", chunk)
        if f and t:
            routes.append({"from": f.group(1).strip(), "to": t.group(1).strip()})
    return {"ok": True, "wf_id": wf_id, "main": main, "nodes": nodes,
            "objects": objects, "attachments": attachments, "routes": routes,
            "login_user": login_user,
            # 2026-08-25 抓包实证（wf_break_capture，1329821 startor=唐阳）：中断仅限启动人
            "can_break": bool(login_user and main.get("startor") == login_user)}


@router.get("/tasks/{wf_id}/detail", summary="任务详情（客户端通道 cpcwf.select，含流程图节点/对象）")
async def task_detail_client(
    wf_id: str,
    request: Request,
):
    """复刻客户端「流程图」：cpcwf.select 响应含 wfprocofwfs(节点) + wfobjofwfs(对象)。"""
    zc = _zipxml_client(request)
    xml = await zc.wf_detail(wf_id)
    login_user = get_session("default").user or ""
    return _parse_wf_detail_xml(wf_id, xml, login_user=login_user)


@router.get("/tasks/{wf_id}/submit-node-info", summary="提交/驳回可选目标路径（cpcwfproc.getsubmitnodeinfo，抓包实证 2026-08-26）")
async def task_submit_node_info(
    wf_id: str,
    request: Request,
    procid: str = Query("", description="当前节点 procid；留空自动取 currprocid"),
):
    """复刻原生客户端「通过/驳回弹窗」的数据源（只读）。

    抓包实证（wf_reject_dialog_capture，1330168/16 测试验证）：
      原生客户端点「驳回」时先调 wfman.jsp?classid=cpcwfproc&action=getsubmitnodeinfo，
      请求体 = 当前节点完整字段（cpcwf.select 的 wfprocofwf chunk 剔除 select 特有字段）。
      响应含 <wfnodecollectionofwfproc> 路径集合，每条 = collectiontype/startnode/endnode/nodes
      —— 这就是「沿箭头」可提交（正向 endnode）/可驳回（逆向 startnode）的目标节点。
      之前用 preproc 链 / stat=7 过滤推导都不对，必须用此接口的路径数据。
    """
    zc = _zipxml_client(request)
    sel = await zc.wf_detail(wf_id)
    # 1) 定位当前节点 chunk
    if not procid:
        m = re.search(r"<currprocid>([^<]*)</currprocid>", sel)
        procid = m.group(1) if m else ""
    if not procid:
        raise HTTPException(status_code=400, detail="无法确定当前节点 procid")
    node_xml = None
    for m in re.finditer(r"<wfprocofwf>(.*?)</wfprocofwf>", sel, re.S):
        c = m.group(1)
        if re.search(r"<procid>\s*" + re.escape(procid) + r"\s*</procid>", c):
            node_xml = c
            break
    if node_xml is None:
        raise HTTPException(status_code=404, detail=f"未找到节点 procid={procid} (wfid={wf_id})")
    # 2) 剔除 select 特有字段（同 wf_reject_dynamic 的做法）
    for tag in ("nextstat", "alarmtime", "autosubmittime", "startor", "sourceuserid"):
        node_xml = re.sub(rf"<{tag}>[^<]*</{tag}>", "", node_xml)
    # 3) 调 getsubmitnodeinfo
    try:
        xml = await zc.call("wfman.jsp", "cpcwfproc", "getsubmitnodeinfo", node_xml)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"getsubmitnodeinfo 调用失败: {type(e).__name__}: {e}")
    # 4) 解析路径集合 wfnodecollectionofwfproc
    paths = []
    for m in re.finditer(r"<wfnodecollectionofwfproc>(.*?)</wfnodecollectionofwfproc>", xml, re.S):
        coll = m.group(1)
        def _g(t):
            mm = re.search(rf"<{t}>([^<]*)</{t}>", coll)
            return mm.group(1).strip() if mm else ""
        paths.append({
            "collectiontype": _g("collectiontype"),
            "startnode": _g("startnode"),
            "endnode": _g("endnode"),
            "nodes": [x for x in _g("nodes").split(",") if x],
        })
    # 5) 节点名/状态映射（从 select 响应）
    names = {}
    for m in re.finditer(r"<wfprocofwf>(.*?)</wfprocofwf>", sel, re.S):
        c = m.group(1)
        pid = re.search(r"<procid>([^<]*)</procid>", c)
        nm = re.search(r"<procname>([^<]*)</procname>", c)
        st = re.search(r"<stat>([^<]*)</stat>", c)
        if pid and pid.group(1).strip():
            names[pid.group(1).strip()] = {
                "procname": nm.group(1) if nm else "",
                "stat": st.group(1) if st else "",
            }
    return {"ok": True, "wf_id": wf_id, "procid": procid, "paths": paths,
            "nodes_map": names, "raw_len": len(xml)}


@router.post("/tasks/{wf_id}/communicate", summary="流程沟通（cpcwfproc.communicate，抓包实证 2026-08-25）")
async def wf_communicate(
    wf_id: str,
    request: Request,
    body: dict = Body(...),
):
    """复刻客户端「沟通」：wfman.jsp?classid=cpcwfproc&action=communicate

    抓包实证（wf_communicate_capture）：请求关键参数
      <wfid>…</wfid><procid>…</procid><cmiuserid>接收人</cmiuserid><cmiopinion>意见标志</cmiopinion>
    其余字段保持模板空。响应 = 完整节点信息 + <opinion>发起人增加了沟通用户XX</opinion>。
    """
    procid = str(body.get("procid", ""))
    user = str(body.get("user", ""))
    cmiopinion = str(body.get("opinion", "1"))
    if not procid or not user:
        raise HTTPException(status_code=422, detail="缺少 procid 或 user（接收人）")
    zc = _zipxml_client(request)
    data = (
        f"<wfid>{wf_id}</wfid><procid>{procid}</procid>"
        f"<cmiuserid>{user}</cmiuserid><cmiopinion>{cmiopinion}</cmiopinion>"
        # 其余节点模板字段保持空（客户端同样只传必要参数）
        f"<procname></procname><proctype>0</proctype><stat>0</stat><operright>0</operright>"
        f"<manager></manager><isskip>0</isskip><minperson>0</minperson><allperson>0</allperson>"
        f"<noticeusers></noticeusers><preproc>0</preproc><absent>0</absent><opinion></opinion>"
        f"<astatime></astatime><aendtime></aendtime><pstatime></pstatime><pendtime></pendtime>"
        f"<procuserofwfprocs></procuserofwfprocs><useropinionofwfprocs></useropinionofwfprocs>"
        f"<wfprocnoticeofwfprocs></wfprocnoticeofwfprocs><wfprocsignofwfprocs></wfprocsignofwfprocs>"
    )
    xml = await zc.call("wfman.jsp", "cpcwfproc", "communicate", data)
    # 提取关键回显：opinion（操作记录）
    opinion = re.search(r"<opinion>([^<]*)</opinion>", xml)
    procname = re.search(r"<procname>([^<]*)</procname>", xml)
    return {"ok": True, "wfid": wf_id, "procid": procid, "user": user,
            "opinion": opinion.group(1).strip() if opinion else "",
            "procname": procname.group(1) if procname else ""}


@router.post("/tasks/{wf_id}/transferto", summary="流程转办（cpcwfproc.transferto，抓包实证 2026-08-25）")
async def wf_transferto(
    wf_id: str,
    request: Request,
    body: dict = Body(...),
):
    """复刻客户端「转办」：wfman.jsp?classid=cpcwfproc&action=transferto

    抓包实证（wf_transfer_capture，wfid=1329931 转办给程宇东）：
      <wfid>…</wfid><procid>…</procid><tranuserid>接收人</tranuserid><tranopinion>意见标志</tranopinion>
    其余字段模板空。响应 = 完整节点信息（转办后 select 里：
      procuser 记录 tempuser=2 + sourceuserid=转办人 + note"XX转办给YY"）。
    """
    procid = str(body.get("procid", ""))
    user = str(body.get("user", ""))
    tranopinion = str(body.get("opinion", "1"))
    if not procid or not user:
        raise HTTPException(status_code=422, detail="缺少 procid 或 user（接收人）")
    zc = _zipxml_client(request)
    data = (
        f"<wfid>{wf_id}</wfid><procid>{procid}</procid>"
        f"<tranuserid>{user}</tranuserid><tranopinion>{tranopinion}</tranopinion>"
        # 其余节点模板字段保持空（客户端同样只传必要参数）
        f"<procname></procname><proctype>0</proctype><stat>0</stat><operright>0</operright>"
        f"<manager></manager><isskip>0</isskip><minperson>0</minperson><allperson>0</allperson>"
        f"<noticeusers></noticeusers><preproc>0</preproc><absent>0</absent><opinion></opinion>"
        f"<astatime></astatime><aendtime></aendtime><pstatime></pstatime><pendtime></pendtime>"
        f"<procuserofwfprocs></procuserofwfprocs><useropinionofwfprocs></useropinionofwfprocs>"
        f"<wfprocnoticeofwfprocs></wfprocnoticeofwfprocs><wfprocsignofwfprocs></wfprocsignofwfprocs>"
    )
    xml = await zc.call("wfman.jsp", "cpcwfproc", "transferto", data)
    # 提取关键回显：opinion（操作记录，如"唐阳转办给程宇东"）
    opinion = re.search(r"<opinion>([^<]*)</opinion>", xml)
    procname = re.search(r"<procname>([^<]*)</procname>", xml)
    return {"ok": True, "wfid": wf_id, "procid": procid, "user": user,
            "opinion": opinion.group(1).strip() if opinion else "",
            "procname": procname.group(1) if procname else ""}


@router.post("/tasks/{wf_id}/break", summary="流程中断（cpcwf.break，抓包实证 2026-08-25；仅启动人）")
async def wf_break(
    wf_id: str,
    request: Request,
    body: dict = Body(...),
):
    """复刻客户端「中断」：wfman.jsp?classid=cpcwf&action=break

    抓包实证（wf_break_capture，1329821 项目资料通用流程，startor=唐阳）：
      客户端 break 请求 = 完整 select 的 <data> 原样回传 + 覆盖：
        <breakprocid>4</breakprocid>（中断到节点，实测=归档节点）
        <breakreason>1</breakreason>（中断原因标志）
        <keephistory>2</keephistory>
      响应 note="唐阳在2026-08-25 15:21:30中断"。
    权限：仅 startor（启动人）可中断，前端按 detail.can_break 控制按钮。
    """
    breakprocid = str(body.get("breakprocid", ""))
    breakreason = str(body.get("breakreason", "1"))
    if not breakprocid:
        raise HTTPException(status_code=422, detail="缺少 breakprocid（中断到的节点）")
    zc = _zipxml_client(request)
    # 复刻客户端：先拿完整流程 data（含全部节点），覆盖 break 参数后回传
    xml = await zc.wf_detail(wf_id)
    m = re.search(r"<data[^>]*>([\s\S]*?)</data>", xml, re.S)
    if not m:
        raise HTTPException(status_code=400, detail="无法获取流程 data")
    data = m.group(1)
    # 覆盖/补全 break 参数（客户端实测值：breakprocid=4, breakreason=1, keephistory=2）
    def _set(tag, val):
        nonlocal data
        if re.search(rf"<{tag}>[^<]*</{tag}>", data):
            data = re.sub(rf"<{tag}>[^<]*</{tag}>", f"<{tag}>{val}</{tag}>", data)
        else:
            data = data.rstrip() + f"<{tag}>{val}</{tag}>"
    _set("breakprocid", breakprocid)
    _set("breakreason", breakreason)
    _set("keephistory", "2")
    xml2 = await zc.call("wfman.jsp", "cpcwf", "break", data)
    # 提取响应回显：note（"XX在...中断"）+ stat
    note = re.search(r"<note>([^<]*)</note>", xml2)
    stat = re.search(r"<stat>([^<]*)</stat>", xml2)
    _cats_cache_invalidate()  # 中断终态 → 大类 chip 即时隐藏
    return {"ok": True, "wfid": wf_id, "breakprocid": breakprocid,
            "note": note.group(1).strip() if note else "",
            "stat": stat.group(1) if stat else ""}


@router.get("/objects/{objid}/ecn", summary="更改通知单(ECN)详情（复刻客户端 ecmman cpcecn.select）")
async def object_ecn_detail(
    objid: str,
    request: Request,
):
    """复刻客户端双击单号 →「更改通知单编辑」2 级弹窗的数据源。

    2026-08-20 抓包实证：ecmman.jsp?classid=cpcecn&action=select
    请求体 = 完整 ECN 字段模板（仅 ecnid 有效）。响应 = 26KB XML（145 字段，
    含 ntcode 单号 / creason 变更原因 / cpcecncm 变更明细 / olddrawid~newrev 图纸版本）。
    """
    zc = _zipxml_client(request)
    xml = await zc.ecn_select(objid)
    # 提取 <data> 内部字段 → dict（保留子表 XML 原样）
    inner = _extract_data_inner(xml)
    fields = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", inner))
    # 子表：物料变更行（2026-08-25 抓包实证：容器 = cpcecnitemlineofcpcecns > cpcecnitemlineofcpcecn；
    # 列定义在 cpcecncm.searchdata 的 itemprop：seq/ItemCode/dcname/bcProductModel/BCclas/bcMarketModel/
    # bcCustomModel/bcdrawid/bcmaterial/bcspec/bcitemunit/bcnote/Itemoration/acname/acProductModel/…）
    # 2026-08-25：cpcecnitemlineofcpcecn 两种结构并存（抓包实证）：
    #  - 属性变更对：mpropname/oldvalue/newvalue + itemcode/dcname/acname/itemoration
    #  - BOM 行变更：bcitemcode/bcname/bcdesc + acitemcode/acname/acdesc + supercode + bomoperation
    # 按字段特征分流；含"-"的值视为无内容（过滤空容器）
    cm_rows = []
    bom_rows_from_itemline = []

    def _has(v): return v and str(v).strip() not in ("", "0", "False", "1", "2", "-1")

    for chunk in re.finditer(r"<cpcecnitemlineofcpcecn>(.*?)</cpcecnitemlineofcpcecn>", inner, re.S):
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1)))
        if (d.get("mpropname") or d.get("oldvalue") or d.get("newvalue") or d.get("itemoration")) \
                and (d.get("itemcode") or d.get("ItemCode") or d.get("dcname") or d.get("acname")):
            cm_rows.append(d)
        elif d.get("bcitemcode") or d.get("acitemcode") or d.get("supercode"):
            bom_rows_from_itemline.append(d)
    # BOM 行变更：cpcecnbomline（主）+ cpcecnitemline 里的 BOM 类型
    bom_rows = []
    for chunk in re.finditer(r"<cpcecnbomlineofcpcecn>(.*?)</cpcecnbomlineofcpcecn>", inner, re.S):
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1)))
        if d.get("bcitemcode") or d.get("acitemcode") or d.get("supercode"):
            bom_rows.append(d)
    bom_rows.extend(bom_rows_from_itemline)
    # 兜底：cpcecncm 子表（保留旧逻辑）
    if not cm_rows:
        for chunk in re.finditer(r"<cpcecncm>(.*?)</cpcecncm>", inner, re.S):
            cm_rows.append(dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1))))
    if not cm_rows:
        # 兜底：变更明细平铺在顶层（抓包实证：acitemcode/bcitemcode/oldrev/newrev 等）。
        # 严格过滤：必须有"非零"真值 + 顶层才用（避免 91280 误触发）
        if (_has(fields.get("acitemcode")) or _has(fields.get("bcitemcode"))) and \
                re.search(r"<cpcecnitemlineofcpcecns?>", inner):
            f = fields
            item_row = {}
            if f.get("acitemcode") or f.get("bcitemcode"):
                item_row = {
                    "acitemcode": f.get("acitemcode", ""), "acname": f.get("acname", ""),
                    "acdesc": f.get("acdesc", ""), "acdrawid": f.get("acdrawid", ""),
                    "acunit": f.get("acunit", ""), "acstat": f.get("acstat", ""),
                    "bcitemcode": f.get("bcitemcode", ""), "bcname": f.get("bcname", ""),
                    "bcdesc": f.get("bcdesc", ""), "bcdrawid": f.get("bcdrawid", ""),
                    "bcunit": f.get("bcunit", ""), "bcseq": f.get("bcseq", ""),
                    "bomoperation": f.get("bomoperation", ""), "bomdid": f.get("bomdid", ""),
                    "bcnote": f.get("bcnote", ""), "bomviewaltsu": f.get("bomviewaltsu", ""),
                    "supercode": f.get("supercode", ""), "supername": f.get("supername", ""),
                    "superdesc": f.get("superdesc", ""), "superpurchflag": f.get("superpurchflag", ""),
                    "invvendor": f.get("invvendor", ""),
                }
            if item_row:
                cm_rows.append(item_row)
    # 子表：BOM 行变更（2026-08-25 抓包实证 ECN 91280"更改明细"：cpcecnbomlineofcpcecns 82 行，
    # 含 supercode/supername + bcitemcode/bcqty/bcunit + acitemcode/acqty/acunit + bomoperation 替换）
    bom_rows = []
    for chunk in re.finditer(r"<cpcecnbomlineofcpcecn>(.*?)</cpcecnbomlineofcpcecn>", inner, re.S):
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1)))
        if d.get("bcitemcode") or d.get("acitemcode") or d.get("supercode"):
            bom_rows.append(d)
    # 图纸变更行：优先 cpcecndrawlineofcpcecns 子表（2026-08-25 抓包实证 ECN 91419：
    # 每行含 olddrawname/newdrawname/oldrevlabel/newrevlabel/olddrawdescription/
    # newdrawdescription/drawoperation/newdrawtype/drawclas/reldocid 等）
    draw_rows = []
    for chunk in re.finditer(r"<cpcecndrawlineofcpcecn>(.*?)</cpcecndrawlineofcpcecn>", inner, re.S):
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1)))
        # 过滤真正有内容（排除空/0/False）的行（2026-08-25 91280 抓包发现空容器也被 re 匹配成 1 行）
        def _has(v): return v and str(v).strip() not in ("", "0", "False", "1", "2", "-1")
        meaningful = ["olddrawname", "newdrawname", "olddrawid", "newdrawid", "reldocid",
                      "olddrawdescription", "newdrawdescription", "invvendor"]
        if any(_has(d.get(k)) for k in meaningful):
            draw_rows.append({
                "olddrawid": d.get("olddrawid", ""), "newdrawid": d.get("newdrawid", ""),
                "olddrawname": d.get("olddrawname", ""), "newdrawname": d.get("newdrawname", ""),
                "drawname": d.get("drawname", ""),
                "olddrawname2": d.get("olddrawname2", "") or d.get("olddrawdrawname", ""),
                "newdrawname2": d.get("newdrawname2", ""),
                "oldrev": d.get("oldrev", ""), "newrev": d.get("newrev", ""),
                "oldrevlabel": d.get("oldrevlabel", ""), "newrevlabel": d.get("newrevlabel", ""),
                "olddrawdescription": d.get("olddrawdescription", ""),
                "newdrawdescription": d.get("newdrawdescription", ""),
                "drawoperation": d.get("drawoperation", ""),
                "newdrawtype": d.get("newdrawtype", ""),
                "drawclas": d.get("drawclas", ""),
                "drawitemid": d.get("drawitemid", ""),
                "oldappid": d.get("oldappid", ""), "newappid": d.get("newappid", ""),
                "reldocid": d.get("reldocid", ""),
                "invvendor": d.get("invvendor", ""),
                "newlocked": d.get("newlocked", ""),
                "ecnid": d.get("ecnid", ""),
            })
    if not draw_rows:
        # 兜底：图纸变更平铺在顶层。严格过滤 + 子表存在
        if (_has(fields.get("olddrawname")) or _has(fields.get("newdrawname")) or _has(fields.get("olddrawid"))) and \
                re.search(r"<cpcecndrawlineofcpcecns?>", inner):
            draw_rows.append({
            "olddrawid": fields.get("olddrawid", ""), "newdrawid": fields.get("newdrawid", ""),
            "olddrawname": fields.get("olddrawname", ""), "newdrawname": fields.get("newdrawname", ""),
            "olddrawname2": fields.get("olddrawname2", "") or fields.get("olddrawdrawname", ""),
            "newdrawname2": fields.get("newdrawname2", ""),
            "oldrev": fields.get("oldrev", ""), "newrev": fields.get("newrev", ""),
            "oldrevlabel": fields.get("oldrevlabel", ""), "newrevlabel": fields.get("newrevlabel", ""),
            "olddrawdescription": fields.get("olddrawdescription", ""),
            "newdrawdescription": fields.get("newdrawdescription", ""),
            "newdrawtype": fields.get("newdrawtype", ""), "newappid": fields.get("newappid", ""),
            "reldocid": fields.get("reldocid", ""),
        })
    attachments = _parse_objattachs(inner)
    return {
        "ok": True,
        "ecnid": objid,
        "fields": {k: v for k, v in fields.items() if v and v not in ("0", "1", "2", "-1", "False")},
        "cm_rows": cm_rows,
        "bom_rows": bom_rows,
        "draw_rows": draw_rows,
        "attachments": attachments,
        "raw_len": len(xml),
    }


def _parse_objattachs(inner: str) -> list:
    """解析 <objattachs> 子表（附件列表）。

    2026-08-27 抓包实证：确认单 cpcecnsamconfirm.select 与变更单 cpcecn.select 的响应
    均**直接**携带此子表（无独立附件接口）。无附件的对象返回空 <objattachs/>；
    有附件的对象返回若干 <objattach> 节点。

    每个附件关键字段：
      docid   文档 ID（下载坐标，传给 file.getdownstatus）
      docname 文件名（如 "GCSCH.250715113-1 盐雾检测报告 ... 合格.pdf"）
      objid   附件所属对象 ID
      reftype 6=文档（可下载）
      stat/refid/locked 状态/引用/锁定标记
    """
    atts = []
    block = re.search(r"<objattachs>(.*?)</objattachs>", inner, re.S)
    if not block:
        return atts
    for chunk in re.finditer(r"<objattach>(.*?)</objattach>", block.group(1), re.S):
        a = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1)))
        if a.get("docid"):
            atts.append({
                "docid": a.get("docid", ""),
                "docname": a.get("docname", ""),
                "objid": a.get("objid", ""),
                "reftype": a.get("reftype", ""),
                "stat": a.get("stat", ""),
                "refid": a.get("refid", ""),
                "locked": a.get("locked", ""),
            })
    return atts


@router.get("/objects/{objid}/samconfirm", summary="物料确认单(SamConfirm)详情（复刻客户端 ecmman cpcecnsamconfirm.select）")
async def object_samconfirm_detail(
    objid: str,
    request: Request,
):
    """复刻客户端双击「物料确认单」对象 → 确认单编辑弹窗数据源。

    2026-08-20 抓包实证：ecmman.jsp?classid=cpcecnsamconfirm&action=select
    请求体 = 完整确认单字段模板（仅 samcon_id 有效）。响应 = 主表 70 字段 +
    子表 cpcecnsamconfirmitemsofecnsamconfirms（确认物料项：itemcode/itemname/drawid…）。
    objtype=101 时前端调用此接口。
    """
    zc = _zipxml_client(request)
    xml = await zc.samconfirm_select(objid)
    inner = _extract_data_inner(xml)
    fields = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", inner))
    # 子表：cpcecnsamconfirmitemsofecnsamconfirms（确认物料项）
    item_rows = []
    for chunk in re.finditer(r"<cpcecnsamconfirmitemsofecnsamconfirm>(.*?)</cpcecnsamconfirmitemsofecnsamconfirm>", inner, re.S):
        c = chunk.group(1)
        item_rows.append(dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", c)))
    # 子表：ecnsamconfirmlineofecnsamconfirms（确认行，部分确认单有）
    line_rows = []
    for chunk in re.finditer(r"<ecnsamconfirmlineofecnsamconfirm>(.*?)</ecnsamconfirmlineofecnsamconfirm>", inner, re.S):
        c = chunk.group(1)
        line_rows.append(dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", c)))
    # 子表：objattachs（附件，2026-08-27 抓包实证：确认单 select 响应直接带此子表；
    # 无附件对象返回空 <objattachs/>，有附件对象返回 <objattach> 列表，含 docid/docname/reftype=6）
    attachments = _parse_objattachs(inner)
    return {
        "ok": True,
        "samcon_id": objid,
        "fields": {k: v for k, v in fields.items() if v and v not in ("0", "1", "2", "-1", "False", "0.0")},
        "item_rows": item_rows,
        "line_rows": line_rows,
        "attachments": attachments,
        "raw_len": len(xml),
    }


@router.get("/objects/{objid}/pdtbatchapply", summary="产品投产通知单(PDTBatchApply)详情（复刻客户端 ecmman cpcpdtbatchapply.select）")
async def object_pdtbatchapply_detail(
    objid: str,
    request: Request,
):
    """复刻客户端双击「产品投产通知单」对象 → 投产通知单弹窗数据源。

    2026-08-31 抓包实证（prodlaunch_popup_v1）：ecmman.jsp?classid=cpcpdtbatchapply&action=select
    请求体 = 完整字段模板（仅 applyid 有效，其余留空）。
    响应 = 主表字段 + 子表 pdtbatchapplylineofpdtbatchapplys（投产产品清单，
    实测 applyid=582 / BA2608270001 → 22 行）。

    头字段：applyid / applycode(申请单号) / code(项目号) / projname(项目名称) /
            projphasename(项目阶段) / manager(项目经理) / creator / createtime /
            updator / stat / wfid / wftempid / note / orgname / invorgid …
    子表每行：itemcode(产品编码) / prodform(产品形态) / itemname(中文名称) /
            productmodel(产品型号) / marketmodel(市场型号) / custommodel(客户型号) /
            prodlifecycle(当前生命周期状态) / iscert(是否完成认证) /
            iserpbatch(是否打开ERP) / ishasrep(有无替代产品) / lineid / seq / rev …

    objtype=202204（产品投产通知单/流程单号类）时前端调用此接口；
    弹窗「流程」tab 用响应里的 wfid 再调 /wf/tasks/{wfid}/detail（cpcwf.select 已实现）。
    """
    zc = _zipxml_client(request)
    try:
        xml = await zc.pdtbatchapply_select(objid)
        inner = _extract_data_inner(xml)
        # 头字段：先剔除子表块，避免子表行的同名标签（applyid/stat/wfid/itemcode…）覆盖头字段
        # （dict() 遇重复键取最后一个，不剔除会拿到最后一行产品清单的值）
        head_inner = re.sub(
            r"<pdtbatchapplylineofpdtbatchapplys>.*?</pdtbatchapplylineofpdtbatchapplys>",
            "", inner, flags=re.S)
        head_inner = re.sub(r"<objattachs>.*?</objattachs>", "", head_inner, flags=re.S)
        fields = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", head_inner))
        # 子表：pdtbatchapplylineofpdtbatchapplys（投产产品清单）
        line_rows = []
        for chunk in re.finditer(
            r"<pdtbatchapplylineofpdtbatchapply>(.*?)</pdtbatchapplylineofpdtbatchapply>", inner, re.S
        ):
            line_rows.append(dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1))))
        attachments = _parse_objattachs(inner)
        return {
            "ok": True,
            "applyid": objid,
            "applycode": fields.get("applycode", ""),
            "wfid": fields.get("wfid", ""),
            "fields": {k: v for k, v in fields.items() if v and v not in ("0", "-1", "False", "0.0")},
            "line_rows": line_rows,
            "attachments": attachments,
            "raw_len": len(xml),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"产品投产通知单查询失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


# =========================== 物料属性弹窗（零部件 attr popup） ===========================
# 2026-08-20 抓包实证（客户端「样品确认单」网格双击物料编码 → 属性弹窗，切 tab）：
#   基本      → mdmanager.jsp?classid=cpcitem&action=select (+ cpcmaterialcategory.select / cpcsysunit.search 辅助)
#   工作流程  → publicman.jsp?classid=cpcobjinfo&action=getobjwf (objid=itemid, objtype=28)
#   关联对象  → bomman.jsp?classid=cpcitemrelobj&action=selectitemobjrel (itemid / invorgid=104)
#   技术文档  → docman.jsp?classid=cpcdoc&action=search  body=<itemid>{itemid}</itemid><getcount>0</getcount>
#              （material_doc_tab_capture 抓包实证：技术文档 tab 唯一 doc 调用即此；
#               实测该服务端对 cpcdoc.search 的 itemid 过滤不生效，返回其完整文档集，与本机客户端行为一致）
async def _itemid_by_code(zc: SinoCCZipClient, itemcode: str, invorgid: str = "104"):
    xml = await zc.call("mdmanager.jsp", "cpcitem", "search",
                        f"<itemcode>{itemcode}</itemcode><invorgid>{invorgid}</invorgid><getcount>0</getcount>")
    d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", _extract_data_inner(xml)))
    return d.get("itemid", "0"), d.get("catid", "0")


@router.get("/materials/byid/{itemid}/detail", summary="物料明细（按 itemid，客户端右键明细表 bomman cpcitem.select）")
async def material_detail_by_id(itemid: str, request: Request):
    """2026-08-24 抓包实证：产品工作区右键「打开明细表」= bomman.jsp?classid=cpcitem&action=select，
    请求体精简 <itemid> 即可（实测返回 191KB / 339 字段完整物料主数据）。
    产品工作区物料行自带 item_id，双击弹窗用此接口加载完整明细。"""
    zc = _zipxml_client(request)
    try:
        xml = await zc.item_select(itemid)
        inner = _extract_data_inner(xml)
        fields = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", inner))
        keep = {k: v for k, v in fields.items() if v and v not in ("0", "1", "2", "-1", "False", "0.0")}
        return {"ok": True, "itemid": itemid, "fields": keep, "raw_len": len(xml)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"物料明细查询失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.get("/materials/byid/{itemid}/bomdetail", summary="物料 BOM 明细表（cpcbom.select 完整 81 字段/子件）")
async def material_bomdetail(itemid: str, request: Request, asmid: str = Query("", description="装配件 asmid；=0 表示采购件/无装配（用 itemcode 查）"),
                             invorgid: str = Query("104"),
                             asmrev: str = Query("", description="装配版本（传 0 表示按 BOM 快照展开，对齐客户端双击子件行为；留空则先 cpcitem.select 取父物料 rev）"),
                             itemcode: str = Query("", description="物料编码；asmid=0 时必传")):
    """2026-08-24 实证：明细表窗口数据源 = cpcbom.select（<bomds><bomd> 81 字段/子件，
    比 selectref 完整）。
    - 第一级（打开明细表）：asmrev = 父物料 rev（cpcitem.select 取，抓包=19）
    - 下一级（双击子件展开）：asmrev=0 直接展开，不查 cpcitem.select（抓包实证 764610/650720/650718）
    - 采购件（asmid=0）：不查 rev，直接 asmid=0 + itemcode 查，返回空 bomds（实测 itemcode=801242002193→2773 字节响应 0 个 bomd）
    子件含 materialcatname(研发分类)/eptag(环保标识)/hasrefdoc(包含文件)/lifecycle/
    costrollup(累计成本)/bomddef6-10/saleunit/stockunit/basetime/ctrltime/ecnid 等，
    对齐客户端「导出 Excel」的列。"""
    zc = _zipxml_client(request)
    try:
        code = itemcode
        rev = asmrev
        # 决策树（2026-08-24 抓包实证 prod_dblclick3/4）：
        # - asmid 缺省/0 → 用 itemid（实证：双击物料 asmid=itemid，764607=803342000551 室外电控盒组件）
        # - asmrev=0（双击子件下一级）→ 不查 rev，直接展开（抓包 764610/650720/650718）
        # - asmrev 空（第一级打开明细表）→ 查 cpcitem.select 取父 rev（抓包 743698→rev=19）
        asmid_arg = asmid if (asmid and asmid != "0") else itemid
        if not asmrev and asmid_arg and asmid_arg != "0":
            item_xml = await zc.item_select(asmid_arg)
            item_inner = _extract_data_inner(item_xml)
            rev_m = re.search(r"<rev>([^<]*)</rev>", item_inner)
            rev = rev_m.group(1).strip() if rev_m else ""
            if not code:
                code_m = re.search(r"<itemcode>([^<]*)</itemcode>", item_inner)
                code = code_m.group(1).strip() if code_m else ""
        xml = await zc.bom_select(asmid_arg, rev=rev or "0", itemcode=code)
        master, children = parse_bomd_response(xml)
        return {"ok": True, "itemid": itemid, "asmid": asmid_arg,
                "rev": rev, "master": master, "children": children, "count": len(children)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"BOM 明细查询失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.get("/materials/byid/{itemid}/bomrpt", summary="多层明细（BOM 报表，cpcbomrpt.getbom 一次返回整棵树）")
async def material_bomrpt(itemid: str, request: Request, itemcode: str = Query("", description="物料编码（缺省则查）"),
                          itemrev: str = Query("", description="物料版本（缺省则查）"),
                          maxlayer: str = Query("0", description="最大层数，0=全部")):
    """2026-08-24 抓包实证（prod_multilevel_capture）：客户端"多层明细" = bomman.jsp cpcbomrpt.getbom。
    一次返回整棵 BOM 树（实测 maxlayer=15，579 行 <bomd>，2MB），每行含 wbs/bomlevel 层级字段。
    请求体必须带完整 reffields（wbs,bomlevel,…40 字段）。"""
    zc = _zipxml_client(request)
    try:
        code = itemcode
        rev = itemrev
        if not code or not rev:
            item_xml = await zc.item_select(itemid)
            item_inner = _extract_data_inner(item_xml)
            if not code:
                cm = re.search(r"<itemcode>([^<]*)</itemcode>", item_inner)
                code = cm.group(1).strip() if cm else ""
            if not rev:
                rm = re.search(r"<rev>([^<]*)</rev>", item_inner)
                rev = rm.group(1).strip() if rm else ""
        xml = await zc.bomrpt_getbom(itemid, code, rev, maxlayer)
        inner = _extract_data_inner(xml)
        rows = []
        for b in re.findall(r"<bomd>([\s\S]*?)</bomd>", inner):
            rows.append({k: v for k, v in re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b)})
        maxlayer_m = re.search(r"<maxlayer>([^<]*)</maxlayer>", inner)
        master = {k: v for k, v in re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", inner)
                  if k in ("itemcode", "itemname", "itemid", "itemrev", "maxlayer", "asmcode", "asmid")}
        return {"ok": True, "itemid": itemid, "itemcode": code, "itemrev": rev,
                "maxlayer": (maxlayer_m.group(1) if maxlayer_m else ""), "master": master,
                "count": len(rows), "rows": rows}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"多层明细查询失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


# 2026-08-26 新增：「多层明细 + 流程单」聚合接口
#   一次返回整棵 BOM + 各行物料「流程未结束」的样品确认单 / 更改通知单
#   显示原则：主流程 stat ∈ {"2","3","4","5","6"} 才显示
#     - stat=2/3/5/6: 各阶段进行中（已确认 2026-08-27）
#     - stat=4: 审批/会签中（SC2608140001 实证：stat=4 + endtime=空 + currprocid=3 仍在中间节点）
#                不要按字面"已归档"理解；用户实测纠正 2026-08-27
#     - stat=7: 已通过（TC2605280003 实证：stat=7 + endtime 有值 + currprocid=99999）— 不显示
#     - stat=0: 草稿 / 其他: 不显示（防噪声）
#   性能：每行物料并发取关联对象（asyncio.gather，限制 8 并发），单物料 ~2 次 selectrel
#   上层调用应传 maxlayer="3"~"5" 控制行数；maxlayer=0 可能返回几百行 → 后台并发队列
@router.get("/materials/byid/{itemid}/bomrpt-active-rels", summary="多层明细 + 各行物料的未结束流程单（聚合版）")
async def material_bomrpt_active_rels(itemid: str, request: Request,
                                       itemcode: str = Query("", description="物料编码（缺省则查）"),
                                       itemrev: str = Query("", description="物料版本（缺省则查）"),
                                       maxlayer: str = Query("0", description="最大层数，0=全部"),
                                       months: int = Query(6, description="时间窗（月）：0=不限；6~24 每 3 月递增，缺省 6"),
                                       concurrency: int = Query(8, description="并发度（每行物料关联对象取数并发上限）")):
    """聚合版多层明细：
    1. 调 cpcbomrpt.getbom 一次返回整棵 BOM（复用 material_bomrpt 解析逻辑）
    2. 对每行物料并发调 cpcitemrelobj.selectrel → 取 objtypeid ∈ {101 样品确认单, 91046 更改通知单}
    3. 对每个 objid 调 publicman.jsp cpcobjinfo.getobjwf → 过滤主流程 stat ∈ {"2","3","4","5","6"}
    4. 返回 BOM rows + {itemcode: [{wfid, wfname, stat, objid, objcode, objname, objsubject, objdef1, objdef2, objtypeid, objtype}, ...]}
    """
    import time
    zc = _zipxml_client(request)
    try:
        code = itemcode
        rev = itemrev
        # 1. 缺失 itemcode/rev 时先用 cpcitem.select 补全
        if not code or not rev:
            item_xml = await zc.item_select(itemid)
            item_inner = _extract_data_inner(item_xml)
            if not code:
                cm = re.search(r"<itemcode>([^<]*)</itemcode>", item_inner)
                code = cm.group(1).strip() if cm else ""
            if not rev:
                rm = re.search(r"<rev>([^<]*)</rev>", item_inner)
                rev = rm.group(1).strip() if rm else ""
        t0 = time.time()
        xml = await zc.bomrpt_getbom(itemid, code, rev, maxlayer)
        inner = _extract_data_inner(xml)
        rows = []
        for b in re.findall(r"<bomd>([\s\S]*?)</bomd>", inner):
            rows.append({k: v for k, v in re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b)})
        maxlayer_m = re.search(r"<maxlayer>([^<]*)</maxlayer>", inner)
        master = {k: v for k, v in re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", inner)
                  if k in ("itemcode", "itemname", "itemid", "itemrev", "maxlayer", "asmcode", "asmid")}
        t_bom = time.time() - t0

        # 2. 收集去重的 itemcode → itemid 映射（bomd 行自带 itemid，selectrel 必须传真实 itemid
        #    才能正确过滤关联对象；传 0 会返回全量对象，既慢又错 —— 2026-08-27 实测修正）
        #    注意：根节点（wbs=0 的输入物料）也要查关联对象 —— 用户截图就是根物料的确认单/变更单
        # 2.1 时间窗口过滤（2026-08-27 / 2026-08-27 改可选）：只查询「物料受控时间(ctrltime) 在最近 N 月内」的物料。
        #     ctrltime 来自 getbom 每行 <bomd>（514/579 行有值；样本 2026-04-27 13:40:54）；
        #     受控时间早于 cutoff 的物料不查关联对象（其流程单不展示，自然也不消耗 getobjwf 请求）。
        #     空 ctrltime（字段未维护）→ 不强制过滤、保留，仅计数提示，避免误删历史物料。
        #     months=0 或省略时间窗 → _cutoff=None → 不做任何时间过滤（全部物料查关联对象）。
        #     实测 814042400004 3 层（6 月窗）：skipped_by_ctrltime=335 / empty_ctrltime=36 / skipped_by_objtime=9399。
        _cutoff = _months_ago(months) if months and months > 0 else None
        _skipped_by_ctrltime = 0
        _empty_ctrltime = 0
        _skipped_by_objtime = 0
        _seen = set()
        _rows_meta = []  # (code, itemid, ctrltime_str)
        for r in rows:
            c = (r.get("itemcode") or "").strip()
            i = (r.get("itemid") or "").strip()
            if c and c not in _seen:
                _seen.add(c)
                _rows_meta.append((c, i or "0", r.get("ctrltime", "")))
        codes = {}  # 仅时间窗内受控物料 code -> itemid（其余物料仍展示在 BOM 树，仅流程单列空）
        if _cutoff is None:
            # 不限时间窗：所有物料均查关联对象（空 ctrltime 仍计数但保留）
            for c, i, ct in _rows_meta:
                if _parse_date(ct) is None:
                    _empty_ctrltime += 1
                codes[c] = i
        else:
            for c, i, ct in _rows_meta:
                d = _parse_date(ct)
                if d is None:
                    _empty_ctrltime += 1
                    codes[c] = i
                elif d >= _cutoff:
                    codes[c] = i
                else:
                    _skipped_by_ctrltime += 1
        if not codes:
            return {"ok": True, "itemid": itemid, "itemcode": code, "itemrev": rev,
                    "maxlayer": (maxlayer_m.group(1) if maxlayer_m else ""), "master": master,
                    "count": len(rows), "rows": rows, "active_rels": {},
                    "skipped": {"no_active_wf": 0, "wf_error": 0, "no_wf": 0, "rel_types": 0,
                                "unique_objids": 0, "cutoff": _cutoff.isoformat() if _cutoff else None,
                                "months": months or 0,
                                "skipped_by_ctrltime": _skipped_by_ctrltime,
                                "empty_ctrltime": _empty_ctrltime, "skipped_by_objtime": 0},
                    "perf_ms": {"bom_ms": int(t_bom * 1000), "rels_ms": 0, "total_ms": int(t_bom * 1000)}}

        # 3. 取关联对象（两段式：selectitemobjrel 类型定义全局一致只查 1 次；
        #    selectrel 只查会产生 101/91046 的 relid，请求量减半以上）
        #    2026-08-27 性能修正（对比初版 209s / 第二版 194s）：
        #    - 初版问题：selectrel 传 itemid=0 → 服务端返回全量对象，427 物料 × 8 relid 全部空跑
        #    - 第二版问题：relid 过滤条件过宽（"申请"也匹配）保留了 7 种；且仍传 itemid=0
        #    - 本版：传真实 itemid（来自 BOM 行）+ relid 仅保留「样品确认单/更改通知单/
        #      确认关系废弃单/更改通知单-生产类型转换/更改物料确认单」5 种 + getobjwf 按唯一 objid 查
        sem = asyncio.Semaphore(max(1, concurrency))

        # 3.1 类型定义（一次）
        xml_defs = await zc.call("bomman.jsp", "cpcitemrelobj", "selectitemobjrel",
                                  f"<itemid>{itemid}</itemid><itemcode>{code}</itemcode><invorgid>104</invorgid><reftypeid>0</reftypeid>")
        rel_types = []
        for m in re.finditer(r"<itemrelobjofitemrelobj>(.*?)</itemrelobjofitemrelobj>", xml_defs, re.S):
            d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", m.group(1)))
            rid = d.get("relid", "")
            rname = d.get("reftypename", "")
            # 2026-08-27 实测（802342000014/814042400004）：101 只来自「样品确认单」，
            # 91046 来自「更改通知单 / 更改通知单-生产类型转换」；申请单类是 202201/202202，
            # 「更改物料确认单」实测无数据，均与需求无关 → 只保留 relid ∈ {3 样品确认单, 1 更改通知单, 5 生产类型转换}
            if rid in ("3", "1", "5"):
                rel_types.append({"relid": rid, "reftypename": rname})
        t_defs = time.time() - t0

        async def one_rels(code_one: str, itemid_one: str) -> tuple:
            """取单个物料的 objtypeid ∈ {101, 91046} 关联对象（未过滤 stat）。每个 relid 单独请求。
            必须传真实 itemid（bomd 行自带）；传 0 服务端不按 itemcode 过滤 → 返回全量对象。
            2026-08-27：必须用 sem 限制并发（曾漏掉信号量 → 1708 个请求全并发轰击服务端，
            响应错乱：unique_objids 虚高到 8170、no_active_wf 21556，实际单物料仅 1~2 个对象）。"""
            nonlocal _skipped_by_objtime
            async with sem:
                objs = []
                for rt in rel_types:
                    try:
                        # selectrel 缓存（物料关联对象短期不变；同一产品反复查看秒回）
                        _skey = f"{rt['relid']}:{itemid_one}"
                        _sc = _SELECTREL_CACHE.get(_skey)
                        if _sc is not None and _time.time() - _sc["ts"] < _SELECTREL_TTL:
                            xrel = _sc["xml"]
                        else:
                            xrel = await zc.call("bomman.jsp", "cpcitemrelobj", "selectrel",
                                                 f"<relid>{rt['relid']}</relid><itemid>{itemid_one}</itemid>"
                                                 f"<itemcode>{code_one}</itemcode><invorgid>104</invorgid><reftypeid>0</reftypeid>")
                            _SELECTREL_CACHE[_skey] = {"xml": xrel, "ts": _time.time()}
                        for mm in re.finditer(r"<itemrelobjofitemrelobj>(.*?)</itemrelobjofitemrelobj>", xrel, re.S):
                            f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", mm.group(1)))
                            oid = f.get("objid", "")
                            t = f.get("objtypeid", "")
                            # objtypeid 取值：101=样品确认单, 91046=更改通知单(ECN), 202204=流程单号类
                            # 只保留这两类（用户需求）
                            if t in ("101", "91046") and oid:
                                # 关联对象编制时间(objdef2) 早于 cutoff → 过滤掉（不展示，也不消耗 getobjwf 请求）
                                # 不限时间窗（_cutoff=None）时跳过此过滤
                                if _cutoff is not None:
                                    _od = _parse_date(f.get("objdef2", ""))
                                    if _od is not None and _od < _cutoff:
                                        _skipped_by_objtime += 1
                                        continue
                                objs.append({
                                    "objid": oid, "objcode": f.get("objcode", ""),
                                    "objname": f.get("objname", ""),
                                    "objsubject": f.get("objsubject", ""),
                                    "objdef1": f.get("objdef1", ""),  # 编制人
                                    "objdef2": f.get("objdef2", ""),  # 编制时间
                                    "objtypeid": t,
                                    "objtypename": "样品确认单" if t == "101" else "变更流程单",
                                    "reftypename": rt["reftypename"],
                                })
                    except Exception:
                        pass
                return code_one, objs

        t1 = time.time()
        # 并发拉所有物料的关联对象（code + itemid 成对传入）
        results = await asyncio.gather(*[one_rels(c, i) for c, i in codes.items()])

        # 4. getobjwf 按全局去重 objid 查一次（同一个 ECN/确认单可能被多个物料引用）
        all_objs = []  # [(code, obj_dict), ...]
        for c, objs in results:
            for o in objs:
                all_objs.append((c, o))
        unique_objids = list({o["objid"] for _, o in all_objs})
        objtype_of = {o["objid"]: o["objtypeid"] for _, o in all_objs}  # 同一 objid 的 objtypeid 一致

        async def wf_check(objid: str, objtype: str) -> tuple:
            # 缓存命中直接返回（同一 objid 本请求内去重 + 跨请求复用）
            cached = _objwf_cache_get(objtype, objid)
            if cached is not None:
                w = cached.get("wf")
                if w:
                    return objid, w, None
                return objid, None, "no_wf"  # 哨兵：该对象无流程
            async with sem:
                try:
                    xml_wf = await zc.call("publicman.jsp", "cpcobjinfo", "getobjwf",
                                            f"<objid>{objid}</objid><objtype>{objtype}</objtype>"
                                            f"<wftempid>0</wftempid><codeid>0</codeid><dsid>0</dsid>")
                    wf_rows = []
                    for b in re.finditer(r"<wfofobj[ >](.*?)</wfofobj>", xml_wf, re.S):
                        f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b.group(1)))
                        wf_rows.append({k: f.get(k, "") for k in ("wfid", "wfname", "stat", "startor", "statime", "wftempid", "code")})
                except Exception:
                    _objwf_cache_set(objtype, objid, None)
                    return objid, None, "wf_error"

                # 2026-09-01 抓包实证（objwf_sc2605080014_v1）：getobjwf 返回的是该 obj 挂接的
                # **所有**流程（含确认后触发的下游归档流程，如 SC2605080014 → 1328115 电控归档
                # stat=4 进行中），直接取「第一个 active」会把下游流程误当成该 obj 自己的流程，
                # 导致已审批完成的确认单被错标为「流程中」。
                # 正确做法：若该 obj 挂了多个流程，用 objtype 专属 select 接口取它**自己的源 wfid**，
                #   以该源 wfid 在 wf_rows 里的 stat 作为判据（与客户端做法一致）。
                #   stat 非 active 时上层（1334-1344）会自动跳过 → 不进入 active_rels。
                src = None
                if str(objtype) in ("101", "91046", "202204") and len(wf_rows) > 1:
                    try:
                        if str(objtype) == "101":
                            xml_src = await zc.samconfirm_select(objid)
                        elif str(objtype) == "91046":
                            xml_src = await zc.ecn_select(objid)
                        else:
                            xml_src = await zc.pdtbatchapply_select(objid)
                        cleaned = xml_src
                        for tag in ("objattachs", "cpcecnsamconfirmitemsofecnsamconfirms",
                                    "cpcecnitemlineofcpcecns", "cpcecnbomlineofcpcecns",
                                    "cpcecndrawlineofcpcecns", "pdtbatchapplylineofpdtbatchapplys"):
                            cleaned = re.sub(rf"<{tag}>.*?</{tag}>", "", cleaned, flags=re.S)
                        m = re.search(r"<wfid>(\d+)</wfid>", cleaned)
                        if m and m.group(1).strip() and m.group(1).strip() != "0":
                            src = m.group(1).strip()
                    except Exception:
                        src = None
                active_stats = {"2", "3", "4", "5", "6"}
                if src:
                    src_wf = next((w for w in wf_rows if w.get("wfid") == src), None)
                    if src_wf:
                        # 以「该对象自己的源流程」状态为准（stat 非 active 时上层自动跳过）
                        _objwf_cache_set(objtype, objid, src_wf)
                        return objid, src_wf, None
                active_wf = [w for w in wf_rows if w.get("stat") in active_stats]
                if active_wf:
                    w = active_wf[0]
                    _objwf_cache_set(objtype, objid, w)
                    return objid, w, None
                first = wf_rows[0] if wf_rows else None
                _objwf_cache_set(objtype, objid, first or None)
                return objid, first, ("no_active_wf" if first else "no_wf")

        # 定期清理过期缓存（防内存膨胀：每次请求清理一次，量小）
        _now = _time.time()
        for _k in [k for k, v in _OBJWF_CACHE.items() if _now - v["ts"] > _OBJWF_TTL]:
            _OBJWF_CACHE.pop(_k, None)
        for _k in [k for k, v in _SELECTREL_CACHE.items() if _now - v["ts"] > _SELECTREL_TTL]:
            _SELECTREL_CACHE.pop(_k, None)

        wf_results = await asyncio.gather(*[wf_check(oid, objtype_of[oid]) for oid in unique_objids])

        wf_by_objid = {oid: (wf, err) for oid, wf, err in wf_results}

        active_rels = {c: [] for c in codes}
        skipped = {"no_active_wf": 0, "wf_error": 0, "no_wf": 0, "rel_types": len(rel_types), "unique_objids": len(unique_objids),
                   "cutoff": _cutoff.isoformat() if _cutoff else None, "months": months or 0,
                   "skipped_by_ctrltime": _skipped_by_ctrltime,
                   "empty_ctrltime": _empty_ctrltime, "skipped_by_objtime": _skipped_by_objtime}
        for c, obj in all_objs:
            wf, err = wf_by_objid.get(obj["objid"], (None, "wf_error"))
            if err == "wf_error":
                skipped["wf_error"] += 1
                continue
            if not wf:
                skipped["no_wf"] += 1
                continue
            if not (wf.get("stat") in {"2", "3", "4", "5", "6"}):
                skipped["no_active_wf"] += 1
                continue
            obj["wfid"] = wf.get("wfid", "")
            obj["wfname"] = wf.get("wfname", "")
            obj["wfstat"] = wf.get("stat", "")
            obj["wfstartor"] = wf.get("startor", "")
            obj["wfstatime"] = wf.get("statime", "")
            active_rels[c].append(obj)

        t_rels = time.time() - t1
        t_total = time.time() - t0
        return {
            "ok": True,
            "itemid": itemid, "itemcode": code, "itemrev": rev,
            "maxlayer": (maxlayer_m.group(1) if maxlayer_m else ""),
            "master": master,
            "count": len(rows),
            "rows": rows,
            "active_rels": active_rels,
            "skipped": skipped,
            "perf_ms": {"bom_ms": int(t_bom * 1000), "defs_ms": int((t_defs - t_bom) * 1000),
                        "rels_ms": int(t_rels * 1000), "total_ms": int(t_total * 1000)},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"多层明细+流程单查询失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


# ============ 确认单查询（2026-11-09）：多层明细 + 各文物料的样品确认单（含供应商） ============
# 需求（用户）：输入整机编码 → 多层明细 → 列出各物料的**全部**确认单（不限流程状态），
#   · 确认单两类：TC 开头 = 临时确认，SC 开头 = 正式确认（前端分两列展示，多个确认单同一行）
#   · 确认单明细带供应商字段（vendor = "全称 简称"，例 SC1805310026 深圳市锐钜科技有限公司 深圳锐钜）
#   · 支持时间窗（与「流程查询」同口径：物料受控时间 + 确认单编制时间）
#   · 支持导出 Excel
# 数据链路（均已抓包/实测实证，无猜测字段）：
#   1) cpcbomrpt.getbom            → 整棵 BOM 行（reuse material_bomrpt 解析）
#   2) cpcitemrelobj.selectrel relid=3 → 物料关联的「样品确认单」对象（objtypeid=101，TC/SC 混列）
#      —— 2026-11-09 实测：814042400004 下 802604400080 → SC2604270019；801242002060 → SC2411200012；
#         802500600193 → SC1812180021 / SC2007160025；64149 → TC2608190002。relid=3 = 样品确认单
#      —— 关联对象行只含 objcode / objdef1(编制人) / objdef2(编制时间)，**不含供应商**
#   3) ecmman cpcecnsamconfirm.select(samcon_id) → 主表取 vendor/vendorname（供应商全称+简称）、
#      samcon_type（临时确认/正常确认）、create_time、creator、stat、wfid
# 时间窗口径（months>0 时）：物料 ctrltime ≥ cutoff 才去查关联对象；确认单 objdef2（编制时间）
#   < cutoff 的不返回。months=0 → 两者都不过滤（查全部）。
_SAMCONF_CACHE: dict = {}   # objid -> {"data": {...}, "ts": float}
_SAMCONF_TTL: float = 900.0  # 确认单主表（供应商/类型）几乎不变，缓存 15 分钟


def _samconf_cache_get(objid: str):
    hit = _SAMCONF_CACHE.get(objid)
    if hit is not None and _time.time() - hit["ts"] < _SAMCONF_TTL:
        return hit["data"]
    return None


def _parse_samconfirm_head(xml: str) -> dict:
    """解析确认单 select 响应主表字段（剔除子表块，防子表同名标签覆盖表头）。

    子表（均不含供应商，且会带来几十个同名标签）：cpcecnsamconfirmitemsofecnsamconfirms /
    ecnsamconfirmlineofecnsamconfirms / objattachs。
    """
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
        "item_type": (f.get("item_type") or "").strip(),
        "con_conclusion": (f.get("con_conclusion") or "").strip(),
    }


@router.get("/materials/byid/{itemid}/bomrpt-samconfirms",
            summary="多层明细 + 各物料确认单（TC 临时 / SC 正式，含供应商；只查采购件 P/OP）")
async def material_bomrpt_samconfirms(itemid: str, request: Request,
                                      itemcode: str = Query("", description="物料编码（缺省则查）"),
                                      itemrev: str = Query("", description="物料版本（缺省则查）"),
                                      maxlayer: str = Query("3", description="最大层数，0=全部"),
                                      months: int = Query(6, description="时间窗（月）：0=不限；缺省 6"),
                                      purchflags: str = Query("P,OP", description="只查这些采购标识的物料，逗号分隔；空=不过滤"),
                                      exclude_prefixes: str = Query("P", description="编码以这些前缀开头的物料排除（不查不判定），逗号分隔；空=不排除"),
                                      concurrency: int = Query(10, description="并发度"),
                                      max_objs: int = Query(800, description="最多取多少张确认单详情（防跑飞）")):
    """多层明细 + 每行物料的样品确认单清单（含供应商全称/简称、类型、编制人时间）。

    口径（2026-11-09 与独立查询软件 sinocc-pdm-query 四轮修正后对齐）：
      · 只查**采购件**（purchflag ∈ purchflags，默认 P/OP），编码以 exclude_prefixes（默认 P）开头的排除；
      · **时间窗只按物料受控时间 ctrltime**：窗外物料整行不出现、不计入缺单；窗内物料列出**全部**确认单
        （不按确认单编制时间剪 —— 老物料照样可能刚做过确认，剪了会误判「无确认单」）；
      · 一张 TC/SC 都没有的采购件 = 缺单（前端整行标红）；取数异常的行进 fetch_failed，绝不当成缺单。
    """
    import time
    zc = _zipxml_client(request)
    try:
        code, rev = itemcode, itemrev
        # itemid 传 0（前端只知道编码）→ 先用编码反查 itemid，否则下面 item_select(0)
        # 拿不到根行受控时间，时间窗过滤会失效（801242001527 实测复现过）
        if (not itemid or itemid == "0") and code:
            itemid, _ = await _itemid_by_code(zc, code)
            if not itemid or itemid == "0":
                return {"ok": False, "error": "item_not_found", "itemcode": code}
        # 根物料主档始终取一次：除了 code/rev，还要根行的受控时间（根行 <bomd> 里 ctrltime 恒空，
        # 不回填的话直接查受控很早的物料当根会因「ctrltime 未知」逃过时间窗 —— 801242001527 实测）
        item_inner = _extract_data_inner(await zc.item_select(itemid))
        if not code:
            m = re.search(r"<itemcode>([^<]*)</itemcode>", item_inner)
            code = (m.group(1).strip() if m else "")
        if not rev:
            m = re.search(r"<rev>([^<]*)</rev>", item_inner)
            rev = (m.group(1).strip() if m else "")
        root_ctrltime = ""
        m = re.search(r"<ctrltime>([^<]*)</ctrltime>", item_inner)
        root_ctrltime = (m.group(1).strip() if m else "")

        t0 = time.time()
        bom_inner = _extract_data_inner(await zc.bomrpt_getbom(itemid, code, rev, maxlayer))
        rows = []
        for b in re.findall(r"<bomd>([\s\S]*?)</bomd>", bom_inner):
            rows.append({k: v for k, v in re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b)})
        maxlayer_m = re.search(r"<maxlayer>([^<]*)</maxlayer>", bom_inner)
        master = {k: v for k, v in re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", bom_inner)
                  if k in ("itemcode", "itemname", "itemid", "itemrev", "maxlayer")}
        # 根行 ctrltime 回填（bomd 里没有；只在确认单查询回填，流程查询保持与桥接原口径）
        if root_ctrltime:
            for r in rows:
                if (r.get("itemcode") or "").strip() == code and not (r.get("ctrltime") or "").strip():
                    r["ctrltime"] = root_ctrltime
        t_bom = time.time() - t0

        # 物料去重 + 三级过滤：P 前缀排除 → 采购标识 → 受控时间窗
        cutoff = _months_ago(months) if months and months > 0 else None
        pf_set = {s.strip().upper() for s in (purchflags or "").split(",") if s.strip()}
        prefix_set = tuple(s.strip().upper() for s in (exclude_prefixes or "").split(",") if s.strip())
        seen, metas = set(), []
        for r in rows:
            c = (r.get("itemcode") or "").strip()
            if not c or c in seen:
                continue
            seen.add(c)
            metas.append((c, (r.get("itemid") or "0").strip(), r.get("ctrltime", ""),
                          (r.get("purchflag") or "").strip()))
        targets = {}   # itemcode -> itemid（仅需查关联对象的物料）
        skipped_ctrltime = skipped_empty_ctrltime = skipped_purchflag = skipped_prefix = 0
        for c, i, ct, pf in metas:
            if prefix_set and c.upper().startswith(prefix_set):
                skipped_prefix += 1
                continue
            if pf_set and pf not in pf_set:
                skipped_purchflag += 1
                continue
            d = _parse_date(ct)
            if d is None:
                skipped_empty_ctrltime += 1
                targets[c] = i
            elif cutoff is None or d >= cutoff:
                targets[c] = i
            else:
                skipped_ctrltime += 1
        # 窗外/排除的物料整行不出现（与独立软件同口径）
        rows_out = [r for r in rows if (r.get("itemcode") or "").strip() in targets]

        empty = {"ok": True, "itemid": itemid, "itemcode": code, "itemrev": rev,
                 "maxlayer": (maxlayer_m.group(1) if maxlayer_m else ""), "master": master,
                 "count": len(rows_out), "rows": rows_out, "samconfirms": {},
                 "skipped": {"skipped_by_ctrltime": skipped_ctrltime, "empty_ctrltime": skipped_empty_ctrltime,
                             "skipped_by_purchflag": skipped_purchflag, "skipped_by_prefix": skipped_prefix,
                             "skipped_by_objtime": 0, "unique_objs": 0, "no_vendor": 0, "truncated": 0,
                             "fetch_failed": [],
                             "cutoff": cutoff.isoformat() if cutoff else None, "months": months or 0,
                             "purchflags": sorted(pf_set), "exclude_prefixes": list(prefix_set)},
                 "perf_ms": {"bom_ms": int(t_bom * 1000), "rels_ms": 0, "objs_ms": 0,
                             "total_ms": int(t_bom * 1000)}}
        if not targets:
            return empty

        sem = asyncio.Semaphore(max(1, concurrency))

        # 1) 每个物料取 relid=3（样品确认单）关联对象。
        #    空结果先重试一次再定论（PDM 服务端偶发返回空壳，重试能救回）；
        #    空结果缓存 45s（防偶发空壳被钉死 300s），正常结果 300s。
        #    取数异常不再当「无确认单」→ 记入 fetch_failed，由前端标黄区分。

        def _rel_rows_of(xml: str) -> list:
            out = []
            for mm in re.finditer(r"<itemrelobjofitemrelobj>(.*?)</itemrelobjofitemrelobj>", xml, re.S):
                out.append(dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", mm.group(1))))
            return out

        async def _selectrel_cached(code_one: str, itemid_one: str) -> str:
            _skey = f"3:{itemid_one}"
            _sc = _SELECTREL_CACHE.get(_skey)
            if _sc is not None:
                _ttl = 45.0 if _sc.get("empty") else _SELECTREL_TTL
                if _time.time() - _sc["ts"] < _ttl:
                    return _sc["xml"]
            _req = (f"<relid>3</relid><itemid>{itemid_one}</itemid>"
                    f"<itemcode>{code_one}</itemcode><invorgid>104</invorgid><reftypeid>0</reftypeid>")
            async with sem:
                xrel = await zc.call("bomman.jsp", "cpcitemrelobj", "selectrel", _req)
                if not _rel_rows_of(xrel):   # 偶发空壳：重试一次
                    xrel = await zc.call("bomman.jsp", "cpcitemrelobj", "selectrel", _req)
            _SELECTREL_CACHE[_skey] = {"xml": xrel, "ts": _time.time(),
                                       "empty": not _rel_rows_of(xrel)}
            return xrel

        fetch_failed: list = []

        async def one_item_rel(code_one: str, itemid_one: str):
            try:
                xrel = await _selectrel_cached(code_one, itemid_one)
            except Exception as e:  # noqa: BLE001
                return code_one, None
            out = []
            for f in _rel_rows_of(xrel):
                if f.get("objtypeid") != "101" or not f.get("objid"):
                    continue
                out.append({"objid": f.get("objid", ""),
                            "objcode": (f.get("objcode") or "").strip(),
                            "objdef1": f.get("objdef1", ""),     # 编制人
                            "objdef2": f.get("objdef2", "")})    # 编制时间
            return code_one, out

        t1 = time.time()
        rel_results = await asyncio.gather(*[one_item_rel(c, i) for c, i in targets.items()])

        # 2) 汇总：itemcode -> [确认单基础行]。
        #    ⚠ 归属坑（11-09 事故，SC2411110016）：一张确认单明细可含多个物料，同一 objid 会被
        #    多个物料引用 —— 归属必须**每个引用者都记**，obj_owner 只用于「取详情去重」。
        #    之前用 obj_owner 做归属去重，确认单被判给第一个物料，其余物料凭空「无确认单」。
        by_code: dict = {c: [] for c in targets}
        obj_owner: dict = {}   # objid -> 首次出现的 itemcode（仅用于详情去重）
        for code_one, objs in rel_results:
            if objs is None:          # 取数异常：标记，不当缺单
                fetch_failed.append(code_one)
                continue
            for o in objs:
                if o["objid"] not in obj_owner:
                    obj_owner[o["objid"]] = code_one
                by_code.setdefault(code_one, []).append(o)
        all_objids = list(obj_owner.keys())
        truncated = 0
        if len(all_objids) > max_objs:
            truncated = len(all_objids) - max_objs
            all_objids = all_objids[:max_objs]

        # 3) 并发取确认单详情（供应商/类型）—— 带缓存，二次查询秒回
        no_vendor = 0

        async def one_obj_detail(objid: str):
            nonlocal no_vendor
            cached = _samconf_cache_get(objid)
            if cached is not None:
                return objid, cached
            async with sem:
                try:
                    d = _parse_samconfirm_head(await zc.samconfirm_select(objid))
                except Exception:
                    d = None
            if d is None:
                return objid, None
            if not d.get("vendor"):
                no_vendor += 1
            _SAMCONF_CACHE[objid] = {"data": d, "ts": _time.time()}
            return objid, d

        t2 = time.time()
        details = dict()
        if all_objids:
            for oid, d in await asyncio.gather(*[one_obj_detail(o) for o in all_objids]):
                details[oid] = d

        # 4) 组装：按 TC/SC 分类（编码前缀为准，samcon_type 兜底）
        sam_map: dict = {}
        for code_one, objs in by_code.items():
            lst = []
            for o in objs:
                if o["objid"] not in details:
                    continue
                d = details[o["objid"]] or {}
                con_code = (d.get("samcon_code") or o["objcode"] or "").strip()
                typ = d.get("samcon_type") or ""
                up = con_code[:2].upper()
                kind = "formal" if up == "SC" else ("temp" if up == "TC" else ("temp" if "临时" in typ else "formal"))
                lst.append({
                    "objid": o["objid"],
                    "code": con_code,
                    "kind": kind,                      # temp=临时确认(TC) / formal=正式确认(SC)
                    "type": typ,                       # 客户端口径：临时确认 / 正常确认
                    "vendor": d.get("vendor") or "",   # "全称 简称"，例：深圳市锐钜科技有限公司 深圳锐钜
                    "vendor_id": d.get("vendor_id") or "",
                    "creator": d.get("creator") or o.get("objdef1") or "",
                    "createtime": d.get("create_time") or o.get("objdef2") or "",
                    "stat": d.get("stat") or "",
                    "wfid": d.get("wfid") or "",
                })
            lst.sort(key=lambda x: (x["kind"], x["createtime"]))
            if lst:
                sam_map[code_one] = lst

        _now = _time.time()
        for _k in [k for k, v in _SAMCONF_CACHE.items() if _now - v["ts"] > _SAMCONF_TTL]:
            _SAMCONF_CACHE.pop(_k, None)

        t3 = time.time()
        total_confirms = sum(len(v) for v in sam_map.values())
        return {
            "ok": True,
            "itemid": itemid, "itemcode": code, "itemrev": rev,
            "maxlayer": (maxlayer_m.group(1) if maxlayer_m else ""),
            "master": master,
            "count": len(rows_out),
            "rows": rows_out,
            "samconfirms": sam_map,
            "skipped": {"skipped_by_ctrltime": skipped_ctrltime, "empty_ctrltime": skipped_empty_ctrltime,
                        "skipped_by_purchflag": skipped_purchflag, "skipped_by_prefix": skipped_prefix,
                        "skipped_by_objtime": 0, "unique_objs": len(obj_owner),
                        "no_vendor": no_vendor, "truncated": truncated,
                        "fetch_failed": fetch_failed,
                        "cutoff": cutoff.isoformat() if cutoff else None, "months": months or 0,
                        "purchflags": sorted(pf_set), "exclude_prefixes": list(prefix_set)},
            "perf_ms": {"bom_ms": int(t_bom * 1000), "rels_ms": int((t2 - t1) * 1000),
                        "objs_ms": int((t3 - t2) * 1000), "total_ms": int((t3 - t0) * 1000),
                        "confirms": total_confirms}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"确认单查询失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.get("/materials/{itemcode}/basic", summary="物料基本属性（复刻客户端属性弹窗-基本 tab）")
async def material_basic(itemcode: str, request: Request):
    zc = _zipxml_client(request)
    itemid, catid = await _itemid_by_code(zc, itemcode)
    if not itemid or itemid == "0":
        return {"ok": False, "error": "item_not_found", "itemcode": itemcode}
    sel = await zc.call("mdmanager.jsp", "cpcitem", "select", f"<itemid>{itemid}</itemid>")
    fields = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", _extract_data_inner(sel)))
    # 补充分类名称
    if catid and catid != "0":
        try:
            cat = await zc.call("mdmanager.jsp", "cpcmaterialcategory", "select", f"<catid>{catid}</catid>")
            catf = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", _extract_data_inner(cat)))
            fields["catname"] = catf.get("catname", "")
        except Exception:
            pass
    return {
        "ok": True,
        "itemcode": itemcode,
        "itemid": itemid,
        "fields": {k: v for k, v in fields.items() if v and v not in ("0", "1", "2", "-1", "False", "0.0")},
        "raw_len": len(sel),
    }


@router.get("/materials/{itemcode}/workflow", summary="物料工作流程（复刻客户端属性弹窗-工作流程 tab）")
async def material_workflow(itemcode: str, request: Request):
    zc = _zipxml_client(request)
    itemid, _ = await _itemid_by_code(zc, itemcode)
    if not itemid or itemid == "0":
        return {"ok": False, "error": "item_not_found", "itemcode": itemcode}
    xml = await zc.call("publicman.jsp", "cpcobjinfo", "getobjwf",
                        f"<objid>{itemid}</objid><objtype>28</objtype><wftempid>0</wftempid><codeid>0</codeid><dsid>0</dsid>")
    # 注意：原生 cpcobjinfo.getobjwf 返回的子表是 <wfofobjs><wfofobj>，不是 cpcobjwf。
    wf_rows = []
    for chunk in re.finditer(r"<wfofobj>(.*?)</wfofobj>", xml, re.S):
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", chunk.group(1)))
        wf_rows.append({
            "wfid": d.get("wfid", ""),
            "wfname": d.get("wfname", ""),
            "stat": d.get("stat", ""),
            "startor": d.get("startor", ""),
            "statime": d.get("statime", ""),
            "subject": d.get("subject", ""),
            "objid": d.get("objid", ""),
            "objtype": d.get("objtype", ""),
            "currprocid": d.get("currprocid", ""),
            "wftempid": d.get("wftempid", ""),
        })
    return {"ok": True, "itemcode": itemcode, "itemid": itemid, "wf_rows": wf_rows, "raw_len": len(xml)}


@router.get("/materials/{itemcode}/related", summary="物料关联对象（复刻客户端属性弹窗-关联对象 tab）")
async def material_related(itemcode: str, request: Request, invorgid: str = "104"):
    zc = _zipxml_client(request)
    itemid, _ = await _itemid_by_code(zc, itemcode)
    if not itemid or itemid == "0":
        return {"ok": False, "error": "item_not_found", "itemcode": itemcode}
    # 2026-08-24 抓包实证（mat_tabs_capture + live 验证）：
    #   关联对象 tab 两段式：
    #   1) cpcitemrelobj.selectitemobjrel → 返回关联类型定义（8 种：样品确认单/更改通知单/
    #      确认关系废弃单/生产类型转换/更改物料确认单/产品退市恢复/产品型号变更/产品型号申请），
    #      每种含 relid + reftypename + querysql(SQL 模板，非数据)
    #   2) 对每种 relid 调 cpcitemrelobj.selectrel → 返回该类型实际关联对象
    #      （每条含 objid/objcode/objname/objsubject/objdef1=编制人/objdef2=编制时间/objtypeid）
    xml_defs = await zc.call("bomman.jsp", "cpcitemrelobj", "selectitemobjrel",
                             f"<itemid>{itemid}</itemid><itemcode>{itemcode}</itemcode><invorgid>{invorgid}</invorgid><reftypeid>0</reftypeid>")
    rel_types = []
    for m in re.finditer(r"<itemrelobjofitemrelobj>(.*?)</itemrelobjofitemrelobj>", xml_defs, re.S):
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", m.group(1)))
        relid = d.get("relid", "")
        if relid and relid != "0":
            rel_types.append({"relid": relid, "reftypename": d.get("reftypename", ""),
                              "objfielddef": d.get("objfielddef", "")})
    groups = []
    all_rows = []
    for rt in rel_types:
        try:
            xml = await zc.call("bomman.jsp", "cpcitemrelobj", "selectrel",
                                f"<relid>{rt['relid']}</relid><itemid>{itemid}</itemid>"
                                f"<itemcode>{itemcode}</itemcode><invorgid>{invorgid}</invorgid><reftypeid>0</reftypeid>")
            rows = []
            for mm in re.finditer(r"<itemrelobjofitemrelobj>(.*?)</itemrelobjofitemrelobj>", xml, re.S):
                rows.append(dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", mm.group(1))))
            all_rows.extend(rows)
            groups.append({"relid": rt["relid"], "reftypename": rt["reftypename"], "rows": rows, "count": len(rows)})
        except Exception as e:
            groups.append({"relid": rt["relid"], "reftypename": rt["reftypename"], "rows": [], "count": 0, "error": str(e)})
    return {"ok": True, "itemcode": itemcode, "itemid": itemid,
            "rel_types": rel_types, "groups": groups,
            "rel_rows": all_rows, "total": len(all_rows), "raw_len": len(xml_defs)}


@router.get("/materials/{itemcode}/docs", summary="物料技术文档（复刻客户端属性弹窗-技术文档 tab）")
async def material_docs(itemcode: str, request: Request):
    zc = _zipxml_client(request)
    itemid, _ = await _itemid_by_code(zc, itemcode)
    if not itemid or itemid == "0":
        return {"ok": False, "error": "item_not_found", "itemcode": itemcode}
    # 2026-08-24 抓包实证（prod_doc_drawid2）：技术文档清单 = cpcitem.select 响应的
    # <shortcutofitems><shortcutofitem>（reftype=6 文档；refid=docid, scname=文档名, refrev=版本）。
    # 验证：743696→3 个（6298780/6301214/6337630 对应截图 3 文档），743698→4 个且含 6337580
    # （= 用户点击文档时 cpcdoc.select 的 docid）。
    # 客户端切 tab 不发清单请求（数据已随 cpcitem.select 返回），点击文档才 cpcdoc.select + getdownstatus。
    sel = await zc.call("mdmanager.jsp", "cpcitem", "select", f"<itemid>{itemid}</itemid>")
    inner = _extract_data_inner(sel)
    docs = []
    scm = re.search(r"<shortcutofitems>([\s\S]*?)</shortcutofitems>", inner, re.S)
    if scm:
        for b in re.findall(r"<shortcutofitem[ >]([\s\S]*?)</shortcutofitem>", scm.group(1)):
            f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b))
            if f.get("reftype") == "6" and f.get("refid"):
                docs.append({
                    "docid": f.get("refid", ""),
                    "docname": f.get("scname", ""),
                    "rev": f.get("refrev", ""),
                    "creator": f.get("creator", ""),
                    "createtime": f.get("createtime", ""),
                    "updatetime": f.get("updatetime", ""),
                    "statname": f.get("suppname", ""),
                })
    drawid = (dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", inner)).get("drawid") or "")
    return {
        "ok": True,
        "itemcode": itemcode,
        "itemid": itemid,
        "drawid": drawid,
        "note": "" if docs else "该物料暂无技术文档（shortcutofitems 为空）。",
        "doc_rows": docs,
        "doc_total": len(docs),
        "raw_len": len(sel),
    }


class FlatPartsExportRequest(BaseModel):
    codes: List[str] = Field(default_factory=list, description="已筛选出的平面件物料编码")


async def _latest_doc_download_rev(zc: SinoCCZipClient, docid: str) -> str:
    """cpcdoc.select(rev=-1) 返回最新版内部版本号；下载必须显式传该版本。

    2026-09-09：老单版本文档（2020/2022 批导入）select(rev=-1) 服务端报 60002
    「文件不存在」→ getdownstatus(-1) 同样 60002 → /wf/objects/{id}/open 404。
    实证显式 rev=1 可取到下载坐标，故此处回退探测 rev=1。"""
    async def _select(rev: str):
        sel = await zc.call("docman.jsp", "cpcdoc", "select",
                            f"<docid>{docid}</docid><rev>{rev}</rev>")
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>",
                            _extract_data_inner(sel)))
        return d, sel
    try:
        d, sel = await _select("-1")
        if "<error>" in sel:
            d1, sel1 = await _select("1")
            if "<error>" not in sel1:
                return "1"
            return "-1"
        rev = d.get("rev", "").strip()
        return rev if rev and rev not in ("0", "-1") else "-1"
    except Exception:
        return "-1"


def _safe_zip_name(name: str) -> str:
    """ZIP 内部文件名限制：去掉路径和 Windows 不允许的字符。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    return (name[:150].strip(" ._")) or "document"


@router.post("/materials/flat-parts/export-zip", summary="一键打包下载平面件技术文档")
async def export_flat_parts_zip(payload: FlatPartsExportRequest, request: Request):
    """对前端筛选出的铭牌/接线铭牌/机型标/说明书逐个查技术文档并打包。

    单个图纸失败不阻断整包；全部失败时才返回错误。同一 docid 会去重，
    避免相同部件在多层 BOM 中重复出现时重复下载。
    """
    codes = list(dict.fromkeys([str(c).strip() for c in payload.codes if str(c).strip()]))
    if not codes:
        raise HTTPException(status_code=400, detail="未提供平面件物料编码")

    zc = _zipxml_client(request)
    zip_buffer = io.BytesIO()
    added_names: set[str] = set()
    seen_docids: set[str] = set()
    total_docs = 0
    failed_docs = 0
    failed_materials = 0
    failures: List[str] = []

    try:
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for code in codes:
                try:
                    itemid, _ = await _itemid_by_code(zc, code)
                except Exception:
                    failures.append(f"{code}: 物料查询失败")
                    failed_materials += 1
                    continue
                if not itemid or itemid == "0":
                    failures.append(f"{code}: 未找到物料")
                    failed_materials += 1
                    continue

                try:
                    sel = await zc.call("mdmanager.jsp", "cpcitem", "select", f"<itemid>{itemid}</itemid>")
                except Exception as e:
                    failures.append(f"{code}: 技术文档清单查询失败")
                    failed_materials += 1
                    continue

                inner = _extract_data_inner(sel)
                scm = re.search(r"<shortcutofitems>([\s\S]*?)</shortcutofitems>", inner, re.S)
                docs = []
                if scm:
                    for b in re.findall(r"<shortcutofitem[ >]([\s\S]*?)</shortcutofitem>", scm.group(1)):
                        f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b))
                        if f.get("reftype") == "6" and f.get("refid"):
                            docs.append((f.get("refid", ""), f.get("scname", "")))

                for docid, docname in docs:
                    total_docs += 1
                    if not docid or docid in seen_docids:
                        continue
                    try:
                        download_rev = await _latest_doc_download_rev(zc, docid)
                        xml = await zc.file_getdownstatus(docid, rev=download_rev)
                        info = _parse_getdownstatus(xml)
                        if not info.get("url") or not info.get("tmpfilename"):
                            raise ValueError("未找到下载坐标")
                        filesize = int(info.get("filesize") or 0)
                        packagesize = filesize if filesize > 0 else 52428800
                        data = await zc.file_download(
                            info["url"], info["tmpfilename"], zc.login_uid, packagesize=packagesize
                        )
                        content = decode_file_body(data, expected_size=filesize)
                        if not content:
                            raise ValueError("下载后解包为空")

                        # 原客户端直接使用 CPCDOC.DOCNAME；内容推断只用于
                        # 服务端没有记录扩展名的异常数据，不覆盖正常扩展名。
                        base = (info.get("docname") or docname or docid).strip() or docid
                        base_root, base_ext = os.path.splitext(base)
                        if not base_ext:
                            detected = _guess_ext(content)
                            base = base_root + detected

                        entry_name = _safe_zip_name(f"{code}_{base}")
                        n = 1
                        while entry_name.lower() in added_names:
                            stem, ext2 = os.path.splitext(entry_name)
                            entry_name = f"{stem}({n}){ext2}"
                            n += 1
                        zf.writestr(entry_name, content)
                        added_names.add(entry_name.lower())
                        seen_docids.add(docid)
                    except Exception as e:
                        failed_docs += 1
                        failures.append(f"{docname or docid}: {type(e).__name__}")
                        continue

        if not added_names:
            detail = "; ".join(failures[:8]) or "所选平面件没有可下载的技术文档"
            raise HTTPException(status_code=404, detail=detail)

        zip_buffer.seek(0)
        fname = "flat-parts-documents.zip"
        return Response(
            content=zip_buffer.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{fname}"',
                "X-Flat-Parts-Docs": str(total_docs),
                "X-Flat-Parts-Failed": str(failed_docs),
                "X-Flat-Parts-Materials-Failed": str(failed_materials),
            },
        )
    finally:
        await zc.close()


@router.get("/docs/{docid}/attr", summary="文档属性（cpcdoc.select；技术文档 tab 右键属性）")
async def doc_attr(docid: str, request: Request, rev: str = Query("-1", description="文档版本，-1=最新")):
    """2026-08-24 抓包实证（prod_doc_attr_capture / prod_doc_drawid2）：
    技术文档行右键「属性」→ docman.jsp cpcdoc.select(docid, rev)。
    精简请求 <docid>+<rev> 实测返回 111 个非空字段（docname/code/rev/revlabel/creator/
    createtime/publishor/publishtime/plmstate/locname/idpath/drawid/exptime 等）。"""
    zc = _zipxml_client(request)
    try:
        xml = await zc.call("docman.jsp", "cpcdoc", "select", f"<docid>{docid}</docid><rev>{rev}</rev>")
        fields = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", _extract_data_inner(xml)))
        return {"ok": True, "docid": docid, "rev": rev,
                "fields": {k: v for k, v in fields.items() if v and v not in ("0", "-1", "False", "0.0")},
                "raw_len": len(xml)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"文档属性查询失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


# 2026-09-14 抓包实证（wf_detail_tabs_v1 / wfid 1331289）：
# 原版客户端切到流程「附件」页签时，对每条附件**串行**发一次
# `docman cpcdoc.select(docid, rev=-1)` 补元数据（间隔 0.3~0.5s，条数=附件数）。
# 桥接复刻同语义但并发合并成一个请求，避免前端 N 次往返。
# 注意：客户端请求体有「复用上一条响应」的错位怪癖（docname/idpath/creator 填上一条的值），
# 服务端只认 docid 所以能跑；桥接**不要模仿**，发干净探针。
_DOC_ATTR_KEEP = (
    "docname", "rev", "revlabel", "locked", "userid", "checktime",
    "creator", "createtime", "updator", "updatetime", "filetime",
    "oldsize", "zipsize", "oldcrc32", "crc32", "idpath", "locname",
    "doctype", "code", "stat", "actived",
)


@router.get("/doc-attrs", summary="批量文档元数据（cpcdoc.select rev=-1；流程「附件」页签懒加载）")
async def doc_attrs(request: Request, docids: str = Query(..., description="逗号分隔的 docid 列表")):
    """批量补文档元数据（rev/revlabel/locked/creator/大小/检出人）。

    用于流程详情「附件」页签：wfattachofwfs 每行只有 docid+docname，缺版本/检出状态，
    与原版客户端一致地按需补全（进页签才发）。
    """
    ids = []
    for x in (docids or "").split(","):
        x = x.strip()
        if x and x not in ids:
            ids.append(x)
    if not ids:
        return {"ok": True, "attrs": {}}
    zc = _zipxml_client(request)
    sem = asyncio.Semaphore(4)

    async def _one(docid: str):
        async with sem:
            try:
                xml = await zc.call("docman.jsp", "cpcdoc", "select",
                                    f"<docid>{docid}</docid><rev>-1</rev>")
                f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", _extract_data_inner(xml)))
                return docid, {k: f.get(k, "") for k in _DOC_ATTR_KEEP}
            except Exception as e:
                return docid, {"error": f"{type(e).__name__}: {e}"}

    try:
        results = await asyncio.gather(*[_one(i) for i in ids])
        return {"ok": True, "attrs": {d: v for d, v in results}}
    finally:
        await zc.close()


# 2026-09-02：文档「流程中」标识批量查询缓存（docid -> {in_wf, wfid, wfname, stat, ts}）。
# 与 _OBJWF_CACHE 同思路：同一文件夹反复展开/刷新时避免重复调服务端。
_DOC_WF_CACHE: dict = {}
_DOC_WF_TTL: float = 300.0


@router.get("/docs/wf-status", summary="批量查询文档流程状态（文档列表「正在走流程」标识）")
async def docs_wf_status(request: Request, docids: str = Query(..., description="逗号分隔的 docid 列表")):
    """2026-09-02 新增：文档列表「正在走流程」标识。
    对每个 docid 调 publicman.jsp cpcobjinfo.getobjwf(objid=docid, objtype=6)，
    解析 <wfofobj> 全列表，只要存在 stat∈{2,3,4,5,6}（进行中）的流程即 in_wf=true。
    内部 5 并发 + TTL 300s 缓存，避免 N 个文档串行拉取拖慢列表渲染。"""
    ids = [x.strip() for x in (docids or "").split(",") if x.strip()]
    if not ids:
        return {"ok": True, "status": {}}
    zc = _zipxml_client(request)
    now = _time.time()
    out: dict = {}
    pending = []
    for i in ids:
        hit = _DOC_WF_CACHE.get(i)
        if hit and now - hit["ts"] < _DOC_WF_TTL:
            out[i] = {k: hit[k] for k in ("in_wf", "wfid", "wfname", "stat")}
        else:
            pending.append(i)
    if pending:
        sem = asyncio.Semaphore(5)

        async def _one(docid: str):
            async with sem:
                try:
                    xml = await zc.call("publicman.jsp", "cpcobjinfo", "getobjwf",
                                        f"<objid>{docid}</objid><objtype>6</objtype>"
                                        f"<wftempid>0</wftempid><codeid>0</codeid><dsid>0</dsid>")
                    rows = []
                    for b in re.finditer(r"<wfofobj[ >]([\s\S]*?)</wfofobj>", xml, re.S):
                        f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b.group(1)))
                        rows.append({k: f.get(k, "") for k in ("wfid", "wfname", "stat", "startor", "statime", "wftempid", "code")})
                    active = [r for r in rows if r["stat"] in ("2", "3", "4", "5", "6")]
                    info = {
                        "in_wf": bool(active),
                        "wfid": active[0]["wfid"] if active else "",
                        "wfname": active[0]["wfname"] if active else "",
                        "stat": active[0]["stat"] if active else "",
                    }
                except Exception as e:
                    info = {"in_wf": False, "wfid": "", "wfname": "", "stat": "", "error": f"{type(e).__name__}: {e}"}
                _DOC_WF_CACHE[docid] = {**info, "ts": _time.time()}
                return docid, info
        results = await asyncio.gather(*[_one(i) for i in pending])
        for docid, info in results:
            out[docid] = {k: v for k, v in info.items() if k != "error"}
    await zc.close()
    return {"ok": True, "status": out}


@router.get("/docs/{docid}/wf", summary="文档所属流程（cpcobjinfo.getobjwf objtype=6）")
async def doc_workflow(docid: str, request: Request):
    """2026-08-24 抓包实证（prod_doc_attr_capture）：文档属性弹窗「工作流程」tab
    = publicman.jsp cpcobjinfo.getobjwf(objid=docid, objtype=6)。响应 <wfofobj>。"""
    zc = _zipxml_client(request)
    try:
        xml = await zc.call("publicman.jsp", "cpcobjinfo", "getobjwf",
                            f"<objid>{docid}</objid><objtype>6</objtype>"
                            f"<wftempid>0</wftempid><codeid>0</codeid><dsid>0</dsid>")
        wf_rows = []
        for b in re.finditer(r"<wfofobj[ >]([\s\S]*?)</wfofobj>", xml, re.S):
            f = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", b.group(1)))
            wf_rows.append({k: f.get(k, "") for k in ("wfid", "wfname", "stat", "startor", "statime", "wftempid", "code")})
        return {"ok": True, "docid": docid, "wf_rows": wf_rows, "raw_len": len(xml)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"文档流程查询失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.get("/objects/{objid}/wf", summary="对象所属流程（复刻客户端 cpcobjinfo.getobjwf，用于流程单号类对象跳转）")
async def object_workflow(objid: str, request: Request, objtype: str = "",
                          include_all: str = Query("0", description="1=额外返回 getobjwf 的全部挂接流程（诊断/多流程选择）")):
    """对象所属流程（前端双击关联对象的「流程」列时调用）。

    **2026-09-01 抓包实证（objwf_sc2605080014_v1）**：客户端打开关联对象的流程时，
    **完全不用** publicman cpcobjinfo getobjwf，而是按 objtype 调该类型的专属 select 接口，
    从响应里取 <wfid>：
      - objtype=101（确认单）   → ecmman cpcecnsamconfirm select(samcon_id)
      - objtype=91046（ECN）    → ecmman cpcecn select(ecnid)
      - objtype=202204（投产单）→ ecmman cpcpdtbatchapply select(applyid)
    实测反例（SC2605080014）：getobjwf 取第一个 = 1328115 电控归档（**错**，那是确认后
    触发的归档流程）；cpcecnsamconfirm select 取 = **1321320 确认单审批**（对，与客户端一致）。
    getobjwf 仅作其他 objtype 的兜底（返回该 obj 挂接的所有流程，按 wfid 倒序）。
    """
    zc = _zipxml_client(request)

    async def _getobjwf_wfs() -> list:
        """getobjwf：该 obj 挂接的所有流程（wfid 倒序）。仅作兜底/诊断用。"""
        try:
            xml = await zc.call("publicman.jsp", "cpcobjinfo", "getobjwf",
                                f"<objid>{objid}</objid><objtype>{objtype or '0'}</objtype>"
                                f"<wftempid>0</wftempid><codeid>0</codeid><dsid>0</dsid>")
        except Exception:
            return []
        out = []
        for b in re.findall(r"<wfofobj>(.*?)</wfofobj>", xml, re.S):
            def _g(t):
                m = re.search(rf"<{t}>([^<]*)</{t}>", b)
                return m.group(1).strip() if m else ""
            out.append({"wfid": _g("wfid"), "wfname": _g("wfname"), "stat": _g("stat"),
                        "startor": _g("startor"), "statime": _g("statime"), "wftempid": _g("wftempid")})
        return out

    # 1) 按 objtype 调专属 select 接口取「该对象自己的」wfid（与客户端一致）
    if str(objtype) in ("101", "91046", "202204"):
        try:
            if str(objtype) == "101":
                xml = await zc.samconfirm_select(objid)
            elif str(objtype) == "91046":
                xml = await zc.ecn_select(objid)
            else:
                xml = await zc.pdtbatchapply_select(objid)
        except Exception:
            xml = ""
        if xml:
            # 剔除子表块后再取 wfid（防子表同名标签覆盖；与 pdtbatchapply 解析同款防御）
            cleaned = xml
            for tag in ("objattachs", "cpcecnsamconfirmitemsofecnsamconfirms",
                        "cpcecnitemlineofcpcecns", "cpcecnbomlineofcpcecns",
                        "cpcecndrawlineofcpcecns", "pdtbatchapplylineofpdtbatchapplys"):
                cleaned = re.sub(rf"<{tag}>.*?</{tag}>", "", cleaned, flags=re.S)
            m = re.search(r"<wfid>(\d+)</wfid>", cleaned)
            if m and m.group(1).strip() and m.group(1).strip() != "0":
                resp = {"ok": True, "objid": objid, "objtype": objtype,
                        "wfid": m.group(1).strip(), "wfname": "", "source": "objtype_select"}
                if include_all == "1":
                    resp["all_wfs"] = await _getobjwf_wfs()
                return resp

    # 2) 兜底：getobjwf（其他 objtype，或专属接口没取到 wfid）
    all_wfs = await _getobjwf_wfs()
    first = next((w for w in all_wfs if w["wfid"]), None)
    if not first:
        return {"ok": False, "objid": objid, "objtype": objtype, "error": "no_wf"}
    resp = {"ok": True, "objid": objid, "objtype": objtype,
            "wfid": first["wfid"], "wfname": first["wfname"], "source": "getobjwf"}
    if include_all == "1":
        resp["all_wfs"] = all_wfs
    return resp


@router.get("/objects/{objid}/open", summary="打开/下载关联对象文件（复刻客户端 fileman 协议）")
async def open_object_file(
    objid: str,
    request: Request,
    objname: str = Query("", description="前端传入的对象名，仅作文件名兜底"),
    rev: str = Query("", description="显式内部版本号（如 ECN 更改前图纸的历史版）；留空=最新版"),
):
    """复刻客户端「附件打开」：

    1. fileman.jsp getdownstatus(docid=objid) → 取得文件服务器 URL + tmpfilename + docname
    2. GET 文件服务器(如 http://10.240.28.24:8011/) 下载真实文件字节
    3. 解包(03 01 00 头 + ZIP/zlib) → 返回真实文档供浏览器下载

    2026-09-10：ECN 更改图纸前/后行是同一 docid（oldrev/newrev 为升版前后版本号），
    此前恒取最新版导致「更改前/后打开都是旧版」。现支持显式 rev：
    ECN 行链接带各自 rev。0910 ecn_draw_open_v1 抓包实证：
    8/8 行「更改后」草稿版全部已在 vault（{docid}_{newrev}.cpc），
    getdownstatus(显式 rev) 可取任意已存在版本（含未激活草稿版）；
    select(rev=-1) 只报已激活版。显式 rev 无坐标时 404 明确报错（不静默回退旧版）。
    """
    zc = _zipxml_client(request)
    try:
        explicit_rev = rev.strip() if (rev and rev.strip().isdigit() and int(rev.strip()) > 0) else ""
        info = {}
        if explicit_rev:
            xml = await zc.file_getdownstatus(objid, rev=explicit_rev)
            info = _parse_getdownstatus(xml)
            if not info.get("url") or not info.get("tmpfilename"):
                latest_rev = ""
                try:
                    latest_rev = await _latest_doc_download_rev(zc, objid)
                except Exception:
                    pass
                raise HTTPException(
                    status_code=404,
                    detail=(f"版本 rev={explicit_rev} 在 vault 中不存在（更改后版本尚未生效/文件未上传），"
                            f"当前最新版为 rev={latest_rev or '?'}。docid={objid}"),
                )
        else:
            download_rev = await _latest_doc_download_rev(zc, objid)
            xml = await zc.file_getdownstatus(objid, rev=download_rev)
            info = _parse_getdownstatus(xml)
        if not info.get("url") or not info.get("tmpfilename"):
            raise HTTPException(
                status_code=404,
                detail=f"未找到下载坐标（objid={objid} 可能不是文档，或 getdownstatus 无结果）: {xml[:300]}",
            )
        # 2026-08-20 修正：必须传 packagesize 覆盖完整文件，否则大文件只下首包损坏
        filesize = int(info.get("filesize") or 0)
        packagesize = filesize if filesize > 0 else 52428800
        data = await zc.file_download(info["url"], info["tmpfilename"], zc.login_uid, packagesize=packagesize)
        content = decode_file_body(data, expected_size=filesize)
        if not content:
            raise HTTPException(status_code=502, detail="下载成功但解包后得到空文件")
        # 2026-09-14：前端行内名可能是「对象全名」（流程附件行的 objname = cpcwf.select
        # 回显的 `工作区\文件夹\文件名`）→ 必须先剥路径，否则落盘名带 `$` 前缀
        # （实测得到 `唐阳的工作区$唐阳$GCFAT....xlsx`，与 PDM 原文件名不一致）。
        row_name = _basename_win(objname)
        # 显式历史版时服务端 docname 仍是当前名（如 EV4），优先用前端传入的行内名（EV3）保真
        if explicit_rev and row_name:
            raw_name = row_name
        else:
            raw_name = (info.get("docname") or row_name or f"{objid}").strip() or f"{objid}"
        base, ext = os.path.splitext(raw_name)
        if not ext:
            raw_name = base + _guess_ext(content)
        fname = _safe_filename(raw_name or f"{objid}.bin")
        mt = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        disp = f"attachment; filename=\"{quote(fname)}\"; filename*=UTF-8''{quote(fname)}"
        return Response(content, media_type=mt, headers={"Content-Disposition": disp})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"文件打开失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.get("/objects/{objid}/save", summary="下载到本地文件（MCP 下载通道；rev 支持 ECN 变更前/后版本）")
async def save_object_file(
    objid: str,
    request: Request,
    rev: str = Query("", description="显式内部版号（ECN 变更前 oldrev / 变更后 newrev，见 pdm_ecn_detail 图纸行）；空=最新可下载版"),
    outdir: str = Query("", description="落盘目录（默认 %LOCALAPPDATA%\\SinoCC-PDM-Bridge\\downloads\\<objid>）"),
):
    """vault → 本地文件（不复刻打开行为，专供 MCP/自动化取文件）。

    与 /open 同一套 getdownstatus + 8011 下载 + 解包逻辑（_download_doc_to_file），
    区别：不回二进制流，落盘后返回 JSON（path/size/sha 等）——MCP 是 JSON 文本
    通道，二进制回传会被 260KB 截断，落盘+返回路径才是 MCP 可用的下载形态。
    显式 rev 无坐标 → 404 + 当前最新版提示（与 /open 同口径，变更后版本未
    入库时明确报错不静默回退旧版）。
    """
    zc = _zipxml_client(request)
    try:
        explicit_rev = rev.strip() if (rev and rev.strip().isdigit() and int(rev.strip()) > 0) else ""
        target_dir = outdir.strip() or os.path.join(
            os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
            "SinoCC-PDM-Bridge", "downloads", str(objid))
        target_dir = os.path.abspath(target_dir)
        # outdir 只允许落到本地盘目录名层面（防 ../ 逃逸由 abspath+exists 校验兜底）
        if outdir.strip() and not os.path.isdir(target_dir):
            os.makedirs(target_dir, exist_ok=True)
        elif not outdir.strip():
            os.makedirs(target_dir, exist_ok=True)
        save_path = await _download_doc_to_file(zc, objid, os.path.join(target_dir, "x"), rev=explicit_rev)
        # 2026-10-10：显式 rev 下载时文件名追加 _rev{N} —— 否则同文档不同版落盘同名
        # 互相覆盖（15:47 实测 rev=4 D.1 1231882B 被 rev=5 E.1 1228786B 覆盖）。
        # 服务端 docname 不含版本区分（getdownstatus 的 docname 恒为当前名）。
        if explicit_rev:
            _d, _fn = os.path.split(save_path)
            _b, _e = os.path.splitext(_fn)
            _tag = f"_rev{explicit_rev}"
            if _tag not in _b:
                _new = os.path.join(_d, _b + _tag + _e)
                if os.path.exists(_new):
                    os.remove(_new)
                os.replace(save_path, _new)
                save_path = _new
        st = os.stat(save_path)
        import hashlib as _hash
        h = _hash.sha256()
        with open(save_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return {"ok": True, "objid": objid,
                "rev": explicit_rev or "latest",
                "path": save_path, "size": st.st_size,
                "sha256": h.hexdigest()[:16],
                "mtime": _dt.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"下载落盘失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.get("/objects/{objid}/cpc-health", summary="体检：存储体 .cpc 是否原版完整（只下 36B 尾段）")
async def cpc_health(objid: str, request: Request, deep: int = 0, rev: str = Query("", description="显式版本号；留空=最新版")):
    """廉价体检：判断服务端存的文件体是原版完整结构，还是旧版桥接上传的坏体。

    坏体（0x01 + zlib(文件)，缺 788B 元数据块 + 36B 尾）在网页端下载完全正常，
    但原版客户端下载后解完第一段去读元数据段 → EOF → 报「解压失败」且不落盘。
    见 docs/captured/cpc_format_v1/PROTOCOL.md。

    只读探测：只取最后 36 字节，不下载整个文件，不写任何数据。

    deep=1 时额外下载元数据块（通常 <200B）校验文件名：
    09-09 实证存在「结构完整但元数据文件名被截断在半个汉字、扩展名丢失」的坏体
    （旧 build_cpc 把名字区误判为 112B），原版客户端同样会报「解压失败」，
    仅看尾段会误判为 complete。

    2026-09-10：加 rev 参数——探测 ECN「更改后」目标版本是否已在 vault 生效
    （getdownstatus(docid, rev=N) 有无下载坐标），不下载文件体。
    """
    zc = _zipxml_client(request)
    try:
        probe_rev = rev.strip() if (rev and rev.strip().isdigit() and int(rev.strip()) > 0) else ""
        if probe_rev:
            xml = await zc.file_getdownstatus(objid, rev=probe_rev)
        else:
            xml = await zc.file_getdownstatus(objid)
        info = _parse_getdownstatus(xml)
        url, tmp = info.get("url"), info.get("tmpfilename")
        filesize = int(info.get("filesize") or 0)
        if not (url and tmp and filesize):
            return {"ok": True, "docid": objid, "rev": probe_rev or "latest",
                    "verdict": "no_coordinates",
                    "detail": f"该版本无下载坐标（版本未生效/文件未上传）: {xml[:200]}"}
        if filesize < CPC_TAIL_SIZE:
            return {"ok": True, "docid": objid, "docname": info.get("docname"),
                    "filesize": filesize, "verdict": "too_small",
                    "detail": f"文件仅 {filesize}B，不足一个尾段，无法判定"}
        # 只取尾段：filepos 为存储文件内偏移，响应 = 3B头 + 36B + 1B终止符
        raw = await zc.file_download(url, tmp, zc.login_uid,
                                     packagesize=CPC_TAIL_SIZE,
                                     filepos=filesize - CPC_TAIL_SIZE)
        body = raw[3:] if raw[:3] == b"\x03\x01\x00" else raw
        verdict = parse_cpc_tail(body, filesize)
        if deep and verdict.get("verdict") == "complete" and verdict.get("meta_z"):
            try:
                mz = await zc.file_download(url, tmp, zc.login_uid,
                                            packagesize=verdict["meta_z"],
                                            filepos=verdict["file_z"])
                mb = mz[3:] if mz[:3] == b"\x03\x01\x00" else mz
                if mb.endswith(b"\x03"):
                    mb = mb[:-1]
                meta = zlib.decompress(mb)
                n = meta[0]
                raw_name = meta[2:2 + max(n - 1, 0)]
                try:
                    name = raw_name.decode("utf-8")
                    valid_utf8 = True
                except UnicodeDecodeError:
                    name = raw_name.decode("utf-8", "replace")
                    valid_utf8 = False
                docname = info.get("docname") or ""
                if not docname:  # getdownstatus 不回显文档名 → 用 cpcdoc select 补
                    try:
                        import re as _re
                        _m = _re.search(r"_(\d+)\.cpc$", tmp or "")
                        sel = await zc.doc_select(objid, _m.group(1) if _m else "1")
                        docname = (dict(_re.findall(r"<([a-zA-Z]\w*)>([^<]*)</\1>", sel))
                                   .get("docname") or "")
                    except Exception:
                        docname = ""
                # docname 来自 XML，含 &amp; 等实体 → 必须先反转义，否则 "&" 会误判不一致
                # （09-09 实证：P3&amp;4… 比元数据名多 4B，纯转义差）
                try:
                    from xml.sax.saxutils import unescape as _unesc
                    docname_cmp = _unesc(docname)
                except Exception:
                    docname_cmp = docname
                verdict["meta_name"] = name
                verdict["meta_name_bytes"] = len(raw_name)
                verdict["meta_name_valid_utf8"] = valid_utf8
                verdict["name_matches_docname"] = (name == docname_cmp)
                # 只有「截断」才算坏体：元数据名是文档名的前缀且长度正好撞上旧上限 112B
                # （09-09 修正：名字不同本身是合法的——原客户端写的是本地文件名，
                #  还可能是 GBK 老文件、或 PDM 把 / 替换成 $ 的净化，都不是缺陷）
                _trunc = (len(raw_name) == CPC_NAME_LEGACY_CAP
                          and len(docname_cmp.encode("utf-8")) > CPC_NAME_LEGACY_CAP
                          and docname_cmp.encode("utf-8")[:len(raw_name)] == raw_name)
                verdict["name_truncated"] = bool(_trunc)
                if _trunc or (not valid_utf8 and len(docname_cmp.encode("utf-8")) > CPC_NAME_LEGACY_CAP):
                    verdict["verdict"] = "name_broken"
                    verdict["detail"] = (
                        f"元数据文件名被截断（元数据 {len(raw_name)}B，"
                        f"文档名 {len(docname_cmp.encode('utf-8'))}B；"
                        f"合法UTF8={valid_utf8}）→ 长名被切在汉字中间/丢扩展名，"
                        "原版客户端打开会报「解压失败」，需用修复版重新检入覆盖")
                elif name != docname_cmp:
                    # 名字不同但非截断：合法差异（原客户端本地名 / GBK 老文件 / PDM 字符净化）
                    verdict["verdict"] = "name_differs"
                    verdict["detail"] = (
                        f"元数据名与文档名不同但非截断（元数据 {len(raw_name)}B vs "
                        f"文档名 {len(docname_cmp.encode('utf-8'))}B）——"
                        "多为原客户端写入的是本地文件名、GBK 老文件或 PDM 把 / 净化成 $，属正常")
            except Exception as e:  # 深检失败不影响基础体检结论
                verdict["deep_error"] = f"{type(e).__name__}: {e}"
        return {"ok": True, "docid": objid, "docname": info.get("docname"),
                "tmpfilename": tmp, "filesize": filesize, **verdict}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"体检失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.post("/objects/{objid}/checkout", summary="检出（锁定）文档，相当于已检出")
async def checkout_object(objid: str, request: Request):
    """复刻客户端「打开即锁定」：docman.jsp cpcdoc.checkout。

    服务端对正在走工作流的文档会返回 40212「在走工作流，不能检出」。
    成功时 lockuser 置为当前用户，可下载编辑；编辑后需 检入(checkin) 生成新版本。
    """
    zc = _zipxml_client(request)
    try:
        resp, req_xml = await zc.doc_checkout(objid)
        res = _parse_doc_lock(resp, "checkout")
        if res.get("ok"):
            # 缓存 checkout 请求体（完整字段），作为后续 checkin 的模板
            CHECKOUT_CACHE[objid] = req_xml
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"检出失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.post("/objects/{objid}/undocheckout", summary="撤销检出（解锁，不生成新版本）")
async def undocheckout_object(objid: str, request: Request):
    """docman.jsp cpcdoc.undocheckout：解除锁定，lockuser 清空。"""
    zc = _zipxml_client(request)
    try:
        xml = await zc.doc_undocheckout(objid)
        return _parse_doc_lock(xml, "undocheckout")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"撤销检出失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


def _parse_doc_lock(xml: str, action: str) -> dict:
    """解析 cpcdoc checkout/undocheckout 响应，提取锁定状态。"""
    import re
    err = re.search(r"<error>.*?<id>([^<]*)</id>.*?<message>([^<]*)</message>", xml, re.S)
    if err:
        return {"ok": False, "action": action, "error_id": err.group(1),
                "message": err.group(2)}
    def g(tag):
        m = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
        return m.group(1) if m else ""
    return {
        "ok": True,
        "action": action,
        "docid": g("docid"),
        "docname": g("docname"),
        "revlabel": g("revlabel"),
        "locked": g("locked"),
        "lockuser": g("lockuser"),
        "wfflag": g("wfflag"),
        "wfid": g("wfid"),
    }


def _file_closed_by_editor(path: str) -> bool:
    """探测编辑程序是否已关闭文件（Windows 独占写锁启发式）。

    Office/WPS 编辑期间以拒绝写共享方式持有文件，append 打开会 PermissionError；
    能以 append 模式打开（不写入任何字节）即视为已关闭。
    """
    try:
        with open(path, "ab"):
            return True
    except PermissionError:
        return False
    except OSError:
        return True  # 文件被移动/删除：按已关闭处理


_WIN_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')
_WIN_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.I)


def _basename_win(name: str) -> str:
    """剥掉前端「对象全名」里的工作区/文件夹路径，只留真实文件名。

    2026-09-14 实证（docid 6428398）：流程关联对象表 cpcwf.select 回显的
    `objname` 是**对象全名**，带工作区路径，如
    `唐阳的工作区\\唐阳\\GCFAT.20260508501 CNA2512-BP001...xlsx`；
    而文档真实文件名（CPCDOC.DOCNAME = 存储体 .cpc 元数据名）只有末段。
    前端右键「下载」把 objname 原样透传 + 带行内 rev，后端显式 rev 分支用它当
    文件名 → `_safe_filename` 把 `\\` 净化为 `$` → 落盘名变成
    `唐阳的工作区$唐阳$GCFAT....xlsx`（与 PDM 里原文件名不一致）。

    只按 Windows 路径分隔符 `\\` 取末段：PDM 工作区路径用 `\\`；而正斜杠 `/`
    可能是文档名本身（原版客户端净化为 `$`，如
    `UL1-KCR-26C/ER4-PSA(B4)...ai`），不能当路径切掉。
    """
    s = (name or "").strip()
    if "\\" in s:
        s = s.rsplit("\\", 1)[-1].strip()
    return s


def _safe_filename(name: str, max_len: int = 120) -> str:
    """把 PDM 文档名净化为 Windows 可用文件名。

    2026-09-10 实证：PDM docname 允许含 `/`（如
    `UL1-KCR-26C/ER4-PSA(B4).PM-1(DZ01)@铭牌.ai`），直接拼本地路径 open() 会
    FileNotFoundError → 检出/只读打开 502。原版客户端把 `/` 净化为 `$`
    （vault 元数据名实证：`UL1-KCR-26C$ER4-PSA(B4)...`），故此处同口径处理，
    其余 Windows 非法字符 → `_`，去首尾空白/点，裁剪超长，保留名加前缀。
    """
    s = (name or "").strip()
    if not s:
        return "unnamed"
    s = s.replace("/", "$").replace("\\", "$")
    s = _WIN_ILLEGAL.sub("_", s)
    s = s.strip().rstrip(". ")
    # 纯空白/纯点被裁空 → 兜底
    if not s:
        return "unnamed"
    if _WIN_RESERVED.match(s):
        s = "_" + s
    if len(s) > max_len:
        base, ext = os.path.splitext(s)
        keep = max_len - len(ext)
        s = (base[:max(1, keep)] if keep > 0 else base[:1]) + ext
    return s


def _checkout_save_path(location: str, docid: str, docname: str) -> str:
    """本地工作副本目录：优先原版 vault location（与客户端行为一致），不可用则降级临时目录。"""
    docname = _safe_filename(_basename_win(docname) or f"{docid}")
    if location:
        loc = location.rstrip("\\/") or location
        try:
            os.makedirs(loc, exist_ok=True)
            test = os.path.join(loc, ".bridge_write_test")
            with open(test, "w") as f:
                f.write("1")
            os.remove(test)
            return os.path.join(loc, docname)
        except OSError:
            pass
    fallback = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                            "SinoCC-PDM-Bridge", "checkout", str(docid))
    os.makedirs(fallback, exist_ok=True)
    return os.path.join(fallback, docname)


@router.get("/objects/{objid}/lock-status", summary="检出状态查询（locked: 1=未检出 2=已检出，2026-09-03 抓包钉死）")
async def lock_status_object(objid: str, request: Request):
    """cpcdoc.select 单行：列表接口只带 locked 不带 userid/checktime，这里补齐。"""
    zc = _zipxml_client(request)
    try:
        rev = await _latest_doc_download_rev(zc, objid)
        xml = await zc.doc_select(objid, rev=rev)
        err = re.search(r"<error>.*?<id>([^<]*)</id>.*?<message>([^<]*)</message>", xml, re.S)
        if err:
            return {"ok": False, "error_id": err.group(1), "message": err.group(2)}
        def g(tag):
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
            return m.group(1) if m else ""
        return {
            "ok": True, "docid": g("docid"), "docname": g("docname"),
            "rev": g("rev"), "revlabel": g("revlabel"),
            "locked": g("locked"), "userid": g("userid"),
            "checktime": g("checktime"), "location": g("location"),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"状态查询失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


async def _download_doc_to_file(zc: SinoCCZipClient, objid: str, save_path: str, docname_fallback: str = "", rev: str = "") -> str:
    """getdownstatus → 8011 下载 → 解包 → 落地本地文件，返回实际文件名。

    rev：显式内部版号（如 ECN 行内 objrev 的草稿版，2026-09-10 抓包实证
    getdownstatus(显式rev) 可取任意已存在版本）；空则取已激活最新版。
    """
    use_rev = rev.strip() if (rev and rev.strip().isdigit() and int(rev.strip()) > 0) else ""
    if not use_rev:
        use_rev = await _latest_doc_download_rev(zc, objid)
    xml = await zc.file_getdownstatus(objid, rev=use_rev)
    info = _parse_getdownstatus(xml)
    if not info.get("url") or not info.get("tmpfilename"):
        raise HTTPException(status_code=404,
                            detail=f"未找到下载坐标（objid={objid}）: {xml[:300]}")
    filesize = int(info.get("filesize") or 0)
    packagesize = filesize if filesize > 0 else 52428800
    data = await zc.file_download(info["url"], info["tmpfilename"], zc.login_uid, packagesize=packagesize)
    content = decode_file_body(data, expected_size=filesize)
    if not content:
        raise HTTPException(status_code=502, detail="下载成功但解包后得到空文件")
    # 2026-09-14：docname_fallback 可能来自前端「对象全名」（带工作区路径）→ 先剥路径
    raw_name = (info.get("docname") or _basename_win(docname_fallback) or f"{objid}").strip() or f"{objid}"
    base, ext = os.path.splitext(raw_name)
    if not ext:
        raw_name = base + _guess_ext(content)
    # 2026-09-10：docname 可能含 `/` 等 Windows 非法字符 → 落地前净化（否则 open() 抛 FileNotFoundError）
    final_path = os.path.join(os.path.dirname(save_path), _safe_filename(raw_name))
    with open(final_path, "wb") as f:
        f.write(content)
    return final_path


@router.post("/objects/{objid}/open-edit", summary="检出并打开桌面应用（无检出权限自动降级只读打开）")
async def open_edit_object(objid: str, request: Request, rev: str = Query("", description="显式内部版号（ECN 行内 objrev 草稿版）；空=已激活版")):
    """复刻客户端「检出 → 下载到 vault → 关联程序打开」。

    2026-09-10：检出成功与只读降级两条路径都按显式 rev 下载（行内版本），
    空则取已激活版——否则双击 ECN「更改后」行会检出旧激活版（09:42 桥接日志实证）。

    检出成功：下载到 checkout 响应 location（原版 vault 路径）→ os.startfile →
    注册 EDIT_WATCHERS 供前端轮询关闭/变更。
    检出失败（40212 走流程中 / 他人已检出 / 无权限）：降级只读——下载到临时目录直接打开，
    错误原因原样透传服务端文案。
    """
    zc = _zipxml_client(request)
    try:
        co_resp, co_req = await zc.doc_checkout(objid)
        res = _parse_doc_lock(co_resp, "checkout")
        if not res.get("ok"):
            # ---- 无检出权限 / 走流程中 / 他人已检出 → 只读打开 ----
            ro_dir = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()),
                                  "SinoCC-PDM-Bridge", "readonly", str(objid))
            os.makedirs(ro_dir, exist_ok=True)
            path = await _download_doc_to_file(zc, objid, os.path.join(ro_dir, "x"), rev=rev)
            try:
                os.startfile(path)  # noqa: 只支持 Windows 本机
            except OSError as e:
                return {"ok": False, "readonly": True, "error_id": res.get("error_id"),
                        "message": res.get("message"), "path": path,
                        "detail": f"文件已下载但无法唤起关联程序: {e}"}
            return {"ok": True, "readonly": True, "error_id": res.get("error_id"),
                    "message": res.get("message"), "path": path}
        # ---- 检出成功 → 下载到 vault location 并打开 ----
        CHECKOUT_CACHE[objid] = co_req
        def g(tag):
            m = re.search(rf"<{tag}>([^<]*)</{tag}>", co_resp)
            return m.group(1) if m else ""
        location = g("location")
        docname = res.get("docname") or g("docname") or f"{objid}"
        save_path = _checkout_save_path(location, objid, docname)
        path = await _download_doc_to_file(zc, objid, save_path, docname, rev=rev)
        EDIT_WATCHERS[objid] = {
            "path": path, "mtime": os.path.getmtime(path), "size": os.path.getsize(path),
            "docname": os.path.basename(path), "readonly": False,
        }
        try:
            os.startfile(path)  # noqa: 只支持 Windows 本机
        except OSError as e:
            return {"ok": True, "readonly": False, "path": path, "docname": os.path.basename(path),
                    "detail": f"已检出并下载到 {path}，但无法唤起关联程序: {e}"}
        return {"ok": True, "readonly": False, "path": path, "docname": os.path.basename(path)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"检出打开失败: {type(e).__name__}: {e}")
    finally:
        await zc.close()


@router.get("/objects/{objid}/edit-status", summary="编辑会话状态（关闭探测 + 变更探测）")
async def edit_status_object(objid: str, request: Request):
    """前端轮询：closed=编辑程序已释放文件；modified=内容相对检出时已变化。"""
    w = EDIT_WATCHERS.get(objid)
    if not w:
        return {"active": False}
    closed = _file_closed_by_editor(w["path"])
    try:
        mtime = os.path.getmtime(w["path"])
        size = os.path.getsize(w["path"])
    except OSError:
        return {"active": True, "closed": True, "modified": True,
                "path": w["path"], "docname": w["docname"], "gone": True}
    modified = (mtime != w["mtime"]) or (size != w["size"])
    return {"active": True, "closed": closed, "modified": modified,
            "path": w["path"], "docname": w["docname"], "readonly": w.get("readonly", False)}


@router.post("/objects/{objid}/edit-cancel", summary="结束编辑会话监视（不检入不撤销）")
async def edit_cancel_object(objid: str, request: Request):
    EDIT_WATCHERS.pop(objid, None)
    return {"ok": True}


class CheckinLocalBody(BaseModel):
    path: str = ""
    reason: str = ""
    rev: str = ""  # 显式覆盖版本号；空=按原版口径发当前 rev（检入=原地覆盖，checkin_rev_v1 实证）


@router.post("/objects/{objid}/checkin-local", summary="检入本地编辑副本（关闭后确认检入用）")
async def checkin_local_object(objid: str, request: Request, body: CheckinLocalBody):
    """读取 EDIT_WATCHERS 登记的本地工作副本并检入（浏览器无法直接上传本地已编辑文件）。"""
    w = EDIT_WATCHERS.get(objid)
    path = body.path or (w or {}).get("path", "")
    if not path:
        raise HTTPException(status_code=400, detail="该文档没有进行中的编辑会话，请先「检出并编辑」")
    if w and os.path.normpath(path) != os.path.normpath(w["path"]):
        raise HTTPException(status_code=400, detail="路径与编辑会话登记不符，拒绝检入")
    if not os.path.isfile(path):
        EDIT_WATCHERS.pop(objid, None)
        raise HTTPException(status_code=400, detail=f"本地文件不存在: {path}")
    with open(path, "rb") as f:
        raw = f.read()
    if not raw:
        raise HTTPException(status_code=400, detail="本地文件为空")
    zc = _zipxml_client(request)
    checkin_ok = False
    try:
        resp = await _do_checkin_raw(zc, objid, raw, body.reason, new_rev=body.rev)
        checkin_ok = True
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"检入失败: {type(e).__name__}: {e}")
    finally:
        if not checkin_ok:
            try:
                await zc.doc_undocheckout(objid)
            except Exception:
                pass
        else:
            # 2026-10-30：检入成功后检出会话已被服务端消费，必须清缓存——
            # 否则下次 checkin-local 复用已消费的旧 checkout 模板、跳过重新
            # 检出 → 40290「没有以共享方式检出，不能检入」（6478454 实测）。
            CHECKOUT_CACHE.pop(objid, None)
        await zc.close()
    EDIT_WATCHERS.pop(objid, None)
    return {"ok": True, "docid": objid, "path": path, "response": str(resp)[:800]}


def _set_xml_field(xml: str, tag: str, value: str) -> str:
    """替换 XML 中 <tag>..</tag>；若不存在则追加到末尾。

    注意：模板是 <Data> 内部字段（无 <Request>/<Data> 包裹，无 </Data>），
    所以兜底是直接追加 <tag>value</tag> 到串尾（2026-08-18 修正：
    原先 replace("</Data>") 在内部字段模板上永远不生效，导致 tmpfilename/
    userid 等缺失字段漏掉 → checkin 60019「上传的文件校验无效」）。
    """
    import re
    pat = re.compile(rf"<{tag}>.*?</{tag}>", re.S)
    repl = f"<{tag}>{value}</{tag}>"
    if pat.search(xml):
        return pat.sub(repl, xml, count=1)
    # 兼容带 <Data> 包裹的模板
    if "</Data>" in xml:
        return xml.replace("</Data>", f"{repl}</Data>", 1)
    return xml + repl


# cpcdoc 检入请求体完整模板（190 字段，2026-08-18 从真实客户端成功 checkin XML 提取）。
# checkin 校验要求文档对象字段齐全且无重复；从 select 模板拼接会缺字段/重复 → 60019。
# 模板值来自 6468550 真实检入，doc 相关字段（docid/docname/wfid 等）在构建时替换。
CHECKIN_TEMPLATE_REAL = (
    '<docid>6468550</docid><docname>CNA2503-BP001_试产_TS-RF260L-BPSR1-XZ-LA10(E1)_运输整改_内部性能、可靠性测试委托申请单.doc'
    'x</docname><rev>1</rev><revlabel></revlabel><code></code><codeid>0</codeid><doctype>1</doctype><keyw'
    'ord></keyword><clas>0</clas><stat>1</stat><flag>3</flag><locked>1</locked><inspect>0</inspect><proji'
    'd>0</projid><ecrid>0</ecrid><ecnid>0</ecnid><doccid>0</doccid><userid>唐阳</userid><location>c:\\sinocc'
    'home\\vault_1\\唐阳\\WF_1329188\\</location><checktime>2026-08-18 14:13:43</checktime><creator>岑祚昇</creato'
    'r><createtime>2026-08-12 08:52:18</createtime><updator>唐阳</updator><updatetime>2026-08-18 10:11:47</'
    'updatetime><updatereason></updatereason><publishor></publishor><publishtime></publishtime><publishst'
    'age>0</publishstage><publishstatus>0</publishstatus><wfflag>1</wfflag><wfid>1329188</wfid><wftempid>'
    '0</wftempid><locid>7</locid><lotid></lotid><efftime></efftime><exptime>9999-12-31</exptime><pagecoun'
    't>1</pagecount><pagenum>1</pagenum><drawid></drawid><usepurpose></usepurpose><appid>43</appid><pictu'
    're></picture><proportion></proportion><weightunit></weightunit><weight>1</weight><material></materia'
    'l><standard></standard><spec></spec><model></model><isziped>2</isziped><zipsize>182272</zipsize><old'
    'size>193673</oldsize><oldfile></oldfile><filetime>2026-08-18 10:11:47</filetime><sourceid>0</sourcei'
    'd><trkvisit>1</trkvisit><rate>0</rate><ratedesc></ratedesc><crc32>619673920</crc32><oldcrc32>2919116'
    '92</oldcrc32><realfilename></realfilename><viewdocid>0</viewdocid><viewdocrev>0</viewdocrev><usersht'
    'id>0</usershtid><issecret>1</issecret><actived>2</actived><isfamily>0</isfamily><familydocid>0</fami'
    'lydocid><flatdocid></flatdocid><drawname></drawname><locversion></locversion><statofdoc>0</statofdoc'
    '><workspacestatus>0</workspacestatus><clientfilename>c:\\sinocchome\\vault_1\\唐阳\\WF_1329188\\CNA2503-BP0'
    '01_试产_TS-RF260L-BPSR1-XZ-LA10(E1)_运输整改_内部性能、可靠性测试委托申请单.docx</clientfilename><note></note><tmpfilenam'
    'e>d:/vault_prd/2026/8/{ADAF8C96-9FFC-42FA-AD5C-7F33199050DF}</tmpfilename><uploadfilename>C:\\Users\\T'
    'ANGYA~1\\AppData\\Local\\Temp\\vault_1.{BDEADF00-C265-11d0-BCED-00A0C90AB50F}\\CNA2503-BP001_试产_TS-RF260L'
    '-BPSR1-XZ-LA10(E1)_运输整改_内部性能、可靠性测试委托申请单.docx.zlb</uploadfilename><clientfiletime></clientfiletime><o'
    'bjaccess>0</objaccess><isupdateright>0</isupdateright><parentid></parentid><parenttype></parenttype>'
    '<idpath>-1000\\6468550\\</idpath><typepath>1\\6\\</typepath><namepath></namepath><namepath2></namepath2>'
    '<doccname></doccname><isattach>0</isattach><ismajor>0</ismajor><mrkdocid>0</mrkdocid><mrkdocrev>0</m'
    'rkdocrev><wftempname></wftempname><autocode>0</autocode><act>2</act><qty>0</qty><refid>0</refid><ref'
    'message></refmessage><hastmprev>0</hastmprev><locname>默认电子仓</locname><familydocname></familydocname>'
    '<flatdocname></flatdocname><modeldocname></modeldocname><flatclientfilename></flatclientfilename><ol'
    'ddocname>CNA2503-BP001_试产_CR-RF260L-BPSR1-LA10(E1)_运输整改_内部性能、可靠性测试委托申请单（模板）.docx</olddocname><ctrlte'
    'xt></ctrltext><md5></md5><keephereids></keephereids><sharemode>0</sharemode><sysbuiltinobj>0</sysbui'
    'ltinobj><statofcompare>0</statofcompare><author></author><drawtype></drawtype><drawdesc></drawdesc><'
    'sqlwhere></sqlwhere><cadvisible>0</cadvisible><cadreadflag>0</cadreadflag><refflag>0</refflag><check'
    'flag>0</checkflag><inheritedid>0</inheritedid><maxsearchrltcmt>0</maxsearchrltcmt><hits>0</hits><lci'
    'd>0</lcid><lccode></lccode><modeltype>0</modeltype><suppname></suppname><suppid>0</suppid><itemid>0<'
    '/itemid><filetype></filetype><notes></notes><istemprev>0</istemprev><isvirtual>0</isvirtual><flatdoc'
    'rev>0</flatdocrev><bigrev>0</bigrev><modeldocrev></modeldocrev><modeldocid></modeldocid><isdraw>0</i'
    'sdraw><activedrev>0</activedrev><docidofcompare>0</docidofcompare><docrevofcompare>0</docrevofcompar'
    'e><opentype>0</opentype><viewdocname></viewdocname><viewdoctempfile></viewdoctempfile><iscover>0</is'
    'cover><pdocid>0</pdocid><picsize></picsize><picpos></picpos><copysourceid>0</copysourceid><copysourc'
    'erev>0</copysourcerev><isimport>0</isimport><favid>0</favid><clasid>0</clasid><wsright></wsright><wo'
    'rkspaceid>0</workspaceid><baselineid>0</baselineid><baselinename></baselinename><revlist></revlist><'
    'revlabellist></revlabellist><bigrevlist></bigrevlist><isclassified>0</isclassified><caddef1></caddef'
    '1><caddef2></caddef2><caddef3></caddef3><caddef4></caddef4><caddef5></caddef5><refrel>0</refrel><ise'
    'xchange>0</isexchange><exdocids></exdocids><exbigrevs></exbigrevs><xmlfilename></xmlfilename><params'
    '></params><keycid>0</keycid><plmstate>审核中</plmstate><drawsharelocid>0</drawsharelocid><keycparams></'
    'keycparams><filetag></filetag><def1></def1><def2></def2><def3></def3><def4></def4><def5></def5><objr'
    'ights></objrights><objdefprops></objdefprops><objcrosss></objcrosss><docofmrks></docofmrks><doctrks>'
    '</doctrks><docofsubdraws></docofsubdraws><cpcdocattachofcpcdocs></cpcdocattachofcpcdocs><cpcdocofcpc'
    'docs></cpcdocofcpcdocs><cpcdoctssrelationofcpcdocs></cpcdoctssrelationofcpcdocs><cpcdockeycvalueofcp'
    'cdocs></cpcdockeycvalueofcpcdocs>'
)


def _set_doc_field(xml: str, tag: str, value: str) -> str:
    """替换模板中 <tag>..</tag> 的字段值（模板字段齐全，只替换不追加）。

    用 lambda 替换避免 value 中反斜杠（Windows 路径）被正则当转义。
    """
    return re.sub(rf"<{tag}>[^<]*</{tag}>", lambda m: f"<{tag}>{value}</{tag}>", xml, count=1)


def _build_checkin_xml(template: str, server_tmp: str, crc_signed: int,
                       zipsize: int, oldsize: int, reason: str,
                       userid: str = "唐阳", old_crc_signed: str = "",
                       new_rev: str = "") -> str:
    """基于真实客户端 190 字段模板生成 checkin 请求体（内部字段）。

    template 为 select/checkout 请求体（含 docid/docname/wfid 等当前文档字段），
    从中提取 doc 相关值替换进 CHECKIN_TEMPLATE_REAL，再注入上传坐标。
    2026-08-18 实证：从 select 模板拼接字段会 60019（字段缺失/重复），
    必须使用完整 190 字段模板。

    2026-10-30 版本号语义钉死（checkin_rev_v1 抓包，docid 5917534 原版检入实证）：
    原版客户端 checkin 请求 <rev>=当前版本（1，未+1）、revlabel 空、hastmprev/
    istemprev/bigrev/activedrev 全 0、revlist 空 → **检入 = 原地覆盖当前版本，
    无升版路径**；响应 revlabel 回填 A.1、locked=1。此前"rev+1"推断是错的已回滚；
    服务端实测也无视 XML rev（发 2 仍写 A.1），此处按原版口径发模板当前 rev。
    new_rev 仅留作显式覆盖参数（未来若抓到升版入口再启用）。
    """
    from datetime import datetime
    import re as _re
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def g(tag):
        m = _re.search(rf"<{tag}>([^<]*)</{tag}>", template)
        return m.group(1) if m else ""

    docid = g("docid") or "0"
    docname = g("docname") or "文档.docx"
    wfid = g("wfid") or "0"
    creator = g("creator") or userid
    createtime = g("createtime") or now
    updator = g("updator") or userid
    idpath = g("idpath") or f"-1000\\{docid}\\"
    typepath = g("typepath") or "1\\6\\"
    olddocname = g("olddocname") or docname
    locid = g("locid") or "7"
    cur_crc = g("crc32") or "0"
    old_crc = old_crc_signed or cur_crc
    if new_rev.strip().isdigit() and int(new_rev) > 0:
        rev_out = str(int(new_rev))
    else:
        rev_out = g("rev") or "1"

    location = f"c:\\sinocchome\\vault_1\\{userid}\\WF_{wfid}\\" if wfid != "0" else f"c:\\sinocchome\\vault_1\\{userid}\\"
    clientfilename = location + docname
    uploadfilename = (f"C:\\Users\\TANGYA~1\\AppData\\Local\\Temp\\vault_1."
                      f"{{BDEADF00-C265-11d0-BCED-00A0C90AB50F}}\\{docname}.zlb")

    xml = CHECKIN_TEMPLATE_REAL
    for tag, val in [
        ("docid", docid), ("docname", docname), ("rev", rev_out), ("revlabel", ""), ("userid", userid),
        ("location", location), ("checktime", now), ("creator", creator), ("createtime", createtime),
        ("updator", updator), ("updatetime", now), ("updatereason", reason or "Web检入"),
        ("wfid", wfid), ("locid", locid), ("zipsize", str(zipsize)), ("oldsize", str(oldsize)),
        ("crc32", str(crc_signed)), ("oldcrc32", str(old_crc)),
        ("clientfilename", clientfilename), ("tmpfilename", server_tmp),
        ("uploadfilename", uploadfilename), ("act", "2"), ("isziped", "2"),
        ("idpath", idpath), ("typepath", typepath), ("olddocname", olddocname),
    ]:
        xml = _set_doc_field(xml, tag, val)
    return xml


async def _do_checkin_raw(zc: SinoCCZipClient, objid: str, raw: bytes, reason: str = "",
                          new_rev: str = "") -> dict:
    """检入核心（checkin / checkin-local 共用）。

    流程：取 checkout 模板 XML → getupstatus 拿上传坐标(GUID) → 直连 8011 上传 zlib 字节
    → cpcdoc.checkin 登记新版本。失败抛 HTTPException，由调用方决定是否撤销检出。
    new_rev：显式覆盖版本号（默认空=按原版口径发模板当前 rev，检入=原地覆盖
    当前版本，checkin_rev_v1 抓包钉死无升版路径）。
    """
    # 1) checkout 模板（优先缓存；无缓存则尝试重新 checkout）
    template = CHECKOUT_CACHE.get(objid)
    if template is None:
        co_resp, co_req = await zc.doc_checkout(objid)
        res = _parse_doc_lock(co_resp, "checkout")
        if not res.get("ok"):
            raise HTTPException(
                status_code=409,
                detail=f"检出失败，无法检入：{res.get('message') or ('错误' + str(res.get('error_id')))}"
                       f"（请先在工作流中「检出」该文档，或确认文档未归档）",
            )
        template = co_req
        CHECKOUT_CACHE[objid] = co_req
    # 2) getupstatus 拿上传坐标
    guid = "{" + str(uuid.uuid4()).upper() + "}"
    upxml = await zc.file_getupstatus(objid, "7", guid)
    info = _parse_getdownstatus(upxml)
    url = info.get("url") or "http://10.240.28.24:8011/"
    server_tmp = info.get("tmpfilename")
    if not server_tmp:
        raise HTTPException(status_code=502, detail="getupstatus 未返回 tmpfilename，无法上传")
    # 3) 上传文件字节
    #    ★ 2026-09-03：必须构造完整 .cpc（0x01+zlib(文件)+zlib(788B元数据)+36B尾），
    #      否则原版客户端下载后解析元数据段 EOF → 「解压失败」，temp 不落盘。
    #      元数据块内文件名取服务端 docname（原版客户端写的是本地文件名，此处等价）。
    m = re.search(r"<docname>([^<]*)</docname>", template or "")
    filename = (m.group(1) if m else "").strip()
    up_resp, payload = await zc.file_upload(url, server_tmp, raw, filename=filename)
    # 4) checkin 登记新版本（crc32/packagesize 必须与上传一致 = 0x01 之后全部字节）
    crc = zlib.crc32(payload) & 0xffffffff
    crc_signed = crc if crc < 2**31 else crc - 2**32
    old_crc = zlib.crc32(raw) & 0xffffffff
    old_crc_signed = old_crc if old_crc < 2**31 else old_crc - 2**32
    sess = get_session("default")
    checkin_xml = _build_checkin_xml(template, server_tmp, crc_signed,
                                     len(payload), len(raw), reason,
                                     userid=sess.user or "唐阳",
                                     old_crc_signed=old_crc_signed,
                                     new_rev=new_rev)
    resp = await zc.doc_checkin(objid, checkin_xml)
    # 检入响应含 <error> → 视为失败，避免继续当成功返回
    if isinstance(resp, str) and "<error>" in resp:
        msg = re.search(r"<message>([^<]*)</message>", resp, re.S)
        raise HTTPException(status_code=409,
                            detail=f"检入被服务端拒绝: {(msg.group(1).strip() if msg else resp)[:200]}")
    return {"ok": True, "docid": objid, "response": resp[:800]}


@router.post("/objects/{objid}/checkin", summary="检入（上传新版本）")
async def checkin_object(
    objid: str,
    request: Request,
    file: UploadFile = File(...),
    reason: str = Query("", description="变更说明"),
):
    """编辑后保存并检入文档实现变更（复刻客户端「检入新版本」）。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    zc = _zipxml_client(request)
    checkin_ok = False
    try:
        resp = await _do_checkin_raw(zc, objid, raw, reason)
        checkin_ok = True
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"检入失败: {type(e).__name__}: {e}")
    finally:
        # 检入失败时撤销检出，避免文档留锁
        if not checkin_ok:
            try:
                await zc.doc_undocheckout(objid)
            except Exception:
                pass
        await zc.close()
    CHECKOUT_CACHE.pop(objid, None)
    return resp


def _parse_getdownstatus(xml: str) -> dict:
    import re
    def g(tag):
        m = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
        return m.group(1) if m else ""
    return {
        "url": g("url"),
        "tmpfilename": g("tmpfilename"),
        "docid": g("docid"),
        "rev": g("rev"),
        "docname": g("docname") or g("realfilename") or g("namepath"),
        "filesize": g("filesize"),
        "isziped": g("isziped"),
    }


def _guess_ext(content: bytes, _depth: int = 0) -> str:
    """按真实内容魔数推断扩展名（含前导点），未知返回空串。"""
    if not content or _depth > 2:
        return ""
    # OOXML / 通用 ZIP
    if content[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        try:
            import zipfile, io
            z = zipfile.ZipFile(io.BytesIO(content))
            names = set(z.namelist())
            # OOXML Office 文档（docx/xlsx/pptx）内部一定有 [Content_Types].xml
            if "[Content_Types].xml" in names:
                ct = z.read("[Content_Types].xml").decode("utf-8", errors="ignore")
                # 宏版 Office 文件和普通 OOXML 都是 ZIP，必须先按主部件类型区分，
                # 否则 .xlsm 会被后面的 workbook.xml 兜底误判为 .xlsx。
                if "spreadsheetml.sheet.macroEnabled" in ct or "excel.sheet.macroEnabled" in ct:
                    return ".xlsm"
                if "wordprocessingml.document.macroEnabled" in ct or "word.document.macroEnabled" in ct:
                    return ".docm"
                if "presentationml.presentation.macroEnabled" in ct or "powerpoint.presentation.macroEnabled" in ct:
                    return ".pptm"
                # Word
                if (
                    "wordprocessingml.document" in ct
                    or "word/document.xml" in names
                ):
                    return ".docx"
                # Excel
                if (
                    "spreadsheetml.sheet" in ct
                    or "xl/workbook.xml" in names
                ):
                    return ".xlsx"
                # PowerPoint
                if (
                    "presentationml.presentation" in ct
                    or "ppt/presentation.xml" in names
                ):
                    return ".pptx"
            # 单成员 ZIP：一般是 .cpc 再包一层，剥开看内部真实类型
            if len(z.namelist()) == 1:
                inner = z.read(z.namelist()[0])
                return _guess_ext(inner, _depth + 1)
        except Exception:
            pass
        return ".zip"
    # OLE 复合文档（旧 .doc/.xls/.ppt）不强行推断，交给原文件名
    if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return ""
    if content[:5] == b"%PDF-":
        return ".pdf"
    # PostScript / EPS / 旧版 Illustrator EPS
    if content[:9] == b"%!PS-Adob":
        # AI EPS 通常带 Adobe Illustrator Creator 注释
        head = content[:4096]
        if b"Adobe Illustrator" in head or b"%%BoundingBox:" in head:
            return ".ai"
        return ".eps"
    if content[:2] in (b"\x78\x9c", b"\x78\x01", b"\x78\xda"):  # zlib 层未解完
        return ""
    # 文本类（可能带 UTF-8 BOM）
    head = content[:3]
    if head == b"\xef\xbb\xbf" or _looks_like_text(content[:4096]):
        sample = content[:4096]
        first_line = sample.split(b"\n", 1)[0]
        if first_line.count(b",") >= 3:
            return ".csv"
        return ".txt"
    return ""


def _looks_like_text(b: bytes) -> bool:
    try:
        s = b.decode("utf-8")
    except Exception:
        return False
    # 允许常见文本字符
    return all(9 <= ord(c) < 127 or c in "\n\r\t" or ord(c) > 127 for c in s[:200])




@router.post("/tasks/{wf_id}/approve", response_model=OpResult, summary="审批通过（?client=1 走客户端通道）")
async def approve(
    wf_id: str,
    body: ApproveIn,
    request: Request,
    client_mode: bool = Query(False, alias="client", description="true=客户端通道（模板化 submit）"),
    client: SinoCCClient = Depends(_client),
):
    """Verified 2026-08-05: cpcwfproc.submit with XML payload in `oStr` field.
    Server-side permission check returns 40901 for non-own tasks (safe).
    client=1 时走复刻客户端通道（2026-08-21 改为动态构造 wf_submit_dynamic，
    旧 WF_SUBMIT 固定模板带 arrivefunc 脚本/缺 flag=6，会把流程推到 99999）。"""
    if client_mode:
        zc = _zipxml_client(request)
        import re
        xml = await zc.wf_detail(wf_id)
        m = re.search(r"<currprocid>([^<]*)</currprocid>", xml)
        if not m or not m.group(1):
            raise HTTPException(status_code=400, detail=f"未找到当前节点 procid（wfid={wf_id}）")
        procid = m.group(1)
        m2 = re.search(r"<currprocname>([^<]*)</currprocname>", xml)
        procname = m2.group(1) if m2 else ""
        try:
            r = await zc.wf_submit_dynamic(wf_id, procid, body.opinion, procname=procname, submitto=body.submitto)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"动态构造提交失败: {e}")
        if "<error>" in r:
            raise HTTPException(status_code=400, detail=r[:600])
        _cats_cache_invalidate()  # 该类型最后一个进行中流程可能刚走完 → 大类 chip 即时隐藏
        return OpResult(ok=True, message=f"approved (client channel, procid={procid})")
    ostr = f'<root><wfid>{wf_id}</wfid><opinion><![CDATA[{body.opinion}]]></opinion></root>'
    res = await client.call(
        endpoint="cpcwfproc",
        method="submit",
        formname="TWFExecForm",
        payload={"oStr": ostr, "OpUser": client.session.user},
    )
    if res.status_code >= 400:
        raise HTTPException(status_code=res.status_code, detail=res.text[:500])
    _cats_cache_invalidate()
    return OpResult(ok=True, message="approved")


@router.post("/tasks/{wf_id}/reject", response_model=OpResult, summary="驳回（测试模式：需 X-Test-Allow: 1 才能走客户端通道）")
async def reject(
    wf_id: str,
    body: ApproveIn,
    request: Request,
    client_mode: bool = Query(False, alias="client"),
    client: SinoCCClient = Depends(_client),
):
    """⚠ 2026-08-21 事故排查：client_mode 走 wf_reject_dynamic 时仍可能被服务端当 submit 处理。
    当前为【测试模式】——必须带 X-Test-Allow: 1 头才允许执行，仅用于抓包定位。"""
    if client_mode:
        if request.headers.get("x-test-allow") != "1":
            raise HTTPException(
                status_code=403,
                detail=(
                    "驳回（客户端通道）处于事故排查测试模式，必须带 X-Test-Allow: 1 头才允许执行；"
                    "正式环境请用原生 SinoCC 客户端操作。"
                ),
            )
        zc = _zipxml_client(request)
        import re as _re
        xml = await zc.wf_detail(wf_id)
        m = _re.search(r"<currprocid>([^<]*)</currprocid>", xml)
        if not m or not m.group(1):
            raise HTTPException(status_code=400, detail=f"未找到当前节点 procid（wfid={wf_id}）")
        procid = m.group(1)
        m2 = _re.search(r"<currprocname>([^<]*)</currprocname>", xml)
        procname = m2.group(1) if m2 else ""
        try:
            r = await zc.wf_reject_dynamic(wf_id, procid, body.opinion,
                                           rejectto=(body.rejectto or ""), procname=procname)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"动态构造驳回失败: {e}")
        if "<error>" in r:
            raise HTTPException(status_code=400, detail=r[:600])
        return OpResult(ok=True, message=f"rejected (client channel, procid={procid}, rejectto={body.rejectto or 'preproc'})")
    ostr = f'<root><wfid>{wf_id}</wfid><opinion><![CDATA[{body.opinion}]]></opinion></root>'
    res = await client.call(
        endpoint="cpcwfproc",
        method="reject",
        formname="TWFExecForm",
        payload={"oStr": ostr, "OpUser": client.session.user},
    )
    if res.status_code >= 400:
        raise HTTPException(status_code=res.status_code, detail=res.text[:500])
    return OpResult(ok=True, message="rejected")


@router.post("/tasks/{wf_id}/transfer", response_model=OpResult, summary="转办")
async def transfer(
    wf_id: str,
    body: TransferIn,
    request: Request,
    client: SinoCCClient = Depends(_client),
):
    res = await client.call(
        endpoint="taskman",
        method="TransferWorkFlow",
        formname="TWFExecForm",
        payload={
            "WFID": wf_id,
            "FromUser": client.session.user,
            "ToUser": body.to_user,
            "Reason": body.reason or "",
        },
    )
    if res.status_code >= 400:
        raise HTTPException(status_code=res.status_code, detail=res.text[:500])
    return OpResult(ok=True, message=f"transferred to {body.to_user}")


@router.post("/tasks/{wf_id}/addsign", response_model=OpResult, summary="加签")
async def addsign(
    wf_id: str,
    body: AddSignIn,
    request: Request,
    client: SinoCCClient = Depends(_client),
):
    method = "AddSignBefore" if body.before else "AddSignAfter"
    res = await client.call(
        endpoint="taskman",
        method=method,
        formname="TWFExecForm",
        payload={
            "WFID": wf_id,
            "FromUser": client.session.user,
            "ToUser": body.to_user,
            "Position": "before" if body.before else "after",
        },
    )
    if res.status_code >= 400:
        raise HTTPException(status_code=res.status_code, detail=res.text[:500])
    return OpResult(ok=True, message=f"addsign {'before' if body.before else 'after'} {body.to_user}")


# =========================== 发起流程（右键「工作流」→ 选模板 → 编制界面 → 创建） ===========================
# 2026-09-02 两轮抓包（file_wf_start_v1 / file_wf_create_v1）钉死协议：
#   ① 右键 → publicman cpcobjinfo.getobjwftempid（仅回显）+ docman cpcdoc.select
#   ② 选流程：workman cpcuserwftemp.search（常用） | wfman cpcwftemp.search（关键字）
#   ③ 编制界面：wfman cpcwftemp.select（wftempid + stat=2 + wfobjs=objid_objtype）
#   ④ 提交 = cpcwf.insert（改造模板响应）→ cpcwf.select → cpcwfproc.submit

@router.get("/launch/templates", summary="发起流程：常用流程列表（cpcuserwftemp.search）")
async def launch_templates(request: Request):
    zc = _zipxml_client(request)
    try:
        xml = await zc.wf_user_wftemps()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"常用流程拉取失败: {e}")
    if "<error>" in xml:
        raise HTTPException(status_code=400, detail=xml[:600])
    items = []
    for m in re.finditer(r"<UserWFTemp>(.*?)</UserWFTemp>", xml, re.S):
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", m.group(1)))
        wftempid = str(d.get("wftempid", "")).strip()
        if not wftempid or wftempid == "0":
            continue
        items.append({
            "wftempid": wftempid,
            "wftempname": d.get("wftempname", ""),
            "namepath": d.get("namepath", ""),
            "ismanual": d.get("ismanual", "1"),
        })
    # ismanual=2（常用/置顶）排前面，其余按模板名
    items.sort(key=lambda x: (0 if x["ismanual"] == "2" else 1, x["wftempname"]))
    return {"ok": True, "templates": items, "total": len(items)}


@router.get("/launch/templates/search", summary="发起流程：流程模板关键字搜索（cpcwftemp.search）")
async def launch_templates_search(request: Request, keyword: str = Query("", description="模板名关键字（WFTempName like）")):
    kw = keyword.strip()
    if not kw:
        return {"ok": True, "templates": [], "total": 0}
    zc = _zipxml_client(request)
    try:
        xml = await zc.wf_wftemp_search(kw)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"模板搜索失败: {e}")
    if "<error>" in xml:
        raise HTTPException(status_code=400, detail=xml[:600])
    items = []
    for m in re.finditer(r"<wftempofwftemp>(.*?)</wftempofwftemp>", xml, re.S):
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", m.group(1)))
        wftempid = str(d.get("wftempid", "")).strip()
        if not wftempid or wftempid == "0":
            continue
        items.append({
            "wftempid": wftempid,
            "wftempname": d.get("wftempname", ""),
            "idpath": d.get("idpath", ""),
            "typepath": d.get("typepath", ""),
            "creator": d.get("creator", ""),
            "stat": d.get("stat", ""),
            "actived": d.get("actived", ""),
        })
    return {"ok": True, "templates": items, "total": len(items)}


@router.get("/launch/templates/{wftempid}/compose", summary="发起流程：编制界面数据（cpcwftemp.select）")
async def launch_template_compose(
    wftempid: str,
    request: Request,
    objid: str = Query("", description="挂载对象 ID"),
    objtype: str = Query("6", description="挂载对象类型"),
):
    zc = _zipxml_client(request)
    try:
        xml = await zc.wf_wftemp_select(wftempid, objid, objtype)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"编制数据拉取失败: {e}")
    if "<error>" in xml:
        raise HTTPException(status_code=400, detail=xml[:600])
    data_m = re.search(r"<data[^>]*>(.*)</data>\s*</response>\s*$", xml, re.S | re.I)
    body = data_m.group(1) if data_m else xml
    # 主记录
    main = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", body))
    # 节点模板
    nodes = []
    for m in re.finditer(r"<wfproctempofwftemp>(.*?)</wfproctempofwftemp>", body, re.S):
        c = m.group(1)
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", c))
        node = {
            "procid": d.get("proctempid", ""),
            "procname": d.get("proctempname", ""),
            "proctype": d.get("proctype", ""),
            "canmodify": d.get("canmodify", ""),
            "operright": d.get("operright", ""),
            "allperson": d.get("allperson", ""),
            "minperson": d.get("minperson", ""),
            "preproc": d.get("preproc", ""),
            "posx": d.get("posx", "0"),
            "posy": d.get("posy", "0"),
            "width": d.get("width", "80"),
            "height": d.get("height", "100"),
            "sufproctype": d.get("sufproctype", ""),
            "endprocid": d.get("endprocid", ""),
            "rejectprocid": d.get("rejectprocid", ""),
            "priority": d.get("priority", ""),
            "period": d.get("period", ""),
            "isskip": d.get("isskip", ""),
            "canreject": d.get("canreject", ""),
            "noticemode": d.get("noticemode", ""),
            "manager": d.get("manager", ""),
            # 执行人模板（procusertempofwfproctemp；userid=流程启动者 表示当前登录用户）
            "executor_tmpl": [],
            # 可选范围（wfuserrangeofwfproctemp）
            "user_range": [],
        }
        for um in re.finditer(r"<procusertempofwfproctemp>(.*?)</procusertempofwfproctemp>", c, re.S):
            ud = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", um.group(1)))
            node["executor_tmpl"].append({
                "userid": ud.get("userid", ""),
                "username": ud.get("username", ""),
                "ismajor": ud.get("ismajor", "1"),
                "isposition": ud.get("isposition", ""),
            })
        for um in re.finditer(r"<wfuserrangeofwfproctemp>(.*?)</wfuserrangeofwfproctemp>", c, re.S):
            ud = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", um.group(1)))
            uid = ud.get("userid", "")
            if uid and uid not in node["user_range"]:
                node["user_range"].append(uid)
        nodes.append(node)
    # 箭头模板
    routes = []
    for m in re.finditer(r"<proccondtempofwftemp>(.*?)</proccondtempofwftemp>", body, re.S):
        c = m.group(1)
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", c))
        routes.append({
            "from": d.get("fromprocid", ""),
            "to": d.get("toprocid", ""),
            "isdefault": d.get("isdefault", ""),
            "conditions": d.get("conditions", ""),
            "points": d.get("points", ""),
        })
    nodes.sort(key=lambda x: (int(x["procid"]) if str(x["procid"]).isdigit() and str(x["procid"]) != "99999" else 99999))
    return {
        "ok": True,
        "wftempid": main.get("wftempid", wftempid),
        "wftempname": main.get("wftempname", ""),
        "wfname": main.get("wfname", main.get("wftempname", "")),
        "submitctrl": main.get("submitctrl", ""),
        "breakprocid": main.get("breakprocid", ""),
        "keephistory": main.get("keephistory", ""),
        "autosubmitmode": main.get("autosubmitmode", ""),
        "prerun": main.get("prerun", ""),
        "nodes": nodes,
        "routes": routes,
    }


@router.post("/launch/create", summary="发起流程：创建（insert→select→submit 三步）")
async def launch_create(body: WfLaunchCreateRequest, request: Request):
    zc = _zipxml_client(request)
    try:
        res = await zc.wf_create_from_template(
            body.wftempid, body.objid or "", body.objtype,
            objname=body.objname, objrev=body.objrev, objrevlabel=body.objrevlabel,
            executors=body.executors or None,
            node_flags=body.node_flags or None,
            opinion=body.opinion,
            subject=body.subject or "",
            objects=body.objects,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"创建流程失败: {e}")
    if not res.get("wfid"):
        raise HTTPException(status_code=400, detail=(res.get("xml") or "")[:800])
    wfid = res["wfid"]
    msg = f"流程已创建 wfid={wfid}"
    submitted = False
    if body.submit:
        procid = str(res.get("currprocid") or "1").strip() or "1"
        try:
            await zc.wf_submit_dynamic(wfid, procid, body.opinion or "同意",
                                       procname="", submitto="")
            submitted = True
            msg += f"，首节点已提交（procid={procid}）"
        except Exception as e:
            msg += f"，但首节点提交失败（流程已创建，可在待办中处理）: {e}"
    _cats_cache_invalidate()  # 新流程可能让某模板新进入「当前流程」大类 → chip 即时出现
    return {"ok": True, "message": msg, "wfid": wfid,
            "wfname": res.get("wfname", ""), "subject": res.get("subject", ""),
            "stat": res.get("stat", ""), "submitted": submitted}


@router.post("/launch/{wfid}/objects/delete", summary="删除流程实例对象（cpcwfobj.delete + 自动重排）")
async def launch_obj_delete(wfid: str, body: WfObjectDeleteRequest, request: Request):
    """复刻原版客户端「删除对象」：先 cpcwf.select 拿该对象完整回显行 → 平铺进
    cpcwfobj.delete（objname 带路径/parentid/stat 等）→ 成功后自动 cpcwf.arrangeobjseq
    把剩余对象 seq 重排为 1..N 连续（2026-09-02 wf_launch_dialog_v2 实证 REQ23→REQ24）。"""
    zc = _zipxml_client(request)
    try:
        res = await zc.wf_obj_remove(wfid, body.objid, body.objtype)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"删除对象失败: {e}")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "删除失败"))
    return {"ok": True, "message": f"对象 {body.objid} 已删除，剩余 {res.get('remaining', 0)} 个已重排",
            "deleted": res.get("deleted"), "remaining": res.get("remaining")}


@router.post("/launch/{wfid}/objects/arrange", summary="重排流程实例对象顺序（cpcwf.arrangeobjseq）")
async def launch_obj_arrange(wfid: str, body: WfObjectArrangeRequest, request: Request):
    """复刻 cpcwf.arrangeobjseq：请求体 = cpcwf 主记录全字段模板（wfid 有值，其余 0/空）
    + wfobjofwfs 行每行只填 seq→newseq（2026-09-02 实证 REQ24：删 seq=4 后 1,2,3,5→1,2,3,4）。"""
    zc = _zipxml_client(request)
    try:
        xml = await zc.wf_obj_arrange(wfid, body.order)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"重排对象失败: {e}")
    return {"ok": True, "message": f"对象顺序已重排（{len(body.order)} 项）", "xml_head": xml[:2000]}


@router.post("/launch/{wfid}/objects/add", summary="追加流程实例对象（cpcwfobj.insert，seq 自 max+1 起）")
async def launch_obj_add(wfid: str, body: WfObjectAddRequest, request: Request):
    """复刻原版客户端执行窗口「关联对象 → 添加对象」：对每个待加对象发独立
    `wfman cpcwfobj insert`（42 标签轻量行，seq = 现有 max seq + 1 递增；
    v1/v2 实证空流程 0→1、已有 seq=1→2）。**追加无需 arrangeobjseq**。
    2026-09-03 wf_detail_add_obj_v1/v2 实证。前端限制本人启动+流程未结束（stat≠7）。"""
    zc = _zipxml_client(request)
    docs = [{"objid": o.objid, "objtype": o.objtype or "6", "objname": o.objname or "",
             "objrev": o.objrev or "", "objrevlabel": o.objrevlabel or ""} for o in body.objects]
    try:
        res = await zc.wf_obj_add(wfid, docs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"添加对象失败: {e}")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "添加失败"))
    return {"ok": True,
            "message": f"已追加 {res.get('count', 0)} 个对象（seq 从 {res.get('max_seq', 0) + 1} 起）",
            "added": res.get("added"), "max_seq": res.get("max_seq"), "wfid": wfid}


@router.post("/launch", response_model=OpResult, summary="发起新流程")
async def launch(
    body: LaunchIn,
    request: Request,
    client: SinoCCClient = Depends(_client),
):
    # Use SetFormData + custom-start method. Many flow templates define a launcher
    # method (TCCPC + template_code + 'Start'). Server is forgiving; the actual
    # method name comes from the template manifest.
    res = await client.call(
        endpoint="taskman",
        method="SetFormData",
        formname="TWFExecForm",
        payload={
            "TemplateCode": body.template_code,
            "Subject": body.subject,
            "Fields": "<root>" + "".join(f"<{k}><![CDATA[{v}]]></{k}>" for k, v in body.fields.items()) + "</root>",
            "OpUser": client.session.user,
            "LaunchFlag": "1",
        },
    )
    if res.status_code >= 400:
        raise HTTPException(status_code=res.status_code, detail=res.text[:500])
    _cats_cache_invalidate()
    return OpResult(ok=True, message="launched (check server response for new WFID)")


# =========================== 流程节点执行人管理（2026-08-24 抓包实证） ===========================
# 抓包 wf_node_exec_capture：
#   - 双击流程节点 → 打开「过程属性」窗口（节点数据来自流程详情 cpcwf.select 的 wfprocofwf 子表）
#   - 执行人增删改 → wfman.jsp?classid=cpcwfproc&action=update，body = 完整节点字段 +
#     <procuserofwfprocs><procuserofwfproc>...</...> 子表（增=加一条 / 删=去掉 / 改=调字段）
#   - 添加执行人先搜索用户：authman.jsp?classid=cpcuser&action=search（userid 模糊）
#   - cpcwfproc 无 select action（50002 非法操作），节点数据必须用 cpcwf.select 的 wfprocofwf chunk
@router.get("/users", summary="搜索 PDM 用户（复刻 authman cpcuser.search，供节点添加执行人）")
async def search_users(request: Request, keyword: str = "", limit: int = 50):
    zc = _zipxml_client(request)
    xml = await zc.call("authman.jsp", "cpcuser", "search", f"<userid>{keyword}</userid>")
    users = []
    for m in re.finditer(r"<user>(.*?)</user>", xml, re.S):
        d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", m.group(1)))
        uid = d.get("userid", "")
        if uid and uid not in ("0", ""):
            users.append({"userid": uid, "username": d.get("username", uid),
                          "orgname": d.get("orgname", ""), "position": d.get("position", "")})
        if len(users) >= limit:
            break
    return {"ok": True, "keyword": keyword, "users": users, "total": len(users)}


class ProcUpdateIn(BaseModel):
    executors: list[dict] = Field(default_factory=list, description="执行人列表：[{userid,username,ismajor,tempuser,rate,orgpath}]；ismajor=2 为决策人（同一节点仅一个）")
    allperson: str | None = Field(default=None, description="节点完成条件：1=任一执行人通过即完成；2=所有执行人都通过才算完成。None=不改（2026-09-01 抓包实证 wf_nodeflag_v2）")


@router.post("/tasks/{wf_id}/proc/{procid}", summary="保存流程节点执行人（复刻 cpcwfproc.update）")
async def proc_update(wf_id: str, procid: str, body: ProcUpdateIn, request: Request):
    zc = _zipxml_client(request)
    # 1) 重新拉 cpcwf.select，定位该节点完整 XML（wfprocofwf chunk）
    sel = await zc.wf_detail(wf_id)
    node_xml = None
    for m in re.finditer(r"<wfprocofwf>(.*?)</wfprocofwf>", sel, re.S):
        chunk = m.group(1)
        if re.search(r"<procid>\s*" + re.escape(procid) + r"\s*</procid>", chunk):
            node_xml = chunk
            break
    if node_xml is None:
        raise HTTPException(status_code=404, detail=f"未找到节点 procid={procid} (wfid={wf_id})")
    # 2) 构造新执行人子表（字段对齐抓包：wfid=0/procid=0/ismajor/tempuser/rate/stat=0）
    def esc(s):
        return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 决策人唯一化兜底：只允许一个 ismajor=2（决策人），其余回落 1（普通执行人）
    seen_decision = False
    parts = []
    for e in body.executors:
        uid = esc(e.get("userid", ""))
        if not uid:
            continue
        ismajor = str(e.get("ismajor", "1") or "1")
        if ismajor == "2":
            if seen_decision:
                ismajor = "1"
            else:
                seen_decision = True
        elif ismajor != "1":
            ismajor = "1"
        parts.append(
            "<procuserofwfproc>"
            "<wfid>0</wfid><procid>0</procid>"
            f"<userid>{uid}</userid>"
            f"<username>{esc(e.get('username', uid))}</username>"
            f"<ismajor>{ismajor}</ismajor>"
            f"<tempuser>{esc(e.get('tempuser', '1'))}</tempuser>"
            f"<rate>{esc(e.get('rate', '1'))}</rate>"
            f"<agent></agent><positionid></positionid><stat>0</stat>"
            f"<orgpath>{esc(e.get('orgpath', ''))}</orgpath>"
            "</procuserofwfproc>"
        )
    new_block = f"<procuserofwfprocs>{''.join(parts)}</procuserofwfprocs>"
    node_xml2 = re.sub(r"<procuserofwfprocs>.*?</procuserofwfprocs>", new_block, node_xml, flags=re.S)
    # 2b) 节点完成条件 allperson（抓包实证：勾选"所有人都通过"后请求里 allperson 由 1 变 2）
    if body.allperson in ("1", "2"):
        if re.search(r"<allperson>.*?</allperson>", node_xml2, re.S):
            node_xml2 = re.sub(r"<allperson>.*?</allperson>", f"<allperson>{body.allperson}</allperson>",
                               node_xml2, count=1, flags=re.S)
        else:
            node_xml2 = re.sub(r"<minperson>.*?</minperson>",
                               lambda m: m.group(0) + f"<allperson>{body.allperson}</allperson>",
                               node_xml2, count=1, flags=re.S)
    # 3) cpcwfproc.update（body = 完整节点字段，build_body 包 <Request><Data>）
    resp = await zc.call("wfman.jsp", "cpcwfproc", "update", node_xml2)
    # 4) 校验响应
    err = re.search(r"<error>\s*<id>([^<]*)</id>", resp)
    if err and err.group(1).strip() not in ("0",):
        msg = re.search(r"<message>([^<]*)</message>", resp)
        return {"ok": False, "error": err.group(1),
                "message": (msg.group(1) if msg else "")[:200], "raw": resp[:400]}
    return {"ok": True, "wf_id": wf_id, "procid": procid,
            "allperson": body.allperson,
            "decision_user": next((e.get("userid") for e in body.executors
                                   if str(e.get("ismajor")) == "2"), None),
            "executor_count": len([e for e in body.executors if e.get('userid')])}


# =========================== 工作流按大类分组（2026-08-24 抓包 wf_category_capture 实证） ===========================
# 客户端左侧树「我处理的工作流」按大类分：
#   cpcwf.selectall(flag) → 该大类下有实例的【流程模板】列表（wf/wftempid/wfname）
#   展开模板 → cpcwf.selectref(wftempid, flag) → 该模板下的【流程实例】列表
# flag 映射：1=当前流程 2=未到达流程 4=我启动的流程 5=通知我的流程 6=我决策的工作流
# 301/302/3 = 「我已完成」分类的三个时间范围（2026-08-25 抓包 wf_completed_view_capture3 实证）：
#   本周=301 / 本月=302 / 全部=3；请求体仅 <flag> 不同，其余字段全 0/空
CATEGORY_NAMES = {"1": "当前流程", "2": "未到达流程", "4": "我启动的流程",
                  "5": "通知我的流程", "6": "我决策的工作流",
                  "301": "我已完成-本周", "302": "我已完成-本月", "3": "我已完成-全部"}
# 「我已完成」三个时间范围 flag（前端归组为一个分类节点，子项懒加载）
DONE_FLAGS = ("301", "302", "3")

# 2026-08-25：分类树缓存（selectall 结果基本不变，60s TTL）
_CATS_CACHE = {"ts": 0.0, "data": None}
CATS_TTL = 60


def _cats_cache_invalidate():
    """流程状态变更（通过/中断/发起）成功后失效分类树缓存。

    2026-09-09：某类型的最后一个进行中流程走完后，「当前流程」大类里该模板 chip
    仍显示最长 60s（前端 op 成功后虽然会调 loadAllTasks()，但撞上本缓存拿到旧数据）。
    在写操作端点成功路径上主动失效，前端紧随的刷新即拿到实时大类。
    """
    _CATS_CACHE["data"] = None
    _CATS_CACHE["ts"] = 0.0


@router.get("/categories", summary="工作流大类+模板列表（复刻客户端左侧树 cpcwf.selectall）")
async def wf_categories(request: Request, flag: str = Query("", description="只拉某分类（如 301/302/3）；空=拉全部快分类，我已完成三时间范围(301/302/3)标记 lazy 由前端展开时再拉"),
                        refresh: int = Query(0, description="1=跳过 60s 缓存强制重拉（前端重试按钮用）")):
    # 2026-08-25：flag=301（我已完成-本周）selectall 曾实测 8.3s，其他分类 <0.3s。
    # 无 flag 时并行拉快分类 + 301/302/3 标记 lazy（前端展开时才拉），页面加载保持秒级。
    # 分类树 60s TTL 缓存。
    import time as _t
    now = _t.time()
    if not flag and not refresh and _CATS_CACHE["data"] is not None and now - _CATS_CACHE["ts"] < CATS_TTL:
        return _CATS_CACHE["data"]
    zc = _zipxml_client(request)

    async def fetch(f, name):
        try:
            xml = await zc.call("wfman.jsp", "cpcwf", "selectall",
                                f"<wfid>0</wfid><wftempid>0</wftempid><flag>{f}</flag><stat>0</stat>")
            templates = []
            seen = set()
            for m in re.finditer(r"<wf>(.*?)</wf>", xml, re.S):
                d = dict(re.findall(r"<([a-zA-Z_0-9]+)>([^<]*)</\1>", m.group(1)))
                t = d.get("wftempid", "")
                if t and t not in seen:
                    seen.add(t)
                    templates.append({"wftempid": t, "wfname": d.get("wfname", "")})
            return {"flag": f, "name": name, "templates": templates,
                    "template_count": len(templates)}
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            # 2026-09-01 修复"分类全变 (0)"：会话失效（HTTP 401）不能吞成空分类——
            # 必须上抛转 401，让前端弹重新登录；其余错误降级为带 error 的空分类。
            if "HTTP 401" in msg:
                raise
            return {"flag": f, "name": name, "templates": [],
                    "template_count": 0, "error": msg}

    async def fetch_guarded(f, name):
        try:
            return await fetch(f, name)
        except RuntimeError as e:
            if "HTTP 401" in str(e):
                raise HTTPException(status_code=401, detail="PDM 会话失效，请重新登录")
            raise

    if flag:
        name = CATEGORY_NAMES.get(flag, flag)
        return {"ok": True, "categories": [await fetch_guarded(flag, name)]}

    results = await asyncio.gather(*[fetch_guarded(f, n) for f, n in CATEGORY_NAMES.items() if f not in DONE_FLAGS])
    cats = list(results)
    # 「我已完成」三个时间范围全部 lazy，由前端展开子项时再拉
    for f in DONE_FLAGS:
        cats.append({"flag": f, "name": CATEGORY_NAMES[f], "templates": [],
                     "template_count": 0, "lazy": True})
    data = {"ok": True, "categories": cats}
    # 2026-09-01 修复：含错误的聚合结果不写 60s 缓存——否则错误（超时/抖动）会把
    # "(0)" 占住整整一分钟，用户看到"为什么没有流程了"。快分类 <0.3s，重拉无负担。
    if not any(c.get("error") for c in cats):
        _CATS_CACHE["ts"] = now
        _CATS_CACHE["data"] = data
    return data


@router.get("/categories/{flag}/template/{wftempid}", summary="大类下某模板的流程实例（cpcwf.selectref）")
async def wf_category_instances(flag: str, wftempid: str, request: Request, limit: int = 200,
                                stat: int = Query(0, description="实例状态筛选：0=全部(660) 1=已完成(34) 2=处理中(129)；「我已完成」分类统一用 flag=3 + stat=0（2026-10-28 抓包 wf_done_1039_v1）")):
    zc = _zipxml_client(request)
    # 2026-08-31 抓包（wf_completed_v2）+ 2026-10-28 抓包纠正（wf_done_1039_v1）：
    #  - selectref 对 flag=301/302 服务端 NPE（id=99999），仅 flag=3 可用；
    #  - 「我已完成」三个时间范围（本周301/本月302/全部3）的实例统一用 flag=3 查询；
    #  - 【10-28 纠正】实例 stat 必须用 0（与原版客户端一致）：抓包实证客户端对
    #    「我已完成-全部 → 出口产品定制申请流程(1039)」只发 selectref flag=3 stat=0，
    #    收到 5 条并显示；此前 08-31 误判「必须 stat=1」导致 1039 等模板 0 条
    #    （stat=1 是另一口径，1039→0 条 / 477→1 条 / 965→17 条，与客户端不符）；
    #    实测矩阵（tools/dbg_done_selectref.py）：1039 stat0=5条 stat1=0条，
    #    477 stat0=109/7含唐阳 stat1=1，965 stat0=736/15含唐阳 stat1=17。
    #  - 周/月范围再按实例 last_time 在客户端范围内过滤（selectref 本身不支持时间范围）。
    _done_range = str(flag) in ("301", "302", "3")
    query_flag = "3" if _done_range else flag
    query_stat = 0 if _done_range else stat
    xml = await zc.call("wfman.jsp", "cpcwf", "selectref",
                        f"<wfid>0</wfid><wftempid>{wftempid}</wftempid><flag>{query_flag}</flag><stat>{query_stat}</stat>")
    em = re.search(r"<error>\s*<id>(\d+)</id>.*?(?:<reason>|<message>)\s*(.*?)\s*(?:</reason>|</message>)", xml, re.S)
    if em:
        return {"ok": True, "flag": flag, "wftempid": wftempid,
                "catname": CATEGORY_NAMES.get(flag, flag),
                "total": 0, "rows": [],
                "server_error": f"服务端错误 id={em.group(1)}: {em.group(2)[:120]}"}
    rows = _parse_wf_instance_xml(xml)
    # 周/月范围过滤（selectref 不支持时间范围，且「我已完成」实例 end_time 恒为空；
    # 改用 last_time 最后操作时间近似「本周/本月我处理过」的实例。仅「我已完成」非全部范围生效）
    if str(flag) == "302":  # 本月
        _today = _dt.now().date()
        rows = [r for r in rows if (lambda d: d and d.year == _today.year and d.month == _today.month)(_parse_date(r.get("last_time", "")))]
    elif str(flag) == "301":  # 本周（周一起，含本周一 00:00 ~ 下周一 00:00）
        _today = _dt.now().date()
        _monday = _today - _tdelta(days=_today.weekday())
        _next_monday = _monday + _tdelta(days=7)
        rows = [r for r in rows if (lambda d: d and _monday <= d < _next_monday)(_parse_date(r.get("last_time", "")))]
    rows.sort(key=lambda r: r.get("last_time") or "", reverse=True)
    return {"ok": True, "flag": flag, "wftempid": wftempid,
            "catname": CATEGORY_NAMES.get(flag, flag),
            "total": len(rows), "rows": rows[:limit]}
