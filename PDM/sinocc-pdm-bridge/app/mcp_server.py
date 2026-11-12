"""MCP server for SinoCC PDM (mcp SDK 2.x low-level API).

Exposes the bridge's business capabilities as Model Context Protocol tools
so any MCP client can drive the PDM directly. Session state is shared with
the FastAPI bridge via the in-process `_session_store`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Optional
from pathlib import Path
from urllib.parse import quote

from .cad_diff import cad_diff
from .cadcraft import cad_dump_entities, cad_info, cad_render

log = logging.getLogger("sinocc.mcp")

# 桥接 FastAPI 基址（读取工具通过 HTTP 代理转发，复用 web UI 已登录的桥接会话）
BRIDGE_BASE = os.environ.get("PDM_BRIDGE_URL", "http://127.0.0.1:9123")

try:
    from mcp.server.lowlevel import Server
    from mcp.server.models import InitializationOptions
    from mcp.types import (
        CallToolRequestParams,
        TextContent,
        Tool,
    )
    _MCP_OK = True
except Exception:  # pragma: no cover
    _MCP_OK = False

from .client import SinoCCClient, get_session
from .client_zipxml import SinoCCZipClient
from .config import load_settings

_SETTINGS = None


def _settings():
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def _client(session_key: str = "default", account: str = "", password: str = "") -> SinoCCClient:
    return SinoCCClient(_settings(), account=account, password=password, session_key=session_key)


# ---------------------------------------------------------------------------
# tool registry (name -> (schema, handler))
# ---------------------------------------------------------------------------
def _tool_input_schema(properties: Dict[str, Any], required: list[str]) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


TOOL_DEFS: list[tuple[str, str, Dict[str, Any], list[str], str]] = [
    (
        "pdm_login",
        "登录 SinoCC PDM（账号+密码）。返回 loginUID 掩码。密码仅存内存不落盘。",
        {
            "account": {"type": "string", "description": "PDM 账号（中文实名，如：唐阳）"},
            "password": {"type": "string", "description": "PDM 密码"},
            "session_key": {"type": "string", "description": "会话标识（默认 default）", "default": "default"},
        },
        ["account", "password"],
        "login",
    ),
    (
        "pdm_use_session",
        "注入已有登录态（免密码）。loginUID 可取自 web 版登录后地址栏/localStorage 的 loginguid。",
        {
            "login_uid": {"type": "string", "description": "形如 CHIGOPDM.a.{...}.xxx 的 loginguid"},
            "session_key": {"type": "string", "description": "会话标识（默认 default）", "default": "default"},
        },
        ["login_uid"],
        "use_session",
    ),
    (
        "pdm_list_tasks",
        "列出我的工作流（个人分类接口 cpcwf.selectall/selectref，与网页左树/原版客户端完全同口径；"
        "2026-10-10 起弃用 stat=2 全表扫描）。bucket: todo=当前流程(到达待办，真正需要我处理) | "
        "notarrived=未到达流程(我是后续节点处理人，还没轮到) | mine=我启动的流程 | done=我已完成 | "
        "notify=通知我的流程 | decision=我决策的工作流",
        {
            "bucket": {"type": "string",
                       "description": "todo/notarrived/mine/done/notify/decision（默认 todo）",
                       "default": "todo"},
            "limit": {"type": "integer", "description": "返回条数（默认 50）", "default": 50},
            "session_key": {"type": "string", "description": "会话标识", "default": "default"},
        },
        [],
        "list_tasks",
    ),
    (
        "pdm_approve",
        "审批通过（客户端通道 wf_submit_dynamic，POST /wf/tasks/{wf_id}/approve?client=1，"
        "与 web UI 审批按钮同款；2026-10-10 起弃用 web 通道 cpcwfproc.submit 直调）。"
        "可选 submitto 指定提交目标节点（先调 pdm_task_submit_nodes 查可选路径）。",
        {
            "wf_id": {"type": "string", "description": "工作流 ID"},
            "opinion": {"type": "string", "description": "审批意见", "default": "同意"},
            "submitto": {"type": "string", "description": "提交目标节点（可选，留空=默认路径）", "default": ""},
            "session_key": {"type": "string", "description": "会话标识", "default": "default"},
        },
        ["wf_id"],
        "approve",
    ),
    (
        "pdm_reject",
        "驳回工作流（客户端通道 wf_reject_dynamic，POST /wf/tasks/{wf_id}/reject?client=1，"
        "与 web UI 驳回按钮同款含 X-Test-Allow 门禁；2026-10-10 起弃用 web 通道 cpcwfproc.reject 直调）。"
        "意见必填；rejectto 可选目标节点（留空=上一节点，先调 pdm_task_submit_nodes 查可选路径）。",
        {
            "wf_id": {"type": "string", "description": "工作流 ID"},
            "opinion": {"type": "string", "description": "驳回理由（必填）"},
            "rejectto": {"type": "string", "description": "驳回目标节点（可选，留空=上一节点）", "default": ""},
            "session_key": {"type": "string", "description": "会话标识", "default": "default"},
        },
        ["wf_id", "opinion"],
        "reject",
    ),
    (
        "pdm_obj_checkin_local",
        "检入本地已编辑副本（Agent 修改文件回传 PDM 的写通道）。"
        "典型链路：pdm_obj_save 落盘 → 本地能力修改该文件 → pdm_obj_checkin_local 传同一 path 检入新版本。"
        "path 必须是本机已存在的文件（MCP 与桥接同进程同机）；文档未检出时服务端自动检出，"
        "检入失败会自动撤销检出不留锁。2026-10-30 新增：此前 MCP 只有下载无回传，"
        "Agent 改完文件无法写回（用 我的文件/杂物/驱动测试委托申请单（模板11）.doc 实测复现）。",
        {
            "objid": {"type": "string", "description": "文档/对象 ID（docid）"},
            "path": {"type": "string", "description": "本地已编辑文件完整路径（pdm_obj_save 返回的 path 或其编辑副本）"},
            "reason": {"type": "string", "description": "变更说明（记录到文档 updatereason）", "default": "MCP 检入"},
            "rev": {"type": "string", "description": "显式覆盖版本号（一般留空；checkin_rev_v1 抓包钉死：检入=原地覆盖当前版本，原版客户端亦无升版路径）", "default": ""},
            "session_key": {"type": "string", "description": "会话标识（默认 default）", "default": "default"},
        },
        ["objid", "path"],
        "checkin_local",
    ),
    (
        "pdm_obj_undocheckout",
        "撤销检出（解锁文档；检入失败/放弃编辑时的兜底清理）。",
        {
            "objid": {"type": "string", "description": "文档/对象 ID（docid）"},
            "session_key": {"type": "string", "description": "会话标识（默认 default）", "default": "default"},
        },
        ["objid"],
        "undocheckout",
    ),
]

# ---------------------------------------------------------------------------
# 只读接口 → MCP 工具（表驱动，经 HTTP 代理到桥接 FastAPI 的同名 GET 端点）。
# kind = ("bridge_get", path_template)；path 占位符从 args 取，其余参数转 query。
# 会话与 web UI 共用桥接进程的 session store（X-PDM-Session 头，默认 default）。
# ---------------------------------------------------------------------------
def _sk_prop() -> Dict[str, Any]:
    return {"session_key": {"type": "string", "description": "桥接会话标识（默认 default，与 web UI 登录共用）", "default": "default"}}

def _rd(name: str, desc: str, path: str, props: Dict[str, Any] | None = None, required: list[str] | None = None):
    """构造一个 bridge_get 型工具定义。

    2026-10-10：路径模板里的 {参数} 自动注入 schema（properties + required）。
    之前只有手工补的 pdm_category_instances(flag/wftempid) / pdm_doc_attr(docid) 合格，
    其余十几个含路径参数的工具（pdm_obj_cpc_health / pdm_task_detail_graph /
    pdm_ecn_detail / pdm_material_* 等）schema 里没有路径参数 —— WorkBuddy 客户端
    additionalProperties:false 直接拒参，_handle 填路径也取不到值 → 工具实际不可用。
    """
    p = dict(props or {})
    p.setdefault("session_key", _sk_prop()["session_key"])
    req = list(required or [])
    for key in re.findall(r"{(\w+)}", path):
        if key not in p:
            p[key] = {"type": "string", "description": f"路径参数 {key}（必填）"}
        if key not in req:
            req.append(key)
    return (name, desc + "（经桥接 FastAPI 代理；需桥接已登录：web UI 登录或先调 pdm_auto_login）", p, req, ("bridge_get", path))

BRIDGE_READ_TOOLS: list[tuple[str, str, Dict[str, Any], list[str], tuple]] = [
    _rd("pdm_auto_login",
        "自动从本机 SinoCC 客户端进程内存扫描登录态（loginUID+用户名）并落库到桥接会话，免账号密码。前提：本机 PDM 客户端已登录",
        "/auth/auto",
        {"userid": {"type": "string", "description": "可选：候选用户名 hint（一般留空自动探测）"}}),
    _rd("pdm_wf_instances",
        "工作流实例列表（客户端通道 cpcwf.selectref；templid 空=对全部模板逐个查询合并，较慢）",
        "/wf/instances",
        {"templid": {"type": "string", "description": "流程模板 ID（空=全部模板）"},
         "limit": {"type": "integer", "description": "每模板最多条数（默认 100）", "default": 100}}),
    _rd("pdm_task_detail_graph",
        "任务详情（客户端通道 cpcwf.select）：流程图节点 wfprocofwfs + 关联对象 wfobjofwfs + 附件 wfattachofwfs（原版工作流执行窗口的两个页签，2026-09-14 实证）",
        "/wf/tasks/{wf_id}/detail"),
    _rd("pdm_task_submit_nodes",
        "提交/驳回可选目标节点路径（cpcwfproc.getsubmitnodeinfo，抓包实证 2026-08-26）",
        "/wf/tasks/{wf_id}/submit-node-info",
        {"procid": {"type": "string", "description": "当前节点 procid（留空自动取 currprocid）"}}),
    _rd("pdm_ecn_detail", "更改通知单(ECN, objtype=91046)详情（ecmman cpcecn.select）", "/wf/objects/{objid}/ecn"),
    _rd("pdm_samconfirm_detail", "物料确认单(objtype=101)详情（ecmman cpcecnsamconfirm.select）", "/wf/objects/{objid}/samconfirm"),
    _rd("pdm_pdtbatchapply_detail", "产品投产通知单(PDTBatchApply)详情（ecmman cpcpdtbatchapply.select）", "/wf/objects/{objid}/pdtbatchapply"),
    _rd("pdm_material_detail_byid", "物料明细（按 itemid；客户端右键明细表 bomman cpcitem.select）", "/wf/materials/byid/{itemid}/detail"),
    _rd("pdm_material_bomdetail",
        "物料 BOM 明细表（cpcbom.select 完整 81 字段/子件；asmid=0 表示无装配需传 itemcode）",
        "/wf/materials/byid/{itemid}/bomdetail",
        {"asmid": {"type": "string", "description": "装配件 asmid；=0 表示采购件/无装配", "default": ""},
         "itemcode": {"type": "string", "description": "物料编码（asmid=0 时必传）", "default": ""},
         "invorgid": {"type": "string", "description": "组织 ID（默认 104）", "default": "104"}}),
    _rd("pdm_material_bomrpt",
        "多层明细/BOM 报表（cpcbomrpt.getbom 一次返回整棵树，注意大响应）",
        "/wf/materials/byid/{itemid}/bomrpt",
        {"itemcode": {"type": "string", "description": "物料编码（缺省则按 itemid 查）", "default": ""},
         "itemrev": {"type": "string", "description": "物料版本（缺省则查）", "default": ""},
         "maxlayer": {"type": "string", "description": "最大层数，0=全部", "default": "0"}}),
    _rd("pdm_material_bomrpt_active",
        "聚合版多层明细：BOM 整树 + 各行物料的未结束流程单（101/91046），耗时较长",
        "/wf/materials/byid/{itemid}/bomrpt-active-rels",
        {"itemcode": {"type": "string", "description": "物料编码（缺省则查）", "default": ""},
         "itemrev": {"type": "string", "description": "物料版本（缺省则查）", "default": ""},
         "maxlayer": {"type": "string", "description": "最大层数，0=全部", "default": "0"},
         "months": {"type": "integer", "description": "流程单时间窗（月）：0=不限，缺省 6", "default": 6},
         "concurrency": {"type": "integer", "description": "并发度（默认 8）", "default": 8}}),
    _rd("pdm_material_basic", "物料基本属性（客户端属性弹窗-基本 tab）", "/wf/materials/{itemcode}/basic"),
    _rd("pdm_material_workflow", "物料工作流程（客户端属性弹窗-工作流程 tab）", "/wf/materials/{itemcode}/workflow"),
    _rd("pdm_material_related", "物料关联对象（客户端属性弹窗-关联对象 tab）", "/wf/materials/{itemcode}/related",
        {"invorgid": {"type": "string", "description": "组织 ID（默认 104）", "default": "104"}}),
    _rd("pdm_material_docs", "物料技术文档（客户端属性弹窗-技术文档 tab）", "/wf/materials/{itemcode}/docs"),
    _rd("pdm_doc_attr", "文档属性（cpcdoc.select）", "/wf/docs/{docid}/attr",
        {"docid": {"type": "string", "description": "文档 docid"},
         "rev": {"type": "string", "description": "文档版本，-1=最新", "default": "-1"}},
        ["docid"]),
    _rd("pdm_docs_wf_status", "批量查询文档流程状态（「正在走流程」标识）", "/wf/docs/wf-status",
        {"docids": {"type": "string", "description": "逗号分隔的 docid 列表"}}, ["docids"]),
    _rd("pdm_doc_wf", "文档所属流程（cpcobjinfo.getobjwf objtype=6）", "/wf/docs/{docid}/wf"),
    _rd("pdm_obj_wf", "对象所属流程（按 objtype 走专属 select 接口取 wfid，2026-09-01 抓包实证）", "/wf/objects/{objid}/wf",
        {"objtype": {"type": "string", "description": "对象类型：6=文档 101=确认单 91046=ECN 28=物料 202204=流程单号类", "default": ""},
         "include_all": {"type": "string", "description": "1=额外返回全部挂接流程（诊断用）", "default": "0"}}),
    _rd("pdm_obj_lock_status", "检出状态查询（locked: 1=未检出 2=已检出）", "/wf/objects/{objid}/lock-status"),
    _rd("pdm_obj_cpc_health", "体检：存储体 .cpc 是否原版完整（只下 36B 尾段校验）", "/wf/objects/{objid}/cpc-health"),
    _rd("pdm_obj_save",
        "下载文档/图纸到本地文件（vault→8011→解包→落盘，返回 path/size/sha256；"
        "拿到 path 后可直接用本地能力打开/预览。ECN 变更前后图纸：先调 pdm_ecn_detail "
        "拿图纸行 oldrev/newrev，再分别传 rev 下载两版；变更后版本未入库时报 404+最新版提示，"
        "不静默回退旧版）",
        "/wf/objects/{objid}/save",
        {"rev": {"type": "string", "description": "显式内部版号（ECN oldrev/newrev）；空=最新可下载版", "default": ""},
         "outdir": {"type": "string", "description": "落盘目录（默认 %LOCALAPPDATA%\\SinoCC-PDM-Bridge\\downloads\\<objid>）", "default": ""}}),
    _rd("pdm_obj_edit_status", "编辑会话状态（关闭探测 + 变更探测）", "/wf/objects/{objid}/edit-status"),
    (
        "pdm_cad_info",
        "CAD 图纸信息（cadcraft-cli info）：实体统计/图层/extents 等 JSON。"
        "支持 DWG(R13-2018)/DXF(R12-2018)。典型链路：pdm_obj_save 落盘 → pdm_cad_info 检查 → pdm_cad_render 出图。",
        {
            "path": {"type": "string", "description": "图纸文件本地路径（DWG/DXF）"},
            "session_key": {"type": "string", "description": "会话标识", "default": "default"},
        },
        ["path"],
        "cad_info",
    ),
    (
        "pdm_cad_render",
        "CAD 图纸渲染出图（cadcraft-cli convert）：DWG/DXF → PNG/SVG/PDF 落盘，返回 out_path。"
        "window=[x0,y0,x1,y1]（图纸单位）可框选局部区域；缺省=按模型空间范围自适应。"
        "注意：cadcraft 保存会丢弃未建模实体——本工具只渲染输出新文件，绝不回存原文件。",
        {
            "path": {"type": "string", "description": "图纸文件本地路径（DWG/DXF）"},
            "out_format": {"type": "string", "description": "导出格式 png/svg/pdf", "default": "png"},
            "out_path": {"type": "string", "description": "输出文件路径（默认与图纸同目录 .png）", "default": ""},
            "window": {"type": "array", "description": "局部出图区域 [x0,y0,x1,y1]（图纸单位，可选）", "default": None},
            "session_key": {"type": "string", "description": "会话标识", "default": "default"},
        },
        ["path"],
        "cad_render",
    ),
    (
        "pdm_cad_dump",
        "CAD 实体清单 dump（--dump-entities 等价实现，cadcraft-cli drawing.inspect）："
        "全部实体的 类型/图层/句柄/几何坐标/文本内容 → JSON。"
        "真实图纸实体可能上万，limit 限制返回条数（0=不限，truncated 标注是否截断）；"
        "两版比对直接用 pdm_cad_diff（同数据源，免手工对表）。",
        {
            "path": {"type": "string", "description": "图纸文件本地路径（DWG/DXF）"},
            "limit": {"type": "integer", "description": "最多返回实体条数（0=不限）", "default": 0},
            "session_key": {"type": "string", "description": "会话标识", "default": "default"},
        },
        ["path"],
        "cad_dump",
    ),
    (
        "pdm_cad_diff",
        "两版图纸结构化差异（ECN 变更前后比对）：增/删/改 × 图层 × 位置 × 旧值/新值，"
        "并单独输出「更改标记层文本清单」（mark_layer，默认 12更改标记；找不到精确层名时"
        "回退含「更改标记」的图层），每条标注 in_old=旧版是否已存在。"
        "tol=位置匹配容差（图纸单位，默认 1.0）。"
        "典型链路：pdm_obj_save 按 oldrev/newrev 分别下载两版 → pdm_cad_diff 两 path 比对。",
        {
            "path_a": {"type": "string", "description": "旧版图纸本地路径（DWG/DXF）"},
            "path_b": {"type": "string", "description": "新版图纸本地路径（DWG/DXF）"},
            "tol": {"type": "number", "description": "位置就近配对容差（图纸单位）", "default": 1.0},
            "mark_layer": {"type": "string", "description": "更改标记层名（默认 12更改标记）", "default": "12更改标记"},
            "session_key": {"type": "string", "description": "会话标识", "default": "default"},
        },
        ["path_a", "path_b"],
        "cad_diff",
    ),
    _rd("pdm_launch_templates", "发起流程：常用流程列表（cpcuserwftemp.search）", "/wf/launch/templates"),
    _rd("pdm_launch_template_search", "发起流程：流程模板关键字搜索（cpcwftemp.search）", "/wf/launch/templates/search",
        {"keyword": {"type": "string", "description": "模板名关键字（留空=全部）"}}),
    _rd("pdm_launch_template_compose", "发起流程：编制界面数据（cpcwftemp.select，返回节点/执行人/挂载对象）", "/wf/launch/templates/{wftempid}/compose",
        {"objid": {"type": "string", "description": "挂载对象 ID（可选）", "default": ""},
         "objtype": {"type": "string", "description": "挂载对象类型（默认 6=文档）", "default": "6"}}),
    _rd("pdm_users_search", "搜索 PDM 用户（authman cpcuser.search，供节点添加执行人）", "/wf/users",
        {"keyword": {"type": "string", "description": "用户名关键字", "default": ""},
         "limit": {"type": "integer", "description": "返回条数（默认 50）", "default": 50}}),
    _rd("pdm_wf_categories",
        "工作流分类树（与 web UI 左侧树一致，cpcwf.selectall）。flag 对照：1=当前流程 2=未到达流程 4=我启动的流程 5=通知我的流程 6=我决策的工作流，301/302/3=我已完成（本周/本月/全部，lazy 占位）。每个分类返回 templates[]（wftempid/wfname）与 template_count，拿 wftempid 再调 pdm_category_instances 查实例",
        "/wf/categories",
        {"flag": {"type": "string", "description": "只拉某分类（1/2/4/5/6/301/302/3）；空=全部快分类（我已完成三项返回 lazy 占位）", "default": ""},
         "refresh": {"type": "integer", "description": "1=跳过 60s 缓存强制重拉（审批/发起操作后建议带 1）", "default": 0}}),
    _rd("pdm_category_instances",
        "网页左树分类下的流程实例（cpcwf.selectref）：flag=分类（1=当前流程 2=未到达流程 4=我启动的流程 5=通知我的流程 6=我决策的工作流；301/302/3=我已完成，服务端统一按 flag=3+stat=1 查询并按 last_time 过滤本周/本月）。wftempid 来自 pdm_wf_categories 返回的模板列表",
        "/wf/categories/{flag}/template/{wftempid}",
        {"flag": {"type": "string", "description": "分类 flag（与 pdm_wf_categories 相同口径：1=当前流程 2=未到达 4=我启动 5=通知我 6=我决策；301/302/3=我已完成）"},
         "wftempid": {"type": "string", "description": "流程模板 ID（pdm_wf_categories 返回的 wftempid）"},
         "limit": {"type": "integer", "description": "返回条数（默认 200）", "default": 200},
         "stat": {"type": "integer", "description": "实例状态筛选：0=全部 1=已完成 2=处理中（我已完成分类忽略此参数，固定 1）", "default": 0}},
        ["flag", "wftempid"]),
    _rd("pdm_items_search",
        "按属性查物料（前缀/模糊；column 决定字段）",
        "/items",
        {"q": {"type": "string", "description": "搜索关键字"},
         "column": {"type": "string", "description": "itemcode/itemname/drawid/spec/material/productmodel/custommodel", "default": "itemcode"},
         "limit": {"type": "integer", "description": "返回条数（≤300，默认 30）", "default": 30},
         "filter_disabled": {"type": "boolean", "description": "true=过滤禁用物料", "default": False}},
        ["q"]),
    _rd("pdm_item_reference",
        "零部件引用查询（2026-09-09 抓包实证 cpcitemsearch getreference）：直接上层装配件 / 引用产品",
        "/items/{code}/reference",
        {"searchflag": {"type": "string", "description": "1=直接上层 2=引用产品", "default": "1"},
         "filter_disabled": {"type": "boolean", "description": "true=过滤禁用引用（仅有效）", "default": True},
         "drawid": {"type": "string", "description": "图号（可空，服务端只认物料编码）", "default": ""}}),
    _rd("pdm_product_ws_root", "产品工作区根（直接子文件夹，cpcworkspace selectref）", "/product/workspace"),
    _rd("pdm_product_folder", "展开产品工作区文件夹（子文件夹 + 物料列表；idpath/typepath/objaccess 来自父响应原样回传）", "/product/folder/{fdrid}",
        {"fdrname": {"type": "string", "description": "文件夹名（回显用）", "default": ""},
         "idpath": {"type": "string", "description": "节点自身 idpath（父响应必带回传）", "default": ""},
         "typepath": {"type": "string", "description": "节点自身 typepath（父响应必带回传）", "default": ""},
         "objaccess": {"type": "string", "description": "节点 objaccess", "default": ""},
         "objaccesstype": {"type": "string", "description": "节点 objaccesstype", "default": ""}}),
    _rd("pdm_my_workspaces", "我的文件：工作区列表（个人工作区+回收站，仅限当前登录用户）", "/product/files/workspaces",
        {"userid": {"type": "string", "description": "当前登录用户名（一般留空自动取会话用户）", "default": ""}}),
    _rd("pdm_my_files_root", "我的文件：个人工作区根（我的文件夹 + 共享给我快捷方式；仅限本人 wsid）", "/product/files/workspace",
        {"wsid": {"type": "string", "description": "个人工作区 wsid（pdm_my_workspaces 返回）", "default": ""},
         "wsname": {"type": "string", "description": "工作区名（如 唐阳的工作区）", "default": ""}}),
    _rd("pdm_shortcut_open",
        "我的文件：展开「共享给我」目录（cpcshortcut selectref；全部参数来自 pdm_my_files_root 响应 shortcut 行原样回传）",
        "/product/files/shortcut/{refid}",
        {"scname": {"type": "string", "description": "共享目录名", "default": ""},
         "idpath": {"type": "string", "description": "本机侧 idpath", "default": ""},
         "typepath": {"type": "string", "description": "本机侧 typepath", "default": ""},
         "sourceidpath": {"type": "string", "description": "源侧完整路径", "default": ""},
         "sourcetypepath": {"type": "string", "description": "源侧类型路径", "default": ""},
         "reftype": {"type": "string", "description": "引用类型（固定 -2）", "default": "-2"},
         "objaccess": {"type": "string", "description": "objaccess 回传", "default": ""},
         "objaccesstype": {"type": "string", "description": "objaccesstype 回传", "default": ""},
         "objaccesspass": {"type": "string", "description": "objaccesspass 回传", "default": ""},
         "wsright": {"type": "string", "description": "workspace 根响应的 wsright", "default": ""}}),
    _rd("pdm_docs_ws_root", "文档工作区根（wsid=-4，直接子文件夹）", "/product/docs/workspace"),
    _rd("pdm_docs_folder", "展开文档工作区文件夹（子文件夹 + 文档列表；idpath/typepath/objaccess 来自父响应原样回传）", "/product/docs/folder/{fdrid}",
        {"fdrname": {"type": "string", "description": "文件夹名（回显用）", "default": ""},
         "idpath": {"type": "string", "description": "节点自身 idpath", "default": ""},
         "typepath": {"type": "string", "description": "节点自身 typepath", "default": ""},
         "objaccess": {"type": "string", "description": "节点 objaccess", "default": ""},
         "objaccesstype": {"type": "string", "description": "节点 objaccesstype", "default": ""}}),
    _rd("pdm_docs_search",
        "文档搜索（cpcexplorer.search，含子文件夹；parentid 为文件夹完整 idpath，如 -4/115179/406246）",
        "/product/docs/search",
        {"q": {"type": "string", "description": "关键字（objname 前缀模糊）"},
         "parentid": {"type": "string", "description": "文件夹完整 idpath（空=全域）", "default": ""},
         "parenttype": {"type": "string", "description": "文件夹完整 typepath", "default": ""},
         "datebegin": {"type": "string", "description": "起 YYYY-MM-DD（默认近一月）", "default": ""},
         "dateend": {"type": "string", "description": "止 YYYY-MM-DD", "default": ""},
         "limit": {"type": "integer", "description": "结果上限（默认 500）", "default": 500}},
        ["q"]),
]

TOOL_DEFS = TOOL_DEFS + BRIDGE_READ_TOOLS


async def _bridge_get(sk: str, path: str, params: Dict[str, Any], timeout: float = 180.0) -> str:
    """经 HTTP 代理调用桥接 FastAPI 的 GET 端点，返回最终 JSON 文本（超长截断）。

    会话复用：桥接进程的 session store（X-PDM-Session 头）与 web UI 登录共享，
    MCP 进程不重复建会话。部分接口（stat=2 全表、bomrpt 聚合）耗时可达 60s+，超时放宽到 180s。
    """
    import httpx

    clean = {k: v for k, v in params.items() if v not in (None, "")}
    async with httpx.AsyncClient(timeout=timeout) as hc:
        r = await hc.get(BRIDGE_BASE + path, params=clean, headers={"X-PDM-Session": sk})
    try:
        data = r.json()
    except Exception:
        data = {"raw": (r.text or "")[:4000]}
    payload = {"ok": r.status_code < 400, "http_status": r.status_code, "bridge": BRIDGE_BASE, "data": data}
    LIMIT = 260_000
    text = json.dumps(payload, ensure_ascii=False)  # 紧凑序列化，体积比 indent=2 小约一半
    if len(text) > LIMIT:
        # 2026-10-10 修复：旧实现按字符硬切，把 JSON 切坏导致消费方解析失败。
        # 现改为递归把大数组截断并显式标注，逐级收缩（200→50→10）保证落在限内，
        # 返回永远是合法 JSON。
        def _shrink(o, cap):
            if isinstance(o, dict):
                return {k: _shrink(v, cap) for k, v in o.items()}
            if isinstance(o, list) and len(o) > cap:
                return [_shrink(v, cap) for v in o[:cap]]
            if isinstance(o, list):
                return [_shrink(v, cap) for v in o]
            return o
        for cap in (200, 50, 10):
            small = _shrink(payload, cap)
            if isinstance(small.get("data"), dict):
                small["data"]["_truncated"] = (
                    f"响应超长：大数组已截断到前 {cap} 项；请用更精确的过滤参数缩小范围后重查")
            text = json.dumps(small, ensure_ascii=False)
            if len(text) <= LIMIT:
                break
        if len(text) > LIMIT:  # 兜底：单条记录本身就巨大（如 base64/raw 字段）
            text = text[:LIMIT] + '...[TRUNCATED 单条记录超长，请用更精确的过滤参数]'
    return text


async def _bridge_post(sk: str, path: str, json_body: Dict[str, Any],
                       params: Dict[str, Any] | None = None,
                       headers: Dict[str, str] | None = None, timeout: float = 120.0):
    """经 HTTP 代理调用桥接 FastAPI 的 POST 端点（写操作，客户端通道）。

    2026-10-10：MCP 写操作弃用 web 通道 cpcwfproc.* 直调，统一走桥接的
    客户端通道端点（wf_submit_dynamic / wf_reject_dynamic，抓包实证模板）。
    """
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as hc:
        r = await hc.post(BRIDGE_BASE + path, json=json_body, params=params or {},
                          headers={"X-PDM-Session": sk, **(headers or {})})
    try:
        data = r.json()
    except Exception:
        data = {"raw": (r.text or "")[:600]}
    return {"ok": r.status_code < 400, "http_status": r.status_code, "data": data}


async def _handle(kind, args: Dict[str, Any], tool_name: str = ""):
    sk = args.get("session_key", "default")

    # 表驱动的只读代理工具（kind = ("bridge_get", path_template)）
    if isinstance(kind, tuple) and kind[0] == "bridge_get":
        path = kind[1]
        for key in re.findall(r"{(\w+)}", path):
            if key not in args or str(args[key]).strip() == "":
                return {"ok": False, "error": f"缺少路径参数 {key}"}
            path = path.replace("{" + key + "}", quote(str(args.pop(key)), safe=""))
        query = {k: v for k, v in args.items() if k != "session_key" and v not in (None, "")}
        return await _bridge_get(sk, path, query)

    if kind == "login":
        c = _client(sk, args["account"], args["password"])
        sess = await c.login(args["account"], args["password"])
        await c.close()
        return {"ok": True, "user": sess.user,
                "login_uid_mask": (sess.login_uid[:10] + "***") if sess.login_uid else "",
                "entcode": sess.entcode}

    if kind == "use_session":
        sess = get_session(sk)
        sess.login_uid = args["login_uid"]
        sess.entcode = _settings().default_entcode
        # 2026-10-10：探针弃用 web 通道 cpcitem.search，改走客户端 ZIP 通道
        # （bomman.jsp cpcitem.search，与 /items 搜索同款，登录链路真实口径）。
        zc = SinoCCZipClient(_settings()).set_login_uid(sess.login_uid).set_user(sess.user)
        try:
            xml = await zc.call("bomman.jsp", "cpcitem", "search",
                                "<itemcode>__probe__</itemcode>"
                                "<maxsearchrltcmt>1</maxsearchrltcmt><invorgid>104</invorgid>")
        except Exception as e:  # noqa: BLE001
            return {"ok": False,
                    "error": f"loginguid 无效或已过期（客户端通道探针失败）: {type(e).__name__}: {e}"}
        finally:
            await zc.close()
        if not xml or "<data" not in xml:
            return {"ok": False, "error": "loginguid 探针无有效响应（可能已过期）"}
        return {"ok": True, "user": sess.user or "session-only",
                "login_uid_mask": args["login_uid"][:10] + "***"}

    if kind == "list_tasks":
        # 2026-10-10 改版：弃用 cpcwf.search stat=2 全表扫描（口径=全公司进行中流程，
        # 只能筛「当前节点候选处理人」——多算了会签/会审候选，又完全看不到「未到达流程」
        # 里后续节点的指派，用户实测与原版客户端对不上）。
        # 改走个人分类接口（与网页左树/原版客户端同源，抓包实证 wf_category_capture）：
        #   cpcwf.selectall(flag) → 模板清单 → cpcwf.selectref(flag, wftempid) → 实例
        # 经本进程 FastAPI 的 /wf/categories* 端点聚合（复用其 60s 分类缓存与
        # 「我已完成」flag=3+stat=1+时间过滤逻辑）。
        bucket = str(args.get("bucket", "todo"))
        limit = int(args.get("limit", 50))
        flag = {"todo": "1", "notarrived": "2", "mine": "4",
                "done": "3", "notify": "5", "decision": "6"}.get(bucket, "1")
        import httpx

        base = BRIDGE_BASE
        hdrs = {"X-PDM-Session": sk}
        try:
            async with httpx.AsyncClient(timeout=120.0) as hc:
                r = await hc.get(f"{base}/wf/categories", params={"flag": flag}, headers=hdrs)
                if r.status_code >= 400:
                    return {"ok": False, "error": f"/wf/categories {r.status_code}: {r.text[:200]}"}
                cats = r.json().get("categories") or []
                cat = cats[0] if cats else {}
                if cat.get("error"):
                    return {"ok": False, "error": f"分类查询失败: {cat['error']}"}
                templates = cat.get("templates") or []

                sem = asyncio.Semaphore(6)

                async def one(t: dict):
                    async with sem:
                        try:
                            rr = await hc.get(
                                f"{base}/wf/categories/{flag}/template/{t['wftempid']}",
                                params={"limit": 500}, headers=hdrs)
                            d = rr.json()
                            return d.get("rows", []), d.get("server_error")
                        except Exception as e:  # noqa: BLE001
                            return [], f"{type(e).__name__}: {e}"

                results = await asyncio.gather(*[one(t) for t in templates])
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"桥接 FastAPI 不可达（{base}）：{type(e).__name__}: {e}"}

        rows: list[dict] = []
        errors: list[str] = []
        for t, (rs, err) in zip(templates, results):
            if err:
                errors.append(f"{t.get('wfname', t.get('wftempid'))}: {err}")
            for x in rs:
                x.setdefault("wf_name", t.get("wfname", ""))
                x.setdefault("wftempid", t.get("wftempid", ""))
                x.setdefault("category", cat.get("name", ""))
            rows.extend(rs)
        rows.sort(key=lambda x: x.get("last_time") or x.get("create_time") or "", reverse=True)
        return {"ok": True, "bucket": bucket, "flag": flag,
                "category": cat.get("name", ""),
                "template_count": len(templates),
                "total": len(rows), "rows": rows[:limit],
                "errors": errors[:5]}

    if kind == "approve":
        # 2026-10-10：弃用 web 通道 cpcwfproc.submit 直调，改走桥接客户端通道端点
        # （POST /wf/tasks/{wf_id}/approve?client=1 → wf_submit_dynamic，与 web UI 同款）。
        return await _bridge_post(
            sk, f"/wf/tasks/{quote(str(args['wf_id']), safe='')}/approve",
            {"opinion": args.get("opinion", "同意"), "submitto": args.get("submitto") or None},
            params={"client": "1"})

    if kind == "reject":
        # 2026-10-10：改走客户端通道端点（X-Test-Allow: 1 与 web UI 驳回按钮同款门禁头）。
        return await _bridge_post(
            sk, f"/wf/tasks/{quote(str(args['wf_id']), safe='')}/reject",
            {"opinion": args["opinion"], "rejectto": args.get("rejectto") or ""},
            params={"client": "1"}, headers={"X-Test-Allow": "1"})

    if kind == "checkin_local":
        # 2026-10-30：Agent 修改文件回传通道——检入本地已编辑副本。
        # path 必须是桥接本机存在的文件（MCP in-process 与桥接同机，JSON 传路径即可，
        # 不走 base64 二进制通道）。服务端自动检出；失败自动撤销检出不留锁。
        path = str(args.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "缺少 path（本地已编辑文件完整路径）"}
        if not os.path.isfile(path):
            return {"ok": False, "error": f"本地文件不存在: {path}"}
        return await _bridge_post(
            sk, f"/wf/objects/{quote(str(args['objid']), safe='')}/checkin-local",
            {"path": path, "reason": args.get("reason") or "MCP 检入",
             "rev": str(args.get("rev") or "")})

    if kind == "undocheckout":
        return await _bridge_post(
            sk, f"/wf/objects/{quote(str(args['objid']), safe='')}/undocheckout", {})

    if kind == "cad_info":
        # 2026-10-12：CADCraft 集成——DWG/DXF 图纸信息（subprocess 驱动 cadcraft-cli，
        # 不经 PDM 服务器；与 pdm_obj_save 落盘链路串联）
        try:
            return await cad_info(args["path"])
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"cad_info 异常: {type(e).__name__}: {e}"}

    if kind == "cad_render":
        try:
            src = Path(args["path"])
            fmt = (args.get("out_format") or "png").lower().lstrip(".")
            outp = args.get("out_path") or str(src.with_suffix(f".{fmt}"))
            return await cad_render(args["path"], outp, fmt, args.get("window"))
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"cad_render 异常: {type(e).__name__}: {e}"}

    if kind == "cad_dump":
        try:
            return await cad_dump_entities(args["path"], int(args.get("limit") or 0))
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"cad_dump 异常: {type(e).__name__}: {e}"}

    if kind == "cad_diff":
        try:
            return await cad_diff(args["path_a"], args["path_b"],
                                  args.get("tol") if args.get("tol") is not None else 1.0,
                                  args.get("mark_layer") or "12更改标记")
        except FileNotFoundError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"cad_diff 异常: {type(e).__name__}: {e}"}

    return {"ok": False, "error": f"unknown tool: {kind}"}


def create_mcp_server() -> Optional[Any]:
    """Build the low-level MCP Server, or None if the mcp package is missing."""
    if not _MCP_OK:
        return None

    _KIND_BY_NAME = {name: kind for name, _, _, _, kind in TOOL_DEFS}

    async def _on_list_tools(ctx, params):
        from mcp.types import ListToolsResult
        return ListToolsResult(tools=[
            Tool(name=name, description=desc,
                 inputSchema=_tool_input_schema(props, req))
            for name, desc, props, req, _ in TOOL_DEFS
        ])

    async def _on_call_tool(ctx, params: CallToolRequestParams):
        from mcp.types import CallToolResult
        kind = _KIND_BY_NAME.get(params.name)
        if kind is None:
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(
                {"ok": False, "error": f"unknown tool {params.name}"}, ensure_ascii=False))])
        try:
            args = params.arguments or {}
            result = await _handle(kind, dict(args), params.name)
            # bridge_get 分支直接返回最终 JSON 文本；其余返回 dict
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
            return CallToolResult(content=[TextContent(type="text", text=text)])
        except Exception as e:
            log.exception("tool %s failed", params.name)
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))])

    server = Server(
        "sinocc-pdm",
        title="SinoCC PDM Bridge",
        description="SinoCC (Chigo) PDM 集成工具：登录/工作流/审批/物料/BOM/产品工作区/我的文件/文档 全量只读查询 + 审批写操作",
        instructions=(
            "会话建立三选一：① pdm_auto_login（本机 SinoCC 客户端已登录时免密码自动建会话）；"
            "② web UI（http://127.0.0.1:9123/）登录后直接用（同一会话存储）；"
            "③ pdm_login（账号密码）。"
            "2026-10-10 起全部工具均走客户端通道（bomman/wfman ZIP 协议，抓包实证）；"
            "web JSON API 通道（cpcwf.search stat 扫描 / cpcbom.getchildren / cpcitem.search web 版）已全部移除。"
            "覆盖：工作流待办（个人分类口径）/实例/详情/提交节点、ECN/确认单/投产单、"
            "物料搜索/明细/BOM 展开/BOM 明细/多层明细/引用反查、产品工作区、"
            "我的文件（个人工作区+共享给我）、文档工作区/搜索、文档属性与下载落盘。"
            "写操作仅审批通过/驳回（pdm_approve/pdm_reject，客户端通道，与 web UI 同款）。"
        ),
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )
    return server
